"""管道安装与真实 /dev/tty 交互；所有安装器均为桩，不联网、不安装软件。"""
import fcntl
import os
from pathlib import Path
import pty
import select
import subprocess
import termios
import time

import pytest

pytestmark = pytest.mark.unit
SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "install.sh"

# 即使宿主机装有云 CLI，也只使用明确指定的模拟安装状态。
PRELUDE = r'''
command() {
  if [ "$2" = uv ] && test -f "$MOCK_UV_MARKER"; then
    printf '/mock/uv\n'
    return
  fi
  case " $MOCK_AVAILABLE " in
    *" $2 "*) printf '/mock/%s\n' "$2" ;;
    *) return 1 ;;
  esac
}
uname() { printf '%s\n' "$MOCK_PLATFORM"; }
test() {
  case "$2" in
    "$HOME/.config/noc-ops/config.toml")
      [ "$MOCK_SETUP" = 1 ] ;;
    "$HOME/.agents/skills/multicloud-ops/SKILL.md")
      [ "$MOCK_SETUP" = 1 ] && [ "$MOCK_SKILL_LOCATION" = agents ] ;;
    "$HOME/.claude/skills/multicloud-ops/SKILL.md")
      [ "$MOCK_SETUP" = 1 ] && [ "$MOCK_SKILL_LOCATION" = claude ] ;;
    *) /bin/test "$@" ;;
  esac
}
awk() {
  case "$2" in
    "$HOME/.agents/skills/multicloud-ops/SKILL.md"|"$HOME/.claude/skills/multicloud-ops/SKILL.md")
      /usr/bin/awk "$1" "$MOCK_SKILL_FILE" ;;
    *) /usr/bin/awk "$@" ;;
  esac
}
uv() {
  if [ "$*" = --version ]; then
    printf 'uv 0.13.5 (Homebrew 2026-10-22 aarch64-apple-darwin)\n'
    return
  fi
  printf 'ACTION uv %s\n' "$*"
  [ "$*" != "tool install $MOCK_FAIL" ] || return 1
  case "$*" in
    'tool install tccli'|'tool install oci-cli') ;;
    'tool install '*) MOCK_AVAILABLE="$MOCK_AVAILABLE ops" ;;
  esac
}
brew() { printf 'ACTION brew %s\n' "$*"; }
npm() { printf 'ACTION npm %s\n' "$*"; }
ops() {
  if [ "$*" = --version ]; then
    printf '%s\n' "$MOCK_OPS_VERSION"
    return
  fi
  printf 'ACTION ops %s\n' "$*"
}
curl() {
  if [ "$MOCK_BOOTSTRAP" = 1 ] && [ "$*" = '-LsSf https://astral.sh/uv/install.sh' ]; then
    printf '%s\n' 'printf "ACTION bootstrap uv\n"' ': > "$MOCK_UV_MARKER"'
  else
    printf 'UNEXPECTED curl\n' >&2
    return 99
  fi
}
node() { printf 'v22.20.0\n'; }
aws() { printf 'aws-cli/2.37.6 Python/3.14.6 Darwin/27.0.0 exe/arm64\n' >&2; }
aliyun() { printf '3.5.2\n'; }
tccli() { printf '3.1.65.1\n'; }
coscli() { printf 'coscli version v1.0.9\n'; }
ve() { printf '1.0.39\n'; }
tosutil() { printf 'tosutil version: v4.1.7\n'; }
gcloud() { printf 'Google Cloud SDK 590.0.0\n'; }
az() {
  [ "$*" = 'version --query "azure-cli" --output tsv --only-show-errors' ] || return 2
  printf 'WARNING: You have 2 update(s) available.\n' >&2
  [ "$MOCK_AZ_MODE" != fail ] || return 1
  [ "$MOCK_AZ_MODE" != warning_only ] || return 0
  printf '2.90.0\n'
}
oci() { printf '3.95.1\n'; }
'''


