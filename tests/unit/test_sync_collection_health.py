"""Offline regressions: failed scans are not authoritative empty inventories."""
import json
import subprocess
from types import SimpleNamespace

import pytest

from noc_ops import collection, deploy_config, sync
from noc_ops.providers.aws import AwsProvider
from noc_ops.providers import aws, aliyun, tencent, volcengine, gcp, azure, oracle

pytestmark = pytest.mark.unit
A, B = "123456789012", "111122223333"
STAMP = "2026-01-01T00:00:00+00:00"


def status(account=A, kind="storage", region=""):
    return {"cloud": "aws", "account": account, "kind": kind, "region": region,
            "status": "ok", "attempted_at": STAMP, "last_success_at": STAMP,
            "preserved": False, "row_count": 1, "errors": []}


def instance(iid="i-old", region="us-east-1"):
    return {"id": iid, "cloud": "aws", "profile": A, "region": region,
            "region_display": region, "flag": "", "name": iid, "type": "t3.micro",
            "state": "running", "az": region + "a", "private_ip": "10.0.0.1",
            "public_ip": "192.0.2.1", "key_name": "test", "ssh_alias": "old-" + iid,
            "key_file": "/tmp/test.pem", "ssh_user": "ubuntu", "verified": True,
            "connect": "direct", "proxyjump": None, "note": ""}


@pytest.fixture
def workspace(monkeypatch, tmp_path):
    for attr, name in (("NOC_DATA", "data.json"), ("SNAPSHOT_FILE", "snapshot.json"),
                       ("USER_CACHE_FILE", "ssh-users.json"), ("SSH_CONFIG", "ssh-config"),
                       ("INVENTORY", "inventory.md")):
        monkeypatch.setattr(sync, attr, tmp_path / name)
    monkeypatch.setattr(sync.paths, "ensure_workspace", lambda: None)
    monkeypatch.setattr(sync.shutil, "which", lambda cmd: "/fake/" + cmd)
    monkeypatch.setattr(deploy_config, "_raw", lambda: {
        "aws": {"accounts": [{"profile": A, "partition": "global"},
                             {"profile": B, "partition": "global"}],
                "scan_regions_global": ["us-east-1", "us-west-2"], "route53_profiles": [A, B]}})
    # Any unmocked cloud subprocess must fail locally; never use real credentials.
    def no_process(*args, **kwargs):
        raise AssertionError("unexpected subprocess")
    monkeypatch.setattr(subprocess, "run", no_process)
    return tmp_path


@pytest.mark.parametrize("mode", ["kind", "account"])
def test_failed_account_keeps_rows_healthy_empty_clears_and_recovery(workspace, monkeypatch, mode):
    old = [{"cloud": "aws", "account": a, "name": "bucket-" + a} for a in (A, B)]
    sync.NOC_DATA.write_text(json.dumps({"storage": old, "collection_status": [status(), status(B)]}))
    monkeypatch.setattr(sync, "PROVIDERS", {"aws": AwsProvider()})
    def scan(self):
        acct = deploy_config.aws_accounts()[0]["profile"]
        if acct == A:
            collection.failed()
        return []
    monkeypatch.setattr(AwsProvider, "list_storage", scan)
    run = (lambda: sync.refresh_kind("storage")) if mode == "kind" else (lambda: sync.refresh_account(A, "storage"))
    assert run() == 0
    data = json.loads(sync.NOC_DATA.read_text())
    assert sorted(data["storage"], key=lambda r: r["account"]) == sorted(old[:1] if mode == "kind" else old, key=lambda r: r["account"])
    a = next(s for s in data["collection_status"] if s["account"] == A)
    assert a["status"] == "error" and a["preserved"]
    assert a["last_success_at"] == STAMP and a["attempted_at"] != STAMP
    b = next(s for s in data["collection_status"] if s["account"] == B)
    assert b["last_success_at"] != STAMP if mode == "kind" else b == status(B)
    assert deploy_config.account_scope("aws") is None
    monkeypatch.setattr(AwsProvider, "list_storage", lambda self: [])
    assert run() == 0
    recovered = json.loads(sync.NOC_DATA.read_text())
    assert recovered["storage"] == ([] if mode == "kind" else old[1:])
    a = next(s for s in recovered["collection_status"] if s["account"] == A)
    assert a["status"] == "ok" and a["last_success_at"] != STAMP and not a["preserved"]


def test_missing_cli_preserves_inventory_and_unknown_legacy_timestamp(workspace, monkeypatch):
    old = [{"cloud": "aws", "account": A, "name": "old"}]
    sync.NOC_DATA.write_text(json.dumps({"storage": old, "generated_at": STAMP}))
    monkeypatch.setattr(sync, "PROVIDERS", {"aws": AwsProvider()})
    monkeypatch.setattr(sync.shutil, "which", lambda cmd: None)
    sync.refresh_kind("storage")
    data = json.loads(sync.NOC_DATA.read_text())
    assert data["storage"] == old
    assert data["collection_status"][0]["errors"] == ["cli_missing"]
    assert data["collection_status"][0]["last_success_at"] is None


