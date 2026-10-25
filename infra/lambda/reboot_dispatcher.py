"""NOC reboot dispatcher —— EventBridge Scheduler 到点触发，调阿里/腾讯 RebootInstance API。
纯 stdlib 手写签名（阿里 RPC HMAC-SHA1 / 腾讯 TC3-HMAC-SHA256），零第三方依赖。
event: {"cloud":"aliyun|tencent","instance_id":"...","region":"...","dry_run":false}
凭证从 Secrets Manager 的 noc/reboot-dispatcher 读取。"""
import base64, datetime, hashlib, hmac, json, time, urllib.parse, urllib.request, uuid

SECRET_ID = "noc/reboot-dispatcher"
_cache = {}

def _creds():
    if "v" not in _cache:
        import boto3
        sm = boto3.client("secretsmanager")
        _cache["v"] = json.loads(sm.get_secret_value(SecretId=SECRET_ID)["SecretString"])
    return _cache["v"]

def _http(req):
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()

# ---- 阿里 ECS RebootInstance（RPC, 签名 v1 HMAC-SHA1）----
def _pe(s):
    return urllib.parse.quote(str(s), safe="~")

def aliyun_reboot(iid, region, dry_run=False):
    c = _creds()["aliyun"]
    p = {"Format": "JSON", "Version": "2014-05-26", "AccessKeyId": c["access_key_id"],
         "SignatureMethod": "HMAC-SHA1", "SignatureVersion": "1.0", "SignatureNonce": uuid.uuid4().hex,
         "Timestamp": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
         "Action": "RebootInstance", "RegionId": region, "InstanceId": iid}
    if dry_run:
        p["DryRun"] = "true"
    canon = "&".join(f"{_pe(k)}={_pe(p[k])}" for k in sorted(p))
    sts = "GET&" + _pe("/") + "&" + _pe(canon)
    p["Signature"] = base64.b64encode(
        hmac.new((c["access_key_secret"] + "&").encode(), sts.encode(), hashlib.sha1).digest()).decode()
    query = "&".join(f"{_pe(k)}={_pe(v)}" for k, v in p.items())
    return _http(urllib.request.Request(f"https://ecs.{region}.aliyuncs.com/?{query}"))

# ---- 腾讯 RebootInstances（TC3-HMAC-SHA256）----
def _h(key, msg):
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()

def tencent_reboot(iid, region, dry_run=False):
    c = _creds()["tencent"]
    lh = iid.startswith("lhins-")
    service = "lighthouse" if lh else "cvm"
    host = f"{service}.tencentcloudapi.com"
    version = "2020-03-24" if lh else "2017-03-12"
    action = "DescribeInstances" if dry_run else "RebootInstances"
    payload = json.dumps({} if dry_run else {"InstanceIds": [iid]})
    ts = int(time.time())
    date = datetime.datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d")
    ct = "application/json; charset=utf-8"
    cr = "\n".join(["POST", "/", "", f"content-type:{ct}\nhost:{host}\n", "content-type;host",
                    hashlib.sha256(payload.encode()).hexdigest()])
    scope = f"{date}/{service}/tc3_request"
    s2s = "\n".join(["TC3-HMAC-SHA256", str(ts), scope, hashlib.sha256(cr.encode()).hexdigest()])
    k = _h(_h(_h(("TC3" + c["secret_key"]).encode(), date), service), "tc3_request")
    sig = hmac.new(k, s2s.encode(), hashlib.sha256).hexdigest()
    req = urllib.request.Request(f"https://{host}", data=payload.encode(), method="POST")
    for hk, hv in {"Authorization": f"TC3-HMAC-SHA256 Credential={c['secret_id']}/{scope}, "
                   f"SignedHeaders=content-type;host, Signature={sig}", "Content-Type": ct, "Host": host,
                   "X-TC-Action": action, "X-TC-Version": version, "X-TC-Region": region,
                   "X-TC-Timestamp": str(ts)}.items():
        req.add_header(hk, hv)
    return _http(req)

def lambda_handler(event, context):
    cloud, iid, region = event.get("cloud"), event.get("instance_id"), event.get("region")
    dry = bool(event.get("dry_run"))
    fn = {"aliyun": aliyun_reboot, "tencent": tencent_reboot}.get(cloud)
    if not fn:
        raise ValueError(f"unsupported cloud: {cloud}")
    status, body = fn(iid, region, dry)
    print(f"[{'DRY' if dry else 'REBOOT'}] {cloud} {iid}@{region} -> HTTP {status}: {body[:500]}")
    ok = status == 200 or (dry and cloud == "aliyun" and "DryRunOperation" in body)
    if not ok:
        raise RuntimeError(f"{cloud} reboot failed HTTP {status}: {body[:300]}")
    return {"ok": ok, "cloud": cloud, "instance_id": iid, "status": status}
