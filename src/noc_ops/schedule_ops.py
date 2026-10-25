"""计划任务（定时重启）—— 统一用 AWS EventBridge Scheduler，本机零常驻。

单一调度引擎，两种触发目标：
- AWS 实例   : universal 目标直调 `ec2:RebootInstances`（云控制面 API 重启）。
- 腾讯/阿里  : 触发派发 Lambda `noc-reboot-dispatcher`（账号/区见 ops-config.toml 的 [scheduler.dispatch]），
               Lambda 从 Secrets Manager 取该云凭证、调其 RebootInstance API（也是云 API 重启，非 OS 内）。

统一接口：create_reboot() / list_all() / delete_row()，行结构对各云一致（看板/agent 共用）。
时间一律北京时间(UTC+8)。一次性传 ISO；循环传 5 段标准 crontab（分 时 日 月 周）。
"""
import json
import os
import re
import subprocess

from . import deploy_config
from . import paths
from .providers.aws import REGION_META

# 调度账号 / 派发中枢 / 已接入派发的云 / 区域 / 角色 等部署配置见 ops-config.toml（经 deploy_config 读）。
_LAMBDA_TAG = "function:noc-reboot-dispatcher"   # list_all 识别派发 Lambda 调度用

TZ = deploy_config.timezone()            # IANA 时区名（北京时间 UTC+8）
DISPLAY_TZ = deploy_config.display_tz()   # 给人看的显示标签
PREFIX = "noc-"                           # 调度命名前缀（通用惯例）


# ==========================================================================
# 通用
# ==========================================================================
def is_supported(cloud, profile):
    if cloud == "aws":
        return profile in deploy_config.sched_accounts()
    return (cloud, profile) in deploy_config.dispatch_clouds()


def unsupported_reason(cloud, profile):
    if cloud not in ("aws", "tencent", "aliyun"):
        return f"未知 cloud: {cloud}"
    if not is_supported(cloud, profile):
        return f"账号 {profile}（{cloud}）还没接入计划任务支持"
    return ""


def _aws(args, timeout=60):
    try:
        r = subprocess.run(["aws", "scheduler", *args, "--output", "json"], capture_output=True,
                           text=True, timeout=timeout, env={**os.environ, "AWS_PAGER": ""})
    except FileNotFoundError:          # 本机没装 aws CLI：按失败返回，由上层按"无计划任务"处理
        return False, "aws CLI 未安装"
    out = (r.stdout or "") + (("\n" + r.stderr) if r.stderr else "")
    return r.returncode == 0, out.strip()


def _json(ok, out):
    if not ok:
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return None


def _sanitize(s):
    return re.sub(r"[^a-zA-Z0-9-]", "-", s).strip("-").lower() or "x"


def make_name(instance_name, when=None):
    base = f"{PREFIX}reboot-{_sanitize(instance_name)}"
    return f"{base}-{re.sub(r'[^0-9]', '', when)[:12]}" if when else f"{base}-cron"


def _resolve_name(instance_id):
    try:
        for i in json.loads(paths.data_json_path().read_text()).get("instances", []):
            if i.get("id") == instance_id:
                return i.get("name", instance_id)
    except (OSError, json.JSONDecodeError):
        pass
    return instance_id


def _quartz_dom_dow(dom, dow):
    """AWS cron 要求 日 与 周 恰有一个为 ?。"""
    if dom == "*" and dow == "*":
        return "*", "?"
    if dom != "*" and dow == "*":
        return dom, "?"
    if dom == "*" and dow != "*":
        return "?", dow
    return dom, "?"


def _parse_crontab(ct):
    f = ct.split()
    if len(f) != 5:
        raise ValueError(f"循环需 5 段 crontab（分 时 日 月 周），收到：{ct!r}")
    return f


def _expr_and_after(when, crontab):
    """统一算 EventBridge 表达式：一次性 at()（触发后自删）/ 循环 cron()。"""
    if when:
        return f"at({when})", ["--action-after-completion", "DELETE"]
    m, h, dom, mon, dow = _parse_crontab(crontab)
    qdom, qdow = _quartz_dom_dow(dom, dow)
    return f"cron({m} {h} {qdom} {mon} {qdow} *)", []


def _create_schedule(sched_account, sched_region, name, expr, after, target, desc):
    return _aws(["create-schedule", "--name", name, "--schedule-expression", expr,
                 "--schedule-expression-timezone", TZ,
                 "--flexible-time-window", json.dumps({"Mode": "OFF"}),
                 "--target", json.dumps(target), "--description", desc, "--state", "ENABLED",
                 "--profile", sched_account, "--region", sched_region] + after)


