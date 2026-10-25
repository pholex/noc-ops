#!/bin/sh
# ops —— 一键安装脚本（托管 curl|sh 目标）。
#
#   curl -fsSL https://raw.githubusercontent.com/pholex/noc-ops/main/scripts/install.sh | sh
#
# 做的事：① 平台自检（macOS / Linux）② 检查云 CLI，交互选择安装或自行安装
#         ③ 仅补装缺失的 uv / noc-ops ④ 未配置时运行 `ops setup` ⑤ 打印下一步。
# 云 CLI 仅在用户选择并确认后安装；凭证仍由用户配置。
#
# 环境变量：
#   NOC_EXTRAS   可选 extra，如 "[mcp]"（默认不装：核心功能只用 stdlib）
#   NOC_PACKAGE  显式指定安装源时替换现有安装（可用本地 wheel 或 URL 做预发布测试）
#   NOC_NONINTERACTIVE=1  不询问，跳过云 CLI 安装，仅补齐缺失的基础工具和配置
set -eu

PKG="${NOC_PACKAGE:-noc-ops}${NOC_EXTRAS:-}"

C_BOLD="" C_DIM="" C_GREEN="" C_YELLOW="" C_RED="" C_RESET=""
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ] && [ "${TERM:-}" != dumb ]; then
  C_BOLD="$(printf '\033[1m')" C_DIM="$(printf '\033[2m')"
  C_GREEN="$(printf '\033[32m')" C_YELLOW="$(printf '\033[33m')"
  C_RED="$(printf '\033[31m')" C_RESET="$(printf '\033[0m')"
fi
say()  { printf '%s==>%s %s\n' "$C_BOLD" "$C_RESET" "$1"; }
warn() { printf '%s! %s%s\n' "$C_YELLOW" "$1" "$C_RESET"; }
die()  { printf '%s! %s%s\n' "$C_RED" "$1" "$C_RESET" >&2; exit 1; }

check_heading() {
  printf '\n%s%s%s\n' "$C_BOLD" "$1" "$C_RESET"
  printf '%s  工具        版本            状态    用途%s\n' "$C_DIM" "$C_RESET"
}

# 固定宽度列只放 ASCII；中文说明在末列，避免中英文宽度不同导致错位。
check_row() (
  row_color="$C_GREEN"
  case "$3" in 未安装|需检查|未配置|待刷新) row_color="$C_YELLOW" ;; esac
  printf '  %-10s  %-14s  %s%s%s  %s\n' "$1" "$2" "$row_color" "$3" "$C_RESET" "$4"
)

version_number() {
  LC_ALL=C sed -nE '
    /^[[:space:]]*(WARNING|WARN|ERROR|INFO|DEBUG)(:|[[:space:]])/d
    s/^[^0-9]*([0-9]+(\.[0-9]+)+[a-zA-Z0-9.+-]*).*$/\1/p
  ' | sed -n '1p'
}

