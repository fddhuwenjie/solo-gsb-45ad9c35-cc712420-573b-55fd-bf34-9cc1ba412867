"""并行变更合入：基于历史基线修订的变更，经三方比对后在同一写事务内合入当前快照。

语义：
- 客户端携带``基线修订号``（自己改动所基于的历史修订）与沿用发布接口格式的整批变更。
- 即使基线已过期（小于当前修订号），也在同一个 SQLite 写事务内逐个比对变更涉及的
  每条路径在基线快照与当前快照中的**存在性与正文**：
  - 基线与当前一致（都不存在，或都存在且正文相同）→ 无冲突；
  - 否则该路径被他人并行改动过（修改 / 删除 / 同名新建）→ 冲突。
- 任一路径冲突：整批不写入，返回冲突路径与当前修订号（409）。
- 无冲突时按提交顺序把变更应用于**当前快照**（他人新增/未触碰的页面自然保留），
  沿用入口页规则与整包站内链接校验；若他人的并行改动使最终链接失效，
  按发布接口同样的格式列出来源与错误目标（422），内容与修订号均不变。
- 全部通过才生成一个新修订；历史修订快照全部保留可读。
"""

from __future__ import annotations

import sqlite3

from .publish import (
    PublishValidationError,
    _apply_changes,
    _commit_snapshot,
    _load_current_pages,
    _validate_final_pages,
)


class BaselineNotFound(Exception):
    """客户端携带的基线修订号不存在（小于 0 或大于当前修订号）。"""

    def __init__(self, base_revision: int) -> None:
        self.base_revision = base_revision
        super().__init__(f"base revision {base_revision} not found")


class MergeConflict(Exception):
    """变更涉及的路径在基线与当前快照之间已被他人改动；整批不写入。"""

    def __init__(self, current_revision: int, conflicts: list[dict[str, str]]) -> None:
        self.current_revision = current_revision
        self.conflicts = conflicts
        super().__init__(f"merge conflict; current revision is {current_revision}")


def _diff_involved_paths(
    base_pages: dict[str, str],
    current_pages: dict[str, str],
    changes: list[dict[str, str]],
) -> list[dict[str, str]]:
    """比对变更涉及的每条路径在基线与当前快照中的存在性与正文。

    返回冲突列表，每项形如 ``{"path": "/a.html", "reason": "modified since baseline"}``。
    """
    conflicts: list[dict[str, str]] = []
    seen: set[str] = set()
    for change in changes:
        path = change["path"]
        if path in seen:
            continue
        seen.add(path)
        in_base = path in base_pages
        in_current = path in current_pages
        if in_base and in_current:
            if base_pages[path] != current_pages[path]:
                conflicts.append({"path": path, "reason": "modified since baseline"})
        elif in_base:
            conflicts.append({"path": path, "reason": "deleted since baseline"})
        elif in_current:
            conflicts.append({"path": path, "reason": "created since baseline"})
        # 两个快照都不存在：存在性一致，无冲突
    return conflicts


def merge(
    conn: sqlite3.Connection,
    base_revision: int,
    changes: list[dict[str, str]],
) -> int:
    """在单个写事务内完成三方比对、整批试用、链接校验与合入提交。

    成功返回新修订号（当前修订号 + 1）。基线不存在抛 :class:`BaselineNotFound`；
    涉及路径存在并行改动抛 :class:`MergeConflict`；入口页规则或整包链接校验
    不通过抛 :class:`PublishValidationError`。失败时事务回滚，
    已发布内容与修订号均不变。
    """
    try:
        # IMMEDIATE：立即获取写锁（事务内的读也是可重复读），
        # 比对、校验与提交之间不会有其他写事务插入。
        conn.execute("BEGIN IMMEDIATE")

        row = conn.execute("SELECT current_revision FROM meta WHERE id = 1").fetchone()
        current = row["current_revision"]
        if base_revision < 0 or base_revision > current:
            raise BaselineNotFound(base_revision)

        # 1) 三方比对：即使基线已过期，也逐路径核对基线与当前快照。
        #    （基线等于当前时两快照相同，自然无冲突，行为与 /publish 一致。）
        base_pages = _load_current_pages(conn, base_revision)
        current_pages = _load_current_pages(conn, current)
        conflicts = _diff_involved_paths(base_pages, current_pages, changes)
        if conflicts:
            raise MergeConflict(current, conflicts)

        # 2) 无同路径冲突：按提交顺序把变更应用于当前快照，
        #    他人新增的页面与原有内容一并保留。
        pages = _apply_changes(current_pages, changes)

        # 3) 沿用入口页规则与整包站内链接校验：他人的并行改动若使最终
        #    链接失效，在此以 source/target 形式报出，整批不写入。
        _validate_final_pages(pages, current)

        # 4) 全部通过：写入新修订快照并推进修订号（仍在同一事务内），
        #    历史修订快照保持可读。
        new_revision = _commit_snapshot(conn, pages, current)
        conn.commit()
        return new_revision
    except Exception:
        conn.rollback()
        raise
