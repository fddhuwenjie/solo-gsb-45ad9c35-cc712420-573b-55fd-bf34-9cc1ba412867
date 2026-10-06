"""并行合入（/merge）端到端测试。运行：pytest tests/test_merge.py"""

from __future__ import annotations

import os
import tempfile

import pytest

# 必须在导入 app 之前指定数据库文件
_TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_TMP.close()
os.environ["DOCS_DB_PATH"] = _TMP.name

from fastapi.testclient import TestClient  # noqa: E402

from app import db  # noqa: E402
from app.main import app  # noqa: E402
from app.merge import _diff_involved_paths  # noqa: E402


@pytest.fixture(scope="module")
def client():
    # 与其他测试模块隔离：直接改用本模块专属的数据库文件
    db.DB_PATH = _TMP.name
    db.init_db()
    with TestClient(app) as c:
        yield c


def publish(client, rev, changes):
    resp = client.post(
        "/publish",
        json={"expected_revision": rev, "changes": changes},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["revision"]


def merge(client, base, changes):
    return client.post("/merge", json={"base_revision": base, "changes": changes})


# ---------- 三方比对纯函数 ----------

def test_diff_involved_paths_reasons():
    base = {"/mod.html": "v1", "/del.html": "x", "/same.html": "s"}
    current = {"/mod.html": "v2", "/new.html": "n", "/same.html": "s"}
    changes = [
        {"op": "upsert", "path": "/mod.html", "content": "mine"},
        {"op": "upsert", "path": "/del.html", "content": "mine"},
        {"op": "upsert", "path": "/new.html", "content": "mine"},
        {"op": "upsert", "path": "/same.html", "content": "mine"},
        {"op": "upsert", "path": "/absent.html", "content": "mine"},
        {"op": "delete", "path": "/mod.html"},  # 同路径重复出现只比对一次
    ]
    conflicts = _diff_involved_paths(base, current, changes)
    assert conflicts == [
        {"path": "/mod.html", "reason": "modified since baseline"},
        {"path": "/del.html", "reason": "deleted since baseline"},
        {"path": "/new.html", "reason": "created since baseline"},
    ]


# ---------- HTTP 端到端 ----------

def test_01_first_merge_requires_index(client):
    resp = merge(client, 0, [{"op": "upsert", "path": "/about.html", "content": "<p>x</p>"}])
    assert resp.status_code == 422
    assert resp.json()["errors"][0]["reason"] == "first publish must include /index.html"
    assert client.get("/revision").json() == {"revision": 0}


def test_02_first_merge_success(client):
    resp = merge(client, 0, [
        {"op": "upsert", "path": "/index.html",
         "content": '<h1 id="top">Home</h1><a href="/about.html">about</a>'},
        {"op": "upsert", "path": "/about.html",
         "content": '<a href="/#top">home</a>'},
    ])
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"revision": 1, "merged": True}


def test_03_merge_with_up_to_date_baseline(client):
    # 基线即当前修订：无冲突，行为与 /publish 一致
    resp = merge(client, 1, [
        {"op": "upsert", "path": "/index.html",
         "content": '<h1 id="top">Home</h1><a href="/about.html">about</a> <a href="/docs/">docs</a>'},
        {"op": "upsert", "path": "/docs/index.html",
         "content": '<a href="../index.html#top">top</a>'},
    ])
    assert resp.status_code == 200, resp.text
    assert resp.json()["revision"] == 2


def test_04_others_publish_in_parallel(client):
    # 其他人基于当前修订正常发布新页面
    assert publish(client, 2, [
        {"op": "upsert", "path": "/other.html", "content": "<p>other</p>"},
    ]) == 3


def test_05_stale_baseline_merge_success(client):
    # 基线已过期（2 < 3），但变更涉及的路径自基线以来未被他人改动 -> 合入成功
    resp = merge(client, 2, [
        {"op": "upsert", "path": "/mine.html", "content": '<a href="/docs/">docs</a>'},
    ])
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"revision": 4, "merged": True}

    pages = client.get("/pages").json()["pages"]
    assert "/mine.html" in pages
    assert "/other.html" in pages  # 他人新增的页面保留
    # 历史快照保持可读且内容不变
    rev2 = client.get("/pages", params={"revision": 2}).json()["pages"]
    assert "/mine.html" not in rev2 and "/other.html" not in rev2


def test_06_conflict_path_created_since_baseline(client):
    # /other.html 在基线（rev 2）之后被他人新建 -> 冲突，整批不写入
    resp = merge(client, 2, [
        {"op": "upsert", "path": "/other.html", "content": "<p>mine</p>"},
    ])
    assert resp.status_code == 409
    body = resp.json()
    assert body["detail"] == "merge conflict"
    assert body["base_revision"] == 2
    assert body["current"] == 4
    assert body["conflicts"] == [{"path": "/other.html", "reason": "created since baseline"}]
    assert client.get("/revision").json() == {"revision": 4}
    assert client.get("/pages").json()["pages"]["/other.html"] == "<p>other</p>"


def test_07_conflict_path_modified_since_baseline(client):
    # 他人修改了 /about.html（rev 5）
    assert publish(client, 4, [
        {"op": "upsert", "path": "/about.html", "content": '<a href="/#top">home v2</a>'},
    ]) == 5
    # 基于 rev 4 的改动也触碰 /about.html -> 冲突
    resp = merge(client, 4, [
        {"op": "upsert", "path": "/about.html", "content": "<p>my about</p>"},
    ])
    assert resp.status_code == 409
    body = resp.json()
    assert body["current"] == 5
    assert body["conflicts"] == [{"path": "/about.html", "reason": "modified since baseline"}]
    assert client.get("/revision").json() == {"revision": 5}


