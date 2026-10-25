"""ops reboot —— 一条命令重启任意一朵云 / 本地 HCI 上的实例（按 cloud 自动选通道）。

为什么单独有它：`Provider.reboot_argv / status_argv` 早就写好了，但**只有 MCP server 接了**
（`mcp_server.reboot_instance`）。走 skill 的客户端（Claude Code / Codex）手里只有 Bash——
公有云还能自己拼 `aws ec2 reboot-instances`，**深信服 SCP HCI / VMware vCenter 却是
Token 认证 + 从 Keychain 取密码的管控 API，拼不出来**，此前只能 SSH 进去 `reboot`
（更重，Windows 机器还无从下手）。本命令把那条现成通道接到 CLI 上，公有云 + 本地 HCI 同一入口。

重启属「低风险可逆」，按本仓约定**免二次确认**直接执行；想先看拼出什么命令用 `--dry-run`。
重启后隔 ~40 秒用 `--status` 复查（同一条命令的只读模式），别立刻 SSH。
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys

from . import ops_log, paths, resources
from .error_patterns import annotate
from .providers import PROVIDERS

USAGE = """用法:
  ops reboot <实例名|ID>            重启（按 cloud 自动选：aws / tccli / aliyun / gcloud / az / oci
                                    / 深信服 SCP API / vCenter API）——低风险，免确认直接执行
  ops reboot <实例名|ID> --status   只查实时状态（只读，不重启）；重启后隔 ~40 秒用它复查
  ops reboot <实例名|ID> --dry-run  只打印将要执行的命令，不动手
选项:
  --cloud / --account / --region <值>  按云、账号、区域收窄；也可直接使用 ops find 输出的 noc:// 引用
  --source <客户端>   审计日志里的调用方身份（claude-code / codex / cursor…；默认 cli）
  --timeout <秒>      单条命令超时，默认 180
  --json              机读输出
