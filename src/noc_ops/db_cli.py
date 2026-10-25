"""ops db —— 托管数据库项目的状态 / 暂停 / 恢复（目前只有 Supabase 项目支持暂停与恢复）。

与 `ops reboot` 的区别：重启是「低风险可逆、免确认」；暂停会让项目对外不可用，所以
**pause / resume 都先回显计划、等确认**（交互式问一次；agent 在向用户确认后传 `--yes`）。
两者都是可逆写操作，执行后记审计日志。只读的 `status` / `--dry-run` 不记账。
"""
import json
import sys

from . import ops_log, paths, resources
from .providers.supabase import SupabaseProvider

USAGE = """用法:
  ops db status <名字|ref>             查项目实时状态（只读）
  ops db pause  <名字|ref> [--yes]     暂停 Supabase 项目（写操作：先回显计划，确认后执行；可用 resume 恢复）
  ops db resume <名字|ref> [--yes]     恢复已暂停的项目（写操作，同上）
选项:
  --cloud / --account / --region <值>  按云、账号、区域收窄；也可直接使用 ops find 输出的 noc:// 引用
  --yes               跳过交互确认（agent 已向用户确认后再传；非终端环境不传则拒绝执行）
  --dry-run           只打印将执行的动作，不动手
  --source <客户端>   审计日志里的调用方身份（claude-code / codex / cursor…；默认 cli）
  --json              机读输出
目标解析：精确 ref → 精确名 → 忽略大小写 → 唯一子串；多个候选一律列出来让你挑。
目前只有 Supabase 项目支持暂停/恢复；其它云的数据库只在清单里只读展示。
"""

_VERBS = ("status", "pause", "resume")


def _load_databases():
    try:
        return json.loads(paths.data_json_path().read_text()).get("databases", [])
    except (OSError, json.JSONDecodeError):
        return []


def resolve(target, rows, **filters):
    return resources.resolve(target, rows, "databases", **filters)


def _fmt(r):
    return (f"{r.get('name')} ({r.get('cloud')}, ref {r.get('ref') or '?'}, 区域 {r.get('region_display') or r.get('region') or '?'}, "
            f"账号 {r.get('account')}, 清单里状态 {r.get('state')})")


def _confirm(plan, yes, as_json=False):
    print(plan, file=sys.stderr if as_json else sys.stdout, flush=True)   # --json 的 stdout 只放 JSON
    if yes:
        return True
    if not sys.stdin.isatty():
        print("未确认：非交互环境请在向用户确认后加 --yes 重跑。", file=sys.stderr)
        return False
    try:
        return input("确认执行？[y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def main(argv):
    verb, target, yes, dry_run, as_json, source = None, None, False, False, False, "cli"
    filters = {}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--yes":
            yes = True
        elif a == "--dry-run":
            dry_run = True
        elif a == "--json":
            as_json = True
        elif a == "--source" and i + 1 < len(argv):
            source = argv[i + 1]
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
        elif verb is None and a in _VERBS:
            verb = a
        elif target is None:
            target = a
        else:
            print(f"多余的参数 {a!r}\n\n{USAGE}", file=sys.stderr)
            return 2
        i += 1
    if not verb or not target:
        print(USAGE, file=sys.stderr)
        return 2

    rows = _load_databases()
    if not rows:
        print("清单里没有数据库：先跑 `ops sync --kind=databases`。", file=sys.stderr)
        return 1
    row, cands = resolve(target, rows, **filters)
    if not row:
        if cands:
            print(f"「{target}」匹配到 {len(cands)} 个，指明是哪个（用 ref 最稳）：", file=sys.stderr)
            for c in sorted(cands, key=lambda x: x.get("name") or "")[:20]:
                print(f"  - {_fmt(c)}\n    {resources.reference(c, 'databases')}", file=sys.stderr)
        else:
            print(f"清单里找不到「{target}」。查 inventory.md，或先 `ops sync --kind=databases` 刷新。", file=sys.stderr)
        return 2
    if row.get("cloud") != "supabase":
        print(f"{_fmt(row)}\n❌ 只有 Supabase 项目支持 {verb}；{row.get('cloud')} 的数据库请走该云控制台/CLI。", file=sys.stderr)
        return 1
    ref = row.get("ref")
    if not ref:
        print(f"{_fmt(row)}\n❌ 清单里没有这个项目的 ref：先 `ops sync --kind=databases` 刷新。", file=sys.stderr)
        return 1

    operation = resources.plan(row, "databases", verb)
    if dry_run:
        if as_json:
            print(json.dumps({**operation, "ref": ref, "dry_run": True}, ensure_ascii=False, indent=2))
        else:
            action = {"status": "查询状态", "pause": "暂停", "resume": "恢复"}[verb]
            print(f"{_fmt(row)}\n将执行：{action} Supabase 项目（预览，不读取 token 或调用 API）")
        return 0

    prov = SupabaseProvider()
    token = prov.token_for(row.get("account", "supabase"))
    if not token:
        print(f"❌ 未找到 Supabase 账号「{row.get('account')}」的 PAT（检查 token_env 或 Keychain，见 ops-config.toml 注释）。", file=sys.stderr)
        return 1

    live = prov.project(token, ref)
    status = (live or {}).get("status", "") or ""
    if verb == "status":
        if live is None:
            print(f"{_fmt(row)}\n❌ 查不到实时状态（PAT 权限或网络）。", file=sys.stderr)
            return 1
        if as_json:
            print(json.dumps({**operation, "target": row.get("name"), "ref": ref, "cloud": "supabase", "status": status}, ensure_ascii=False))
        else:
            print(f"{_fmt(row)}\n实时状态：{status}")
        return 0

    if live is None or not status:
        print("无法确认实时状态，不执行变更；检查凭证与网络后重试。", file=sys.stderr)
        return 1

    # ---- 写操作：pause / resume ----
    if verb == "pause" and status and status != "ACTIVE_HEALTHY":
        print(f"{_fmt(row)}\n❌ 当前状态 {status}，不是运行中（ACTIVE_HEALTHY），不执行暂停。", file=sys.stderr)
        return 1
    if verb == "resume" and status and status != "INACTIVE":
        print(f"{_fmt(row)}\n❌ 当前状态 {status}，不是已暂停（INACTIVE），不执行恢复。", file=sys.stderr)
        return 1

    action = "暂停" if verb == "pause" else "恢复"
    plan = (f"{_fmt(row)}\n将执行：{action} Supabase 项目（POST /v1/projects/<ref>/{'pause' if verb == 'pause' else 'restore'}）"
            + ("\n  暂停期间项目对外不可用；可用 `ops db resume` 恢复。" if verb == "pause" else "\n  恢复通常需要几分钟，之后用 `ops db status` 复查。"))
    if not _confirm(plan, yes, as_json):
        print("已取消。", file=sys.stderr if as_json else sys.stdout)
        return 3

    ok, err = (prov.pause if verb == "pause" else prov.restore)(token, ref)
    ops_log.record(source, f"db-{verb}", target=row.get("name") or ref, cloud="supabase",
                   detail=ref, result="ok" if ok else "fail", account=row.get("account"))
    if as_json:
        print(json.dumps({**operation, "target": row.get("name"), "ref": ref, "cloud": "supabase", "action": verb,
                          "ok": ok, "error": err}, ensure_ascii=False, indent=2))
    else:
        print(("✅ " if ok else "❌ ") + (f"{action}请求已受理" if ok else f"{action}失败：{err}"))
        if ok:
            print(f"\n稍后复查：ops db status {ref}；清单刷新：ops sync --kind=databases")
    return 0 if ok else 1
