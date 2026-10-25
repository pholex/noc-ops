"""ops —— 多云运维 CLI 入口。按首参分发子命令；非内建/非插件的输入交给 noc_ops.default_command 扩展（如终端 agent）。"""
import sys

USAGE = """ops —— 多云运维 CLI（AWS / 阿里云 / 腾讯云 / 火山引擎 / GCP / Azure / Oracle / Cloudflare / Supabase）
用法:
  ops "<自然语言>"                   自然语言运维（需安装提供终端 agent 的扩展包）
  ops                                交互模式（REPL，同上）
  ops sync [--dry-run|--probe=all|--probe=<实例>]   同步实例清单 + SSH 配置（全量扫云；--probe=<实例> 只探一台的用户名，不扫云）
  ops sync --local                   只重扫内网机器（VMware/Sangfor/内网群晖，需在内网，不触云）
  ops sync --tailscale               只重扫 Tailscale 组网上的机器（人在哪都能刷，不触云）
  ops sync --index                   只刷新 runbooks/操作日志索引到 data.json（零网络，秒级）
  ops sync --kind=<键>               只刷一类资源（instances/domains/billing…），扫所有账号
  ops sync --account=<账号>          只刷一个云账号的资源（AWS 账号 ID / 阿里云 profile 名）
  ops reboot <实例名|ID> [--status|--dry-run]
                                     重启一台实例（按 cloud 自动选 aws/tccli/aliyun/ve/gcloud/az/oci
                                     /深信服 SCP API/vCenter API）；--status 只查状态（只读）
  ops db status|pause|resume <名字|ref> [--yes|--dry-run]
                                     托管数据库项目：查状态（只读）/ 暂停 / 恢复（写操作，先确认；目前仅 Supabase）
  ops noc  [start|stop|status|restart] [-f] [--port N] [--no-open]   NOC 看板（默认后台启动；-f 前台）
  ops netprobe [--target <host[:port]>]... [--json|--no-write]
                                     探测**本机**出口链路（接口全景 + 按目标分别探出口 IP
                                     + 归属反查），纯只读；--target 额外探你关心的目标通不通
  ops find [目标] [--kind instances] [--cloud C --account A --region R] [--json]  查询资源与唯一引用
  ops update [--check|--dry-run] [--yes]  检查或更新 noc-ops 并刷新 skill
  ops init [--cloud 云] [--dry-run]    交互式生成部署配置 ops-config.toml（发现账号/区域/key）
  ops doctor [--fix]                 体检（依赖 / 凭证 / 工作区 / 客户端）；--fix 自动安全修复
  ops setup [--with-mcp]             装/刷新 skill（默认）；--with-mcp 另接 MCP（无 shell GUI 才需要）
  ops license activate <key>         激活授权（Polar key 或自签 NOCK1.… token）；status 查看状态
  ops log <action> [target] ...      记一条操作日志（供 skill 写操作后记账）
  ops --version | -V | version       打印 noc-ops 版本及已装扩展包的版本
工作区（runbook/inventory/data）默认 ~/.noc，可用环境变量 NOC_HOME 或 `ops setup` 指定。
"""


def _log_cmd(a):
    """ops log <action> [target] [--cloud C] [--detail D] [--result R] [--source S]
    记一条审计日志。身份只有调用方自己知道，故由调用方用 --source 显式声明
    （Claude Code→claude-code、Codex→codex、Cursor→cursor、终端人手→cli…）。
    省略时落到通用默认 `skill`——表示「经 skill 调用、但客户端没自报身份」，
    不臆测成某个具体客户端。"""
    from . import ops_log
    kv, pos, i = {}, [], 0
    while i < len(a):
        if a[i].startswith("--"):
            kv[a[i][2:]] = a[i + 1] if i + 1 < len(a) else ""
            i += 2
        else:
            pos.append(a[i])
            i += 1
    action = kv.get("action") or (pos[0] if pos else "")
    target = kv.get("target") or (pos[1] if len(pos) > 1 else "")
    if not action:
        print("用法: ops log <action> [target] [--cloud C] [--detail D] [--result ok] [--source <who, 默认 skill>]",
              file=sys.stderr)
        return 2
    ops_log.record(kv.get("source", "skill"), action, target=target,
                   cloud=kv.get("cloud", ""), detail=kv.get("detail", ""), result=kv.get("result", "ok"),
                   account=kv.get("account"))   # 省略则按 target 反查清单（跨多账号自动拆条）
    return 0


