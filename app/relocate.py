"""页面搬迁事务：移动单个已发布页面，并在同一写事务内改写受影响的站内 href。

搬迁语义：
- 旧路径（已发布且非 ``/index.html``）消失，新路径（未占用）接管同一正文与元素 id。
- 其他页面中解析后指向旧页的站内 href 改指新页（绝对 href 用绝对新路径，
  相对 href 用相对新路径），原始查询串与 ``#片段`` 文本原样保留。
- 搬迁页自身的相对 href 以新位置为基准重定位，使其原本指向的页面与片段仍可到达；
  纯片段（``#x``）/纯查询（``?x``）指向本页，无需改动；绝对 href 与位置无关，
  仅当其指向旧页本身（绝对自指）时改指新页。
- 外部链接、非链接内容（正文文本、注释、其他标签的属性等）一律不改。

沿用 :mod:`app.linkcheck` 的既有解析规则，并在搬迁后的最终页面集合上再跑一次
整包链接校验作为兜底：新路径冲突或任一链接无法维持指向时整体回滚，
内容与修订号均不变。成功时在同一事务内生成新修订快照，历史修订仍可读取。
"""

from __future__ import annotations

import posixpath
import re
import sqlite3
from html import unescape
from html.parser import HTMLParser

from .linkcheck import check_links, is_external, resolve_target
from .publish import INDEX, PublishConflict, PublishValidationError, _load_current_pages

# 仅匹配 <a> 开始标签内的 href 属性（前面必须是空白，避免命中 data-href 之类）。
_HREF_ATTR_RE = re.compile(
    r"""(?<![^\s])href\s*=\s*("[^"]*"|'[^']*'|[^\s>]+)""",
    re.IGNORECASE,
)


class _AnchorCollector(HTMLParser):
    """收集页面内每个 ``<a>`` 开始标签的精确字节范围与标签原文。"""

    def __init__(self, line_starts: list[int]) -> None:
        super().__init__(convert_charrefs=True)
        self._line_starts = line_starts
        # (起始偏移, 结束偏移, 标签原文)
        self.anchors: list[tuple[int, int, str]] = []

    def _abs_offset(self) -> int:
        line, col = self.getpos()
        return self._line_starts[line - 1] + col

    def _record(self, tag_text: str) -> None:
        start = self._abs_offset()
        self.anchors.append((start, start + len(tag_text), tag_text))

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            self._record(self.get_starttag_text())

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            self._record(self.get_starttag_text())


def _line_starts(html: str) -> list[int]:
    starts = [0]
    for i, ch in enumerate(html):
        if ch == "\n":
            starts.append(i + 1)
    return starts


def _replace_href_in_tag(tag_text: str, new_value: str) -> str:
    """在单个 <a> 标签原文内替换 href 的值，保留引号与值内首尾空白。"""
    m = _HREF_ATTR_RE.search(tag_text)
    assert m is not None
    token = m.group(1)
    if token[:1] in ("'", '"'):
        quote = token[0]
        inner = token[1:-1]
        leading = inner[: len(inner) - len(inner.lstrip())]
        trailing = inner[len(inner.rstrip()) :]
        new_token = f"{quote}{leading}{new_value}{trailing}{quote}"
    else:
        new_token = new_value
    return tag_text[: m.start(1)] + new_token + tag_text[m.end(1) :]


def _raw_href(tag_text: str) -> str | None:
    """从 <a> 标签原文提取 href 的原始文本（不去除值内首尾空白），无 href 返回 None。"""
    m = _HREF_ATTR_RE.search(tag_text)
    if m is None:
        return None
    token = m.group(1)
    if token[:1] in ("'", '"'):
        return token[1:-1]
    return token


def rewrite_anchor_hrefs(html: str, rewrite) -> str:
    """逐个改写页面内的 ``<a href>``。

    ``rewrite(raw_href)`` 收到保留 HTML 实体与空白的原始值，返回新的
    href 文本（去除首尾空白后的形式）或 ``None``（不改）。
    除 <a href> 之外的任何字节（正文、注释、其他标签、属性排版、HTML 实体）
    都原样保留；无任何 href 变化时返回原字符串。
    """
    collector = _AnchorCollector(_line_starts(html))
    collector.feed(html or "")
    collector.close()

    edits: list[tuple[int, int, str]] = []
    for start, end, tag_text in collector.anchors:
        raw = _raw_href(tag_text)
        if raw is None:
            continue
        new_href = rewrite(raw)
        if new_href is not None and new_href != raw.strip():
            edits.append((start, end, _replace_href_in_tag(tag_text, new_href)))

    if not edits:
        return html

    out = html
    for start, end, new_tag in reversed(edits):
        out = out[:start] + new_tag + out[end:]
    return out


def _split_href(href: str) -> tuple[str, str, str]:
    """把 href 拆成 (路径部分, 含?的查询串原文, 含#的片段原文)，后两者保留原文。"""
    if "#" in href:
        before, fragment = href.split("#", 1)
        fragment = "#" + fragment
    else:
        before, fragment = href, ""
    if "?" in before:
        path_part, query = before.split("?", 1)
        query = "?" + query
    else:
        path_part, query = before, ""
    return path_part, query, fragment