# 只读取已安装 skill 的 frontmatter metadata.version，不从程序版本推断。
read_skill_version() {
  awk '
    NR == 1 { if ($0 !~ /^---\r?$/) exit; next }
    /^---\r?$/ { exit }
    /^metadata:[[:space:]]*$/ { metadata = 1; next }
    /^[^[:space:]]/ { metadata = 0 }
    metadata && /^  version:[[:space:]]*/ {
      value = $0
      sub(/^  version:[[:space:]]*/, "", value)
      gsub(/["\047]/, "", value)
      sub(/[[:space:]]*$/, "", value)
      if (value ~ /^[0-9]+(\.[0-9]+)+[a-zA-Z0-9.+-]*$/) print value
      exit
    }
  ' "$1" 2>/dev/null
}

check_base_tools() {
  HAVE_UV=0
  NEED_NOC=1
  NEED_SETUP=1
  REINSTALL_NOC=0
  check_heading "基础环境"
  if command -v uv >/dev/null 2>&1; then
    base_version="$(uv --version </dev/null 2>/dev/null)" || die "已找到 uv，但无法运行；请先修复，不会自动覆盖。"
    HAVE_UV=1
    check_row uv "$(printf '%s\n' "$base_version" | version_number)" 已安装 包管理器
  else
    check_row uv - 未安装 包管理器
  fi
  if command -v ops >/dev/null 2>&1; then
    base_version="$(ops --version </dev/null 2>/dev/null)" || die "已找到 ops，但无法读取版本；请先修复，不会自动覆盖。"
    base_version="$(printf '%s\n' "$base_version" | sed -n '1p')"
    case "$base_version" in
      'noc-ops '[0-9]*) ;;
      *) die "PATH 中的 ops 未识别为 noc-ops；请检查命令冲突，不会自动覆盖。" ;;
    esac
    NEED_NOC=0
    check_row noc-ops "$(printf '%s\n' "$base_version" | version_number)" 已安装 多云运维工具
  else
    check_row noc-ops - 未安装 多云运维工具
  fi
  # 显式传入安装源或 extra 表示用户要求这份安装，保留预发布测试与 MCP 安装入口。
  if [ -n "${NOC_PACKAGE:-}" ] || [ -n "${NOC_EXTRAS:-}" ]; then
    NEED_NOC=1
    REINSTALL_NOC=1
    printf '  · 已显式指定安装源或 extra，将安装指定包：%s\n' "$PKG"
  fi
  if test -s "$HOME/.config/noc-ops/config.toml" &&
    { test -s "$HOME/.agents/skills/multicloud-ops/SKILL.md" ||
      test -s "$HOME/.claude/skills/multicloud-ops/SKILL.md"; }; then
    skill_file="$HOME/.agents/skills/multicloud-ops/SKILL.md"
    test -s "$skill_file" || skill_file="$HOME/.claude/skills/multicloud-ops/SKILL.md"
    skill_version="$(read_skill_version "$skill_file")" || skill_version=""
    skill_detail="技能"
    if [ -z "$skill_version" ]; then
      skill_version=-
      skill_detail="技能（版本未标注）"
    fi
    if [ "$NEED_NOC" = 0 ]; then
      NEED_SETUP=0
      check_row skill "$skill_version" 已配置 "$skill_detail"
    else
      check_row skill "$skill_version" 待刷新 "$skill_detail"
    fi
  else
    check_row skill - 未配置 技能
  fi
}

base_plan() {
  NEED_UV=0
  if [ "$HAVE_UV" = 0 ]; then
    case " $CLOUD_SELECTION " in
      *' 3 '*|*' 9 '*) NEED_UV=1 ;;
    esac
    [ "$NEED_NOC" = 0 ] || NEED_UV=1
  fi
  BASE_CHANGES=$((NEED_UV + NEED_NOC + NEED_SETUP))
}

show_base_plan() {
  if [ "$BASE_CHANGES" = 0 ]; then
    return 0
  fi
  say "待执行的基础步骤："
  [ "$NEED_UV" = 0 ] || printf '  安装缺失的 uv（官方安装脚本）。\n'
  if [ "$NEED_NOC" = 1 ]; then
    if [ "$REINSTALL_NOC" = 1 ]; then
      printf '  按显式指定的安装源或 extra 安装：uv tool install --force %s\n' "$PKG"
    else
      printf '  安装缺失的 noc-ops：uv tool install %s\n' "$PKG"
    fi
  fi
  [ "$NEED_SETUP" = 0 ] || printf '  运行 ops setup，初始化工作区与客户端 skill。\n'
  return 0
}

