"""
OCR 任务路由
------------
核心设计：后台异步处理 + SSE 广播

流程：
  POST /api/ocr   → 每张图建一条任务（queued），原图落盘，spawn 后台协程
  后台协程        → 预处理 → 流式调用 VLM → 状态/片段实时广播给订阅者 → 结果回写 SQLite
  GET  .../stream → SSE 订阅：
                    - 若任务已完成：一次性重放结果（前端刷新页面/回看历史场景）
                    - 若进行中：实时推送 progress / chunk / done / error 事件

这样即使前端断线重连，也能从 DB 补齐最终结果，不会丢任务。
"""
import asyncio
import json
import logging
import re
import time
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Body, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse

from app import db
from app.config import LANG_DEFAULT, LANG_DETECT, MAX_UPLOAD_MB, UPLOAD_DIR
from app.services import vlm_client, validation
from app.services.image_prep import is_allowed, preprocess_image, probe_thumb_b64

logger = logging.getLogger("docpivot.ocr")
router = APIRouter(prefix="/api")


def _validation_of(task: dict) -> dict | None:
    """DB 里的 validation 是 JSON 字符串，转成对象返回；空/损坏一律 None"""
    raw = task.get("validation")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None

# ---------------------------------------------------------------------------
# 内存态：每个任务的 SSE 订阅队列（片段实时推送用，最终结果以 DB 为准）
# ---------------------------------------------------------------------------
_subscribers: dict[str, set[asyncio.Queue]] = {}


def _publish(task_id: str, event: str, payload: dict) -> None:
    """向该任务的所有 SSE 订阅者广播一条事件"""
    for q in list(_subscribers.get(task_id, ())):
        q.put_nowait((event, payload))


def _subscribe(task_id: str) -> asyncio.Queue:
    _subscribers.setdefault(task_id, set()).add(asyncio.Queue())
    return next(iter(_subscribers[task_id]))


def _unsubscribe(task_id: str, q: asyncio.Queue) -> None:
    _subscribers.get(task_id, set()).discard(q)


# ---------------------------------------------------------------------------
# 后台识别协程
# ---------------------------------------------------------------------------
def _strip_code_fence(md: str) -> str:
    """兜底剥离模型偶发的 ```markdown 围栏（Prompt 已禁止，但双保险）"""
    s = md.strip()
    if s.startswith("```"):
        first_nl = s.find("\n")
        if first_nl != -1:
            s = s[first_nl + 1:]
        if s.rstrip().endswith("```"):
            s = s.rstrip()[:-3]
    return s.strip()


# Markdown 水平线（--- / *** / ___）
_HR_LINE = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$")


def _normalize_hr(md: str) -> str:
    """
    规范化水平线：确保 `---` 前后都有空行。

    原因：Markdown 的 setext 语法中"文字行 + 紧跟 ---"会把上一行变成标题
    （如 `登録番号:xxx\\n---` → 登録番号行被渲染成 h2，内容被破坏）。
    模型经常忽略空行，这里强制补齐，保证 `---` 永远渲染为分隔线。
    """
    lines = md.splitlines()
    out: list[str] = []
    for line in lines:
        if _HR_LINE.match(line):
            if out and out[-1].strip():       # 上一行非空 → 补空行
                out.append("")
            out.append("---")
            out.append("")                     # 下一行前也补空行
        else:
            out.append(line)
    return "\n".join(out)


def _norm_line(line: str) -> str:
    """归一化单行用于重复检测：去掉标题/列表标记与空白（'## 店名' 与 '店名' 视为同一行）"""
    s = line.strip().lstrip("#").lstrip("*-•").strip()
    return re.sub(r"\s+", "", s)