def _run(tmp_path, answers=None, available="uv node brew npm", platform="Darwin",
         noninteractive=False, fail="", configured=False, bootstrap=False,
         package="", extras="", ops_version="noc-ops 0.2.3", expected_code=0,
         az_mode="ok", skill_content="---\nname: multicloud-ops\n---\n",
         skill_location="agents"):
    skill_file = tmp_path / "SKILL.md"
    skill_file.write_text(skill_content)
    env = {**os.environ, "MOCK_AVAILABLE": available, "MOCK_PLATFORM": platform,
           "MOCK_FAIL": fail, "NOC_NONINTERACTIVE": "1" if noninteractive else "0",
           "MOCK_SETUP": "1" if configured else "0", "MOCK_OPS_VERSION": ops_version,
           "MOCK_BOOTSTRAP": "1" if bootstrap else "0",
           "MOCK_UV_MARKER": str(tmp_path / "uv-installed"),
           "NOC_PACKAGE": package, "NOC_EXTRAS": extras, "NO_COLOR": "1",
           "MOCK_AZ_MODE": az_mode, "MOCK_SKILL_FILE": str(skill_file),
           "MOCK_SKILL_LOCATION": skill_location}
    # 用文件作为 shell 的输入管道源，避免测试自身写入大脚本时堵塞 PTY。
    source = tmp_path / "input.sh"
    source.write_text(PRELUDE + SCRIPT.read_text())
    if answers is None:
        with source.open() as stream:
            result = subprocess.run(["/bin/sh"], stdin=stream, capture_output=True,
                                    text=True, env=env, start_new_session=True, timeout=10)
        assert result.returncode == expected_code, result.stderr
        assert "UNEXPECTED" not in result.stderr
        return result.stdout + result.stderr

    master, slave = pty.openpty()

    def attach_terminal():
        os.setsid()
        fcntl.ioctl(slave, termios.TIOCSCTTY, 0)

    # stdin 是脚本管道，选择必须读 /dev/tty，不能从 stdin 抢走脚本正文。
    with source.open() as stream:
        feeder = subprocess.Popen(["/bin/cat"], stdin=stream, stdout=subprocess.PIPE)
        proc = subprocess.Popen(["/bin/sh"], stdin=feeder.stdout, stdout=slave,
                                stderr=slave, env=env, preexec_fn=attach_terminal)
    feeder.stdout.close()
    os.close(slave)
    output = bytearray()
    try:
        os.write(master, answers.encode())
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if select.select([master], [], [], 0.1)[0]:
                try:
                    chunk = os.read(master, 65536)
                except OSError:  # Linux PTY closes with EIO; macOS returns EOF.
                    break
                if not chunk:
                    break
                output.extend(chunk)
            elif proc.poll() is not None:
                break
        proc.wait(timeout=1)
        text = output.decode()
        assert proc.returncode == expected_code, text
        assert "UNEXPECTED" not in text
        return text
    finally:
        os.close(master)
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        feeder.wait(timeout=1)


@pytest.mark.parametrize("answers", ["q\n", "1 3\nq\n"])
def test_exit_before_any_installation(tmp_path, answers):
    out = _run(tmp_path, answers, available="ops node brew npm")
    assert "未安装任何软件" in out
    assert "ACTION" not in out


@pytest.mark.parametrize("answers", ["\n", "0\n", "3\nn\n0\n"])
def test_manual_or_cancelled_selection_only_installs_noc(tmp_path, answers):
    out = _run(tmp_path, answers)
    assert "ACTION uv tool install noc-ops" in out
    assert "ACTION ops setup" in out
    assert "ACTION uv tool install tccli" not in out
    assert "ACTION brew" not in out and "ACTION npm" not in out


def test_selection_is_confirmed_deduplicated_and_skips_existing(tmp_path):
    out = _run(tmp_path, "2 3 3 4 5 6 7 8 9\ny\n", available="uv ops node brew npm aws")
    assert "2.37.6" in out
    menu = out.split("以下云 CLI 尚未安装", 1)[1]
    assert "[aws]" not in menu and "[aliyun] 未安装" in menu
    assert "ACTION brew install awscli" not in out
    assert out.count("ACTION uv tool install tccli") == 1
    for action in ("brew install aliyun-cli", "npm install -g @volcengine/cli",
                   "brew install --cask gcloud-cli", "brew install azure-cli",
                   "uv tool install oci-cli", "ops setup"):
        assert f"ACTION {action}" in out
    assert "coscli：请按上方官方指引手工安装" in out
    assert "tosutil：请按上方官方指引手工安装" in out


