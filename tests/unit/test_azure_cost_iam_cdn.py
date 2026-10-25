"""AzureProvider.list_billing / list_iam / list_cdn：三类只读资源的归一化（_az 全桩，不碰真 az）。

覆盖：costmanagement query 正常 → 行结构；extension 不可用退回 consumption usage list；两路皆败不抛；
RBAC 按主体聚合 + Graph createdDateTime 回填；Graph 403 静默；Front Door 与经典 CDN 两种树；
Microsoft.Cdn 未注册 → 空 + 一行提示；所有 az 调用只读。
"""
import subprocess

import pytest

from noc_ops.providers import PROVIDERS, azure

pytestmark = pytest.mark.unit

SUB = "00000000-0000-0000-0000-000000000000"
ACCOUNTS = [{"subscription": SUB}]
MGMT = "https://management.azure.com"


def _dispatch(responses, calls):
    """按 argv 片段匹配假响应：key 是 (子命令串) 或 ("rest", url 子串)。缺省 None（=az 失败）。"""
    def run(account, args, timeout=60, soft=False):
        calls.append(list(args))
        if args[1] == "rest":
            url = args[args.index("--url") + 1]
            for (kind, needle), resp in responses.items():
                if kind == "rest" and needle in url:
                    return resp() if callable(resp) else resp
            return None
        key = " ".join(args[1:4])
        for (kind, needle), resp in responses.items():
            if kind == "cmd" and key.startswith(needle):
                return resp() if callable(resp) else resp
        return None
    return run


def _setup(monkeypatch, responses):
    calls = []
    monkeypatch.setattr(azure, "_az", _dispatch(responses, calls))
    monkeypatch.setattr(type(PROVIDERS["azure"]), "_accounts", staticmethod(lambda: ACCOUNTS))
    return calls


def _assert_read_only(calls):
    """硬规矩：所有 az 调用都是 list/show/query 类，绝无写操作。
    `az rest` 只许 GET；唯一例外是 Cost Management 的 query（查询语义的 POST，用户 2026-10-25 放开）。"""
    for args in calls:
        assert args[0] == "az"
        if args[1] == "rest":
            method, url = args[args.index("--method") + 1], args[args.index("--url") + 1]
            assert method == "get" or (method == "post" and "/Microsoft.CostManagement/query" in url), args
        else:
            assert args[1:4] in (["costmanagement", "query", "--type"],
                                 ["consumption", "usage", "list"],
                                 ["role", "assignment", "list"]), args[:4]


# ───────────────────────── billing ─────────────────────────

def _months():
    start, _end, cur = azure.AzureProvider._month_window(3)
    first = start[:7]
    y, m = int(first[:4]), int(first[5:])
    m += 1
    if m > 12:
        m, y = 1, y + 1
    return first, f"{y:04d}-{m:02d}", cur


def test_billing_costmanagement_query_rows(monkeypatch):
    m1, m2, cur = _months()
    calls = _setup(monkeypatch, {
        ("cmd", "costmanagement query"): {
            # 列顺序故意与直觉相反（实测是 Cost, BillingMonth, ServiceName, Currency），验证按 columns 定位
            "columns": [{"name": "ServiceName", "type": "String"}, {"name": "Currency", "type": "String"},
                        {"name": "BillingMonth", "type": "DateTime"}, {"name": "Cost", "type": "Number"}],
            "rows": [["Virtual Machines", "USD", f"{m1}-01T00:00:00", 12.345],
                     ["Storage", "USD", f"{m2}-01T00:00:00", 0],          # 0 金额跳过
                     ["Storage", "USD", f"{cur}-01T00:00:00", 3.2]],
        },
    })
    rows = PROVIDERS["azure"].list_billing()
    assert [(r["service"], r["month"], r["amount"]) for r in rows] == [
        ("Virtual Machines", m1, 12.35), ("Storage", cur, 3.2)]
    assert rows[0]["partial"] is False and rows[1]["partial"] is True
    assert rows[0]["cloud"] == "azure" and rows[0]["account"] == SUB and rows[0]["currency"] == "USD"
    assert rows[0]["period_start"] == f"{m1}-01" and rows[0]["fetched_at"]
    assert set(rows[0]) == {"cloud", "account", "service", "month", "amount", "currency",
                            "period_start", "partial", "fetched_at"}
    # argv：订阅 scope、ActualCost、Monthly、按 ServiceName 分组；没退到 consumption
    q = next(c for c in calls if c[1] == "costmanagement")
    assert q[1:3] == ["costmanagement", "query"]
    assert q[q.index("--scope") + 1] == f"/subscriptions/{SUB}"
    assert q[q.index("--type") + 1] == "ActualCost" and q[q.index("--dataset-granularity") + 1] == "Monthly"
    assert q[q.index("--dataset-grouping") + 1:q.index("--dataset-grouping") + 3] == ["name=ServiceName", "type=Dimension"]
    assert q[q.index("--time-period") + 1].startswith("from=") and q[q.index("--time-period") + 1].endswith("-01")
    assert not any(c[1] == "consumption" for c in calls)
    _assert_read_only(calls)


