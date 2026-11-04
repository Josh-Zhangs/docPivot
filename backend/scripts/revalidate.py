"""批量重算历史任务的数字自检结果
====================================
用途：改了 validation.py 的规则后，历史任务的 validation 字段仍是旧结论，
前端会继续显示旧横幅。本脚本按最新规则全量重算并写回 SQLite。

特点：
- 只改 validation 字段，不动 markdown，也不动 edited 标记（不算人工修正）
- 默认先算后比再写，打印每条任务的结论变化，便于确认是修复还是回归

用法（在 backend 目录下执行）:
    .venv\\Scripts\\python.exe scripts\\revalidate.py --dry-run   # 只看变化，不落库
    .venv\\Scripts\\python.exe scripts\\revalidate.py             # 重算并写回
"""
import json
import sys
from pathlib import Path

# 允许以 `python scripts/revalidate.py` 方式运行时导入 app 包
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db                      # noqa: E402
from app.services import validation     # noqa: E402


def summarize(val: dict) -> str:
    """把校验结果压成一行摘要：PASS / WARN(n) / FAIL(n) + 规则数"""
    issues = val.get("issues") or []
    errors = [i for i in issues if i["level"] == "error"]
    if val.get("checks_run", 0) == 0:
        return "未校验"
    if errors:
        return f"FAIL({len(errors)})"
    if issues:
        return f"WARN({len(issues)})"
    return "PASS"


def main() -> int:
    dry_run = "--dry-run" in sys.argv

    tasks = [t for t in db.list_tasks(limit=1000) if (t.get("markdown") or "").strip()]
    if not tasks:
        print("没有可重算的任务（markdown 为空）")
        return 0

    changed = 0
    print(f"{'ID':<14}{'文件':<20}{'旧':<12}{'新':<12}说明")
    print("-" * 96)

    for t in tasks:
        md = t["markdown"]
        old_raw = t.get("validation")
        old = None
        if old_raw:
            try:
                old = json.loads(old_raw)
            except (json.JSONDecodeError, TypeError):
                old = None

        new = validation.validate_markdown(md)
        old_s = summarize(old) if old else "无"
        new_s = summarize(new)

        note = ""
        if old_s != new_s:
            changed += 1
            note = "<< 结论变化"
        # 结论没变但消息文本变了（例如措辞调整）也提示一下
        elif old and [i["message"] for i in old.get("issues", [])] != [
            i["message"] for i in new.get("issues", [])
        ]:
            changed += 1
            note = "<< 提示内容变化"

        fname = (t.get("filename") or "")[:18]
        print(f"{t['id']:<14}{fname:<20}{old_s:<12}{new_s:<12}{note}")

        # 有变化时把具体条目打出来，方便判断是修复还是引入了误报
        if note:
            for i in new.get("issues", []):
                print(f"    → ({i['level']}) {i['message']}")

        if not dry_run:
            db.update_task(t["id"], validation=json.dumps(new, ensure_ascii=False))

    print("-" * 96)
    if dry_run:
        print(f"共 {len(tasks)} 条，{changed} 条有变化（--dry-run 未写库）")
    else:
        print(f"共 {len(tasks)} 条，{changed} 条有变化，已写回数据库")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
