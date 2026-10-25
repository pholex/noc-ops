"""Tier 1：离线自签授权（与 Polar 并存）。

客户端验签是纯 stdlib（pow + hashlib）；测试用 cryptography 造一对临时 RSA 密钥来
签发 token（等价于作者机器 `ops license issue`），验证客户端能正确接受/拒绝。
无 cryptography 的环境 skip（客户端验签本身不依赖它，只有"签发"依赖）。
"""
import json
from datetime import datetime, timedelta, timezone

import pytest

from noc_ops import license as lic

pytestmark = pytest.mark.unit

crypto = pytest.importorskip("cryptography")
from cryptography.hazmat.primitives import hashes, serialization           # noqa: E402
from cryptography.hazmat.primitives.asymmetric import padding, rsa         # noqa: E402


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def signer(monkeypatch):
    """临时 RSA 密钥对：公钥塞进 license 模块，返回一个 sign(payload)->token 的签发器。"""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    monkeypatch.setattr(lic, "SIGNING_PUBKEY_N", key.public_key().public_numbers().n)
    monkeypatch.setattr(lic, "SIGNING_PUBKEY_E", 65537)

    def make(payload: dict) -> str:
        body = lic._b64u_encode(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode())
        sig = key.sign(f"{lic._OFFLINE_PREFIX}.{body}".encode("ascii"),
                       padding.PKCS1v15(), hashes.SHA256())
        return f"{lic._OFFLINE_PREFIX}.{body}.{lic._b64u_encode(sig)}"

    return make


def test_valid_token_accepted(signer):
    tok = signer({"sub": "acme", "tier": "pro", "exp": _iso(datetime.now(timezone.utc) + timedelta(days=30))})
    ok, payload = lic._verify_offline(tok)
    assert ok and payload["sub"] == "acme"


def test_no_expiry_is_perpetual(signer):
    ok, payload = lic._verify_offline(signer({"sub": "self", "tier": "pro"}))
    assert ok and "exp" not in payload


def test_expired_rejected(signer):
    tok = signer({"sub": "x", "exp": _iso(datetime.now(timezone.utc) - timedelta(days=1))})
    ok, reason = lic._verify_offline(tok)
    assert not ok and "过期" in reason


def test_tampered_payload_rejected(signer):
    tok = signer({"sub": "cheap", "tier": "free"})
    prefix, body, sig = tok.split(".", 2)
    forged_body = lic._b64u_encode(json.dumps({"sub": "cheap", "tier": "enterprise"}, sort_keys=True).encode())
    ok, reason = lic._verify_offline(f"{prefix}.{forged_body}.{sig}")
    assert not ok and "签名无效" in reason


def test_wrong_prefix_rejected(signer):
    ok, reason = lic._verify_offline("POLARKEY-1234-5678")
    assert not ok and "非自签" in reason


def test_impersonation_wrong_key_rejected(signer, monkeypatch):
    """安全核心：用**另一把**私钥签的合法结构 token 必须被拒（否则谁都能自签）。"""
    evil = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    body = lic._b64u_encode(json.dumps({"sub": "pirate", "tier": "enterprise"}, sort_keys=True).encode())
    sig = evil.sign(f"{lic._OFFLINE_PREFIX}.{body}".encode("ascii"), padding.PKCS1v15(), hashes.SHA256())
    forged = f"{lic._OFFLINE_PREFIX}.{body}.{lic._b64u_encode(sig)}"
    ok, reason = lic._verify_offline(forged)          # signer fixture 已把"真"公钥塞进模块
    assert not ok and "签名无效" in reason


def test_not_configured_rejects(monkeypatch, signer):
    # 未内嵌公钥（N=0）时，自签校验一律拒绝
    tok = signer({"sub": "x"})
    monkeypatch.setattr(lic, "SIGNING_PUBKEY_N", 0)
    ok, reason = lic._verify_offline(tok)
    assert not ok and "未启用" in reason


def test_check_passes_offline_without_touching_polar(signer, monkeypatch, tmp_path):
    """有有效自签 key → check() 放行且**绝不**调 Polar（_post 抛错也无所谓）。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv(lic._DEV_ENV, raising=False)
    monkeypatch.setattr(lic, "ORGANIZATION_ID", "org_test")

    def boom(*a, **k):
        raise AssertionError("check() 不应触网——离线 key 有效时不该调 Polar")

    monkeypatch.setattr(lic, "_post", boom)
    tok = signer({"sub": "offline-user", "tier": "pro"})
    lic._save({"offline_token": tok})
    ok, reason = lic.check()
    assert ok and "离线自签授权" in reason and "offline-user" in reason


def test_check_falls_through_to_polar_when_offline_invalid(signer, monkeypatch, tmp_path):
    """自签 key 无效（过期）→ 回落 Polar 逻辑（这里 Polar 也没 key → 未激活拦下）。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv(lic._DEV_ENV, raising=False)
    monkeypatch.setattr(lic, "ORGANIZATION_ID", "org_test")
    expired = signer({"sub": "x", "exp": _iso(datetime.now(timezone.utc) - timedelta(days=1))})
    lic._save({"offline_token": expired})
    ok, reason = lic.check()
    assert not ok and "未激活" in reason


def test_env_var_token_supported(signer, monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv(lic._DEV_ENV, raising=False)
    monkeypatch.setattr(lic, "ORGANIZATION_ID", "org_test")
    monkeypatch.setenv(lic._OFFLINE_ENV, signer({"sub": "ci-runner", "tier": "pro"}))
    ok, reason = lic.check()
    assert ok and "ci-runner" in reason


def test_rsa_verify_rejects_garbage(signer):
    # 随机 sig 不应通过（k 长度对但内容错）
    tok = signer({"sub": "x"})
    prefix, body, _ = tok.split(".", 2)
    import os
    k = (lic.SIGNING_PUBKEY_N.bit_length() + 7) // 8
    fake_sig = lic._b64u_encode(os.urandom(k))
    ok, _ = lic._verify_offline(f"{prefix}.{body}.{fake_sig}")
    assert not ok


def test_issue_roundtrip_via_monkeypatched_signer(signer, monkeypatch):
    """模拟 ops license issue：monkeypatch _load_signing_key 返回临时私钥，issue 出的 token 能被验证。"""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    monkeypatch.setattr(lic, "SIGNING_PUBKEY_N", key.public_key().public_numbers().n)
    monkeypatch.setattr(lic, "_load_signing_key", lambda: key)
    ok, tok = lic.issue("roundtrip-user", days=7, tier="team")
    assert ok and tok.startswith(lic._OFFLINE_PREFIX + ".")
    vok, payload = lic._verify_offline(tok)
    assert vok and payload["sub"] == "roundtrip-user" and payload["tier"] == "team" and "exp" in payload