def _license_cmd(a):
    """ops license [activate <key> | status | check | keygen | issue <名> [--days N] [--tier T]]"""
    from . import license as lic
    sub = a[0] if a else "status"
    if sub == "activate":
        if len(a) < 2:
            print("用法: ops license activate <你的key>（Polar key 或自签 NOCK1.… token）", file=sys.stderr)
            return 2
        ok, msg = lic.activate(a[1])
        print(msg)
        return 0 if ok else 1
    if sub == "check":
        ok, reason = lic.check(force=True)
        print(("✅ " if ok else "❌ ") + reason)
        return 0 if ok else 1
    if sub == "keygen":                       # 作者一次性：生成签发密钥对
        ok, msg = lic.keygen()
        print(msg)
        return 0 if ok else 1
    if sub == "issue":                        # 作者签发离线 key：ops license issue <名> [--days N] [--tier T]
        if len(a) < 2:
            print("用法: ops license issue <名/客户标识> [--days N] [--tier T]", file=sys.stderr)
            return 2
        days = None
        tier = "pro"
        rest = a[2:]
        for i, tok in enumerate(rest):
            if tok == "--days" and i + 1 < len(rest):
                days = int(rest[i + 1])
            elif tok == "--tier" and i + 1 < len(rest):
                tier = rest[i + 1]
        ok, out = lic.issue(a[1], days=days, tier=tier)
        print(out if ok else f"签发失败：{out}", file=sys.stderr if not ok else sys.stdout)
        return 0 if ok else 1
    print(lic.status_text())
    return 0


# 需要有效订阅授权才放行的命令（核心价值）；setup/init/doctor/license/help 不拦，便于安装与激活
_GATED = {"sync", "noc", ""}


def _gate(cmd):
    """对受限命令做 license 校验；未配置（开发模式）直接放行。不通过则打印原因并退出。"""
    # ⚠️ 临时关闭授权 gate —— 放开使用。要恢复：删掉下面这行 return 即可。
    # （license 校验逻辑本身完好，仅此处短路；等离线自签 key 配好后再打开。）
    return
    if cmd not in _GATED:  # noqa: 以下为原 gate 逻辑，暂被上面的 return 短路
        return
    from . import license as lic
    ok, reason = lic.check()
    if not ok:
        print(f"🔒 需要有效订阅：{reason}", file=sys.stderr)
        raise SystemExit(3)


PLUGIN_GROUP = "noc_ops.commands"


def _plugin_commands() -> dict:
    """已安装的插件子命令 {名字: EntryPoint}。

    插件包在自己的 pyproject 里声明 `[project.entry-points."noc_ops.commands"]`，
    值形如 `hello = acme_ops.hello:main`，被调函数签名 `main(argv) -> int|None`。
    依赖只有一条方向：插件 import noc_ops；核心不认识任何插件。公司/团队专属子命令
    （团队的 IM 机器人、AI 用量限额、流量报表之类）靠这个口挂进来，公开 wheel 里不含它们。
    内建子命令名优先，插件不能覆盖内建。"""
    from importlib.metadata import entry_points
    try:
        eps = entry_points(group=PLUGIN_GROUP)
    except TypeError:                                  # Python 3.10/3.11 的 dict 风格返回
        eps = entry_points().get(PLUGIN_GROUP, [])
    out = {}
    for ep in eps:
        if ep.name in _BUILTIN or ep.name in out:
            continue
        out[ep.name] = ep
    return out


_BUILTIN = frozenset({"sync", "reboot", "db", "noc", "init", "doctor", "setup",
                      "netprobe", "find", "update", "log", "license", "help", "-h", "--help",
                      "version", "-V", "--version", ""})
_VERSION_FLAGS = ("version", "-V", "--version")


def _version_text() -> str:
    """`noc-ops X.Y.Z`，再每行一个经 entry point 挂入的扩展包（去重）及其版本。
    扩展包版本来自 EntryPoint.dist 的元数据，核心仍不 import 任何插件。"""
    from . import __version__
    lines = [f"noc-ops {__version__}"]
    seen = set()
    eps = list(_plugin_commands().values())
    dflt = _default_handler()
    if dflt is not None:
        eps.append(dflt)
    for ep in eps:
        dist = getattr(ep, "dist", None)
        if dist is None:
            continue
        name = dist.metadata["Name"]
        if name in seen or name == "noc-ops":
            continue
        seen.add(name)
        lines.append(f"{name} {dist.version}")
    return "\n".join(lines) + "\n"


