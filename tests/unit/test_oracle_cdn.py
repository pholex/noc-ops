"""OCI Media Streams CDN inventory and failure preservation; no cloud calls."""
import json
import subprocess

import pytest

from noc_ops import deploy_config, sync
from noc_ops.providers import oracle

pytestmark = pytest.mark.unit
COMP = "ocid1.compartment.oc1..example"
CHANNEL = {"id": "ocid1.streamdistributionchannel.oc1..example", "display-name": "video",
           "domain-name": "video.example.com", "lifecycle-state": "ACTIVE"}
EDGE = {"id": "ocid1.streamcdnconfig.oc1..example", "display-name": "edge",
        "is-enabled": True, "lifecycle-state": "ACTIVE", "time-updated": "2026-01-02T00:00:00Z"}
ACCOUNT = {"profile": "demo", "account": "demo-account", "compartment": COMP,
           "regions": {"ap-tokyo-1": ["Tokyo", "JP"]}}


@pytest.fixture
def environment(monkeypatch, tmp_path):
    deploy_config.reload()
    monkeypatch.setattr(deploy_config, "_raw", lambda: {"oracle": {"accounts": [ACCOUNT]}})
    monkeypatch.setattr(sync, "PROVIDERS", {"oracle": oracle.OracleProvider()})
    monkeypatch.setattr(sync, "NOC_DATA", tmp_path / "data.json")
    monkeypatch.setattr(sync.paths, "ensure_workspace", lambda: None)
    monkeypatch.setattr(sync.shutil, "which", lambda cmd: "/mock/oci")
    sync.NOC_DATA.write_text(json.dumps({"cdn": []}))
    calls = []
    responses = {("stream-distribution-channel", "list"): {"data": {"items": [CHANNEL]}},
                 ("stream-cdn-config", "list"): {"data": {"items": [EDGE]}},
                 ("stream-cdn-config", "get"): {"data": {"config": {"type": "EDGE"}}}}

    def run(cmd, **kwargs):
        calls.append(cmd)
        assert cmd[:2] == ["oci", "media-services"]
        value = responses[tuple(cmd[2:4])]
        if isinstance(value, Exception):
            raise value
        if value is None:
            return subprocess.CompletedProcess(cmd, 1, "", "NotAuthorizedOrNotFound")
        return subprocess.CompletedProcess(cmd, 0, json.dumps(value), "")

    monkeypatch.setattr(subprocess, "run", run)
    yield responses, calls
    deploy_config.reload()


def test_edge_rows_and_scoped_commands(environment):
    responses, calls = environment
    row, = oracle.OracleProvider().list_cdn()
    assert row["domain"] == row["origin"] == "video.example.com"
    assert row["service"] == "OCI Edge Stream CDN"
    assert row["distribution_channel_id"] == CHANNEL["id"]
    assert row["enabled"] is True and row["status"] == "active"
    assert row["account"] == "demo-account" and row["region_display"] == "Tokyo"
    assert row["last_modified"] == "2026-01-02"
    assert calls[0][4:7] == ["--compartment-id", COMP, "--all"]
    assert calls[1][4:7] == ["--distribution-channel-id", CHANNEL["id"], "--all"]
    assert calls[2][4:6] == ["--stream-cdn-config-id", EDGE["id"]]
    assert all(cmd[-4:] == ["--profile", "demo", "--region", "ap-tokyo-1"] for cmd in calls)
    assert all(oracle.OracleProvider.is_read(" ".join(cmd[1:])) for cmd in calls)


def test_akamai_whitelist_and_unknown_enabled(environment):
    responses, _ = environment
    responses[("stream-cdn-config", "list")] = {"data": {"items": [{**EDGE, "is-enabled": None}]}}
    responses[("stream-cdn-config", "get")] = {"data": {"config": {
        "type": "AKAMAI_MANUAL", "edge-hostname": "cdn.example.com",
        "edge-token-key": "fixture-secret", "origin-auth-secret-key-a": "fixture-secret"}}}
    row, = oracle.OracleProvider().list_cdn()
    assert row["domain"] == "cdn.example.com" and row["enabled"] is None
    assert row["service"] == "Media Streams / Akamai"
    assert "fixture-secret" not in json.dumps(row)