# 子 shell 隔离变量；只查询本机版本，不检查凭证或调用云 API。
# 关闭 stdin，避免云 CLI 消耗 curl | sh 管道中尚未执行的安装脚本。
check_cloud_cli() (
  cloud_label="$1"
  cloud_cli="$2"
  cloud_install_url="$3"
  shift 3
  if cloud_cli_path="$(command -v "$cloud_cli")"; then
    if cloud_version="$("$cloud_cli" "$@" </dev/null 2>&1)" && [ -n "$cloud_version" ]; then
      cloud_version="$(printf '%s\n' "$cloud_version" | version_number)"
      if [ -n "$cloud_version" ]; then
        check_row "$cloud_cli" "$cloud_version" 已安装 "$cloud_label"
        return 0
      fi
    fi
    check_row "$cloud_cli" - 需检查 "${cloud_label}（版本读取失败）"
  else
    check_row "$cloud_cli" - 未安装 "$cloud_label"
    printf '    安装指引：%s\n' "$cloud_install_url"
  fi
)

# 同一份映射用于预览与执行；不使用 eval，也不升级已在 PATH 中的 CLI。
cloud_install() (
  cloud_action="$1"
  case "$2" in
    1) cloud_cli=aws; set -- brew install awscli ;;
    2) cloud_cli=aliyun; set -- brew install aliyun-cli ;;
    3) cloud_cli=tccli; set -- uv tool install tccli ;;
    4) cloud_cli=coscli; set -- manual ;;
    5) cloud_cli=ve; set -- npm install -g @volcengine/cli ;;
    6) cloud_cli=tosutil; set -- manual ;;
    7) cloud_cli=gcloud; set -- brew install --cask gcloud-cli ;;
    8) cloud_cli=az; set -- brew install azure-cli ;;
    9) cloud_cli=oci; set -- uv tool install oci-cli ;;
    *) return 1 ;;
  esac
  if command -v "$cloud_cli" >/dev/null 2>&1; then
    printf '  %s：已安装，跳过（不升级）。\n' "$cloud_cli"
    return 0
  fi
  case "$1" in
    manual)
      printf '  %s：请按上方官方指引手工安装。\n' "$cloud_cli"
      return 0 ;;
    brew)
      if [ "$PLATFORM" != Darwin ] || ! command -v brew >/dev/null 2>&1; then
        printf '  %s：自动安装需要 macOS + Homebrew，请按上方官方指引手工安装。\n' "$cloud_cli"
        return 0
      fi ;;
    npm)
      if ! command -v npm >/dev/null 2>&1; then
        printf '  %s：自动安装需要 npm，请按上方官方指引手工安装。\n' "$cloud_cli"
        return 0
      fi ;;
  esac
  printf '  %s：%s\n' "$cloud_cli" "$*"
  [ "$cloud_action" = install ] || return 0
  if "$@" </dev/tty; then
    if command -v "$cloud_cli" >/dev/null 2>&1; then
      printf '  ✓ %s 安装完成；仍需自行配置凭证。\n' "$cloud_cli"
    else
      warn "$cloud_cli 安装命令已完成，但当前 PATH 未找到该工具；请按官方指引检查 PATH。"
    fi
  else
    warn "$cloud_cli 安装失败；可稍后按官方指引手工安装，将继续处理其余步骤。"
  fi
)