def _dedupe_repeated_blocks(md: str) -> str:
    """
    删除模型"先摘要式复述、再完整转写"造成的重复块。

    典型症状（山形1 实测）：开头先用 `# 店名` `## 公司名` 把抬头复述一遍，
    紧接着又把同一段抬头当正文输出一次。人眼看就是内容重复。

    算法：按块长从大到小扫描，若两块在"相邻距离内"（允许中间夹 1~2 行，
    如法人番号）内容完全相同（忽略标题标记），则删掉其中标题标记更多的那块
    （标题多的那份通常是"摘要"，保留完整正文更接近原图顺序）。
    只检测长度 ≥2 的块，避免误删收据里正常出现的单行重复（如相同金额行）。
    """
    lines = md.splitlines()
    norm = [_norm_line(l) for l in lines]
    n = len(lines)
    drop: set[int] = set()

    for size in range(n // 2, 1, -1):          # 块长 ≥2
        i = 0
        while i + size <= n:
            if any(k in drop for k in range(i, i + size)):
                i += 1
                continue
            block = norm[i:i + size]
            if not any(block):                  # 全空行块不处理
                i += 1
                continue
            # 在紧邻范围内找相同块（允许中间夹 1~2 行）
            hit = -1
            for j in range(i + 1, min(n - size + 1, i + size + 2)):
                if norm[j:j + size] == block and not any(k in drop for k in range(j, j + size)):
                    hit = j
                    break
            if hit == -1:
                i += 1
                continue
            # 标题标记更多的那份是"摘要"，删掉；相等时保留先出现的（更贴近原图顺序）
            head_i = sum(1 for l in lines[i:i + size] if l.lstrip().startswith("#"))
            head_j = sum(1 for l in lines[hit:hit + size] if l.lstrip().startswith("#"))
            victim = i if head_i > head_j else hit
            if head_i == head_j:
                victim = hit                    # 一样像正文：删后面的重复
            drop.update(range(victim, victim + size))
            i += size

    return "\n".join(l for k, l in enumerate(lines) if k not in drop)


# 识别"管道行"（可能构成表格）与"分隔行"（GFM 表格的合法性标志）
_PIPE_LINE = re.compile(r"^\s*\|.*\|\s*$")
_DELIM_LINE = re.compile(r"^\s*\|[\s:|-]+\|\s*$")
# 裸 GFM 表格的分隔行：行首行尾不包 |（如 "--- | ---" 或 ":---: | ---:"）。
# 注意纯 "---"（用户特意保留的 HR 分隔线）不含 |，不会误伤
_DELIM_LOOSE = re.compile(r"^\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+$")
_HAS_PIPE = re.compile(r"\|")
# 三级及更深的标题行（### ~ ######）：模型常给明细行滥用 ###，需降级为普通文本
_DEEP_HEADING = re.compile(r"^\s*#{3,}\s*(.*)$")


def _flatten_pipe_row(line: str) -> str:
    """把一行管道分隔的单元格拍平为空格连接的普通文本行"""
    cells = [c.strip() for c in line.strip().strip("|").split("|")]
    return " ".join(c for c in cells if c).strip()


def _demote_deep_headings(md: str) -> str:
    """把 `###` 及更深的标题降级为普通文本行（标题层级定死为 # / ## 两级）"""
    return "\n".join(
        _DEEP_HEADING.match(l).group(1).strip() if _DEEP_HEADING.match(l) else l
        for l in md.splitlines()
    )


# 图片链接记法：![alt](url)。V1 输入是照片，模型输出的 ![]() 都是它"脑补的链接"
# （如把 logo + 网址写成 ![SEIBU 西武](https://www.seibu.jp)），
# 前端渲染成 <img> 后 src 是网页地址，只会显示破损图标
_IMG_LINK = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)[^)]*\)")


def _strip_image_links(md: str) -> str:
    """
    图片记法拍平为纯文本：`![alt](url)` → `alt url`（alt 为空则只留 url）。
    与禁表格同理：prompt 禁令是软约束，模型仍可能输出；后处理是硬约束，
    保证入库的 markdown 里不存在任何会被渲染成 <img> 的语法。
    """
    def repl(m: re.Match) -> str:
        alt, url = m.group(1).strip(), m.group(2).strip()
        if alt and url and alt != url:
            return f"{alt} {url}"
        return alt or url

    return _IMG_LINK.sub(repl, md)


