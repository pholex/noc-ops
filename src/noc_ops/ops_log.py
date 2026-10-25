"""ops 操作日志（审计线）：每次**写操作**追加一行 JSON 到 ops-log.jsonl。

谁(source) 在 什么时候(ts,北京时间) 对 哪台(target/cloud)、哪个账号(account) 做了 什么(action/detail) 结果如何(result)。
agent.py / mcp_server.py / `ops log` 的写操作处调用 record()；sync.py 把 tail() 并进 data.json 供看板「操作日志」tab。

account 取自 target 反查清单（data.json）：一条日志的 target 若跨多个账号，**拆成多条**各记其账号；
反查不到留空。显式传 account 则直用、不拆。历史缺失 account 的日志用 backfill_accounts() 反查补入。
本地运行时数据，已 gitignore。record() 永不抛异常——记日志失败绝不能影响主流程。
"""
import json
import re
from collections import OrderedDict
from datetime import datetime, timedelta, timezone

from . import paths

_BJ = timezone(timedelta(hours=8))
_IDX_CACHE = {"key": None, "idx": {}}   # (data.json 路径, mtime) -> 名→账号索引（避免每次写都重解析）


def _account_index() -> dict:
    """从工作区 data.json 建「资源标识 → 账号」索引（按 data.json mtime 缓存）。
    同时登记「cloud|名」与「名」两种键：同名跨云资源（如 AWS 桶 orf 与阿里云 OSS orf）靠 cloud 消歧。"""
    try:
        p = paths.data_json_path()
        key = (str(p), p.stat().st_mtime)
    except OSError:
        return {}
    if _IDX_CACHE["key"] == key:
        return _IDX_CACHE["idx"]
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    ix = {}

    def add(k, a):
        if k and a and k not in ix:
            ix[str(k)] = a

    def add_res(cloud, name, a):
        add(f"{cloud or 'aws'}|{name}", a)
        add(name, a)

    for i in data.get("instances", []):
        add_res(i.get("cloud"), i.get("name"), i.get("profile"))   # 实例账号在 profile 字段
        add_res(i.get("cloud"), i.get("id"), i.get("profile"))
    for key in ("storage", "databases", "serverless", "iam", "apikeys", "messaging"):
        for x in data.get(key, []):
            add_res(x.get("cloud"), x.get("name"), x.get("account"))
    for x in data.get("domains", []):
        add_res(x.get("cloud"), x.get("domain"), x.get("account"))
    _IDX_CACHE["key"], _IDX_CACHE["idx"] = key, ix
    return ix


def _resolve_account_groups(target: str, cloud: str, idx: dict) -> "OrderedDict":
    """把 target 按账号分组：{账号: 该账号名下的 target 串}。保序；反查不到归到 ""。
    target 可含多个（逗号/空格分隔）；形如 "桶/对象" 时取桶名反查。空 target → {"": ""}。"""
    toks = [t for t in re.split(r"[,\s]+", (target or "").strip()) if t]
    if not toks:
        return OrderedDict([("", "")])
    groups = OrderedDict()
    for tok in toks:
        head = tok.split("/")[0]
        acct = (idx.get(f"{cloud}|{tok}") or idx.get(f"{cloud}|{head}")
                or idx.get(tok) or idx.get(head) or "")
        groups.setdefault(acct, []).append(tok)
    return OrderedDict((a, " ".join(ts)) for a, ts in groups.items())


def record(source, action, target="", cloud="", detail="", result="ok", account=None):
    """追加一条操作日志。source=调用方自报身份（agent/codex/claude-code/cursor/gemini/cli…，
    经 skill 但未自报时默认 skill）；result=ok|exitN|timeout|fail|...。
    account=None 时按 target 反查清单（跨多账号则拆成多条）；显式传 account 则直用、不拆。"""
    try:
        ts = datetime.now(_BJ).strftime("%Y-%m-%dT%H:%M:%S")
        target = target or ""
        if account is not None:
            groups = OrderedDict([(account, target)])
        else:
            groups = _resolve_account_groups(target, cloud or "", _account_index())
        p = paths.ops_log_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            for acct, tgt in groups.items():
                entry = {"ts": ts, "source": source, "action": action,
                         "target": tgt or "", "account": acct or "",
                         "cloud": cloud or "", "detail": (detail or "")[:300], "result": result}
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def backfill_accounts() -> int:
    """给历史日志补 account（仅处理无 account 字段的旧条目；跨多账号 target 拆成多条）。
    幂等：已带 account 字段（含曾解析为空）的条目原样保留，不重处理。返回补到非空账号的条数。"""
    try:
        lines = paths.ops_log_path().read_text(encoding="utf-8").splitlines()
    except OSError:
        return 0
    idx = _account_index()
    out, dirty, filled = [], False, 0
    for ln in lines:
        if not ln.strip():
            continue
        try:
            e = json.loads(ln)
        except json.JSONDecodeError:
            out.append(ln)
            continue
        if "account" in e:                 # 已处理过，原样保留（幂等）
            out.append(ln)
            continue
        for acct, tgt in _resolve_account_groups(e.get("target", ""), e.get("cloud", ""), idx).items():
            out.append(json.dumps({**e, "account": acct or "", "target": tgt or ""}, ensure_ascii=False))
            if acct:
                filled += 1
        dirty = True
    if dirty:
        try:
            paths.ops_log_path().write_text("\n".join(out) + "\n", encoding="utf-8")
        except OSError:
            pass
    return filled


def tail(n=2500):
    """读最近 n 条，时间倒序（最新在前）。读不到返回空。

    默认 2500 = data.json/看板能看到的窗口（原 300 太紧，2026-08-05 放开）；
    完整历史永远在 .state/ops-log.jsonl，不受此值影响。
    """
    try:
        lines = paths.ops_log_path().read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for ln in lines[-n:]:
        try:
            out.append(json.loads(ln))
        except json.JSONDecodeError:
            pass
    out.reverse()
    return out