def test_billing_falls_back_to_consumption_when_extension_missing(monkeypatch):
    """costmanagement 在 extension 里；本机没装 → _az 返回 None → 退回 consumption usage list 聚合。"""
    m1, _m2, cur = _months()
    calls = _setup(monkeypatch, {
        # ("cmd", "costmanagement query") 缺省 → None
        ("cmd", "consumption usage list"): [
            # 不带 --include-meter-details（会挂死），meterDetails 为 None → 按 consumedService 聚合
            {"pretaxCost": "1.004", "currency": "USD", "usageStart": f"{m1}-03T00:00:00Z",
             "meterDetails": None, "consumedService": "Microsoft.Compute", "instanceName": "vm-a"},
            {"pretaxCost": "2.001", "currency": "USD", "usageStart": f"{m1}-15T00:00:00Z",
             "meterDetails": None, "consumedService": "Microsoft.Compute", "instanceName": "vm-b"},
            {"pretaxCost": "0", "currency": "USD", "usageStart": f"{m1}-15T00:00:00Z",
             "meterDetails": None, "consumedService": "Microsoft.Network"},
            {"pretaxCost": "0.5", "currency": "USD", "usageStart": f"{cur}-02T00:00:00Z",
             "meterDetails": {"meterCategory": "Storage"}, "consumedService": "Microsoft.Storage"},
        ],
    })
    rows = PROVIDERS["azure"].list_billing()
    assert [(r["service"], r["month"], r["amount"], r["partial"]) for r in rows] == [
        ("Microsoft.Compute", m1, 3.0, False),         # 两台 VM 同服务聚合、round 2；0 金额行跳过
        ("Storage", cur, 0.5, True),                   # API 给了 meterCategory 就优先用它
    ]
    assert [c[1] for c in calls if c[1] != "rest"] == ["costmanagement", "consumption"]
    c = next(c for c in calls if c[1] == "consumption")
    assert c[1:4] == ["consumption", "usage", "list"] and "--include-meter-details" not in c
    assert c[c.index("--start-date") + 1] == f"{m1}-01" and c[c.index("--subscription") + 1] == SUB
    assert c[c.index("--end-date") + 1] == azure.AzureProvider._month_window(3)[1]
    _assert_read_only(calls)


def test_billing_both_paths_fail_returns_empty_with_hint(monkeypatch, capsys):
    _setup(monkeypatch, {})
    assert PROVIDERS["azure"].list_billing() == []
    err = capsys.readouterr().err
    assert SUB in err and "成本" in err and err.count("\n") == 1


def test_billing_free_subscription_no_cost_hint(monkeypatch, capsys):
    """路 ① 形态对但零行（免费订阅）→ 不出行、一行提示、不再退路 ②（空≠失败）。"""
    calls = _setup(monkeypatch, {
        ("cmd", "costmanagement query"): {"properties": {"columns": [{"name": "Cost"}, {"name": "BillingMonth"},
                                                                   {"name": "ServiceName"}, {"name": "Currency"}],
                                                        "rows": []}},
    })
    assert PROVIDERS["azure"].list_billing() == []
    assert "无成本记录" in capsys.readouterr().err
    assert [c[1] for c in calls if c[1] != "rest"] == ["costmanagement"]


# ───────────────────────── billing：路 ① 计费档案 scope（MCA） ─────────────────────────

SUB2 = "11111111-1111-1111-1111-111111111111"          # 同租户、同档案下另一个订阅（配置里没写）
BA = {"value": [{"name": "ba-1", "id": "/providers/Microsoft.Billing/billingAccounts/ba-1",
                 "properties": {"hasReadAccess": True, "agreementType": "MicrosoftCustomerAgreement"}}]}