def _fix_pseudo_tables(md: str) -> str:
    """
    表格一律拍平为纯文本行（V1 输出形态定死为"逐行文本"，与原图逐行对齐）。
    覆盖两种形态：
    - 包裹式伪表格（每行以 `|` 开头结尾，可能无分隔行）→ 逐行拆掉管道
    - 裸 GFM 表格（行首无 `|`，如 `Item | Quantity` 下面跟 `--- | ---` 分隔行）
      → 以下一行是分隔行为特征识别出整块，丢弃分隔行、逐行拍平。
      实测 7B 在西武宣传单上输出这种形态；旧逻辑因行首无 `|` 而整块漏放行，
      前端 remark-gfm 便把它渲染成了真表格。
    设计原因：7B 视觉模型在收据类文档上会固执输出表格，且列语义经常错乱
    （合计/备注塞进明细列）；逐行文本既符合人眼对照习惯，也让下游
    字段抽取（V2）更稳定——不用先解析表格结构。
    """
    lines = md.splitlines()
    out: list[str] = []
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        nxt = lines[i + 1] if i + 1 < n else ""
        # 裸 GFM 表格块：当前行含 | 且下一行是分隔行 → 表头 + 分隔行 + 数据行
        if _HAS_PIPE.search(line) and _DELIM_LOOSE.match(nxt):
            text = _flatten_pipe_row(line)
            if text:
                out.append(text)
            i += 2  # 跳过分隔行
            while i < n and _HAS_PIPE.search(lines[i]) and lines[i].strip():
                out.append(_flatten_pipe_row(lines[i]))
                i += 1
            continue
        # 包裹式伪表格：逐行处理，分隔行直接丢弃
        if _PIPE_LINE.match(line):
            if not _DELIM_LINE.match(line):
                text = _flatten_pipe_row(line)
                if text:
                    out.append(text)
            i += 1
            continue
        out.append(line)
        i += 1
    return "\n".join(out)


async def _process(task_id: str, raw: bytes, model: str | None) -> None:
    t0 = time.monotonic()
    try:
        # 1. 预处理（CPU 操作放线程池，避免阻塞事件循环）
        db.update_task(task_id, status="preprocessing")
        _publish(task_id, "progress", {"task_id": task_id, "status": "preprocessing"})
        image_b64 = await asyncio.to_thread(preprocess_image, raw)

        # 2. 语言判定 → 决定加载哪套提示词（ja/zh）。
        #    放在 preprocessing 状态内完成：探测只要 0.3~1s，用户看到的是"预处理中"，
        #    不会出现"卡在 recognizing 却没有字"的空窗
        if LANG_DETECT:
            thumb_b64 = await asyncio.to_thread(probe_thumb_b64, raw)
            lang = await vlm_client.detect_language(thumb_b64, model=model)
        else:
            lang = LANG_DEFAULT
        db.update_task(task_id, lang=lang)

        # 3. 流式识别（按语言加载对应提示词）
        use_model = model or _default_model()
        db.update_task(task_id, status="recognizing", model=use_model)
        _publish(task_id, "progress",
                 {"task_id": task_id, "status": "recognizing", "lang": lang})

        full_md: list[str] = []
        async for chunk in vlm_client.stream_ocr(image_b64, model=model, lang=lang):
            full_md.append(chunk)
            _publish(task_id, "chunk", {"task_id": task_id, "chunk": chunk})

        markdown = _strip_image_links(
            _demote_deep_headings(
                _dedupe_repeated_blocks(
                    _normalize_hr(_fix_pseudo_tables(_strip_code_fence("".join(full_md))))
                )
            )
        )
        # 数字自检：识别定稿后立即执行（纯规则，亚毫秒），结果随任务落库
        val = validation.validate_markdown(markdown)
        duration_ms = int((time.monotonic() - t0) * 1000)
        db.update_task(
            task_id,
            status="done",
            markdown=markdown,
            duration_ms=duration_ms,
            validation=json.dumps(val, ensure_ascii=False),
        )
        _publish(
            task_id,
            "done",
            {"task_id": task_id, "status": "done", "markdown": markdown,
             "duration_ms": duration_ms, "validation": val, "lang": lang},
        )
        logger.info("任务 %s 识别完成（%s，%s），耗时 %.1fs，数字自检 %s",
                    task_id, use_model, lang, time.monotonic() - t0,
                    "通过" if val["ok"] else f"{len(val['issues'])} 条可疑")

    except Exception as e:  # noqa: BLE001 —— 后台任务必须兜底，失败落库
        logger.exception("任务 %s 失败", task_id)
        db.update_task(task_id, status="failed", error=str(e)[:500])
        _publish(task_id, "error", {"task_id": task_id, "status": "failed", "error": str(e)[:500]})


