"""Shared resource identity, selection and operation policy for CLI and MCP."""
import argparse
import json
from urllib.parse import quote

from . import paths

KINDS = ("instances", "databases", "domains", "storage", "serverless", "cdn", "iam", "apikeys", "messaging")
REGISTRARS = {"AWS Route53": "aws", "阿里云": "aliyun", "腾讯云": "tencent", "火山引擎": "volcengine",
              "Cloudflare": "cloudflare", "Google Cloud": "gcp", "Azure DNS": "azure", "OCI DNS": "oracle"}


def account_of(row):
    return str(row.get("_acct") or row.get("profile") or row.get("account") or "")


def cloud_of(row):
    return row.get("cloud") or REGISTRARS.get(row.get("registrar"), "")


def identity(row):
    return str(row.get("id") or row.get("ref") or row.get("name") or row.get("domain") or "")


def reference(row, kind="instances"):
    return "noc://" + "/".join(quote(str(v), safe="") for v in
                               (kind, cloud_of(row), account_of(row), row.get("region") or "", identity(row)))


def scoped(rows, *, cloud=None, account=None, region=None):
    return [r for r in rows if (cloud is None or cloud_of(r) == cloud)
            and (account is None or account_of(r) == account)
            and (region is None or r.get("region") == region)]


def matches(target, rows, kind="instances", **filters):
    rows = scoped(rows, **filters)
    target = (target or "").strip()
    if not target:
        return []
    if target.startswith("noc://"):
        return [r for r in rows if reference(r, kind) == target]
    for fields, folded, substring in ((('id', 'ref'), False, False),
                                     (('name', 'domain'), False, False),
                                     (('ssh_alias', 'public_ip', 'private_ip'), False, False),
                                     (('name', 'domain', 'ssh_alias'), True, False),
                                     (('name', 'domain', 'id', 'ref', 'ssh_alias'), True, True)):
        def hit(row):
            query = target.casefold() if folded else target
            for field in fields:
                value = str(row.get(field) or "")
                value = value.casefold() if folded else value
                if value and (query in value if substring else query == value):
                    return True
            return False
        hits = [r for r in rows if hit(r)]
        if hits:
            return hits
    return []


def resolve(target, rows, kind="instances", **filters):
    hits = matches(target, rows, kind, **filters)
    return (hits[0], []) if len(hits) == 1 else (None, hits)


def load():
    try:
        data = json.loads(paths.data_json_path().read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def capabilities(row, kind):
    if kind == "databases" and cloud_of(row) == "supabase" and row.get("ref"):
        return ["inventory", "status", "pause", "resume"]
    if kind == "instances":
        from .providers import PROVIDERS
        from .providers.base import Provider
        provider = PROVIDERS.get(cloud_of(row))
        result = ["inventory"]
        for action, method in (("status", "status_argv"), ("reboot", "reboot_argv")):
            if provider and getattr(type(provider), method) is not getattr(Provider, method):
                result.append(action)
        return result
    return ["inventory"]


def plan(row, kind, action, argv=None):
    return {"resource_ref": reference(row, kind), "target": row.get("name") or row.get("domain"),
            "id": identity(row), "cloud": cloud_of(row), "account": account_of(row),
            "region": row.get("region") or "", "kind": kind, "action": action,
            "read_only": action in ("inventory", "status"),
            "confirmation_required": action not in ("inventory", "status", "reboot"),
            **({"argv": argv} if argv is not None else {})}


def describe(row, kind):
    return {**row, "resource_ref": reference(row, kind), "capabilities": capabilities(row, kind)}


def query(data, target="", kind="instances", *, cloud=None, account=None, region=None):
    filters = dict(cloud=cloud, account=account, region=region)
    rows = matches(target, data.get(kind, []), kind, **filters) if target else scoped(data.get(kind, []), **filters)
    return {"kind": kind, "count": len(rows), "resources": [describe(r, kind) for r in rows],
            "collection_status": [s for s in data.get("collection_status", []) if s.get("kind") == kind
                                  and (not cloud or s.get("cloud") == cloud)
                                  and (not account or s.get("account") == account)
                                  and (not region or s.get("region") in ("", region))]}


def main(argv=None):
    parser = argparse.ArgumentParser(description="只读查询工作区清单，输出可跨 CLI/MCP 使用的唯一资源引用。")
    parser.add_argument("target", nargs="?", default="")
    parser.add_argument("--kind", choices=KINDS, default="instances")
    for flag in ("cloud", "account", "region"):
        parser.add_argument("--" + flag)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    data = load()
    filters = dict(cloud=args.cloud, account=args.account, region=args.region)
    payload = query(data, args.target, args.kind, **filters)
    rows = payload["resources"]
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        for r in payload["resources"]:
            print(f"{r.get('name') or r.get('domain') or identity(r)}  {r['resource_ref']}  可用操作：{', '.join(r['capabilities'])}")
        print(f"共 {len(rows)} 条。清单状态不代表实时状态；实时查询请使用 status。")
    return 0 if rows else 1