@pytest.mark.parametrize("mode", ["full", "kind", "account"])
def test_aws_region_failure_preserves_ssh_and_snapshot(workspace, monkeypatch, capsys, mode):
    keep, remove = instance(), instance("i-gone", "us-west-2")
    sync.NOC_DATA.write_text(json.dumps({"instances": [keep, remove],
                                       "collection_status": [status(kind="instances", region="us-east-1")]}))
    snapshot = {r["id"]: {**{k: r.get(k) for k in sync.WATCH_FIELDS}, "vpc": "vpc-old",
                          "profile": A, "region": r["region"]} for r in (keep, remove)}
    sync.SNAPSHOT_FILE.write_text(json.dumps(snapshot))
    monkeypatch.setattr(sync, "_providers", lambda clouds=None: {})
    monkeypatch.setattr(sync, "_collect_kind", lambda *args, **kwargs: [])
    monkeypatch.setattr(sync.ops_log, "backfill_accounts", lambda: None)
    monkeypatch.setattr(sync, "write_inventory", lambda *args: None)
    def describe(profile, region):
        if profile == A and region == "us-east-1":
            raise subprocess.TimeoutExpired("aws", 1)
        return []
    monkeypatch.setattr(sync, "describe", describe)
    if mode == "full":
        monkeypatch.setattr(sync.sys, "argv", ["ops", "--probe=none"])
        sync.main()
    elif mode == "kind":
        sync.refresh_kind("instances")
    else:
        sync.refresh_account(A, "instances")
    data = json.loads(sync.NOC_DATA.read_text())
    assert data["instances"] == [keep]
    text = sync.SSH_CONFIG.read_text()
    assert "Host old-i-old" in text and "User ubuntu" in text
    assert "old-i-gone" not in text
    if mode == "full":
        assert json.loads(sync.SNAPSHOT_FILE.read_text()) == {"i-old": snapshot["i-old"]}
        output = capsys.readouterr().out
        assert "- i-gone" in output and "- i-old" not in output
        assert "✅ 无变更" not in output
    else:
        assert json.loads(sync.SNAPSHOT_FILE.read_text()) == snapshot


@pytest.mark.parametrize("call", [
    lambda: aws._aws_json(A, None, ["s3api", "list-buckets"]),
    lambda: aliyun._ali_json({"profile": "test"}, ["ecs", "DescribeInstances"]),
    lambda: tencent._tc_json(["cvm", "DescribeInstances"]),
    lambda: volcengine._ve_json({}, ["ecs", "DescribeInstances"]),
    lambda: gcp._gcloud({"project": "test"}, ["gcloud", "compute", "instances", "list"]),
    lambda: azure._az({"subscription": "test"}, ["az", "vm", "list"]),
    lambda: oracle._oci_raw({"profile": "test"}, ["oci", "compute", "instance", "list"]),
])
def test_cli_errors_record_failure_even_when_provider_returns_empty(monkeypatch, call):
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=1, stdout="", stderr="denied"))
    old = [{"name": "old"}]
    def collect():
        call()
        return []
    with collection.Session({}) as session:
        assert session.collect("test", A, "instances", old, collect) == old
        assert session.failures()[0]["errors"]


def test_later_page_failure_discards_partial_inventory(monkeypatch):
    results = iter([
        SimpleNamespace(returncode=0, stdout=json.dumps({"Items": [{"id": "new"}], "TotalCount": 2}), stderr=""),
        SimpleNamespace(returncode=1, stdout="", stderr="timeout"),
    ])
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: next(results))
    old = [{"id": "old-1"}, {"id": "old-2"}]
    with collection.Session({}) as session:
        rows = session.collect("tencent", A, "instances", old,
                               lambda: tencent._paged(["test", "List"], "Items", page_size=1))
        assert rows == old and session.failures()[0]["preserved"]


def test_worker_errors_and_optional_enrichment_have_distinct_semantics():
    @collection.observed
    def failed_call():
        return None
    def scan():
        with collection.ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda _: failed_call(), range(2)))
        return [{"name": "new"}]
    with collection.Session({"collection_status": [status()]}) as session:
        assert session.collect("aws", A, "storage", [{"name": "old"}], scan) == [{"name": "old"}]
        assert session.failures()
    with collection.Session({}) as session:
        assert session.collect("aws", A, "storage", [], collection.optional(scan)) == [{"name": "new"}]
        assert not session.failures()
        s = next(iter(session.statuses.values()))
        assert s["status"] == "partial" and s["last_success_at"] is None


