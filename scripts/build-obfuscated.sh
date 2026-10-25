#!/usr/bin/env bash
# 构建「混淆分发版」wheel（C 方案：PyArmor 字节码混淆，保护源码）。
#
# 产物：build_obf/dist/noc_ops-<版本>-py3-none-macosx_11_0_arm64.whl
#   - 全部 .py 经 PyArmor 混淆（明文不可读）
#   - 捆绑 pyarmor_runtime（arm64 .so）+ 数据文件（assets/dashboard/skill）
#   - 平台标签限定 Apple Silicon（arm64 mac）；装错平台 pip 直接拒绝
#
# 限制（重要）：
#   - 当前用 PyArmor *trial* 版：仅基础混淆、仅生成当前平台 runtime、商用需购授权。
#   - runtime 是 arm64-mac 专用；Intel Mac / Linux 用户需在对应平台另行构建（或购跨平台授权）。
#
# 前置：uv；并在 .venv 里装好 pyarmor 与 wheel：
#   uv pip install pyarmor wheel
#
# 用法：bash scripts/build-obfuscated.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PY=".venv/bin/python"
OUT="build_obf"
PLATFORM_TAG="macosx_11_0_arm64"
VERSION="$(sed -n 's/^__version__ = "\([^"]*\)"/\1/p' src/noc_ops/__init__.py)"

echo "==> 清理旧产物"
rm -rf "$OUT"

echo "==> PyArmor 混淆 src/noc_ops"
"$PY" -m pyarmor.cli gen -O "$OUT" -r src/noc_ops

echo "==> 拷贝数据文件（PyArmor 只处理 .py）"
cp -R src/noc_ops/assets "$OUT/noc_ops/"
cp -R src/noc_ops/dashboard/index.html src/noc_ops/dashboard/favicon.svg \
      src/noc_ops/dashboard/vendor "$OUT/noc_ops/dashboard/"
cp -R src/noc_ops/skill "$OUT/noc_ops/"
cp README.md "$OUT/README.md"
find "$OUT" -name '.DS_Store' -delete

echo "==> 写打包配置（含 runtime 包）"
cat > "$OUT/pyproject.toml" <<EOF
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "noc-ops"
version = "$VERSION"
description = "多云运维 ops CLI（混淆分发版）—— AWS / 腾讯云 / 阿里云"
readme = "README.md"
requires-python = ">=3.11"
dependencies = []   # 核心零依赖；MCP 为可选 extra

[project.scripts]
ops = "noc_ops.cli:main"
ops-mcp = "noc_ops.mcp_server:main"

[tool.hatch.build.targets.wheel]
packages = ["noc_ops", "pyarmor_runtime_000000"]
artifacts = [
    "noc_ops/dashboard/index.html",
    "noc_ops/dashboard/favicon.svg",
    "noc_ops/dashboard/vendor/**",
    "noc_ops/assets/runbooks/*.md",
    "noc_ops/assets/*.toml",
    "noc_ops/skill/*.tmpl",
    "pyarmor_runtime_000000/*.so",
]
EOF

echo "==> 构建 wheel"
uv build --wheel "$OUT" -o "$OUT/dist"

echo "==> 重打平台标签 → ${PLATFORM_TAG} （限 arm64-mac，装错平台 fail-fast）"
"$PY" -m wheel tags --platform-tag "${PLATFORM_TAG}" --remove \
    "${OUT}/dist/noc_ops-${VERSION}-py3-none-any.whl" >/dev/null

WHEEL="${OUT}/dist/noc_ops-${VERSION}-py3-none-${PLATFORM_TAG}.whl"
echo
echo "✅ 完成：$WHEEL"
echo "   发布前可自检：unzip -l \"$WHEEL\" | grep -E 'pyarmor_runtime|deploy_config'"
