"""SQLite 连接与初始化。

表结构：
- meta：单行，记录当前已发布修订号（0 表示尚未发布）。
- pages：按修订号保存每个页面的完整快照。读接口永远只读取
  ``revision = 当前修订号`` 的那一批行，因此发布是原子的，
  重启进程后数据仍在（文件型 SQLite）。
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

DB_PATH = os.environ.get("DOCS_DB_PATH", str(Path(__file__).resolve().parent.parent / "data" / "docs.db"))

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS meta (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    current_revision INTEGER NOT NULL DEFAULT 0
);
INSERT OR IGNORE INTO meta (id, current_revision) VALUES (1, 0);

CREATE TABLE IF NOT EXISTS pages (
    revision INTEGER NOT NULL,
    path     TEXT    NOT NULL,
    content  TEXT    NOT NULL,
    PRIMARY KEY (revision, path)
);
"""


def connect() -> sqlite3.Connection:
    """打开一个配置好的数据库连接（每次请求一个连接）。"""
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    """创建表并写入初始修订号 0。幂等，可重复执行。"""
    conn = connect()
    try:
        conn.executescript(_SCHEMA)
        conn.commit()
    finally:
        conn.close()