choose_cloud_clis() {
  CLOUD_SELECTION=""
  base_plan
  if [ "${NOC_NONINTERACTIVE:-0}" = 1 ] || ! ( : </dev/tty ) 2>/dev/null; then
    say "非交互模式：跳过云 CLI 安装，仅补齐缺失的基础工具和配置。"
    show_base_plan
    return
  fi
  cloud_missing=""
  cloud_menu=""
  for cloud_entry in '1 AWS aws' '2 阿里云 aliyun' '3 腾讯云 tccli' \
    '4 腾讯云COS coscli' '5 火山引擎 ve' '6 火山引擎TOS tosutil' \
    '7 GoogleCloud gcloud' '8 Azure az' '9 OracleCloud oci'; do
    # 映射是固定文本；仅按空格拆分，不含用户输入。
    set -- $cloud_entry
    if ! command -v "$3" >/dev/null 2>&1; then
      cloud_missing="${cloud_missing:+$cloud_missing }$1"
      cloud_menu="${cloud_menu}
  $1 $2 [$3] 未安装"
    fi
  done
  if [ -z "$cloud_missing" ]; then
    show_base_plan
    [ "$BASE_CHANGES" != 0 ] || return 0
    while :; do
      printf '是否执行以上基础步骤？[y 继续 / 回车或 q 退出]：'
      if ! IFS= read -r cloud_confirm </dev/tty; then
        say "已退出。"
        exit 0
      fi
      case "$cloud_confirm" in
        y|Y|yes|YES) return ;;
        ''|n|N|q|Q) say "已退出，未安装任何软件。"; exit 0 ;;
        *) warn "请输入 y 继续，或回车 / q 退出。" ;;
      esac
    done
  fi
  printf '  只需安装你使用的云工具；已安装不代表凭证已配置，版本也不代表最新。\n'
  printf '  云 CLI 只在选择并确认后安装。\n'
  printf '\n以下云 CLI 尚未安装，请按需选择：\n'
  printf '  回车 / 0   云 CLI 我自行处理，仅补齐缺失的基础工具和配置（默认）\n'
  printf '%s\n' "$cloud_menu"
  cat <<'EOF'
  q          退出，不安装任何软件

自动安装方式：AWS / 阿里云 / Google Cloud / Azure 使用 macOS Homebrew；
腾讯云 / Oracle 使用 uv；火山引擎使用 npm。COS / TOS 提供手工安装指引。
缺少 Homebrew / npm 或平台不适用时，提供手工指引，不自动安装这些包管理器。
EOF
  show_base_plan
  while :; do
    CLOUD_SELECTION=""
    printf '\n选择（多个编号用空格分隔；回车自行安装；q 退出）：'
    if ! IFS= read -r cloud_choice </dev/tty; then
      say "已退出。"
      exit 0
    fi
    case "$cloud_choice" in
      q|Q) say "已退出，未安装任何软件。"; exit 0 ;;
      ''|0) return ;;
      *[!1-9\ ]*) warn "请输入 1–9 的编号，以空格分隔；或输入 0 / q。"; continue ;;
    esac
    CLOUD_SELECTION=""
    cloud_valid=1
    for cloud_id in $cloud_choice; do
      case "$cloud_id" in
        [1-9])
          case " $cloud_missing " in
            *" $cloud_id "*) ;;
            *) cloud_valid=0; continue ;;
          esac
          case " $CLOUD_SELECTION " in
            *" $cloud_id "*) ;;
            *) CLOUD_SELECTION="${CLOUD_SELECTION:+$CLOUD_SELECTION }$cloud_id" ;;
          esac ;;
        *) cloud_valid=0 ;;
      esac
    done
    if [ "$cloud_valid" != 1 ] || [ -z "$CLOUD_SELECTION" ]; then
      warn "请输入菜单中缺失工具的有效编号，以空格分隔。"
      continue
    fi
    say "将按以下方式处理所选云 CLI："
    for cloud_id in $CLOUD_SELECTION; do
      cloud_install preview "$cloud_id"
    done
    base_plan
    show_base_plan
    printf '确认执行以上步骤？[y 确认 / 回车重新选择 / q 退出]：'
    if ! IFS= read -r cloud_confirm </dev/tty; then
      say "已退出。"
      exit 0
    fi
    case "$cloud_confirm" in
      y|Y|yes|YES) return ;;
      q|Q) say "已退出，未安装任何软件。"; exit 0 ;;
    esac
  done
}

# ① 平台自检
PLATFORM="$(uname -s)"
case "$PLATFORM" in
  Darwin|Linux) ;;
  *) die "目前仅支持 macOS 与 Linux，不支持 Windows（WSL 未验证）。" ;;
esac

