"""
SQLite 任务存储
--------------
V1 使用标准库 sqlite3，零依赖零运维。
设计约定：
- 只在 async 环境外做短平快的同步读写（任务量小，无性能压力）
- 任务状态机：queued -> preprocessing -> recognizing -> done / failed
"""
import sqlite3
import uuid
from datetime import datetime
from typing import Any

from app.config import DB_PATH

# check_same_thread=False：FastAPI 线程池中执行 to_thread 时可复用连接
_conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
_conn.row_factory = sqlite3.Row


def init_db() -> None:
    """建表（幂等，启动时调用）；含轻量迁移：老库补 edited 列"""
    _conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tasks (
            id          TEXT PRIMARY KEY,              -- uuid
            filename    TEXT NOT NULL,                 -- 原始文件名
            stored_path TEXT NOT NULL,                 -- 原图落盘路径
            status      TEXT NOT NULL DEFAULT 'queued',-- 状态机
            markdown    TEXT DEFAULT '',               -- 识别结果
            error       TEXT DEFAULT '',               -- 失败原因
            model       TEXT DEFAULT '',               -- 使用的模型
            duration_ms INTEGER DEFAULT 0,             -- 识别耗时
            created_at  TEXT NOT NULL,
            edited      INTEGER DEFAULT 0              -- 1=结果经人工修正
        )
        """
    )
    # 老库迁移：已存在的 tasks 表补 edited 列
    cols = [r[1] for r in _conn.execute("PRAGMA table_info(tasks)")]
    if "edited" not in cols:
        _conn.execute("ALTER TABLE tasks ADD COLUMN edited INTEGER DEFAULT 0")
    # 数字自检结果（JSON 字符串；NULL=未校验）
    if "validation" not in cols:
        _conn.execute("ALTER TABLE tasks ADD COLUMN validation TEXT")
    # 批量标记：一次多张上传的任务共享 batch_id（前端生成），
    # 持久化到库里才能在刷新/换浏览器后仍保持分组（此前只存前端内存，刷新即散）
    if "batch_id" not in cols:
        _conn.execute("ALTER TABLE tasks ADD COLUMN batch_id TEXT")
    if "batch_size" not in cols:
        _conn.execute("ALTER TABLE tasks ADD COLUMN batch_size INTEGER")
    # 文档语言（ja/zh/en）：识别前由缩略图探测得出，决定加载哪套提示词；
    # 落库后前端可显示"日本語/中文"徽章，批量评测时也能按语言分组统计
    if "lang" not in cols:
        _conn.execute("ALTER TABLE tasks ADD COLUMN lang TEXT")
    # 批量组自定义名称（用户给这次批处理起的名字，如"11月对账单"）：
    # 存在组内每个任务上而不是单独建表——批量组本身就是 batch_id 的派生视图，
    # 去掉一张表就少一次 JOIN 与一致性问题（改名=给同组所有行写同一个值）
    if "batch_name" not in cols:
        _conn.execute("ALTER TABLE tasks ADD COLUMN batch_name TEXT")
    _conn.commit()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def create_task(
    filename: str,
    stored_path: str,
    task_id: str | None = None,
    batch_id: str | None = None,
    batch_size: int | None = None,
) -> str:
    """新建一条识别任务，返回 task_id（允许外部显式指定 id，保证落盘文件名与任务一致）"""
    task_id = task_id or uuid.uuid4().hex[:12]
    _conn.execute(
        "INSERT INTO tasks (id, filename, stored_path, status, created_at, batch_id, batch_size) "
        "VALUES (?, ?, ?, 'queued', ?, ?, ?)",
        (task_id, filename, str(stored_path), _now(), batch_id, batch_size),
    )
    _conn.commit()
    return task_id


def update_task(task_id: str, **fields: Any) -> None:
    """按字段更新任务（status / markdown / error / model / duration_ms）"""
    if not fields:
        return
    sets = ", ".join(f"{k} = ?" for k in fields)
    _conn.execute(f"UPDATE tasks SET {sets} WHERE id = ?", (*fields.values(), task_id))
    _conn.commit()


def get_task(task_id: str) -> dict | None:
    row = _conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return dict(row) if row else None


def list_tasks(limit: int = 100) -> list[dict]:
    rows = _conn.execute(
        "SELECT * FROM tasks ORDER BY created_at DESC, id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


def rename_batch(batch_id: str, name: str | None) -> int:
    """
    给批量组改名（同组所有任务写同一个 batch_name），返回受影响行数。
    name 为空 → 写入 NULL，前端回落到默认的「批量处理 N 张」。
    """
    cur = _conn.execute(
        "UPDATE tasks SET batch_name = ? WHERE batch_id = ?",
        (name or None, batch_id),
    )
    _conn.commit()
    return cur.rowcount


def delete_task(task_id: str) -> bool:
    """删除任务记录，同时清理原图文件"""
    from pathlib import Path

    task = get_task(task_id)
    if not task:
        return False
    _conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    _conn.commit()
    stored = Path(task["stored_path"])
    if stored.exists():
        stored.unlink(missing_ok=True)
    return True