def test_invalid_input_never_runs_a_partial_selection(tmp_path):
    out = _run(tmp_path, "3 11\n3;touch /tmp/no\nq\n")
    assert "ACTION" not in out
    assert "请输入菜单中缺失工具的有效编号" in out and "请输入 1–9" in out


def test_existing_cli_number_is_not_selectable(tmp_path):
    out = _run(tmp_path, "1 3\nq\n", available="uv ops node brew npm aws")
    assert "请输入菜单中缺失工具的有效编号" in out
    assert "ACTION" not in out


@pytest.mark.parametrize("answers,proceed", [("q\n", False), ("\n", False),
                                           ("n\n", False), ("y\n", True)])
def test_all_installed_skips_cloud_menu_and_confirms_noc(tmp_path, answers, proceed):
    out = _run(tmp_path, answers,
               available="uv ops node aws aliyun tccli coscli ve tosutil gcloud az oci")
    assert "以下云 CLI 尚未安装" not in out
    assert "选择（多个编号" not in out
    assert "是否执行以上基础步骤" in out
    if proceed:
        assert "ACTION uv tool install" not in out
        assert "ACTION ops setup" in out
        assert out.count("ACTION") == 1
    else:
        assert "ACTION" not in out


@pytest.mark.parametrize("platform,available", [("Linux", "uv ops node brew"),
                                                ("Darwin", "uv ops node")])
def test_unavailable_installers_fall_back_to_manual(tmp_path, platform, available):
    out = _run(tmp_path, "1 2 5 7 8\ny\n", platform=platform, available=available)
    assert "ACTION brew" not in out and "ACTION npm" not in out
    assert "请按上方官方指引手工安装" in out
    assert "ACTION ops setup" in out


def test_failed_cli_install_does_not_abort_noc(tmp_path):
    out = _run(tmp_path, "3\ny\n", fail="tccli")
    assert "tccli 安装失败" in out
    assert "ACTION ops setup" in out


@pytest.mark.parametrize("noninteractive", [False, True])
def test_unattended_install_skips_cloud_installers(tmp_path, noninteractive):
    out = _run(tmp_path, noninteractive=noninteractive)
    assert "非交互模式" in out
    assert "ACTION brew" not in out and "ACTION npm" not in out
    assert "ACTION uv tool install tccli" not in out
    assert "ACTION ops setup" in out


ALL_CLOUDS = "aws aliyun tccli coscli ve tosutil gcloud az oci"


@pytest.mark.parametrize("has_uv", [False, True])
def test_everything_ready_finishes_without_prompt_or_changes(tmp_path, has_uv):
    out = _run(tmp_path, "\n", available=f"ops {ALL_CLOUDS}" + (" uv" if has_uv else ""),
               configured=True)
    noc_row = next(line for line in out.splitlines() if line.strip().startswith("noc-ops "))
    assert noc_row.split() == ["noc-ops", "0.2.3", "已安装", "多云运维工具"]
    assert "本次无需安装或刷新，检查完成" in out
    assert "是否执行" not in out and "选择（" not in out
    assert "ACTION" not in out


def test_existing_noc_only_initializes_missing_setup(tmp_path):
    out = _run(tmp_path, "y\n", available=f"ops {ALL_CLOUDS}")
    assert "安装缺失的 uv" not in out and "安装缺失的 noc-ops" not in out
    assert "ACTION ops setup" in out and out.count("ACTION") == 1


def test_new_noc_is_installed_without_force_and_gets_setup(tmp_path):
    out = _run(tmp_path, "y\n", available=f"uv {ALL_CLOUDS}")
    assert "ACTION uv tool install noc-ops" in out
    assert "ACTION uv tool install --force" not in out
    assert "ACTION ops setup" in out


