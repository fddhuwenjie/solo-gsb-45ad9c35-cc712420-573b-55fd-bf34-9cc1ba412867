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

echo "== 并行合入：基线仍是 revision 2（当前也是 2），先看本端视图"

echo "== 8) 其他人并行发布 revision 3：新增 /news.html 并替换 /index.html 加上新闻链接（预期 200，revision=3）"
post_publish '{"expected_revision":2,"changes":[
  {"op":"upsert","path":"/index.html","content":"<h1 id=\"top\">首页</h1> <a href=\"/docs/\">文档目录</a> <a href=\"/news.html\">新闻</a>"},
  {"op":"upsert","path":"/news.html","content":"<a href=\"/#top\">回到首页</a>"}
]}'

echo "== 9) 并行合入：本端基于过期基线 2，只改未被触碰的 /docs/index.html（预期 200，revision=4，/news.html 原样保留）"
post_merge '{"base_revision":2,"changes":[
  {"op":"upsert","path":"/docs/index.html","content":"<a href=\"../index.html?x=1#top\">文档目录（本端更新）</a>"}
]}'

echo "== 10) 并行合入冲突：本端仍基于基线 2 改已被别人修改的 /index.html（预期 409，列出冲突路径与当前修订号，整批不写入）"
post_merge '{"base_revision":2,"changes":[
  {"op":"upsert","path":"/index.html","content":"<h1>本端旧视图</h1>"}
]}'

echo "== 11) 基线修订不存在（预期 404）"
post_merge '{"base_revision":99,"changes":[]}'

echo "== 12) 其他人从 revision 4 发布 revision 5：删除 /news.html 并清掉引用；本端基于旧基线 4 新增仍链接它的页面（预期 422，列出来源与错误目标，内容及修订号不变）"
post_publish '{"expected_revision":4,"changes":[
  {"op":"upsert","path":"/index.html","content":"<h1 id=\"top\">首页</h1> <a href=\"/docs/\">文档目录</a>"},
  {"op":"delete","path":"/news.html"}
]}'
post_merge '{"base_revision":4,"changes":[
  {"op":"upsert","path":"/mine.html","content":"<a href=\"/news.html\">新闻已被别人删除</a>"}
]}'

echo "== 13) 既有接口不受影响：/publish 过期 expected_revision 仍为 409"
post_publish '{"expected_revision":1,"changes":[]}'

echo "== 当前修订号与最终页面集合"
curl -sS "$BASE/revision"; echo
curl -sS "$BASE/pages"; echo

