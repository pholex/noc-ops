"""GcpProvider.list_apikeys：Cloud API Keys 元数据盘点（_gcloud 全部打桩，绝不碰真 gcloud）。"""
import pytest

from noc_ops import sync
from noc_ops.providers import PROVIDERS, gcp

pytestmark = pytest.mark.unit

ACCOUNTS = [{"project": "proj-a", "configuration": "c"},
            {"project": "proj-b", "configuration": "c"}]

# 照搬 `gcloud services api-keys list --format json` 的真实形状（2026-10-24 实测）
VERTEX = {
    "createTime": "2026-10-24T03:05:11.828561Z", "updateTime": "2026-10-24T03:05:11.897049Z",
    "displayName": "API key 1", "etag": 'W/"x"',
    "name": "projects/777788889999/locations/global/keys/00000000-0000-0000-0000-000000000001",
    "uid": "00000000-0000-0000-0000-000000000001",
    "restrictions": {"apiTargets": [{"service": "aiplatform.googleapis.com"}]},
    "serviceAccountEmail": "vertex-express@proj-a.iam.gserviceaccount.com",
}
GEMINI_IP = {
    "createTime": "2025-01-10T00:00:00Z", "displayName": "gemini-ip", "uid": "u-gemini",
    "name": "projects/1/locations/global/keys/u-gemini",
    "restrictions": {"apiTargets": [{"service": "generativelanguage.googleapis.com"}],
                     "serverKeyRestrictions": {"allowedIps": ["1.2.3.4"]}},
}
OPEN = {   # 旧式无限制标准 key：没 restrictions、没 SA
    "createTime": "2024-03-01T00:00:00Z", "displayName": "old key", "uid": "u-open",
    "name": "projects/1/locations/global/keys/u-open",
}
MAPS_ONLY = {   # 非 AI 用途：不进 AI 密钥 tab
    "createTime": "2026-02-01T00:00:00Z", "displayName": "maps-web", "uid": "u-maps",
    "name": "projects/1/locations/global/keys/u-maps",
    "restrictions": {"apiTargets": [{"service": "maps-backend.googleapis.com"}],
                     "browserKeyRestrictions": {"allowedReferrers": ["https://x.example/*"]}},
}
MULTI = {
    "createTime": "2026-06-01T00:00:00Z", "displayName": "maps+gemini", "uid": "u-multi",
    "name": "projects/1/locations/global/keys/u-multi",
    "restrictions": {"apiTargets": [{"service": "maps-backend.googleapis.com"},
                                    {"service": "generativelanguage.googleapis.com", "methods": ["*"]}]},
}


def _setup(monkeypatch, by_project):
    calls = []

    def run(account, args, timeout=60):
        calls.append((account["project"], " ".join(args[1:4])))
        return by_project.get(account["project"])
    monkeypatch.setattr(gcp, "_gcloud", run)
    monkeypatch.setattr(type(PROVIDERS["gcp"]), "_accounts", staticmethod(lambda: ACCOUNTS))
    return calls


def test_rows_classified_and_metadata_only(monkeypatch):
    calls = _setup(monkeypatch, {"proj-a": [VERTEX, OPEN, GEMINI_IP, MAPS_ONLY], "proj-b": [MULTI]})
    rows = PROVIDERS["gcp"].list_apikeys()
    # 按 账号→名称 排序
    assert [(r["account"], r["name"]) for r in rows] == [
        ("proj-a", "API key 1"), ("proj-a", "gemini-ip"), ("proj-a", "old key"), ("proj-b", "maps+gemini")]
    by = {r["name"]: r for r in rows}

    v = by["API key 1"]
    assert v["cloud"] == "gcp" and v["id"] == "00000000-0000-0000-0000-000000000001"
    assert v["purpose"] == "Vertex AI" and v["targets"] == ["aiplatform.googleapis.com"]
    assert v["unrestricted"] is False and v["key_type"] == "授权 key"
    assert v["service_account"] == "vertex-express@proj-a.iam.gserviceaccount.com"
    assert v["client_restriction"] == "" and v["created"] == "2026-10-24" and v["updated"] == "2026-10-24"
    assert v["status"] == "Active" and v["expires"] is None
    assert isinstance(v["age_days"], int) and v["age_days"] >= 0

    g = by["gemini-ip"]
    assert g["purpose"] == "Gemini API" and g["client_restriction"] == "IP"
    assert g["key_type"] == "标准 key" and g["service_account"] is None

    o = by["old key"]
    assert o["unrestricted"] is True and o["purpose"] == "无限制" and o["age_days"] > 365

    m = by["maps+gemini"]
    assert m["purpose"] == "maps-backend / Gemini API"     # 未知服务用短名，已知的用用途名
    assert "maps-web" not in by                            # 只限定非 AI 服务的 key 被排除；无限制的保留

    # 只盘点元数据：行里绝不出现密钥串字段，也绝不调 get-key-string
    for r in rows:
        assert "keyString" not in r and "key_string" not in r
    assert all(k == "services api-keys list" for _, k in calls)
    assert [p for p, _ in calls] == ["proj-a", "proj-b"]


def test_failure_or_empty_yields_no_rows(monkeypatch):
    """API 未启用 / 缺权限（_gcloud 返回 None）或空列表 → 该项目无行，不报错、不影响其它项目。"""
    _setup(monkeypatch, {"proj-a": None, "proj-b": []})
    assert PROVIDERS["gcp"].list_apikeys() == []


def test_sync_wiring(monkeypatch):
    """apikeys 已接进 _collect_kind 与按 tab 单刷清单（漏一处 = 看板有、agent 不知道）。"""
    monkeypatch.setattr(sync.shutil, "which", lambda cmd: "/mock/bin/" + cmd)
    _setup(monkeypatch, {"proj-a": [VERTEX], "proj-b": None})
    assert "apikeys" in sync.SINGLE_REFRESH_KINDS
    rows = sync._collect_kind("apikeys", clouds={"gcp"})
    assert [r["name"] for r in rows] == ["API key 1"]
