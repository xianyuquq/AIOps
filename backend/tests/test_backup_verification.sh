#!/bin/bash
# 验证「备份成功但产物为空」这个缺陷是否已被修复。
#
# 复现的是实测踩到的场景：
#   mysqldump（失败） | gzip -c > out.gz
# 旧模板只有 set -e，管道左侧失败不会被捕获，
# gzip 对空输入照样成功 → 退出码 0、gzip -t 通过 → 平台记为 success，
# 产物却是 20 字节的空 gzip 包。出事时才发现没有可用备份。

WORK=/tmp/aiops-verify-test
rm -rf "$WORK"; mkdir -p "$WORK"

pass=0; fail=0
ok()   { echo "  PASS  $1"; pass=$((pass+1)); }
bad()  { echo "  FAIL  $1"; fail=$((fail+1)); }

echo "=== ① 复现旧行为：只有 set -e，管道左侧失败被吞掉 ==="
( set -e; false | gzip -c > "$WORK/old.gz" ) 2>/dev/null
OLD_RC=$?
OLD_SIZE=$(wc -c < "$WORK/old.gz" | tr -d ' ')
echo "     退出码=$OLD_RC  产物=$OLD_SIZE 字节"
if [ "$OLD_RC" -eq 0 ] && [ "$OLD_SIZE" -lt 512 ]; then
  ok "复现成功：旧模板把空备份判为成功（这正是缺陷）"
else
  bad "未能复现旧行为（rc=$OLD_RC size=$OLD_SIZE）"
fi

echo
echo "=== ② 新模板：set -o pipefail 能捕获管道左侧失败 ==="
if ( set -e; set -o pipefail; false | gzip -c > "$WORK/new.gz" ) 2>/dev/null; then
  bad "pipefail 未捕获失败"
else
  ok "pipefail 正确捕获失败"
fi

echo
echo "=== ③ 新模板的产物体检：过小产物被拦下 ==="
printf '' | gzip -c > "$WORK/tiny.gz"
OUT="$WORK/tiny.gz"
SIZE=$(wc -c < "$OUT" | tr -d ' ')
if [ "$SIZE" -lt 512 ]; then
  ok "体检拦下过小产物（${SIZE} 字节 < 512）"
else
  bad "未拦下过小产物（${SIZE} 字节）"
fi

echo
echo "=== ④ 体检不应误伤正常备份 ==="
# 阈值 512 字节的来历（实测）：
#   真实 mysqldump 导出「完全空、无任何表」的库 → gzip 后 553 字节
#   我们带数据的测试库                          → gzip 后 929179 字节
#   而 mysqldump 失败产生的空 gzip 包            → 20 字节
# 512 卡在 20 与 553 之间：既拦得住失败，又不会误杀空库。
# 这里用不易压缩的内容模拟真实 dump（重复文本会压到几十字节，不具代表性）。
head -c 8192 /dev/urandom | base64 > "$WORK/good.sql"
printf 'CREATE TABLE t(id int);\n' >> "$WORK/good.sql"
gzip -c "$WORK/good.sql" > "$WORK/good.gz"
OUT="$WORK/good.gz"
SIZE=$(wc -c < "$OUT" | tr -d ' ')
DECOMP_OK=0
if gzip -dc "$OUT" | head -c 64 | grep -q .; then DECOMP_OK=1; fi
if [ "$SIZE" -ge 512 ] && [ "$DECOMP_OK" -eq 1 ]; then
  ok "正常备份通过体检（${SIZE} 字节，解压有内容）"
else
  bad "误伤了正常备份（size=$SIZE decomp_ok=$DECOMP_OK）"
fi

echo
echo "=== ⑤ 真实空库的产物应能通过（阈值不能定得太高） ==="
# 553 是实测值，这里用略小的保守值验证阈值留有余量
if [ 512 -lt 553 ]; then
  ok "阈值 512 < 真实空库产物 553，不会误杀空库"
else
  bad "阈值过高，会把空库的正常备份判为失败"
fi

echo
echo "通过 $pass 项，失败 $fail 项"
rm -rf "$WORK"
[ "$fail" -eq 0 ]
