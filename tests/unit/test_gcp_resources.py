"""GcpProvider.list_databases / list_serverless / list_domains：Cloud SQL · Functions+Run · Domains+DNS 只读盘点。

_gcloud 或 subprocess.run 全部打桩，绝不碰真 gcloud。假 JSON 照搬各 `gcloud … list --format json` 的字段形状
（2026-10-25 对照 gcloud 590 的 REST 资源定义写成），值一律示例值（项目 my-project-123、example.com、203.0.113.x）。
"""
import subprocess

import pytest

from noc_ops import sync
from noc_ops.providers import PROVIDERS, gcp

pytestmark = pytest.mark.unit

ACCOUNTS = [{"project": "my-project-123", "configuration": "c"},
            {"project": "other-project-456"}]

# ---- Cloud SQL（gcloud sql instances list）----
PG = {
    "name": "pg-main", "project": "my-project-123", "region": "asia-northeast1", "gceZone": "asia-northeast1-b",
    "databaseVersion": "POSTGRES_15", "state": "RUNNABLE", "connectionName": "my-project-123:asia-northeast1:pg-main",
    "createTime": "2026-03-02T08:00:00.000Z",
    "ipAddresses": [{"ipAddress": "203.0.113.10", "type": "PRIMARY"}, {"ipAddress": "10.8.0.3", "type": "PRIVATE"}],
    "settings": {"tier": "db-custom-2-7680", "dataDiskSizeGb": "20", "dataDiskType": "PD_SSD",
                 "availabilityType": "REGIONAL", "activationPolicy": "ALWAYS"},
}
MYSQL_STOPPED = {   # 停掉的实例 state 仍是 RUNNABLE，只有 activationPolicy=NEVER 能看出来
    "name": "mysql-dev", "region": "us-central1", "databaseVersion": "MYSQL_8_0", "state": "RUNNABLE",
    "ipAddresses": [{"ipAddress": "10.8.0.9", "type": "PRIVATE"}],
    "settings": {"tier": "db-f1-micro", "dataDiskSizeGb": "10", "dataDiskType": "PD_HDD",
                 "availabilityType": "ZONAL", "activationPolicy": "NEVER"},
}
MSSQL = {
    "name": "mssql", "region": "europe-west1", "databaseVersion": "SQLSERVER_2019_STANDARD",
    "state": "PENDING_CREATE", "settings": {"tier": "db-custom-4-15360"},
}

# ---- Cloud Functions（gcloud functions list --v2）----
FN_GEN2 = {
    "name": "projects/my-project-123/locations/asia-northeast1/functions/hello-http",
    "environment": "GEN_2", "state": "ACTIVE", "updateTime": "2026-09-30T12:34:56.000000Z",
    "buildConfig": {"runtime": "python312", "entryPoint": "main"},
    "serviceConfig": {"availableMemory": "256M", "timeoutSeconds": 60,
                      "uri": "https://hello-http-abc-an.a.run.app",
                      "service": "projects/my-project-123/locations/asia-northeast1/services/hello-http"},
}
FN_GEN1 = {
    "name": "projects/my-project-123/locations/us-central1/functions/legacy",
    "environment": "GEN_1", "state": "ACTIVE", "updateTime": "2024-01-15T00:00:00Z",
    "buildConfig": {"runtime": "nodejs18"},
    "serviceConfig": {"availableMemory": "512M", "timeoutSeconds": 540},
}

# ---- Cloud Run（gcloud run services list，Knative 风格）----
def _run_service(name, region, image, mem="512Mi", timeout=300, labels=None, ready="2026-10-01T01:02:03.000000Z"):
    return {
        "apiVersion": "serving.knative.dev/v1", "kind": "Service",
        "metadata": {"name": name, "namespace": "123456789012", "creationTimestamp": "2026-01-01T00:00:00Z",
                     "labels": {"cloud.googleapis.com/location": region, **(labels or {})},
                     "annotations": {"serving.knative.dev/lastModifier": "deployer@example.com"}},
        "spec": {"template": {"spec": {"timeoutSeconds": timeout,
                                       "containers": [{"image": image, "resources": {"limits": {"memory": mem, "cpu": "1000m"}}}]}}},
        "status": {"url": f"https://{name}-xyz-an.a.run.app",
                   "conditions": [{"type": "Ready", "status": "True", "lastTransitionTime": ready}]},
    }

RUN_API = _run_service("api", "asia-northeast1", "asia-docker.pkg.dev/my-project-123/app/api:v3", mem="1Gi", timeout=120)
RUN_SHADOW = _run_service("hello-http", "asia-northeast1", "gcr.io/my-project-123/gcf/hello-http",
                          labels={"goog-managed-by": "cloudfunctions"})     # 2nd gen 函数的影子服务，不该重复计

