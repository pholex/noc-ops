"""DSM 错误 119（SID not found）：清缓存、重登一次后重试；其他错误照常抛。"""
import pytest

from noc_ops.providers import synology as syn

pytestmark = pytest.mark.unit


def _setup(monkeypatch, responses):
    calls = {"logins": 0, "sids": []}
    syn._SESSION_CACHE["u:x"] = ("old", 9e12)

    def fake_get(url):
        if "auth.cgi" in url:
            calls["logins"] += 1
            return {"success": True, "data": {"sid": f"new{calls['logins']}"}}
        calls["sids"].append(url.split("_sid=")[1].split("&")[0])
        return responses.pop(0)
    monkeypatch.setattr(syn, "_get", fake_get)
    monkeypatch.setattr(syn, "_keychain_pw", lambda s, a: "pw")
    return calls


def test_119_relogins_once_and_retries(monkeypatch):
    calls = _setup(monkeypatch, [{"success": False, "error": {"code": 119}},
                                 {"success": True, "data": {"model": "m"}}])
    data, sid = syn._api_relogin("u", "x", "svc", "acct", "old", "SYNO.Core.System", method="info")
    assert data == {"model": "m"} and sid == "new1"
    assert calls["logins"] == 1 and calls["sids"] == ["old", "new1"]


def test_other_error_not_retried(monkeypatch):
    calls = _setup(monkeypatch, [{"success": False, "error": {"code": 105}}])
    with pytest.raises(syn.SynologyApiError):
        syn._api_relogin("u", "x", "svc", "acct", "old", "SYNO.Core.System", method="info")
    assert calls["logins"] == 0


def test_119_twice_raises(monkeypatch):
    _setup(monkeypatch, [{"success": False, "error": {"code": 119}}] * 2)
    with pytest.raises(syn.SynologyApiError):
        syn._api_relogin("u", "x", "svc", "acct", "old", "SYNO.Core.System", method="info")
