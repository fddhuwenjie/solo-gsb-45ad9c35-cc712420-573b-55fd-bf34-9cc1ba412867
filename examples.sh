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