def _default_model() -> str:
    from app.config import OLLAMA_MODEL
    return OLLAMA_MODEL


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------
@router.post("/ocr")
async def create_ocr(
    background: BackgroundTasks,
    files: list[UploadFile] = File(...),
    model: str | None = None,
    batch_id: str | None = None,
    batch_size: int | None = None,
):
    """
    上传 1..N 张图片，每张建一个识别任务（异步执行）。
    batch_id/batch_size：前端对一次多张上传生成的批量标记，随任务落库，
    使分组在刷新/换浏览器后仍然保留（此前只存前端内存，刷新即散）。
    """
    if not files:
        raise HTTPException(400, "未收到任何文件")

    results = []
    for f in files:
        filename = f.filename or "unnamed"
        if not is_allowed(filename):
            raise HTTPException(400, f"不支持的文件类型: {filename}")

        raw = await f.read()
        if len(raw) > MAX_UPLOAD_MB * 1024 * 1024:
            raise HTTPException(400, f"{filename} 超过 {MAX_UPLOAD_MB}MB 上限")

        # 原图落盘：uploads/{task_id}_{原名}
        suffix = "." + filename.rsplit(".", 1)[-1].lower()
        task_id = uuid4_hex()
        stored = UPLOAD_DIR / f"{task_id}_{Path(filename).stem[:50]}{suffix}"
        stored.write_bytes(raw)

        db.create_task(filename, stored, task_id=task_id, batch_id=batch_id, batch_size=batch_size)
        results.append({"task_id": task_id, "filename": filename})
        background.add_task(_run_task, task_id, raw, model)

    return {"tasks": results}


async def _run_task(task_id: str, raw: bytes, model: str | None) -> None:
    await _process(task_id, raw, model)


def uuid4_hex() -> str:
    import uuid
    return uuid.uuid4().hex[:12]