# ② 先检查与询问，再安装任何软件；从管道执行时通过 /dev/tty 接收选择。
check_base_tools
check_heading "云平台 CLI"
check_cloud_cli "AWS" aws "https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html" --version
check_cloud_cli "阿里云" aliyun "https://github.com/aliyun/aliyun-cli#installation" version
check_cloud_cli "腾讯云" tccli "https://cloud.tencent.com/document/product/1278/46705" --version
check_cloud_cli "腾讯云 COS" coscli "https://cloud.tencent.com/document/product/436/63144" --version
check_cloud_cli "火山引擎" ve "https://github.com/volcengine/volcengine-cli/blob/master/docs/1-GettingStarted.md" version
check_cloud_cli "火山引擎 TOS" tosutil "https://docs.volcengine.com/docs/6349/152751" version
check_cloud_cli "Google Cloud" gcloud "https://cloud.google.com/sdk/docs/install" --version
check_cloud_cli "Microsoft Azure" az "https://learn.microsoft.com/cli/azure/install-azure-cli" version --query '"azure-cli"' --output tsv --only-show-errors
check_cloud_cli "Oracle Cloud" oci "https://docs.oracle.com/iaas/Content/API/SDKDocs/cliinstall.htm" --version
printf '\n%s  Cloudflare / Supabase 使用 API token，无需 CLI。%s\n\n' "$C_DIM" "$C_RESET"

choose_cloud_clis
base_plan
if [ "$BASE_CHANGES" = 0 ] && [ -z "$CLOUD_SELECTION" ]; then
  say "本次无需安装或刷新，检查完成。"
  exit 0
fi

if [ "$NEED_UV" = 1 ]; then
  say "未发现 uv，自动安装（官方脚本）…"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
  command -v uv >/dev/null 2>&1 || die "uv 安装后仍未在 PATH，请重开终端后重跑。"
fi
export PATH="$HOME/.local/bin:$PATH"
for cloud_id in $CLOUD_SELECTION; do
  cloud_install install "$cloud_id"
done

if [ "$NEED_SETUP" = 1 ]; then
  if command -v node >/dev/null 2>&1; then
    NODE_MAJOR="$(node -v | sed -E 's/^v([0-9]+).*/\1/')"
    [ "$NODE_MAJOR" -ge 22 ] 2>/dev/null || warn "Node $(node -v) 低于 22：skill 只会装到 Claude Code；升级 Node 后重跑 ops setup 即扇出全客户端"
  else
    warn "未装 node/npx（推荐）：skill 多客户端扇出需要 Node ≥ 22；没有则只装 Claude Code"
  fi
fi

# ③ 仅补装缺失的 noc-ops；只有显式指定安装源/extra 才替换现有安装。
if [ "$NEED_NOC" = 1 ]; then
  say "安装 ops…"
  if [ "$REINSTALL_NOC" = 1 ]; then
    uv tool install --force "$PKG"
  else
    uv tool install "$PKG"
  fi || die "uv tool install 失败（检查网络；可 NOC_PACKAGE=<wheel路径或URL> 指定安装源）。"

  command -v ops >/dev/null 2>&1 || die "装完未找到 ops 命令；确认 ~/.local/bin 在 PATH。"
fi

# ④ 已有工作区配置和 skill 时不自动刷新，避免重复改写。
if [ "$NEED_SETUP" = 1 ]; then
  say "接入 LLM 客户端（skill）…"
  ops setup
fi

# ⑤ 下一步
cat <<'EOF'

✅ 检查与所选安装步骤已完成。首次使用可继续：
   ops init        # 交互生成部署配置（发现账号/区域/key）
   ops doctor      # 体检；--fix 自动修
   ops sync        # 首次扫云、生成清单
   ops noc         # 起 NOC 看板

  终端内自然语言入口不在公开包；在 Claude Code / Codex 等客户端里用 skill 即可。
  无 shell 的 GUI 客户端（如 Claude Desktop）另需：uv tool install --force 'noc-ops[mcp]' && ops setup --with-mcp
EOF