def test_fresh_install_bootstraps_missing_uv(tmp_path):
    out = _run(tmp_path, "0\n", available="node", bootstrap=True)
    assert "ACTION bootstrap uv" in out
    assert "ACTION uv tool install noc-ops" in out and "ACTION ops setup" in out


def test_existing_noc_can_install_only_selected_cloud(tmp_path):
    out = _run(tmp_path, "3\ny\n", available="ops", configured=True, bootstrap=True)
    assert "ACTION bootstrap uv" in out and "ACTION uv tool install tccli" in out
    assert "ACTION uv tool install noc-ops" not in out and "ACTION ops setup" not in out


def test_cancel_uv_dependent_selection_does_not_install_uv(tmp_path):
    out = _run(tmp_path, "3\nn\n0\n", available="ops", configured=True)
    assert "ACTION" not in out


def test_unrelated_ops_command_is_not_overwritten(tmp_path):
    out = _run(tmp_path, available="uv ops", ops_version="some-other-ops 1.0", expected_code=1)
    assert "未识别为 noc-ops" in out
    assert "ACTION" not in out


@pytest.mark.parametrize("package,extras", [("/tmp/noc_ops-test.whl", ""), ("", "[mcp]")])
def test_explicit_package_or_extra_is_still_installed(tmp_path, package, extras):
    out = _run(tmp_path, "y\n", available=f"uv ops {ALL_CLOUDS}", configured=True,
               package=package, extras=extras)
    assert f"ACTION uv tool install --force {package or 'noc-ops'}{extras}" in out
    assert "ACTION ops setup" in out


def test_versions_exclude_warnings_and_runtime_details(tmp_path):
    out = _run(tmp_path, "\n", available=f"uv ops {ALL_CLOUDS}", configured=True)
    for cli, version in [("uv", "0.13.5"), ("aws", "2.37.6"), ("az", "2.90.0"),
                         ("coscli", "1.0.9"), ("tosutil", "4.1.7")]:
        row = next(line for line in out.splitlines() if line.strip().startswith(cli + " "))
        assert row.split()[:3] == [cli, version, "已安装"]
    assert "WARNING" not in out and "Python/" not in out and "Homebrew" not in out
    assert "\033[" not in out


@pytest.mark.parametrize("az_mode", ["warning_only", "fail"])
def test_unreadable_version_is_not_reported_as_success(tmp_path, az_mode):
    out = _run(tmp_path, "\n", available=f"uv ops {ALL_CLOUDS}", configured=True, az_mode=az_mode)
    row = next(line for line in out.splitlines() if line.strip().startswith("az "))
    assert row.split()[:3] == ["az", "-", "需检查"]
    assert "WARNING" not in out


@pytest.mark.parametrize("location", ["agents", "claude"])
def test_skill_shows_installed_version_not_current_program_version(tmp_path, location):
    out = _run(tmp_path, "\n", available=f"uv ops {ALL_CLOUDS}", configured=True,
               skill_content='---\nname: multicloud-ops\nmetadata:\n  version: "0.1.9"\n---\n',
               skill_location=location)
    row = next(line for line in out.splitlines() if line.strip().startswith("skill "))
    assert row.split()[:3] == ["skill", "0.1.9", "已配置"]
    assert "ACTION" not in out


@pytest.mark.parametrize("content", [
    '---\nname: multicloud-ops\n---\nmetadata:\n  version: "0.2.3"\n',
    '---\nname: multicloud-ops\nother:\n  version: "0.2.3"\n---\n',
])
def test_legacy_skill_version_is_not_guessed(tmp_path, content):
    out = _run(tmp_path, "\n", available=f"uv ops {ALL_CLOUDS}", configured=True,
               skill_content=content)
    row = next(line for line in out.splitlines() if line.strip().startswith("skill "))
    assert row.split()[:3] == ["skill", "-", "已配置"]
    assert "版本未标注" in row
    assert "ACTION" not in out
