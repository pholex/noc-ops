#!/usr/bin/env bash
# 发布前必跑：在干净的 Linux 容器里按 INSTALL.md 走一遍完整安装（需本机 Docker）。
#
#   bash scripts/fresh-install-test/run.sh            # 构建当前源码的 wheel → 容器里装 → 分步跑 → 打印摘要
#   PLATFORM=linux/amd64 bash scripts/fresh-install-test/run.sh   # 换架构（默认跟随本机）
#
# 全文日志落在 $TMPDIR 下（结尾打印路径）；任何一步出现 Python Traceback（行首）即视为失败。
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
HERE="$ROOT/scripts/fresh-install-test"
CTX="$(mktemp -d "${TMPDIR:-/tmp}/noc-fresh-install.XXXXXX")"
LOG="$CTX/run.log"
PLATFORM="${PLATFORM:-}"
PFLAG=()
[ -n "$PLATFORM" ] && PFLAG=(--platform "$PLATFORM")

echo "==> 构建 wheel → $CTX"
uv build --wheel --out-dir "$CTX" >/dev/null
cp "$HERE/Dockerfile" "$HERE/run_install_test.sh" "$CTX/"

echo "==> 构建镜像并运行"
docker build -q "${PFLAG[@]}" -t noc-fresh-install-test -f "$CTX/Dockerfile" "$CTX" >/dev/null
docker run --rm "${PFLAG[@]}" noc-fresh-install-test > "$LOG" 2>&1 || true

echo "==> 摘要（每步退出码）"
grep -E '^############|^\[exit=' "$LOG" | paste - - - - 2>/dev/null || grep -E '^############|^\[exit=' "$LOG"
if grep -q "^Traceback" "$LOG"; then
  echo "❌ 出现 Traceback，见 $LOG"; exit 1
fi
echo "✅ 无 Traceback。完整日志：$LOG"