BP = {"value": [{"name": "BP-1", "properties": {"currency": "CNY"}}]}


def _profile_table(m1, cur):
    # 真实形态（2026-10-25 实测）：Cost/BillingMonth/ServiceName/SubscriptionId/Currency；
    # 含一条 ServiceName/SubscriptionId 皆空的信用抵扣负数行，和一条 0.00002 的存储零头。
    return {"properties": {
        "columns": [{"name": "Cost"}, {"name": "BillingMonth"}, {"name": "ServiceName"},
                    {"name": "SubscriptionId"}, {"name": "Currency"}],
        "rows": [[-1.6e-05, f"{m1}-01T00:00:00", "", "", "CNY"],
                 [1.6e-05, f"{m1}-01T00:00:00", "Storage", SUB2, "CNY"],
                 [12.5, f"{m1}-01T00:00:00", "Virtual Machines", SUB, "CNY"],
                 [3.25, f"{cur}-01T00:00:00", "Bandwidth", SUB2, "CNY"]],
    }}


def test_billing_profile_scope_rows_and_other_subscription(monkeypatch):
    m1, _m2, cur = _months()
    calls = _setup(monkeypatch, {
        ("rest", "/billingAccounts?"): BA,
        ("rest", "/billingProfiles?"): BP,
        ("rest", "/Microsoft.CostManagement/query"): _profile_table(m1, cur),
    })
    rows = PROVIDERS["azure"].list_billing()
    # 负数抵扣行与四舍五入为 0.00 的零头都不出行；行的 account 取表里的 SubscriptionId（配置外的订阅也看得到）
    assert [(r["account"], r["service"], r["month"], r["amount"], r["currency"]) for r in rows] == [
        (SUB, "Virtual Machines", m1, 12.5, "CNY"), (SUB2, "Bandwidth", cur, 3.25, "CNY")]
    assert rows[0]["partial"] is False and rows[1]["partial"] is True
    # 走了档案级 POST 就不再退订阅级两路
    assert not any(c[1] in ("costmanagement", "consumption") for c in calls)
    q = next(c for c in calls if c[1] == "rest" and "/Microsoft.CostManagement/query" in c[c.index("--url") + 1])
    assert q[q.index("--method") + 1] == "post"
    assert "/billingAccounts/ba-1/billingProfiles/BP-1/" in q[q.index("--url") + 1]
    import json
    body = json.loads(q[q.index("--body") + 1])
    assert body["type"] == "ActualCost" and body["dataset"]["granularity"] == "Monthly"
    assert [g["name"] for g in body["dataset"]["grouping"]] == ["ServiceName", "SubscriptionId"]
    assert body["timePeriod"]["from"].endswith("-01")
    _assert_read_only(calls)


def test_billing_profile_queried_once_for_two_subscriptions(monkeypatch):
    """档案是租户级的：配置两个订阅共享同一档案 → 只 POST 一次，不出重复行。"""
    m1, _m2, cur = _months()
    calls = _setup(monkeypatch, {
        ("rest", "/billingAccounts?"): BA,
        ("rest", "/billingProfiles?"): BP,
        ("rest", "/Microsoft.CostManagement/query"): _profile_table(m1, cur),
    })
    monkeypatch.setattr(type(PROVIDERS["azure"]), "_accounts",
                        staticmethod(lambda: [{"subscription": SUB}, {"subscription": SUB2}]))
    rows = PROVIDERS["azure"].list_billing()
    assert len(rows) == 2
    assert sum(1 for c in calls if "/Microsoft.CostManagement/query" in " ".join(c)) == 1


def test_billing_profile_query_failure_falls_back_to_subscription_paths(monkeypatch):
    """档案找得到但 query 失败（429/权限）→ 退回订阅级 costmanagement → consumption。"""
    m1, _m2, _cur = _months()
    calls = _setup(monkeypatch, {
        ("rest", "/billingAccounts?"): BA,
        ("rest", "/billingProfiles?"): BP,
        ("cmd", "consumption usage"): [{"pretaxCost": "2.0", "consumedService": "Microsoft.Compute",
                                        "usageStart": f"{m1}-03T00:00:00Z", "currency": "USD"}],
    })
    rows = PROVIDERS["azure"].list_billing()
    assert [(r["service"], r["amount"], r["account"]) for r in rows] == [("Microsoft.Compute", 2.0, SUB)]
    assert [c[1] for c in calls if c[1] != "rest"] == ["costmanagement", "consumption"]