# ---- Cloud Domains / Cloud DNS ----
REG_AUTO = {
    "name": "projects/my-project-123/locations/global/registrations/example.com", "domainName": "example.com",
    "state": "ACTIVE", "createTime": "2023-05-01T00:00:00Z", "expireTime": "2099-05-01T00:00:00Z",
    "managementSettings": {"renewalMethod": "AUTOMATIC_RENEWAL", "transferLockState": "LOCKED"},
}
REG_MANUAL = {
    "name": "projects/my-project-123/locations/global/registrations/example.co.uk", "domainName": "example.co.uk",
    "state": "SUSPENDED", "createTime": "2024-02-01T00:00:00Z", "expireTime": "2020-01-01T00:00:00Z",
    "managementSettings": {"renewalMethod": "MANUAL_RENEWAL"}, "issues": ["UNVERIFIED_EMAIL"],
}
ZONE_PUBLIC = {"name": "example-com", "dnsName": "example.com.", "visibility": "public",
               "creationTime": "2023-05-02T00:00:00.000Z", "dnssecConfig": {"state": "on"}}
ZONE_PRIVATE = {"name": "corp-internal", "dnsName": "corp.example.", "visibility": "private",
                "creationTime": "2025-07-07T00:00:00.000Z"}


def _setup(monkeypatch, responses):
    """responses：{(project, "sub cmd 词"): JSON}，缺的键当 None（= gcloud 失败 / API 未启用）。"""
    calls = []

    def run(account, args, timeout=60):
        key = " ".join(a for a in args[1:5] if not a.startswith("-"))    # 如 "sql instances list" / "functions list"
        calls.append((account["project"], key, list(args)))
        return responses.get((account["project"], key))
    monkeypatch.setattr(gcp, "_gcloud", run)
    monkeypatch.setattr(type(PROVIDERS["gcp"]), "_accounts", staticmethod(lambda: ACCOUNTS))
    return calls


# ===== Cloud SQL =====
def test_databases_rows(monkeypatch):
    calls = _setup(monkeypatch, {("my-project-123", "sql instances list"): [MSSQL, PG, MYSQL_STOPPED],
                                 ("other-project-456", "sql instances list"): []})
    rows = PROVIDERS["gcp"].list_databases()
    assert [r["name"] for r in rows] == ["pg-main", "mssql", "mysql-dev"]      # 账号→区域→名
    by = {r["name"]: r for r in rows}

    pg = by["pg-main"]
    assert pg["cloud"] == "gcp" and pg["account"] == "my-project-123" and pg["flag"] == gcp._FLAG
    assert pg["region"] == pg["region_display"] == "asia-northeast1"
    assert pg["engine"] == "PostgreSQL 15" and pg["class"] == "db-custom-2-7680"
    assert pg["storage"] == "20 GB SSD" and pg["state"] == "available"
    assert pg["endpoint"] == "203.0.113.10" and pg["multi_az"] is True and pg["created"] == "2026-03-02"
    assert "内网 10.8.0.3" in pg["note"] and "高可用" in pg["note"]

    my = by["mysql-dev"]
    assert my["engine"] == "MySQL 8.0" and my["state"] == "stopped"           # RUNNABLE + NEVER = 停机
    assert my["endpoint"] == "—" and my["storage"] == "10 GB HDD" and my["multi_az"] is False

    ms = by["mssql"]
    assert ms["engine"] == "SQL Server 2019 Standard" and ms["state"] == "pending_create"
    assert ms["storage"] == "—" and ms["endpoint"] == "—"

    # 两个项目各一条只读 list；带 --project 与 --format json
    assert [(p, k) for p, k, _ in calls] == [("my-project-123", "sql instances list"),
                                            ("other-project-456", "sql instances list")]
    argv = calls[0][2]
    assert argv[:4] == ["gcloud", "sql", "instances", "list"]
    assert "--project" in argv and argv[argv.index("--project") + 1] == "my-project-123"
    assert "json" in argv


def test_sql_engine_names():
    assert gcp._sql_engine("POSTGRES_15") == "PostgreSQL 15"
    assert gcp._sql_engine("MYSQL_5_7") == "MySQL 5.7"
    assert gcp._sql_engine("MYSQL_8_4") == "MySQL 8.4"
    assert gcp._sql_engine("SQLSERVER_2022_ENTERPRISE") == "SQL Server 2022 Enterprise"
    assert gcp._sql_engine(None) == "—"