def test_08_conflict_path_deleted_since_baseline(client):
    # 他人删除了 /mine.html（rev 6）
    assert publish(client, 5, [{"op": "delete", "path": "/mine.html"}]) == 6
    # 基于 rev 4（当时 /mine.html 还在）的改动触碰它 -> 冲突
    resp = merge(client, 4, [
        {"op": "upsert", "path": "/mine.html", "content": "<p>mine again</p>"},
    ])
    assert resp.status_code == 409
    body = resp.json()
    assert body["conflicts"] == [{"path": "/mine.html", "reason": "deleted since baseline"}]
    assert client.get("/revision").json() == {"revision": 6}
    assert "/mine.html" not in client.get("/pages").json()["pages"]


def test_09_multiple_conflicts_all_listed(client):
    # 他人再新增 /brand-new.html（rev 7），凑齐三种冲突类型
    assert publish(client, 6, [
        {"op": "upsert", "path": "/brand-new.html", "content": "<p>new</p>"},
    ]) == 7
    resp = merge(client, 4, [
        {"op": "upsert", "path": "/brand-new.html", "content": "<p>a</p>"},  # 基线后新建
        {"op": "upsert", "path": "/about.html", "content": "<p>b</p>"},      # 基线后修改
        {"op": "upsert", "path": "/mine.html", "content": "<p>c</p>"},       # 基线后删除
        {"op": "upsert", "path": "/fresh.html", "content": "<p>d</p>"},      # 无冲突
    ])
    assert resp.status_code == 409
    assert resp.json()["conflicts"] == [
        {"path": "/brand-new.html", "reason": "created since baseline"},
        {"path": "/about.html", "reason": "modified since baseline"},
        {"path": "/mine.html", "reason": "deleted since baseline"},
    ]
    assert client.get("/revision").json() == {"revision": 7}
    assert "/fresh.html" not in client.get("/pages").json()["pages"]


def test_10_baseline_not_found(client):
    resp = merge(client, 99, [])
    assert resp.status_code == 404
    assert resp.json()["detail"] == "base revision not found"
    assert resp.json()["base_revision"] == 99

    resp = merge(client, -1, [])
    assert resp.status_code == 422  # 请求模型拒绝负基线
    assert client.get("/revision").json() == {"revision": 7}


def test_11_parallel_change_breaks_final_links(client):
    # 他人删除 /about.html 并同步去掉 index 里的链接（rev 8）
    assert publish(client, 7, [
        {"op": "upsert", "path": "/index.html",
         "content": '<h1 id="top">Home</h1> <a href="/docs/">docs</a>'},
        {"op": "delete", "path": "/about.html"},
    ]) == 8
    # 基于 rev 7 的改动新增指向 /about.html 的页面：路径本身无冲突，
    # 但他人的并行删除使最终链接失效 -> 422 列出来源与错误目标，内容与修订号不变
    resp = merge(client, 7, [
        {"op": "upsert", "path": "/newcomer.html",
         "content": '<a href="/about.html">about</a>'},
    ])
    assert resp.status_code == 422
    e = resp.json()["errors"][0]
    assert e["source"] == "/newcomer.html"
    assert e["target"] == "/about.html"
    assert e["reason"] == "target page not found"
    assert client.get("/revision").json() == {"revision": 8}
    assert "/newcomer.html" not in client.get("/pages").json()["pages"]


def test_12_entry_page_cannot_be_deleted(client):
    resp = merge(client, 8, [{"op": "delete", "path": "/index.html"}])
    assert resp.status_code == 422
    assert "must not be deleted" in resp.json()["errors"][0]["reason"]
    assert client.get("/revision").json() == {"revision": 8}


def test_13_changes_applied_in_commit_order(client):
    # 同一批内先增后删同一路径：按提交顺序应用，最终不存在
    resp = merge(client, 8, [
        {"op": "upsert", "path": "/temp.html", "content": "<p>t</p>"},
        {"op": "delete", "path": "/temp.html"},
    ])
    assert resp.status_code == 200, resp.text
    assert resp.json()["revision"] == 9
    assert "/temp.html" not in client.get("/pages").json()["pages"]


def test_14_empty_changes_merge(client):
    before = client.get("/pages").json()
    resp = merge(client, 8, [])
    assert resp.status_code == 200
    assert resp.json()["revision"] == 10
    assert client.get("/pages").json()["pages"] == before["pages"]


def test_15_historical_snapshots_preserved(client):
    # 每个历史修订仍是当时完整快照
    rev4 = client.get("/pages", params={"revision": 4}).json()["pages"]
    assert "/mine.html" in rev4 and "/other.html" in rev4
    rev6 = client.get("/pages", params={"revision": 6}).json()["pages"]
    assert "/mine.html" not in rev6 and "/about.html" in rev6
    rev8 = client.get("/pages", params={"revision": 8}).json()["pages"]
    assert "/about.html" not in rev8 and "/brand-new.html" in rev8


def test_16_publish_and_relocate_unaffected(client):
    # /publish 对过期 expected_revision 仍返回 409
    resp = client.post("/publish", json={
        "expected_revision": 1,
        "changes": [{"op": "upsert", "path": "/x.html", "content": "<p>x</p>"}],
    })
    assert resp.status_code == 409
    assert resp.json()["detail"] == "revision conflict"
    assert resp.json()["current"] == 10

    # /relocate 仍可用
    resp = client.post("/relocate", json={
        "expected_revision": 10,
        "old_path": "/other.html",
        "new_path": "/other-moved.html",
    })
    assert resp.status_code == 200, resp.text
    assert resp.json()["revision"] == 11
    pages = client.get("/pages").json()["pages"]
    assert "/other.html" not in pages and "/other-moved.html" in pages