# ───────────────────────── 安全分类：az rest 按 HTTP 方法 ─────────────────────────

def test_az_rest_read_only_by_http_method():
    from noc_ops.aws_safety import is_read_only_azure
    q = "/providers/Microsoft.Billing/billingAccounts/x/billingProfiles/y/providers/Microsoft.CostManagement/query?api-version=2023-11-01"
    assert is_read_only_azure(f"rest --method get --url {MGMT}/subscriptions/{SUB}/resourcegroups?api-version=2021-04-01")
    assert is_read_only_azure(f"rest --url {MGMT}/x")                                   # 省略 --method = GET
    assert is_read_only_azure(f"rest --method post --url {MGMT}{q} --body '{{}}'")       # 唯一放开的 POST
    assert is_read_only_azure(f"rest --method=POST --url {MGMT}{q}")
    assert not is_read_only_azure(f"rest --method post --url {MGMT}/subscriptions/{SUB}/providers/Microsoft.Compute/virtualMachines/v/restart?api-version=2024-03-01")
    assert not is_read_only_azure(f"rest --method put --url {MGMT}/subscriptions/{SUB}/x")
    assert not is_read_only_azure(f"rest --method delete --url {MGMT}/subscriptions/{SUB}/x")
    assert not is_read_only_azure(f"rest --method patch --url {MGMT}{q}")              # query 也只放 POST
    assert is_read_only_azure("vm list -o json") and not is_read_only_azure("vm restart -n v -g g")


# ───────────────────────── IAM ─────────────────────────

ROLE_ASSIGNMENTS = [
    {"principalId": "u-1", "principalName": "user@example.com", "principalType": "User",
     "roleDefinitionName": "Owner", "scope": f"/subscriptions/{SUB}"},
    {"principalId": "u-1", "principalName": "user@example.com", "principalType": "User",
     "roleDefinitionName": "Contributor", "scope": f"/subscriptions/{SUB}/resourceGroups/rg-a"},
    {"principalId": "u-1", "principalName": "user@example.com", "principalType": "User",
     "roleDefinitionName": "Owner", "scope": f"/subscriptions/{SUB}/resourceGroups/rg-b"},   # 角色去重
    {"principalId": "sp-9", "principalName": "11111111-aaaa-bbbb-cccc-222222222222", "principalType": "ServicePrincipal",
     "roleDefinitionName": "Reader", "scope": f"/subscriptions/{SUB}"},
    {"principalId": "mi-5", "principalName": None, "principalType": "ManagedIdentity",
     "roleDefinitionName": None, "roleDefinitionId": "/subscriptions/x/providers/Microsoft.Authorization/roleDefinitions/abc",
     "scope": f"/subscriptions/{SUB}"},
]
GRAPH_USERS = {"value": [{"id": "u-1", "userPrincipalName": "User@Example.com",
                          "createdDateTime": "2024-03-05T10:00:00Z"}]}


def test_iam_aggregates_role_assignments_per_principal(monkeypatch):
    calls = _setup(monkeypatch, {
        ("cmd", "role assignment list"): ROLE_ASSIGNMENTS,
        ("rest", "graph.microsoft.com/v1.0/users"): GRAPH_USERS,
    })
    rows = PROVIDERS["azure"].list_iam()
    by = {r["name"]: r for r in rows}
    assert set(by) == {"user@example.com", "11111111-aaaa-bbbb-cccc-222222222222", "mi-5"}   # 无 principalName 退 principalId
    u = by["user@example.com"]
    assert u["detail"] == "User · 角色: Owner, Contributor · 作用域 3 个"
    assert u["created"] == "2024-03-05"                        # Graph createdDateTime 回填（UPN 大小写不敏感）
    assert u["cloud"] == "azure" and u["account"] == SUB and u["last_used"] == "—"
    assert u["key_count"] is None and u["oldest_key_days"] is None
    assert set(u) == {"name", "cloud", "account", "created", "last_used", "key_count", "oldest_key_days", "detail"}
    assert by["11111111-aaaa-bbbb-cccc-222222222222"]["detail"] == "ServicePrincipal · 角色: Reader · 作用域 1 个"
    assert by["11111111-aaaa-bbbb-cccc-222222222222"]["created"] == ""
    assert by["mi-5"]["detail"] == "ManagedIdentity · 角色: abc · 作用域 1 个"   # 无角色名退 id 尾段
    r = calls[0]
    assert r[1:4] == ["role", "assignment", "list"] and "--all" in r and "--include-inherited" in r
    assert r[r.index("--subscription") + 1] == SUB
    graph = [c for c in calls if c[1] == "rest"]
    assert len(graph) == 1 and "$select=id,userPrincipalName,createdDateTime" in graph[0][graph[0].index("--url") + 1]
    _assert_read_only(calls)


