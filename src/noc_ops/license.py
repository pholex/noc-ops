"""ops license —— Polar 订阅授权 gate（离线友好，零运行期第三方依赖，纯 stdlib）。

校验走 Polar **公开** 的 customer-portal 端点：只需 license key + organization_id，**无需任何 secret**，
可安全放进分发的 CLI（Polar 文档明确说该端点 "can be safely used on a public client"）。
订阅生命周期由 Polar 托管：用户停订 → Polar 吊销/失效 key → 校验返回非 granted → gate 拦下。
**我们这边不需要 webhook 服务器。**

离线友好：缓存最近一次成功校验，断网时给 GRACE_DAYS 宽限期（对中国区/离线友好）。

⚠️ 安全红线：此文件**绝不**放 client secret / access token —— 那是能管理整个 org 的服务端凭证，
一旦进客户端就能被扒出来伪造/吊销 license、读客户数据。这里只放 organization_id / product_id（非密标识）。
"""
import base64
import hashlib
import hmac
import json
import os
import socket
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

POLAR_BASE = "https://api.polar.sh"
# 本机 dev 旁路：设此环境变量（任意非空值）即放行 gate，全程不碰 Polar。
# 仅作者自用机器用——分发给客户的客户端不会设它，故不削弱对外授权。
_DEV_ENV = "NOC_LICENSE_DEV"
# —— 非密配置（随客户端分发 OK）——
ORGANIZATION_ID = "6285ba4a-b77d-487b-a354-375a2dd55208"   # 非密，可随客户端分发
PRODUCT_ID = "83b789e8-786f-4d2a-a1bf-15898cc52a03"   # 仅作归属展示，非密
GRACE_DAYS = 14        # 断网宽限：最近一次成功校验后 N 天内仍放行
RECHECK_HOURS = 24     # 在线最多每 N 小时校验一次（少打扰）
_TIMEOUT = 8

# —— 离线自签授权（与 Polar 并存，作者自己当签发方，零联网）——
# 作者用私钥签发 token，客户端只内嵌**公钥** (N, E) 离线验签（RSA-2048 PKCS#1v1.5 / SHA-256，
# 纯 stdlib：pow + hashlib）。公钥非密、随客户端分发 OK；私钥只在作者机器 Keychain，绝不进代码。
# 用法：`ops license keygen`（一次性生成密钥对，把打印的 N/E 粘到这里）→ `ops license issue <名>`
# 签发 → 用户 `ops license activate <token>` 安装。校验优先级：dev 旁路 > 离线自签 > Polar。
_OFFLINE_PREFIX = "NOCK1"            # 自签 token 前缀，一眼区分于 Polar license key
_OFFLINE_ENV = "NOC_LICENSE_KEY"    # 也可用环境变量直接提供 token（CI / 无状态服务器友好）
_SIGNING_KEYCHAIN = ("noc-license-signing", "author")   # (service, account)
SIGNING_PUBKEY_E = 65537
SIGNING_PUBKEY_N = 0                 # 2048-bit 模数：keygen 后填入（0 = 离线自签未启用）
# SHA-256 的 EMSA-PKCS1-v1_5 DigestInfo DER 前缀（RFC 8017）
_SHA256_DIGESTINFO = bytes.fromhex("3031300d060960864801650304020105000420")


def _configured() -> bool:
    return bool(ORGANIZATION_ID) and "填" not in ORGANIZATION_ID and "<" not in ORGANIZATION_ID


def _license_file() -> Path:
    return Path.home() / ".config" / "noc-ops" / "license.json"


def _now():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(s):
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            d = datetime.strptime(s, fmt)
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue
    return None


def _load() -> dict:
    try:
        return json.loads(_license_file().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save(rec: dict):
    p = _license_file()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")


def _post(path: str, payload: dict):
    """POST JSON 到 Polar 公开端点；返回解析后的 dict。失败抛 urllib 异常。"""
    req = urllib.request.Request(
        POLAR_BASE + path, data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json",
                 "User-Agent": "noc-ops"})   # 默认 Python-urllib UA 会被 Polar 的 Cloudflare 挡(1010)
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8"))