def test_atomic_json_write_failure_keeps_previous_file(workspace, monkeypatch):
    sync.NOC_DATA.write_text('{"old":true}')
    def fail_replace(*a):
        raise OSError("disk failure")
    monkeypatch.setattr(sync.os, "replace", fail_replace)
    with pytest.raises(OSError):
        sync._save_json(sync.NOC_DATA, {"new": True})
    assert json.loads(sync.NOC_DATA.read_text()) == {"old": True}
    assert not list(workspace.glob(".data.json.*"))


def test_billing_cache_does_not_claim_new_success(monkeypatch):
    from datetime import datetime, timezone
    rows = [{"fetched_at": datetime.now(timezone.utc).isoformat()}]
    previous = {"billing": rows, "collection_status": [status(kind="billing")]}
    monkeypatch.setattr(sync, "_collect_kind", lambda kind: pytest.fail("fresh billing cache must not be scanned"))
    with collection.Session(previous) as session:
        assert sync._billing_with_cache(previous) == rows
        result = {}
        session.attach(result)
        assert result["collection_status"] == previous["collection_status"]


@pytest.mark.parametrize("cloud", ["cloudflare", "supabase"])
def test_api_error_and_missing_token_keep_old_rows(monkeypatch, cloud):
    import urllib.error
    import urllib.request
    from noc_ops.providers.cloudflare import CloudflareProvider
    from noc_ops.providers.supabase import SupabaseProvider
    provider = CloudflareProvider() if cloud == "cloudflare" else SupabaseProvider()
    old = [{"name": "old"}]
    def denied(*args, **kwargs):
        raise urllib.error.URLError("offline")
    monkeypatch.setattr(urllib.request, "urlopen", denied)
    with collection.Session({}) as session:
        result = session.collect(cloud, "test", "storage", old,
                                 lambda: provider._api("fake-token", "/test"))
        assert result == old and session.failures()
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=1, stdout="", stderr=""))
    def no_token():
        assert provider._token({"account": "test"}) == ""
        return []
    with collection.Session({}) as session:
        assert session.collect(cloud, "test", "storage", old, no_token) == old
        assert session.failures()[0]["errors"] == ["credential_unavailable"]


@pytest.mark.parametrize("cloud,identity", [("gcp", "project"), ("azure", "subscription"), ("oracle", "profile")])
def test_provider_account_scoping_uses_resource_identity(monkeypatch, cloud, identity):
    from noc_ops.providers.base import Provider
    accounts = [{identity: "first"}, {identity: "second"}]
    monkeypatch.setattr(deploy_config, "_raw", lambda: {cloud: {"accounts": accounts}})
    class TestProvider(Provider):
        cli = ""
        def list_storage(self):
            selected = deploy_config.cloud_accounts(cloud)
            assert len(selected) == 1
            key = selected[0][identity]
            if key == "first":
                collection.failed()
            return [{"cloud": cloud, "account": key, "name": "fresh"}]
    provider = TestProvider()
    provider.cloud = cloud
    old = [{"cloud": cloud, "account": "first", "name": "old"}]
    with collection.Session({"storage": old}) as session:
        rows = sync._provider_rows(provider, "storage")
        assert rows == old + [{"cloud": cloud, "account": "second", "name": "fresh"}]
        assert len(session.failures()) == 1
    assert deploy_config.account_scope(cloud) is None


@pytest.mark.parametrize("fallback_ok", [True, False])
def test_azure_billing_fallback_reports_only_total_failure(monkeypatch, fallback_ok):
    provider = azure.AzureProvider()
    monkeypatch.setattr(provider, "_accounts", lambda: [{"subscription": "demo"}])
    def unavailable_profile(*args):
        collection.failed()
        return None, None
    def unavailable(*args):
        collection.failed()
        return None
    monkeypatch.setattr(provider, "_billing_profile", unavailable_profile)
    monkeypatch.setattr(provider, "_cost_query", unavailable)
    monkeypatch.setattr(provider, "_cost_consumption", lambda *args: [("Compute", "2026-01", 1.0, "USD")] if fallback_ok else None)
    old = [{"name": "old"}]
    with collection.Session({}) as session:
        rows = session.collect("azure", "billing", "billing", old, provider.list_billing)
        if fallback_ok:
            assert rows[0]["service"] == "Compute" and rows[0]["account"] == "demo"
            assert not session.failures()
            assert next(iter(session.statuses.values()))["status"] == "ok"
        else:
            assert rows == old and session.failures()


def test_shared_azure_billing_is_collected_once(workspace, monkeypatch):
    monkeypatch.setattr(deploy_config, "_raw", lambda: {"azure": {"accounts": [
        {"subscription": "one"}, {"subscription": "two"}]}})
    calls = []
    rows = [{"cloud": "azure", "account": account, "service": "Compute"} for account in ("one", "two")]
    def scan(self):
        calls.append(1)
        return rows
    monkeypatch.setattr(azure.AzureProvider, "list_billing", scan)
    with collection.Session({}) as session:
        assert sync._provider_rows(azure.AzureProvider(), "billing") == rows
        assert len(calls) == 1
        assert len(session.statuses) == 1