def test_iam_graph_forbidden_is_silent(monkeypatch, capsys):
    """没 Graph 读权限（403）→ created 留空，stderr 不刷任何提示。"""
    _setup(monkeypatch, {("cmd", "role assignment list"): ROLE_ASSIGNMENTS})
    rows = PROVIDERS["azure"].list_iam()
    assert len(rows) == 3 and all(r["created"] == "" for r in rows)
    assert capsys.readouterr().err == ""


def test_iam_skips_graph_when_no_user_principals(monkeypatch):
    calls = _setup(monkeypatch, {
        ("cmd", "role assignment list"): [ROLE_ASSIGNMENTS[3]],
        ("rest", "graph.microsoft.com"): GRAPH_USERS,
    })
    assert len(PROVIDERS["azure"].list_iam()) == 1
    assert not any(c[1] == "rest" for c in calls)


def test_iam_role_list_failure_returns_empty(monkeypatch):
    _setup(monkeypatch, {})
    assert PROVIDERS["azure"].list_iam() == []


# ───────────────────────── CDN ─────────────────────────

AFD_PID = f"/subscriptions/{SUB}/resourceGroups/rg-cdn/providers/Microsoft.Cdn/profiles/fd-prod"
CLASSIC_PID = f"/subscriptions/{SUB}/resourceGroups/rg-cdn/providers/Microsoft.Cdn/profiles/cdn-legacy"
CDN_TREE = {
    ("rest", "/providers/Microsoft.Cdn/profiles?"): {"value": [
        {"id": AFD_PID, "name": "fd-prod", "sku": {"name": "Standard_AzureFrontDoor"},
         "properties": {"provisioningState": "Succeeded"}},
        {"id": CLASSIC_PID, "name": "cdn-legacy", "sku": {"name": "Standard_Microsoft"},
         "properties": {"provisioningState": "Succeeded"}},
    ]},
    # Front Door 树
    ("rest", f"{AFD_PID}/afdEndpoints?"): {"value": [
        {"id": f"{AFD_PID}/afdEndpoints/web", "name": "web",
         "properties": {"hostName": "web-abc123.z01.azurefd.net", "enabledState": "Enabled",
                        "provisioningState": "Succeeded"}},
        {"id": f"{AFD_PID}/afdEndpoints/api", "name": "api",
         "properties": {"hostName": "api-abc123.z01.azurefd.net", "enabledState": "Disabled",
                        "provisioningState": "Succeeded"}},
    ]},
    ("rest", f"{AFD_PID}/customDomains?"): {"value": [
        {"id": f"{AFD_PID}/customDomains/www-example-com", "properties": {"hostName": "www.example.com"}},
    ]},
    ("rest", f"{AFD_PID}/originGroups?"): {"value": [
        {"id": f"{AFD_PID}/originGroups/og-web", "name": "og-web"},
    ]},
    ("rest", f"{AFD_PID}/originGroups/og-web/origins?"): {"value": [
        {"properties": {"hostName": "origin.example.com"}},
        {"properties": {"hostName": "origin2.example.com"}},
    ]},
    ("rest", f"{AFD_PID}/afdEndpoints/web/routes?"): {"value": [
        {"properties": {"customDomains": [{"id": f"{AFD_PID}/customDomains/WWW-EXAMPLE-COM"}],   # 大小写不敏感
                        "originGroup": {"id": f"{AFD_PID}/originGroups/og-web"}}},
    ]},
    ("rest", f"{AFD_PID}/afdEndpoints/api/routes?"): {"value": []},
    # 经典 CDN 树
    ("rest", f"{CLASSIC_PID}/endpoints?"): {"value": [
        {"id": f"{CLASSIC_PID}/endpoints/static", "name": "static",
         "properties": {"hostName": "static.azureedge.net", "resourceState": "Running",
                        "provisioningState": "Succeeded",
                        "origins": [{"name": "o1", "properties": {"hostName": "bucket.blob.core.windows.net"}}]}},
    ]},
    ("rest", f"{CLASSIC_PID}/endpoints/static/customDomains?"): {"value": [
        {"properties": {"hostName": "cdn.example.com"}},
    ]},
}