def _dist_tag(ep) -> str:
    dist = getattr(ep, "dist", None)
    return f"  [{dist.metadata['Name']}]" if dist is not None else ""


def _plugin_usage() -> str:
    cmds = _plugin_commands()
    dflt = _default_handler()
    if not cmds and dflt is None:
        return ""
    lines = ["", "已安装的扩展（来自其它包，经 entry point 挂入）:"]
    for name, ep in sorted(cmds.items()):
        lines.append(f"  ops {name:<28}{_dist_tag(ep)}")
    if dflt is not None:
        lines.append(f"  ops \"<自然语言>\" / ops         {_dist_tag(dflt)}  ← 默认处理器 {dflt.name}")
    return "\n".join(lines) + "\n"


DEFAULT_GROUP = "noc_ops.default_command"


def _default_handler():
    """既非内建也非插件子命令的输入（自然语言 / 无参 REPL）交给谁：entry point 组 noc_ops.default_command。
    公开包不带这类处理器；终端 agent 之类由扩展包提供，签名 `main(argv) -> int|None`。装了多个取名字最小的。"""
    from importlib.metadata import entry_points
    try:
        eps = list(entry_points(group=DEFAULT_GROUP))
    except TypeError:
        eps = list(entry_points().get(DEFAULT_GROUP, []))
    return sorted(eps, key=lambda e: e.name)[0] if eps else None


def _run_default(argv) -> int:
    ep = _default_handler()
    if ep is None:
        print("ops：没有这个子命令，也没有安装处理自然语言输入的扩展包。\n"
              "  可用子命令见 `ops help`；在 Claude Code / Codex 等客户端里用 multicloud-ops skill 即可自然语言运维。",
              file=sys.stderr)
        return 2
    try:
        fn = ep.load()
    except Exception as e:
        print(f"默认处理器 `{ep.name}`（{ep.value}）加载失败：{e}", file=sys.stderr)
        return 2
    return int(fn(argv) or 0)


def _run_plugin(cmd, argv) -> bool:
    """命中插件子命令则执行并返回 True（随后 SystemExit），未命中返回 False。"""
    ep = _plugin_commands().get(cmd)
    if ep is None:
        return False
    try:
        fn = ep.load()
    except Exception as e:                             # 插件装坏了不该把整个 ops 拖死
        print(f"插件子命令 `ops {cmd}` 加载失败（{ep.value}）：{e}", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(fn(argv) or 0)


def main():
    args = sys.argv[1:]
    cmd = args[0] if args else ""
    if cmd in _VERSION_FLAGS:
        print(_version_text(), end="")
        return
    if cmd == "license":
        raise SystemExit(_license_cmd(args[1:]))
    if cmd not in ("-h", "--help", "help"):
        _gate(cmd)
    if cmd not in _BUILTIN and _run_plugin(cmd, args[1:]):
        return
    if cmd == "sync":
        from . import sync
        sync.main()
    elif cmd == "reboot":
        from . import reboot_cli
        raise SystemExit(reboot_cli.main(args[1:]))
    elif cmd == "db":
        from . import db_cli
        raise SystemExit(db_cli.main(args[1:]))
    elif cmd == "noc":
        from .dashboard import server
        raise SystemExit(server.main(args[1:]) or 0)
    elif cmd == "find":
        from . import resources
        raise SystemExit(resources.main(args[1:]))
    elif cmd == "update":
        from . import update
        raise SystemExit(update.main(args[1:]))
    elif cmd == "init":
        from . import init_wizard
        raise SystemExit(init_wizard.main(args[1:]))
    elif cmd == "doctor":
        from . import doctor
        raise SystemExit(doctor.main(args[1:]))
    elif cmd == "setup":
        from . import setup_clients
        raise SystemExit(setup_clients.main(args[1:]))
    elif cmd == "netprobe":
        from . import egress_probe
        raise SystemExit(egress_probe.main(args[1:]))
    elif cmd == "log":
        raise SystemExit(_log_cmd(args[1:]))
    elif cmd in ("-h", "--help", "help"):
        print(USAGE + _plugin_usage())
    else:
        raise SystemExit(_run_default(args))


if __name__ == "__main__":
    main()
