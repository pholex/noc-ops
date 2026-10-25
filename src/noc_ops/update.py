"""Inspect releases and update an existing uv tool while retaining its receipt."""
import argparse
import json
import re
import shutil
import subprocess
import sys
import tomllib
import urllib.request
from pathlib import Path

from . import __version__

PYPI = "https://pypi.org/pypi/noc-ops/json"


def latest_version():
    request = urllib.request.Request(PYPI, headers={"User-Agent": "noc-ops/" + __version__})
    with urllib.request.urlopen(request, timeout=15) as response:
        data = json.load(response)
    versions = [v for v, files in data.get("releases", {}).items()
                if re.fullmatch(r"\d+\.\d+\.\d+", v) and files and any(not f.get("yanked") for f in files)]
    if not versions:
        raise ValueError("没有可用的稳定版本")
    return max(versions, key=version_key)


def version_key(version):
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", version)
    if not match:
        raise ValueError("版本必须是 X.Y.Z")
    return tuple(int(v) for v in match.groups())


def skill_version():
    skill = Path.home() / ".agents/skills/multicloud-ops/SKILL.md"
    try:
        text = skill.read_text().split("---", 2)[1]
        found = re.search(r'^  version:\s*["\']?([\w.+-]+)', text, re.M)
        return found.group(1) if found else None
    except (OSError, IndexError):
        return None


def update_plan(prefix=None):
    prefix = Path(prefix or sys.prefix)
    uv = shutil.which("uv")
    if not uv:
        raise ValueError("未找到 uv；请使用原安装方式更新 noc-ops")
    try:
        receipt = tomllib.loads((prefix / "uv-receipt.toml").read_text())["tool"]
        main = next(r for r in receipt["requirements"] if r["name"] == "noc-ops")
    except (OSError, ValueError, KeyError, StopIteration):
        raise ValueError("当前不是可识别的 uv tool 安装；请用原安装方式更新，再运行 ops setup") from None
    custom = any(k in main for k in ("directory", "url", "git", "path"))
    return {"current": __version__, "skill": skill_version(),
            "source": "自定义来源（保持原来源）" if custom else "包索引（遵守安装时的版本约束）",
            "extensions": [r["name"] for r in receipt["requirements"] if r["name"] != "noc-ops"],
            "commands": [[uv, "tool", "upgrade", "noc-ops"], [str(prefix / "bin/ops"), "setup"]]}


def main(argv=None):
    parser = argparse.ArgumentParser(description="检查稳定版本，或更新当前 uv tool 安装并刷新客户端 skill。")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--check", action="store_true", help="只查询 PyPI，不安装")
    modes.add_argument("--dry-run", action="store_true", help="预览更新步骤，不联网、不安装")
    parser.add_argument("--yes", action="store_true", help="执行已确认的更新计划")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.check:
            latest = latest_version()
            payload = {"current": __version__, "latest": latest, "skill": skill_version(),
                       "update_available": version_key(latest) > version_key(__version__)}
        else:
            payload = update_plan()
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        elif args.check:
            print(f"noc-ops：{__version__}；PyPI 稳定版：{latest}；skill：{payload['skill'] or '未标注/未安装'}")
            print("可更新：ops update" if payload["update_available"] else "未发现更高的稳定版本。")
        else:
            import shlex
            print(f"当前 noc-ops {__version__}；{payload['source']}")
            print("保留原有 extra、扩展与版本约束；只更新工具并刷新 skill。")
            for command in payload["commands"]:
                print("  " + shlex.join(command))
        if args.check or args.dry_run:
            return 0
        if not args.yes:
            if not sys.stdin.isatty():
                print("未执行。确认上面的计划后，用 --yes 执行。", file=sys.stderr)
                return 2
            print("执行更新？[y/N] ", end="", file=sys.stderr, flush=True)
            if input().strip().lower() not in ("y", "yes"):
                return 0
        for index, command in enumerate(payload["commands"]):
            # Let uv retain constraints/extras/plugins from its own receipt rather
            # than reconstructing (and accidentally dropping) requirements here.
            result = subprocess.run(command, stdout=sys.stderr, stderr=sys.stderr)
            if result.returncode:
                print("更新失败，请检查 uv 输出。" if index == 0 else "工具已更新，但 skill 刷新失败；请重跑 ops setup。", file=sys.stderr)
                return 1
        print("更新流程完成；可运行 ops --version 查看实际版本。", file=sys.stderr if args.json else sys.stdout)
        return 0
    except (OSError, ValueError) as exc:
        print(f"更新未完成：{exc}。检查网络、uv 安装方式与版本约束；ops update --dry-run 可预览。", file=sys.stderr)
        return 1
    except (EOFError, KeyboardInterrupt):
        print("已取消。", file=sys.stderr)
        return 2