def test_cdn_front_door_and_classic(monkeypatch):
    calls = _setup(monkeypatch, CDN_TREE)
    rows = PROVIDERS["azure"].list_cdn()
    by = {r["id"]: r for r in rows}
    assert set(by) == {"web", "api", "static"}
    web = by["web"]
    assert web == {
        "id": "web", "domain": "web-abc123.z01.azurefd.net", "aliases": "www.example.com",
        "origin": "origin.example.com, origin2.example.com", "status": "Succeeded", "enabled": True,
        "cert": "—", "price_class": "Standard_AzureFrontDoor", "last_modified": "",
        "account": SUB, "cloud": "azure", "partition": "global", "comment": "fd-prod",
    }
    assert by["api"]["enabled"] is False and by["api"]["aliases"] == "" and by["api"]["origin"] == ""
    st = by["static"]
    assert st["domain"] == "static.azureedge.net" and st["aliases"] == "cdn.example.com"
    assert st["origin"] == "bucket.blob.core.windows.net" and st["status"] == "Running" and st["enabled"] is True
    assert st["price_class"] == "Standard_Microsoft" and st["comment"] == "cdn-legacy"
    # 全部 az rest GET、带 api-version、带 --subscription；profile 级映射各查一次（不随端点数翻倍）
    urls = [c[c.index("--url") + 1] for c in calls]
    assert all("api-version=" in u for u in urls)
    assert all(c[c.index("--subscription") + 1] == SUB for c in calls)
    assert sum(1 for u in urls if "/customDomains?" in u and AFD_PID in u) == 1
    assert sum(1 for u in urls if "/originGroups?" in u) == 1
    _assert_read_only(calls)


def test_cdn_provider_not_registered_is_empty_with_one_hint(monkeypatch, capsys):
    """Microsoft.Cdn 未注册 → profiles GET 失败 → 该类空、一行提示、不抛。"""
    _setup(monkeypatch, {})
    assert PROVIDERS["azure"].list_cdn() == []
    err = capsys.readouterr().err
    assert "CDN" in err and SUB in err and err.count("\n") == 1


def test_cdn_empty_subscription_no_noise(monkeypatch, capsys):
    """订阅没任何 profile（实测未注册 provider 时 list 也常返回空 value）→ 空且静默。"""
    _setup(monkeypatch, {("rest", "/providers/Microsoft.Cdn/profiles?"): {"value": []}})
    assert PROVIDERS["azure"].list_cdn() == []
    assert capsys.readouterr().err == ""


def test_cdn_follows_next_link(monkeypatch):
    page2 = {"value": [{"id": CLASSIC_PID, "name": "cdn-legacy", "sku": {"name": "Standard_Microsoft"}}]}
    tree = {
        ("rest", "profiles?api-version"): {"value": [], "nextLink": f"{MGMT}/subscriptions/{SUB}/providers/Microsoft.Cdn/profiles?$skiptoken=abc"},
        ("rest", "profiles?$skiptoken=abc"): page2,
        ("rest", f"{CLASSIC_PID}/endpoints?"): {"value": []},
    }
    _setup(monkeypatch, tree)
    assert PROVIDERS["azure"].list_cdn() == []          # 翻到第二页拿到 profile，但它没端点


# ───────────────────────── _az 子进程防护 ─────────────────────────

def test_az_disables_dynamic_extension_install_and_closes_stdin(monkeypatch):
    """缺 extension 时 az 默认会交互问"要不要装"——sync 子进程里必须关 stdin、禁动态安装，让它直接失败退路。"""
    seen = {}

    def fake_run(args, **kw):
        seen.update(kw)
        return subprocess.CompletedProcess(args, 2, stdout="", stderr="ERROR: 'costmanagement' is misspelled or not recognized by the system.")
    monkeypatch.setattr(azure.subprocess, "run", fake_run)
    assert azure._az({"subscription": SUB}, ["az", "costmanagement", "query"], soft=True) is None
    assert seen["env"]["AZURE_EXTENSION_USE_DYNAMIC_INSTALL"] == "no"
    assert seen["stdin"] is subprocess.DEVNULL