# ==========================================================================
# 创建
# ==========================================================================
def create_reboot(cloud, profile, region, instance_id, instance_name, when=None, crontab=None):
    """建一条定时重启。when=一次性 ISO 北京时间；crontab=循环 5 段 crontab（北京时间）。二选一。
    返回 (ok, 调度名 或 错误信息)。"""
    if not is_supported(cloud, profile):
        return False, unsupported_reason(cloud, profile)
    if bool(when) == bool(crontab):
        return False, "when 与 crontab 必须二选一"
    try:
        expr, after = _expr_and_after(when, crontab)
    except ValueError as e:
        return False, str(e)
    name = make_name(instance_name, when)
    if cloud == "aws":
        acct = deploy_config.sched_accounts()[profile]
        target = {"Arn": f"arn:{acct['partition']}:scheduler:::aws-sdk:ec2:rebootInstances",
                  "RoleArn": acct["role"], "Input": json.dumps({"InstanceIds": [instance_id]})}
        ok, out = _create_schedule(profile, region, name, expr, after, target,
                                   f"noc reboot {instance_name} ({instance_id})")
    else:
        target = {"Arn": deploy_config.dispatch()["lambda"], "RoleArn": deploy_config.dispatch()["role"],
                  "Input": json.dumps({"cloud": cloud, "instance_id": instance_id,
                                       "region": region, "account": profile})}
        ok, out = _create_schedule(deploy_config.dispatch()["account"], deploy_config.dispatch()["region"], name, expr, after, target,
                                   f"noc reboot {instance_name} ({instance_id}) via {cloud}")
    return (True, name) if ok else (False, out)


# ==========================================================================
# 列举 / 删除
# ==========================================================================
def _row(det, sched_account, sched_region):
    tgt = det.get("Target", {}) or {}
    arn = tgt.get("Arn", "")
    expr = det.get("ScheduleExpression", "")
    one = expr.startswith("at(")
    when = expr[3:-1] if one else ""
    try:
        inp = json.loads(tgt.get("Input", "{}"))
    except json.JSONDecodeError:
        inp = {}
    if _LAMBDA_TAG in arn:                       # 腾讯/阿里：经 Lambda 派发
        cloud = inp.get("cloud", "?")
        iid = inp.get("instance_id", "")
        ireg = inp.get("region", "")
        account = inp.get("account", "")
        disp, flag = deploy_config.nonaws_region_display().get(ireg, (ireg, "🏳️"))
    else:                                        # AWS：universal 目标直接重启
        cloud = "aws"
        try:
            iid = (json.loads(tgt.get("Input", "{}")).get("InstanceIds") or [""])[0]
        except json.JSONDecodeError:
            iid = ""
        ireg = sched_region
        account = sched_account
        _, disp, flag = REGION_META.get(sched_region, (sched_region, sched_region, "🏳️"))
    m = re.match(r"noc reboot (.+?) \(", det.get("Description", "") or "")
    iname = m.group(1) if m else _resolve_name(iid)
    return {"name": det.get("Name", ""), "cloud": cloud, "account": account, "region": ireg,
            "region_display": disp, "flag": flag, "instance": iname, "instance_id": iid,
            "action": "重启", "kind": "一次性" if one else "循环",
            "expr": when if one else expr, "timezone": TZ,
            "state": det.get("State", ""), "next_run": when if one else "",
            "_handle": det.get("Name", ""), "_sched_account": sched_account, "_sched_region": sched_region}


def list_all():
    """汇总所有 noc 计划任务（AWS 直重启 + 经 Lambda 派发的腾讯/阿里），行结构一致。"""
    rows, seen = [], set()
    for profile, acct in deploy_config.sched_accounts().items():
        for region in acct["regions"]:
            d = _json(*_aws(["list-schedules", "--name-prefix", PREFIX,
                             "--profile", profile, "--region", region]))
            for s in (d or {}).get("Schedules", []):
                key = (profile, region, s["Name"])
                if key in seen:
                    continue
                seen.add(key)
                det = _json(*_aws(["get-schedule", "--name", s["Name"],
                                   "--profile", profile, "--region", region]))
                if det:
                    rows.append(_row(det, profile, region))
    rows.sort(key=lambda r: (r["kind"] != "一次性", r["next_run"] or r["expr"]))
    return rows


def delete_row(row):
    """删一条计划任务（统一是 EventBridge 调度，用调度所在账号/区删）。返回 (ok, out)。"""
    return _aws(["delete-schedule", "--name", row["_handle"],
                 "--profile", row["_sched_account"], "--region", row["_sched_region"]])


if __name__ == "__main__":
    _accts = list(deploy_config.sched_accounts())
    if _accts:
        print("supported aws/first:", is_supported("aws", _accts[0]))
    for _cloud, _acct in sorted(deploy_config.dispatch_clouds()):
        print(f"supported {_cloud}/{_acct}:", is_supported(_cloud, _acct))
    print("当前所有计划任务:", json.dumps(list_all(), ensure_ascii=False, indent=2))