@router.post("/tasks/{task_id}/rerun")
async def rerun_task(task_id: str, background: BackgroundTasks, model: str | None = None):
    """
    重新识别：用已存原图再跑一遍（新任务，历史保留，便于对比两次结果）。
    可选 model 参数：如 ?model=qwen2.5vl:32b，用于双模型对比。
    """
    import shutil

    task = db.get_task(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    stored = Path(task["stored_path"])
    if not stored.exists():
        raise HTTPException(404, "原图文件已丢失，无法重新识别")

    raw = stored.read_bytes()
    new_id = uuid4_hex()
    # 复制而非共用原图：避免删除旧任务时连带删掉新任务的图
    new_stored = UPLOAD_DIR / f"{new_id}_{Path(task['filename']).stem[:50]}{stored.suffix}"
    shutil.copyfile(stored, new_stored)

    db.create_task(task["filename"], new_stored, task_id=new_id)
    background.add_task(_run_task, new_id, raw, model)
    return {"task_id": new_id, "filename": task["filename"]}


@router.patch("/tasks/{task_id}")
def patch_task_markdown(task_id: str, payload: dict = Body(...)):
    """人工修正：前端双击编辑后保存 Markdown，落库并标记 edited=1"""
    markdown = payload.get("markdown")
    if markdown is None:
        raise HTTPException(400, "缺少 markdown 字段")
    if not isinstance(markdown, str):
        raise HTTPException(400, "markdown 必须是字符串")
    if not db.get_task(task_id):
        raise HTTPException(404, "任务不存在")
    # 人工修正后重跑数字自检：修正即生效，用户立刻能看到自检结论的变化
    val = validation.validate_markdown(markdown)
    db.update_task(task_id, markdown=markdown, edited=1,
                   validation=json.dumps(val, ensure_ascii=False))
    return {"ok": True, "validation": val}


@router.get("/ocr/{task_id}/stream")
async def stream_result(task_id: str):
    """SSE：实时推送识别进度与 Markdown 片段；已完成任务一次性重放"""
    task = db.get_task(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")

    q: asyncio.Queue = _subscribe(task_id)

    async def gen():
        try:
            # 1) 先发送当前快照（处理刷新/晚订阅的情况）
            snapshot = db.get_task(task_id)  # type: ignore[assignment]
            yield sse("snapshot", {
                "task_id": task_id,
                "status": snapshot["status"],
                "markdown": snapshot["markdown"],
                "error": snapshot["error"],
                "duration_ms": snapshot["duration_ms"],
                "validation": _validation_of(snapshot),
                "lang": snapshot.get("lang") or "",
            })
            # 已终态则直接结束
            if snapshot["status"] in ("done", "failed"):
                return

            # 2) 进行中：实时转发队列事件
            while True:
                try:
                    event, payload = await asyncio.wait_for(q.get(), timeout=60)
                except asyncio.TimeoutError:
                    yield sse("ping", {})  # 心跳，防止代理断开空闲连接
                    continue
                yield sse(event, payload)
                if event in ("done", "error"):
                    return
        finally:
            _unsubscribe(task_id, q)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    })


def sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@router.get("/tasks")
def get_tasks():
    tasks = db.list_tasks()
    for t in tasks:
        t["validation"] = _validation_of(t)
    return {"tasks": tasks}


@router.get("/tasks/{task_id}")
def get_task_detail(task_id: str):
    task = db.get_task(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    task["validation"] = _validation_of(task)
    return task


@router.delete("/tasks/{task_id}")
def remove_task(task_id: str):
    if not db.delete_task(task_id):
        raise HTTPException(404, "任务不存在")
    return {"ok": True}


@router.patch("/batches/{batch_id}")
def rename_batch(batch_id: str, payload: dict = Body(...)):
    """
    批量组重命名（前端点组头右侧的铅笔图标触发）。
    name 传空串 = 清除自定义名，界面回落到默认「批量处理 N 张」。
    只改 batch_name，不动任务本身，也不影响识别结果。
    """
    raw = payload.get("name")
    name = str(raw).strip()[:60] if raw is not None else ""
    updated = db.rename_batch(batch_id, name)
    if updated == 0:
        raise HTTPException(404, "批量组不存在")
    return {"batch_id": batch_id, "name": name, "updated": updated}


@router.get("/files/{task_id}")
def get_original(task_id: str):
    """取任务原图（前端历史回看时展示左栏图片用）"""
    task = db.get_task(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    path = Path(task["stored_path"])
    if not path.exists():
        raise HTTPException(404, "原图文件已丢失")
    return FileResponse(path)


@router.get("/models")
async def get_models():
    """健康检查 + 模型清单（顺便验证 Mac 的 Ollama 可达性）"""
    try:
        return {"ok": True, "models": await vlm_client.list_models()}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:200], "models": []}
