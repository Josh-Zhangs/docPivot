"""
Ollama VLM 客户端
-----------------
对 Ollama /api/chat 的流式封装。
- 地址与模型全部来自 config（.env），本机 Ollama / Tailscale 远程 Mac / 未来 vLLM 均可切换
- 生成器逐 token 返回文本片段，供路由层经 SSE 推给前端
"""
import json
import logging
from collections.abc import AsyncIterator

import httpx

from app.config import LANG_DEFAULT, OLLAMA_BASE_URL, OLLAMA_MODEL, OLLAMA_TIMEOUT
from app.services.prompt import LANG_PROBE_PROMPT, prompt_for

logger = logging.getLogger("docpivot.vlm")


async def stream_ocr(
    image_b64: str, model: str | None = None, lang: str | None = None
) -> AsyncIterator[str]:
    """
    调用 Ollama 视觉模型做 OCR，流式 yield Markdown 片段。

    :param image_b64: 预处理后的图片 base64（不带 data: 前缀）
    :param model: 可覆盖模型名（默认取配置），方便后续做 7b/32b 对比
    :param lang: 文档语言（ja/zh），决定加载哪套提示词；None 则用默认语言
    """
    payload = {
        "model": model or OLLAMA_MODEL,
        "messages": [
            {
                "role": "user",
                "content": prompt_for(lang),
                "images": [image_b64],
            }
        ],
        "stream": True,
        # temperature 压低：OCR 要的是确定性转录，不要创造性
        "options": {"temperature": 0.1, "num_predict": 4096},
    }

    timeout = httpx.Timeout(OLLAMA_TIMEOUT, connect=15.0)
    # trust_env=False：忽略系统代理环境变量（HTTP_PROXY 等），
    # Tailscale 内网地址必须直连，否则会被本机代理拦截返回 502
    async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
        async with client.stream(
            "POST", f"{OLLAMA_BASE_URL}/api/chat", json=payload
        ) as resp:
            if resp.status_code != 200:
                body = (await resp.aread()).decode("utf-8", errors="replace")
                raise RuntimeError(f"Ollama 返回 {resp.status_code}: {body[:200]}")

            # Ollama 流式返回：每行一个 JSON 对象
            async for line in resp.aiter_lines():
                line = line.strip()
                if not line:
                    continue
                obj = json.loads(line)
                # Ollama 出错时也会返回 200，错误信息在 error 字段里
                if obj.get("error"):
                    raise RuntimeError(f"Ollama 错误: {obj['error']}")
                content = (obj.get("message") or {}).get("content", "")
                if content:
                    yield content
                if obj.get("done"):
                    break


async def list_models() -> list[str]:
    """列出远端 Ollama 已安装的模型（前端可选、健康检查用）"""
    async with httpx.AsyncClient(timeout=10.0, trust_env=False) as client:
        resp = await client.get(f"{OLLAMA_BASE_URL}/api/tags")
        resp.raise_for_status()
        data = resp.json()
    return [m["name"] for m in data.get("models", [])]


# ---------------------------------------------------------------------------
# 语言探测
# ---------------------------------------------------------------------------
_ZH_HINTS = ("zh", "cn", "chinese", "中文", "汉语", "漢語")
_JA_HINTS = ("ja", "jp", "japanese", "日本語", "日文", "日語")
_EN_HINTS = ("en", "english", "英語", "英文")


def _normalize_lang(text: str) -> str | None:
    """
    把模型五花八门的答法归一成 ja / zh / en。
    注意顺序：先查中文/日文关键词再查英文——"japanese" 里含 "ja"，
    而 "english" 里也含 "en"，按 zh → ja → en 的顺序判断不会互相抢。
    """
    t = (text or "").strip().lower()
    if not t:
        return None
    for k in _ZH_HINTS:
        if k in t:
            return "zh"
    for k in _JA_HINTS:
        if k in t:
            return "ja"
    for k in _EN_HINTS:
        if k in t:
            return "en"
    return None


async def detect_language(image_b64: str, model: str | None = None) -> str:
    """
    判断文档语言（ja / zh / en），用于自动加载对应提示词。

    为什么要单独一次调用，而不是让主提示词"顺便判断"：
    主提示词一旦包含判断步骤，模型就多了一件要权衡的事，实测更容易在正文里
    夹带"这是一张日语小票"这类元描述（我们花了很多轮才把这类话压下去）。
    探测只花 1 个 token、图片是 768px 缩略图，开销远低于把主线提示词搞复杂。

    失败一律回退 LANG_DEFAULT：语言判定属于"锦上添花"，绝不能影响识别主流程。
    """
    payload = {
        "model": model or OLLAMA_MODEL,
        "messages": [
            {"role": "user", "content": LANG_PROBE_PROMPT, "images": [image_b64]}
        ],
        "stream": False,
        # temperature=0：判语言要确定性；num_predict=8：只够输出一个词
        "options": {"temperature": 0, "num_predict": 8},
    }
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=15.0),
                                     trust_env=False) as client:
            resp = await client.post(f"{OLLAMA_BASE_URL}/api/chat", json=payload)
            resp.raise_for_status()
            content = ((resp.json().get("message") or {}).get("content") or "")
    except Exception:  # noqa: BLE001 —— 探测失败不能拖垮识别
        logger.warning("语言探测调用失败，回退默认语言 %s", LANG_DEFAULT, exc_info=True)
        return LANG_DEFAULT

    lang = _normalize_lang(content)
    if lang is None:
        logger.info("语言探测返回无法解析（%r），回退默认语言 %s", content[:40], LANG_DEFAULT)
        return LANG_DEFAULT
    return lang