目标解析：精确 ID → 精确名 → 忽略大小写 → 唯一子串；匹配到多台就列出来让你挑，绝不替你猜。
"""

# 没有管控 API 的 provider：如实说该怎么办，别让调用方对着 NotImplementedError 猜
_NO_API_HINT = {
    "synology": "群晖只做只读纳管：重启走 DSM 控制面板，或 SSH 进去 `sudo reboot`。",
    "local": "手工登记的内网主机 / ESXi 宿主机没有接管控 API：SSH 进去 `sudo reboot`。"
             "⚠️ ESXi 宿主重启会带走它上面所有 VM，动手前务必确认。",
    "cloudflare": "Cloudflare 是只读纳管的 DNS，没有可重启的机器。",
    "supabase": "Supabase 是托管 Postgres 项目，没有重启；暂停/恢复用 `ops db pause|resume <名>`（先确认）。",
}


def _load_instances():
    try:
        return json.loads(paths.data_json_path().read_text()).get("instances", [])
    except (OSError, json.JSONDecodeError):
        return []


def resolve(target, rows, **filters):
    return resources.resolve(target, rows, "instances", **filters)


def _fmt(inst):
    region = inst.get("region_display") or inst.get("region") or "?"
    return (f"{inst.get('name')} ({inst.get('id')}, {inst.get('cloud')}/{region}, "
            f"账号 {inst.get('profile')}, 清单里状态 {inst.get('state')})")


def _run(argv, timeout):
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                           env={**os.environ, "AWS_PAGER": ""})
    except FileNotFoundError:
        return False, f"命令不存在：{argv[0]}（PATH 里找不到，`ops doctor` 可体检）"
    except subprocess.TimeoutExpired:
        return False, f"命令超时（{timeout}s）：{' '.join(argv[:3])}…"
    out = (r.stdout or "").strip()
    err = (r.stderr or "").strip()
    body = "\n".join(x for x in (out, f"[stderr] {err}" if err else "") if x)
    return r.returncode == 0, body or f"(无输出，退出码 {r.returncode})"


def _argv_for(prov, inst, status_only):
    """(argv, 错误说明)。provider 没实现 / 账号对不上时不抛异常，给能照做的下一步。"""
    try:
        return (prov.status_argv(inst) if status_only else prov.reboot_argv(inst)), None
    except NotImplementedError:
        hint = _NO_API_HINT.get(inst.get("cloud"), "该 provider 未实现这个动作。")
        alias = inst.get("ssh_alias")
        if alias and not status_only:
            hint += f" 这台的 SSH 别名是 `{alias}`。"
        return None, hint
    except (ValueError, KeyError) as e:
        return None, f"{e}（清单里的 profile/账号与 ops-config.toml 对不上，`ops sync` 后重试）"


def main(argv):
    target, status_only, dry_run, as_json = None, False, False, False
    source, timeout = "cli", 180
    filters = {}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--status":
            status_only = True
        elif a == "--dry-run":
            dry_run = True
        elif a == "--json":
            as_json = True
        elif a == "--source" and i + 1 < len(argv):
            source = argv[i + 1]
            i += 1
        elif a == "--timeout" and i + 1 < len(argv):
            try:
                timeout = int(argv[i + 1])
                if timeout <= 0:
                    raise ValueError
            except ValueError:
                print("--timeout 必须是正整数", file=sys.stderr)
                return 2
            i += 1
        elif a in ("--cloud", "--account", "--region") and i + 1 < len(argv):
            filters[a[2:]] = argv[i + 1]
            i += 1
        elif a in ("-h", "--help"):
            print(USAGE)
            return 0
        elif a.startswith("-"):
            print(f"未知选项：{a}\n\n{USAGE}", file=sys.stderr)
            return 2
        elif target is None:
            target = a
        else:
            print(f"只能指定一个目标（多收到了 {a!r}）\n\n{USAGE}", file=sys.stderr)
            return 2
        i += 1

    if not target:
        print(USAGE, file=sys.stderr)
        return 2

    instances = _load_instances()
    if not instances:
        print("清单为空：先跑 `ops sync`（本地网络机器用 `ops sync --local`）。", file=sys.stderr)
        return 1

    inst, cands = resolve(target, instances, **filters)
    if not inst:
        if cands:
            print(f"「{target}」匹配到 {len(cands)} 台，指明是哪台（用 ID 最稳）：", file=sys.stderr)
            for c in sorted(cands, key=lambda x: x.get("name") or "")[:20]:
                print(f"  - {_fmt(c)}\n    {resources.reference(c)}", file=sys.stderr)
        else:
            print(f"清单里找不到「{target}」。查 inventory.md，或先 `ops sync` 刷新"
                  "（本地网络机器要 `ops sync --local`）。", file=sys.stderr)
        return 2

    prov = PROVIDERS.get(inst.get("cloud"))
    if not prov:
        print(f"未知 cloud：{inst.get('cloud')}（{_fmt(inst)}）", file=sys.stderr)
        return 1

    cmd, why = _argv_for(prov, inst, status_only)
    if cmd is None:
        print(f"{_fmt(inst)}\n❌ {'查状态' if status_only else '重启'}走不通：{why}", file=sys.stderr)
        return 1

    operation = resources.plan(inst, "instances", "status" if status_only else "reboot", cmd)
    if dry_run and as_json:
        print(json.dumps({**operation, "dry_run": True}, ensure_ascii=False, indent=2))
        return 0
    if dry_run:
        print(f"{_fmt(inst)}\n将执行（{'状态查询' if status_only else '重启'}）：\n  {shlex.join(cmd)}")
        return 0

    action = "查状态" if status_only else "重启"
    if not as_json:      # --json 的 stdout 只放 JSON，进度行会让调用方 json.loads 直接炸
        print(f"{action} → {_fmt(inst)}", flush=True)
    ok, out = _run(cmd, timeout)
    if not ok:
        out = annotate(out)

    if not status_only:   # 只读不记账；写操作才进审计线
        ops_log.record(source, "reboot", target=inst.get("name") or inst.get("id"),
                       cloud=inst.get("cloud", ""), detail=inst.get("id", ""),
                       result="ok" if ok else "fail", account=resources.account_of(inst))

    if as_json:
        print(json.dumps({**operation, "target": inst.get("name"), "id": inst.get("id"), "cloud": inst.get("cloud"),
                          "action": "status" if status_only else "reboot",
                          "ok": ok, "output": out}, ensure_ascii=False, indent=2))
    else:
        print(("✅ " if ok else "❌ ") + (f"{action}命令已执行" if ok else f"{action}失败"))
        print(out)
        if ok and not status_only:
            print(f"\n隔 ~40 秒复查：ops reboot {inst.get('id')} --status（别立刻 SSH，重启中必然连不上）")
    return 0 if ok else 1