# ────────────────────────── 离线自签授权 ──────────────────────────
def _b64u_encode(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _b64u_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _offline_configured() -> bool:
    return SIGNING_PUBKEY_N > 0


def _rsa_verify(message: bytes, sig: bytes, n: int, e: int) -> bool:
    """RSASSA-PKCS1-v1_5 / SHA-256 验签，纯 stdlib（pow + hashlib），恒定时间比较。"""
    if n <= 0:
        return False
    k = (n.bit_length() + 7) // 8
    if len(sig) != k:
        return False
    em = pow(int.from_bytes(sig, "big"), e, n).to_bytes(k, "big")
    t = _SHA256_DIGESTINFO + hashlib.sha256(message).digest()
    ps_len = k - len(t) - 3
    if ps_len < 8:                       # PS 至少 8 字节 0xFF（RFC 8017）
        return False
    expected = b"\x00\x01" + b"\xff" * ps_len + b"\x00" + t
    return hmac.compare_digest(em, expected)


def _verify_offline(token: str):
    """校验自签 token。返回 (True, payload_dict) 或 (False, 原因字符串)。"""
    if not token or not token.startswith(_OFFLINE_PREFIX + "."):
        return False, "非自签 key"
    if not _offline_configured():
        return False, "客户端未内嵌签发公钥（离线自签未启用）"
    try:
        _, body_b64, sig_b64 = token.split(".", 2)
        sig = _b64u_decode(sig_b64)
        payload = json.loads(_b64u_decode(body_b64))
    except Exception:
        return False, "自签 key 格式错误"
    # 验签对象 = "前缀.payload段" 的 ascii 字节：前缀入签防跨产品重放，payload 入签防篡改
    signed = f"{_OFFLINE_PREFIX}.{body_b64}".encode("ascii")
    if not _rsa_verify(signed, sig, SIGNING_PUBKEY_N, SIGNING_PUBKEY_E):
        return False, "自签 key 签名无效"
    exp = payload.get("exp")
    if exp:
        d = _parse(exp)
        if d and d < _now():
            return False, f"自签 key 已过期（{exp}）"
    return True, payload


def install_offline(token: str):
    """校验并把自签 token 存到本机 license.json。返回 (ok, msg)。"""
    ok, res = _verify_offline(token)
    if not ok:
        return False, f"安装失败：{res}"
    _save({**_load(), "offline_token": token})
    return True, f"✅ 已安装离线授权（{res.get('sub', '?')}，到期 {res.get('exp') or '永久'}）"


def keygen():
    """一次性：生成 RSA-2048 签发密钥对。私钥存 Keychain，公钥 (N,E) 打印出来粘进本文件。"""
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
    except ImportError:
        return False, "签发需要 cryptography：pip install cryptography（仅作者机器需要，不影响客户端验签）"
    import subprocess
    key = rsa.generate_private_key(public_exponent=SIGNING_PUBKEY_E, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM,
                            serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption())
    b64 = base64.b64encode(pem).decode()      # PEM 多行 → base64 单行存 Keychain
    subprocess.run(["security", "add-generic-password", "-U",
                    "-s", _SIGNING_KEYCHAIN[0], "-a", _SIGNING_KEYCHAIN[1], "-w", b64], check=True)
    n = key.public_key().public_numbers().n
    print("✅ 私钥已存入 Keychain。把下面这行的 N 粘进 src/noc_ops/license.py 的 SIGNING_PUBKEY_N：\n")
    print(f"SIGNING_PUBKEY_N = {n}\n")
    return True, "keygen 完成（公钥已打印；私钥仅存于本机 Keychain，绝不入库）"


def _load_signing_key():
    """从 Keychain 读作者签发私钥（cryptography 私钥对象）；无则 None。"""
    try:
        from cryptography.hazmat.primitives import serialization
    except ImportError:
        return None
    import subprocess
    try:
        r = subprocess.run(["security", "find-generic-password",
                            "-s", _SIGNING_KEYCHAIN[0], "-a", _SIGNING_KEYCHAIN[1], "-w"],
                           capture_output=True, text=True, check=True)
    except Exception:
        return None
    return serialization.load_pem_private_key(base64.b64decode(r.stdout.strip()), password=None)


def issue(sub: str, days: int | None = None, tier: str = "pro"):
    """作者签发一个离线 key（返回 token 字符串）。私钥从 Keychain 读。返回 (ok, token_or_msg)。"""
    try:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
    except ImportError:
        return False, "签发需要 cryptography：pip install cryptography"
    priv = _load_signing_key()
    if priv is None:
        return False, "Keychain 里没有签发私钥——先 `ops license keygen`"
    payload = {"sub": sub, "tier": tier, "iat": _iso(_now())}
    if days:
        payload["exp"] = _iso(_now() + timedelta(days=days))
    body_b64 = _b64u_encode(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8"))
    sig = priv.sign(f"{_OFFLINE_PREFIX}.{body_b64}".encode("ascii"),
                    padding.PKCS1v15(), hashes.SHA256())
    return True, f"{_OFFLINE_PREFIX}.{body_b64}.{_b64u_encode(sig)}"


def activate(key: str):
    """把 license key 激活到本机。自签 token（NOCK1.…）走离线安装，否则走 Polar 激活。"""
    if key.startswith(_OFFLINE_PREFIX + "."):
        return install_offline(key)
    if not _configured():
        return False, "license 未配置（ORGANIZATION_ID 占位）——开发模式无需激活。"
    try:
        body = _post("/v1/customer-portal/license-keys/activate",
                     {"key": key, "organization_id": ORGANIZATION_ID, "label": socket.gethostname()})
    except urllib.error.HTTPError as e:
        return False, {403: "激活失败：席位已满（先在其它机器停用）", 404: "激活失败：license key 不存在"
                       }.get(e.code, f"激活失败：HTTP {e.code}")
    except (urllib.error.URLError, socket.timeout, OSError) as e:
        return False, f"联网失败，无法激活：{e}"
    lic = body.get("license_key", {}) or {}
    _save({"key": key, "activation_id": body.get("id"), "status": lic.get("status"),
           "expires_at": lic.get("expires_at"), "last_ok": _iso(_now())})
    return True, "✅ 已激活到本机"


def check(force: bool = False):
    """授权 gate。返回 (ok, reason)。
    - 本机 dev 旁路（NOC_LICENSE_DEV 环境变量）→ 直接放行；
    - 离线自签 key（NOCK1.…，环境变量或 license.json）有效 → 直接放行，不触网；
    - 未配置（开发模式）→ 直接放行；
    - 在线 → 校验 status=granted 且未过期，刷新缓存；
    - 断网/5xx → 落到本地宽限期（GRACE_DAYS）。"""
    if os.environ.get(_DEV_ENV):
        return True, f"本机 dev 旁路（{_DEV_ENV}）放行"
    # 离线自签授权（与 Polar 并存）：有有效自签 key 直接放行、零联网；无效则继续尝试 Polar
    tok = os.environ.get(_OFFLINE_ENV) or _load().get("offline_token")
    if tok and tok.startswith(_OFFLINE_PREFIX + "."):
        ok, res = _verify_offline(tok)
        if ok:
            return True, f"离线自签授权（{res.get('sub', '?')}，到期 {res.get('exp') or '永久'}）"
    if not _configured():
        return True, "license gate 未配置（开发模式放行）"
    rec = _load()
    if not rec.get("key"):
        return False, "未激活：运行 `ops license activate <你的key>`"
    last = _parse(rec.get("last_ok"))
    need = force or last is None or (_now() - last) > timedelta(hours=RECHECK_HOURS)
    if need:
        payload = {"key": rec["key"], "organization_id": ORGANIZATION_ID}
        if rec.get("activation_id"):
            payload["activation_id"] = rec["activation_id"]
        body = None
        try:
            body = _post("/v1/customer-portal/license-keys/validate", payload)
        except urllib.error.HTTPError as e:
            if e.code in (403, 404):
                return False, "license 无效或未授权——请重新激活 / 续订"
            # 5xx 等 → 落宽限
        except (urllib.error.URLError, socket.timeout, OSError):
            pass  # 断网 → 落宽限
        if body is not None:
            status, exp = body.get("status"), body.get("expires_at")
            if status != "granted":
                _save({**rec, "status": status})
                return False, f"订阅无效（status={status}）——请在 Polar 续订"
            if exp and _parse(exp) and _parse(exp) < _now():
                return False, "license 已过期——请续订"
            _save({**rec, "status": status, "expires_at": exp, "last_ok": _iso(_now())})
            return True, "ok"
    # 离线 / 校验未完成 → 宽限期
    if last and (_now() - last) <= timedelta(days=GRACE_DAYS):
        return True, f"离线宽限中（剩 ~{GRACE_DAYS - (_now() - last).days} 天，请尽快联网校验）"
    return False, "离线超过宽限期——请联网后重试以续期授权"


def status_text() -> str:
    """`ops license status` 的可读输出。"""
    if os.environ.get(_DEV_ENV):
        return f"✅ 本机 dev 旁路开启（环境变量 {_DEV_ENV}）——gate 放行，未走 Polar 校验。"
    tok = os.environ.get(_OFFLINE_ENV) or _load().get("offline_token")
    if tok and tok.startswith(_OFFLINE_PREFIX + "."):
        ok, res = _verify_offline(tok)
        if ok:
            return (f"✅ 离线自签授权：{res.get('sub', '?')}  tier={res.get('tier', '?')}  "
                    f"到期={res.get('exp') or '永久'}（零联网、不走 Polar）")
        return f"❌ 离线自签 key 无效：{res}（将回落 Polar 校验）"
    if not _configured():
        return "license gate 未配置（开发模式）。填好 license.ORGANIZATION_ID 后生效。"
    rec = _load()
    if not rec.get("key"):
        return "未激活。运行 `ops license activate <你的key>` 激活。"
    ok, reason = check()
    masked = rec["key"][:8] + "…" if rec.get("key") else "—"
    return (f"key={masked}  status={rec.get('status','?')}  到期={rec.get('expires_at') or '无'}\n"
            f"最近校验={rec.get('last_ok','?')}  →  {'✅ ' if ok else '❌ '}{reason}")