# ===== Functions + Run =====
def test_serverless_rows_and_dedup(monkeypatch):
    calls = _setup(monkeypatch, {("my-project-123", "functions list"): [FN_GEN2, FN_GEN1],
                                 ("my-project-123", "run services list"): [RUN_SHADOW, RUN_API],
                                 ("other-project-456", "functions list"): [],
                                 ("other-project-456", "run services list"): None})
    rows = PROVIDERS["gcp"].list_serverless()
    assert [(r["name"], r["kind"]) for r in rows] == [("api", "run"), ("hello-http", "function"), ("legacy", "function")]
    by = {r["name"]: r for r in rows}

    fn = by["hello-http"]
    assert fn["cloud"] == "gcp" and fn["account"] == "my-project-123" and fn["region"] == "asia-northeast1"
    assert fn["runtime"] == "python312" and fn["memory"] == 256 and fn["timeout"] == 60
    assert fn["code_mb"] is None and fn["last_modified"] == "2026-09-30" and fn["state"] == "active"

    legacy = by["legacy"]
    assert legacy["runtime"] == "nodejs18 (gen1)" and legacy["memory"] == 512 and legacy["timeout"] == 540

    api = by["api"]
    assert api["runtime"] == "Cloud Run" and api["memory"] == 1024 and api["timeout"] == 120
    assert api["code_mb"] is None and api["last_modified"] == "2026-10-01"
    assert "api:v3" in api["note"] and "by deployer@example.com" in api["note"]

    # functions 用 --v2 统一格式；run 不带 --region（跨区域全列）
    fn_argv = next(a for p, k, a in calls if k == "functions list")
    assert fn_argv[:5] == ["gcloud", "functions", "list", "--v2", "--project"]
    run_argv = next(a for p, k, a in calls if k == "run services list")
    assert run_argv[:4] == ["gcloud", "run", "services", "list"] and "--region" not in run_argv
    # 每项目两次调用，Run 失败（None）不影响 Functions
    assert sorted(k for p, k, _ in calls if p == "other-project-456") == ["functions list", "run services list"]


def test_mem_mb_parsing():
    assert gcp._mem_mb("256M") == 256 and gcp._mem_mb("256Mi") == 256
    assert gcp._mem_mb("1G") == 1024 and gcp._mem_mb("2Gi") == 2048
    assert gcp._mem_mb("512MB") == 512 and gcp._mem_mb(128) == 128
    assert gcp._mem_mb(None) is None and gcp._mem_mb("lots") is None


# ===== Domains + DNS =====
def test_domains_registrations_and_zones(monkeypatch):
    calls = _setup(monkeypatch, {("my-project-123", "domains registrations list"): [REG_AUTO, REG_MANUAL],
                                 ("my-project-123", "dns managed-zones list"): [ZONE_PUBLIC, ZONE_PRIVATE],
                                 ("other-project-456", "domains registrations list"): None,   # Cloud Domains API 未启用
                                 ("other-project-456", "dns managed-zones list"): [ZONE_PRIVATE]})
    rows = PROVIDERS["gcp"].list_domains()
    # example.com 既注册又托管 → 只留注册行；corp.example 两个项目各一个托管区
    assert [(r["account"], r["domain"], r["type"]) for r in rows] == [
        ("my-project-123", "example.com", "gTLD"),
        ("my-project-123", "example.co.uk", "ccTLD"),
        ("my-project-123", "corp.example", "DNS zone"),
        ("other-project-456", "corp.example", "DNS zone"),
    ]
    assert all(r["registrar"] == "Google Cloud" for r in rows)

    auto = rows[0]
    assert auto["reg_date"] == "2023-05-01" and auto["expire_date"] == "2099-05-01"
    assert isinstance(auto["days_left"], int) and auto["days_left"] > 365 * 50
    assert auto["note"] == "自动续费 ✓"

    manual = rows[1]
    assert manual["days_left"] < 0 and manual["expire_date"] == "2020-01-01"
    assert "未自动续费 ⚠" in manual["note"] and "SUSPENDED" in manual["note"] and "UNVERIFIED_EMAIL" in manual["note"]

    zone = rows[2]
    assert zone["expire_date"] == "" and zone["days_left"] is None
    assert "corp-internal" in zone["note"] and "私有" in zone["note"]

    # 注册与托管区各一条只读 list，每项目两次
    assert [(p, k) for p, k, _ in calls] == [
        ("my-project-123", "domains registrations list"), ("my-project-123", "dns managed-zones list"),
        ("other-project-456", "domains registrations list"), ("other-project-456", "dns managed-zones list")]
    assert calls[0][2][:4] == ["gcloud", "domains", "registrations", "list"]
    assert calls[1][2][:4] == ["gcloud", "dns", "managed-zones", "list"]


