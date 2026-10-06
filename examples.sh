#!/bin/sh
# 可直接执行的端到端 HTTP 调用示例（依赖 curl）。
# 用法：./examples.sh            # 默认 http://127.0.0.1:8000
#       BASE=http://localhost:8000 ./examples.sh
set -eu

BASE="${BASE:-http://127.0.0.1:8000}"

post_publish() {
  curl -sS -w '\n  -> HTTP %{http_code}\n' -X POST "$BASE/publish" \
    -H 'Content-Type: application/json' -d "$1"
}

post_merge() {
  curl -sS -w '\n  -> HTTP %{http_code}\n' -X POST "$BASE/merge" \
    -H 'Content-Type: application/json' -d "$1"
}

echo "== 健康检查"
curl -sS "$BASE/health"; echo

echo "== 当前修订号（初始为 0）"
curl -sS "$BASE/revision"; echo

echo "== 1) 首次发布必须包含 /index.html（预期 422）"
post_publish '{"expected_revision":0,"changes":[{"op":"upsert","path":"/about.html","content":"<p>about</p>"}]}'

echo "== 2) 首次发布：/index.html + /about.html（预期 200，revision=1）"
post_publish '{"expected_revision":0,"changes":[
  {"op":"upsert","path":"/index.html","content":"<h1 id=\"top\">首页</h1><a href=\"/about.html\">关于</a> <a href=\"https://example.com\">外链不检查</a>"},
  {"op":"upsert","path":"/about.html","content":"<a href=\"/#top\">回到首页锚点</a>"}
]}'

echo "== 3) 删除仍被 index 引用的 about.html（预期 422，列出来源与错误目标）"
post_publish '{"expected_revision":1,"changes":[{"op":"delete","path":"/about.html"}]}'

echo "== 4) 用过期修订号发布（预期 409 conflict）"
post_publish '{"expected_revision":0,"changes":[]}'

echo "== 5) 第二批：替换 index 去掉 about 链接，同时删除 about；再加相对/目录/查询串链接（预期 200，revision=2）"
post_publish '{"expected_revision":1,"changes":[
  {"op":"upsert","path":"/index.html","content":"<h1 id=\"top\">首页</h1> <a href=\"/docs/\">文档目录</a>"},
  {"op":"delete","path":"/about.html"},
  {"op":"upsert","path":"/docs/index.html","content":"<a href=\"../index.html?x=1#top\">查询参数不改变目标</a>"}
]}'

echo "== 6) 读取当前修订的完整页面集合"
curl -sS "$BASE/pages"; echo

echo "== 7) 读取历史 revision=1 的完整集合"
curl -sS "$BASE/pages?revision=1"; echo

echo "== 8) 并行合入：基于过期基线 revision=1 新增页面，与已发生的改动无同路径冲突（预期 200，revision=3）"
post_merge '{"base_revision":1,"changes":[
  {"op":"upsert","path":"/mine.html","content":"<a href=\"/index.html\">首页</a>"}
]}'

echo "== 9) 并行合入冲突：基线 revision=1 的改动触碰此后已被删除的 /about.html（预期 409，列出冲突路径与当前修订号，整批不写入）"
post_merge '{"base_revision":1,"changes":[
  {"op":"upsert","path":"/about.html","content":"<p>my about</p>"}
]}'

echo "== 10) 他人的并行删除使最终链接失效：基于 revision=2 新增指向 /about.html 的页面（预期 422，列出来源与错误目标）"
post_merge '{"base_revision":2,"changes":[
  {"op":"upsert","path":"/newcomer.html","content":"<a href=\"/about.html\">about</a>"}
]}'

echo "== 11) 不存在的基线（预期 404）"
post_merge '{"base_revision":99,"changes":[]}'

echo "== 12) 合入结果：他人新增的页面保留，历史快照仍可读取"
curl -sS "$BASE/pages"; echo
curl -sS "$BASE/pages?revision=2"; echo
