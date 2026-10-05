"""FastAPI 入口：静态文档包链接一致性发布 API。"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

from . import db
from .publish import PublishConflict, PublishValidationError, publish
from .relocate import relocate


class RelocateRequest(BaseModel):
    """页面搬迁：把已发布的旧页面移动到未占用的新路径，并重写受影响的站内 href。"""

    expected_revision: int = Field(ge=0, description="客户端预期的当前修订号，实现乐观锁")
    old_path: str = Field(description="已发布的旧页面路径，必须以 / 开头，且不能是 /index.html")
    new_path: str = Field(description="未占用的新页面路径，必须以 / 开头，且不能与旧路径相同")

    @field_validator("old_path", "new_path")
    @classmethod
    def _validate_relocate_path(cls, v: str) -> str:
        if not v.startswith("/"):
            raise ValueError("path must start with '/'")
        if len(v) < 2:
            raise ValueError("path must not be empty")
        if "#" in v or "?" in v:
            raise ValueError("path must not contain query string or fragment")
        return v


class Change(BaseModel):
    """单条变更：新增/替换页面（upsert）或删除页面（delete）。"""

    op: Literal["upsert", "delete", "put"] = Field(
        description="upsert/put=新增或替换；delete=删除"
    )
    path: str = Field(description="页面路径，必须以 / 开头，如 /docs/intro.html")
    content: str | None = Field(default=None, description="upsert 时的完整 HTML 内容")

    @field_validator("path")
    @classmethod
    def _validate_path(cls, v: str) -> str:
        if not v.startswith("/"):
            raise ValueError("path must start with '/'")
        if len(v) < 2:
            raise ValueError("path must not be empty")
        if "#" in v or "?" in v:
            raise ValueError("path must not contain query string or fragment")
        return v

    @field_validator("content")
    @classmethod
    def _validate_content(cls, v, info):
        op = info.data.get("op")
        if op in ("upsert", "put") and v is None:
            raise ValueError("content is required for upsert")
        return v


class PublishRequest(BaseModel):
    expected_revision: int = Field(ge=0, description="客户端预期的当前修订号，实现乐观锁")
    changes: list[Change] = Field(default_factory=list)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    yield


app = FastAPI(
    title="静态文档包链接一致性发布 API",
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/revision")
def current_revision():
    conn = db.connect()
    try:
        row = conn.execute("SELECT current_revision FROM meta WHERE id = 1").fetchone()
        return {"revision": row["current_revision"]}
    finally:
        conn.close()


@app.get("/pages")
def list_pages(revision: int | None = None):
    """返回同一修订的完整页面集合（path -> content）。

    不传 revision 时返回当前修订。revision 不存在返回 404。
    """
    conn = db.connect()
    try:
        current = conn.execute(
            "SELECT current_revision FROM meta WHERE id = 1"
        ).fetchone()["current_revision"]
        rev = current if revision is None else revision
        if rev < 0 or rev > current:
            raise HTTPException(status_code=404, detail=f"revision {rev} not found")
        if rev == 0:
            return {"revision": 0, "pages": {}}
        rows = conn.execute(
            "SELECT path, content FROM pages WHERE revision = ? ORDER BY path", (rev,)
        ).fetchall()
        return {"revision": rev, "pages": {row["path"]: row["content"] for row in rows}}
    finally:
        conn.close()


@app.post("/publish")
def publish_endpoint(req: PublishRequest):
    """批量新增/替换/删除页面。校验与发布在同一 SQLite 写事务内完成。"""
    conn = db.connect()
    try:
        new_revision = publish(
            conn,
            expected_revision=req.expected_revision,
            changes=[c.model_dump() for c in req.changes],
        )
        return {"revision": new_revision, "published": True}
    except PublishConflict as exc:
        return JSONResponse(
            status_code=409,
            content={
                "detail": "revision conflict",
                "expected": req.expected_revision,
                "current": exc.current_revision,
            },
        )
    except PublishValidationError as exc:
        return JSONResponse(
            status_code=422,
            content={"detail": "validation failed", "errors": exc.errors},
        )
    finally:
        conn.close()


@app.post("/relocate")
def relocate_endpoint(req: RelocateRequest):
    """搬迁单个已发布页面并改写受影响的站内 href。校验与发布在同一 SQLite 写事务内完成。"""
    conn = db.connect()
    try:
        new_revision = relocate(
            conn,
            expected_revision=req.expected_revision,
            old_path=req.old_path,
            new_path=req.new_path,
        )
        return {
            "revision": new_revision,
            "relocated": True,
            "old_path": req.old_path,
            "new_path": req.new_path,
        }
    except PublishConflict as exc:
        return JSONResponse(
            status_code=409,
            content={
                "detail": "revision conflict",
                "expected": req.expected_revision,
                "current": exc.current_revision,
            },
        )
    except PublishValidationError as exc:
        return JSONResponse(
            status_code=422,
            content={"detail": "validation failed", "errors": exc.errors},
        )
    finally:
        conn.close()