def test_dns_zone_not_shadowed_across_projects(monkeypatch):
    """去重只在同一项目内：A 项目注册的域名，B 项目托管同名区照常列出（跨项目托管是真实拓扑，不是重复）。"""
    _setup(monkeypatch, {("my-project-123", "domains registrations list"): [REG_AUTO],
                         ("other-project-456", "dns managed-zones list"): [ZONE_PUBLIC]})
    rows = PROVIDERS["gcp"].list_domains()
    assert [(r["account"], r["type"]) for r in rows] == [("my-project-123", "gTLD"), ("other-project-456", "DNS zone")]


# ===== 失败路径：API 未启用 / CLI 缺失 =====
API_DISABLED_STDERR = (
    "ERROR: (gcloud.sql.instances.list) [sa@my-project-123.iam.gserviceaccount.com] does not have permission to "
    "access projects instance [my-project-123] (or it may not exist): Cloud SQL Admin API has not been used in "
    "project my-project-123 before or it is disabled. Enable it by visiting https://console.developers.google.com/"
    "apis/api/sqladmin.googleapis.com/overview?project=my-project-123 then retry.\n"
)


def test_api_not_enabled_yields_empty_and_one_hint(monkeypatch, capsys):
    """真 gcloud 报错形态（2026-10-25 实测）：rc=1，stderr 带「has not been used in project … or it is disabled」。
    走到 subprocess 层：返回空、stderr 一行「启用 API」提示、不抛、不去启用。"""
    seen = []

    def fake_run(cmd, **kw):
        seen.append((cmd, kw))
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr=API_DISABLED_STDERR)
    monkeypatch.setattr(gcp.subprocess, "run", fake_run)
    monkeypatch.setattr(type(PROVIDERS["gcp"]), "_accounts", staticmethod(lambda: ACCOUNTS[:1]))

    assert PROVIDERS["gcp"].list_databases() == []
    err = capsys.readouterr().err
    assert err.count("⚠️ GCP my-project-123 sql instances") == 1 and "services enable" in err
    # 永远只读：没有任何 enable / create / delete 调用；带 --quiet 且关 stdin（否则 gcloud 的「要启用吗？」会挂死）
    cmd, kw = seen[0]
    assert cmd[:4] == ["gcloud", "sql", "instances", "list"] and "--quiet" in cmd
    assert "--configuration" in cmd and cmd[cmd.index("--configuration") + 1] == "c"
    assert kw.get("stdin") is subprocess.DEVNULL
    assert not any(w in " ".join(cmd) for w in ("enable", "create", "delete", "update", "patch"))


def test_cli_missing_yields_empty_no_raise(monkeypatch, capsys):
    """本机没装 gcloud（FileNotFoundError）：三类全部返回空，提示一行，`ops sync` 不崩。"""
    def boom(cmd, **kw):
        raise FileNotFoundError(2, "No such file or directory", "gcloud")
    monkeypatch.setattr(gcp.subprocess, "run", boom)
    monkeypatch.setattr(type(PROVIDERS["gcp"]), "_accounts", staticmethod(lambda: ACCOUNTS[:1]))

    p = PROVIDERS["gcp"]
    assert p.list_databases() == [] and p.list_serverless() == [] and p.list_domains() == []
    err = capsys.readouterr().err
    assert "跑 gcloud 失败" in err and "No such file" in err


def test_timeout_yields_empty_no_raise(monkeypatch):
    def slow(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, kw.get("timeout", 60))
    monkeypatch.setattr(gcp.subprocess, "run", slow)
    monkeypatch.setattr(type(PROVIDERS["gcp"]), "_accounts", staticmethod(lambda: ACCOUNTS[:1]))
    assert PROVIDERS["gcp"].list_serverless() == []


# ===== sync 接线 =====
def test_sync_collect_kinds(monkeypatch):
    """三类都走 sync._collect_kind（全量与按 tab 单刷共用）；域名行拿到 abandoned 标记，数据库行 note 不被清空。"""
    monkeypatch.setattr(sync.shutil, "which", lambda cmd: "/mock/bin/" + cmd)
    _setup(monkeypatch, {("my-project-123", "sql instances list"): [PG],
                         ("my-project-123", "functions list"): [FN_GEN2],
                         ("my-project-123", "domains registrations list"): [REG_AUTO]})
    dbs = sync._collect_kind("databases", clouds={"gcp"})
    assert [d["name"] for d in dbs] == ["pg-main"] and "内网" in dbs[0]["note"]
    fns = sync._collect_kind("serverless", clouds={"gcp"})
    assert [f["name"] for f in fns] == ["hello-http"]
    doms = sync._collect_kind("domains", clouds={"gcp"})
    assert [d["domain"] for d in doms] == ["example.com"] and doms[0]["abandoned"] == ""
    assert sync.REGISTRAR_CLOUD.get("Google Cloud") == "gcp"