def _is_directory_form(path_part: str) -> bool:
    """与 linkcheck.resolve_target 的目录判定保持一致。"""
    return path_part.endswith("/") or path_part.rstrip("/") == "."


def _repoint_other_page_href(source: str, old: str, new: str, href: str) -> str | None:
    """其他页面：解析后指向旧页的站内 href 改指新页；其余不改。

    ``href`` 是标签原文中的值（HTML 实体、查询串、片段均未解码），
    目标判定用反转义后的值，输出保留原始查询串与片段文本。
    """
    h = href.strip()
    if not h or is_external(h):
        return None
    target, _fragment, reason = resolve_target(source, unescape(h))
    if reason is not None or target != old:
        return None

    path_part, query, fragment = _split_href(h)
    if path_part.startswith("/"):
        base = new
    else:
        base = posixpath.relpath(new, posixpath.dirname(source))
    return base + query + fragment


def _rebase_moved_page_href(old: str, new: str, href: str) -> str | None:
    """搬迁页自身：以新位置为基准重定位相对 href；绝对 href 仅自指时改指新页。"""
    h = href.strip()
    if not h or is_external(h):
        return None

    path_part, query, fragment = _split_href(h)
    if path_part == "":
        return None  # #片段 / ?查询 / 空路径：仍指向本页，片段随 id 原样保留

    target, _fragment, reason = resolve_target(old, unescape(h))
    if reason is not None:
        return None  # 现有快照本就通过校验，不会发生；留给整包校验兜底

    new_dir = posixpath.dirname(new)

    if path_part.startswith("/"):
        # 绝对路径与页面位置无关；只有指向旧页自身的绝对链接需要改指新页。
        return new + query + fragment if target == old else None

    # 相对 href：以新页面所在目录为基准重新计算。
    if _is_directory_form(path_part):
        if target == old:
            # 以目录形式自指（如旧页是某目录的 index.html，href="."）：
            # 新位置是普通文件，直接给出相对到新页自身的文件名。
            base = posixpath.basename(new)
        else:
            rel = posixpath.relpath(posixpath.dirname(target), new_dir)
            base = "." if rel == "." else rel + "/"
    else:
        base = posixpath.relpath(target, new_dir)
    return base + query + fragment


def relocate(
    conn: sqlite3.Connection,
    expected_revision: int,
    old_path: str,
    new_path: str,
) -> int:
    """在单个写事务内完成页面搬迁、链接改写、整包校验与新修订提交。

    成功返回新修订号。过期修订号抛 :class:`PublishConflict`；
    新路径冲突（已占用，含新旧相同）、旧页不存在、旧页为入口页或搬迁后
    链接无法维持指向时抛 :class:`PublishValidationError`，事务回滚，
    已发布内容与修订号均不变。
    """
    try:
        conn.execute("BEGIN IMMEDIATE")

        row = conn.execute("SELECT current_revision FROM meta WHERE id = 1").fetchone()
        current = row["current_revision"]
        if expected_revision != current:
            raise PublishConflict(current)

        pages = _load_current_pages(conn, current)

        pre_errors: list[dict[str, str]] = []
        if old_path == INDEX:
            pre_errors.append(
                {"source": "", "target": old_path, "reason": "entry page /index.html cannot be relocated"}
            )
        if old_path not in pages:
            pre_errors.append(
                {"source": "", "target": old_path, "reason": "source page not found"}
            )
        if new_path in pages:
            pre_errors.append(
                {"source": "", "target": new_path, "reason": "new path already exists"}
            )
        if pre_errors:
            raise PublishValidationError(pre_errors)

        # 1) 改写其他页面指向旧页的站内 href。
        new_pages: dict[str, str] = {}
        for path, content in pages.items():
            if path == old_path:
                continue
            new_pages[path] = rewrite_anchor_hrefs(
                content, lambda h, s=path: _repoint_other_page_href(s, old_path, new_path, h)
            )

        # 2) 以新位置重定位搬迁页自身的相对 href，正文与 id 随内容原样保留。
        moved_content = rewrite_anchor_hrefs(
            pages[old_path], lambda h: _rebase_moved_page_href(old_path, new_path, h)
        )
        new_pages[new_path] = moved_content

        # 3) 沿用整包链接校验兜底：任一链接无法维持指向即整体失败。
        link_errors = check_links(new_pages)
        if link_errors:
            raise PublishValidationError(link_errors)

        # 4) 写入新修订快照并推进修订号（仍在同一事务内）。
        new_revision = current + 1
        conn.executemany(
            "INSERT INTO pages (revision, path, content) VALUES (?, ?, ?)",
            [(new_revision, path, content) for path, content in sorted(new_pages.items())],
        )
        conn.execute("UPDATE meta SET current_revision = ? WHERE id = 1", (new_revision,))
        conn.commit()
        return new_revision
    except Exception:
        conn.rollback()
        raise