@pytest.mark.parametrize("response", [{}, {"opc-total-items": "0"}, {"data": {"items": []}}])
def test_empty_channels(environment, response):
    responses, calls = environment
    responses[("stream-distribution-channel", "list")] = response
    assert oracle.OracleProvider().list_cdn() == []
    assert len(calls) == 1


@pytest.mark.parametrize("failure", [None, FileNotFoundError("oci"), {"data": []},
                                     {"data": {"items": [{}]}}])
@pytest.mark.parametrize("resource", ["stream-distribution-channel", "stream-cdn-config"])
def test_list_failure_preserves_old_account_rows(environment, failure, resource):
    responses, _ = environment
    old = {"id": "old", "cloud": "oracle", "account": "demo-account"}
    other = {"id": "other", "cloud": "oracle", "account": "another-account"}
    sync.NOC_DATA.write_text(json.dumps({"cdn": [old, other]}))
    responses[(resource, "list")] = failure
    assert sync.refresh_account("demo", "cdn") == 0
    data = json.loads(sync.NOC_DATA.read_text())
    assert sorted(r["id"] for r in data["cdn"]) == ["old", "other"]
    assert data["collection_status"][0]["status"] == "error"
    assert data["collection_status"][0]["preserved"] is True
    responses[(resource, "list")] = {"data": {"items": []}}
    assert sync.refresh_account("demo-account", "cdn") == 0
    assert json.loads(sync.NOC_DATA.read_text())["cdn"] == [other]


def test_detail_failure_keeps_summary_and_marks_partial(environment):
    responses, _ = environment
    responses[("stream-cdn-config", "get")] = None
    assert sync.refresh_kind("cdn") == 0
    data = json.loads(sync.NOC_DATA.read_text())
    row, = data["cdn"]
    assert row["id"] == EDGE["id"] and row["enabled"] is True
    assert row["service"] == "OCI Media Streams CDN" and row["domain"] == ""
    assert data["collection_status"][0]["status"] == "partial"


def test_multiple_accounts_regions_and_default_region(environment, monkeypatch):
    _, calls = environment
    second = {**ACCOUNT, "profile": "other", "account": "other", "regions": {}}
    first = {**ACCOUNT, "regions": {**ACCOUNT["regions"], "us-ashburn-1": ["Ashburn", "US"]}}
    monkeypatch.setattr(deploy_config, "_raw", lambda: {"oracle": {"accounts": [first, second]}})
    rows = oracle.OracleProvider().list_cdn()
    assert {(r["account"], r["region"]) for r in rows} == {
        ("demo-account", "ap-tokyo-1"), ("demo-account", "us-ashburn-1"), ("other", "")}
    assert all("--region" not in cmd for cmd in calls[-3:])


def test_deleted_resources_and_missing_compartment(environment, monkeypatch):
    responses, calls = environment
    responses[("stream-cdn-config", "list")] = {"data": {"items": [{**EDGE, "lifecycle-state": "DELETED"}]}}
    assert oracle.OracleProvider().list_cdn() == []
    assert len(calls) == 2
    calls.clear()
    responses[("stream-distribution-channel", "list")] = {"data": {"items": [{**CHANNEL, "lifecycle-state": "DELETED"}]}}
    assert oracle.OracleProvider().list_cdn() == [] and len(calls) == 1
    calls.clear()
    monkeypatch.setattr(deploy_config, "_raw", lambda: {"oracle": {"accounts": [{"profile": "demo"}]}})
    assert oracle.OracleProvider().list_cdn() == [] and not calls
