#!/usr/bin/env python3
"""ops MCP server —— 把多云运维能力暴露给任意 MCP 客户端（Codex Desktop / Claude Desktop / Cursor …）。

结构化工具（客户端的 LLM 负责编排，本 server 负责执行 + 安全）：
- list_instances / get_instance / instance_status : 查实例（读工作区 data.json 或实时）
- query_domains                                   : 查域名（含到期/剩余天数）
- query_databases / query_storage / query_serverless / query_cdn / query_iam / query_apikeys / query_messaging / query_billing : 查数据库/存储/函数/CDN/IAM/AI 密钥/消息事件/成本（读快照）
- reboot_instance                                 : 重启实例（自动按云选 aws/tccli/aliyun/ve/gcloud/az/oci；低风险，直接执行；GCP=reset 硬重启、Azure=vm restart 优雅重启、Oracle=instance action SOFTRESET 优雅重启）
- schedule_task / list_scheduled_tasks / cancel_scheduled_task : 定时重启计划任务（AWS EventBridge Scheduler，云端执行）
- run_ssh                                         : 经 SSH 在实例上跑命令（用 ssh config 别名）
- resolve_proxy_upstream                          : 解析某台 v2ray/xray 机器的上游 IP → 归属实例
- sync                                            : 全量同步（重扫实例 + 域名，刷新 data.json）

安全：读 + 重启 直接执行；其余写操作不在此暴露。客户端（Codex 等）对每次工具调用还有自己的审批弹窗。
传输：stdio（被 MCP 客户端以子进程方式拉起）。
"""
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

from . import ops_log
from . import paths
from . import schedule_ops
try:
    from mcp.server.mcpserver import MCPServer
except ImportError:                       # mcp 是可选 extra（仅无 shell 的 GUI 客户端需要 ops-mcp）
    import sys as _sys
    print("ops-mcp 需要可选依赖 mcp。安装：uv tool install --force 'noc-ops[mcp]'", file=_sys.stderr)
    raise SystemExit(2)
from .aws_safety import is_read_only_ssh
from .error_patterns import annotate
from .providers import PROVIDERS

# PATH 加固：GUI 客户端（如 Codex Desktop）拉起本 server 时子进程 PATH 可能很简陋，
# 显式补上各 CLI 所在目录（aws=/usr/local/bin、aliyun/sshpass=/opt/homebrew/bin、
# tccli 与 python 同级 bin、ssh=/usr/bin），确保都能找到。
_PY_BIN = str(Path(sys.executable).parent)
_EXTRA_PATHS = ["/usr/local/bin", "/opt/homebrew/bin", _PY_BIN, "/usr/bin", "/bin"]
os.environ["PATH"] = os.pathsep.join(_EXTRA_PATHS + [os.environ.get("PATH", "")])

DATA = paths.data_json_path()
RUNBOOKS = paths.runbooks_dir()

# 给 MCP 客户端（Codex 等）的行为指引——把 ops 的运维约定传过去，使其与 CLI agent / skill 一致。
_INSTRUCTIONS = """\
ops —— 多云运维（AWS / 阿里云 / 腾讯云 / Google Cloud / Microsoft Azure / Oracle Cloud / Cloudflare / Supabase / 群晖 Synology + 本地自建机器 VMware ESXi / 深信服 Sangfor）。用本 server 的结构化工具完成运维，遵守以下约定：

1. 先查再动：涉及具体实例先用 list_instances / get_instance 解析到确切的 ID·cloud·region·profile；\
**动手前先 list_runbooks + read_runbook 看有没有可复用的运维剧本**，有就照着执行。
2. 安全分级：只读（query_* / list_* / instance_status）直接用；reboot_instance 低风险可直接用；\
其它写操作（cloud_cli 带 confirm=true、run_ssh 里的破坏性命令、删资源等）先把计划讲清、经用户确认再做。\
解析不到目标 / 有歧义 / 缺连接信息时就停下如实回报，绝不猜测或对不确定对象动手。
3. **能力沉淀（重要）**：完成一项**之前没有对应 runbook** 的运维后，主动提议用 write_runbook 把它沉淀成剧本\
（kebab 命名；含 触发意图 / 目标解析 / 执行步骤与命令 / 是否写操作 / 注意事项），用户同意后写。这样能力随用增长。
4. 计划任务：定时重启用 schedule_task（一次性传 ISO 北京时间如 2026-06-06T22:30:00，循环传 5 段 crontab 如 "30 22 * * *"）；\
查/删用 list_scheduled_tasks / cancel_scheduled_task。
"""
mcp = MCPServer("multicloud-ops", instructions=_INSTRUCTIONS)


def _load():
    try:
        return json.loads(DATA.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {"instances": [], "domains": [], "totals": {}}


def _find_instance(id_or_name, cloud=None, account=None, region=None):
    from .resources import resolve
    return resolve(id_or_name, _load().get("instances", []), cloud=cloud, account=account, region=region)[0]


def _target_error(target, cloud=None, account=None, region=None):
    from .resources import matches, reference
    rows = matches(target, _load().get("instances", []), cloud=cloud, account=account, region=region)
    return ("目标不唯一，请指定云、账号、区域或完整引用：\n" + "\n".join(reference(r) for r in rows)
            if rows else f"未找到实例: {target}")


@mcp.tool()
def find_resources(target: str = "", kind: str = "instances", cloud: str | None = None,
                   account: str | None = None, region: str | None = None) -> str:
    """只读查询清单。返回唯一资源引用和支持的操作；同名、同 ID 必须收窄范围。"""
    from . import resources
    if kind not in resources.KINDS:
        return "不支持的资源类型"
    return json.dumps(resources.query(_load(), target, kind, cloud=cloud, account=account, region=region), ensure_ascii=False)


def _run(argv, timeout=120):
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                           env={**os.environ, "AWS_PAGER": ""})
    except subprocess.TimeoutExpired:
        return False, f"命令超时: {' '.join(argv[:3])}…"
    out = (r.stdout or "") + (("\n[stderr]\n" + r.stderr) if r.stderr else "")
    out = out.strip()
    if r.returncode != 0:
        out = annotate(out)  # 失败时追加确定性诊断（凭证/权限/限额/超时…）
    return r.returncode == 0, out


def _ssh(target, command, timeout=120):
    """返回 (ok, out)。ok=False 表示 SSH 连接失败或远程命令退出非零——调用方需区分，别把失败当成空输出。"""
    return _run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=12", target, command], timeout)


# --------------------------------------------------------------------------
# 读类工具
# --------------------------------------------------------------------------
@mcp.tool()
def list_instances(cloud: str = "", region: str = "", state: str = "") -> str:
    """列出多云实例。可按 cloud(aws|tencent|aliyun|volcengine|gcp|azure|oracle，只读纳管的 cloudflare/supabase 无实例)、region 显示名、state 过滤。
    数据来自上次 sync 的快照；要最新状态先调 sync 或 instance_status。"""
    rows = _load().get("instances", [])
    if cloud:
        rows = [i for i in rows if i.get("cloud") == cloud]
    if region:
        rows = [i for i in rows if region.lower() in (i.get("region_display", "").lower())]
    if state:
        rows = [i for i in rows if i.get("state") == state]
    fields = ["cloud", "name", "id", "profile", "region_display", "type",
              "private_ip", "public_ip", "state", "ssh_alias"]
    return json.dumps([{k: i.get(k) for k in fields} for i in rows], ensure_ascii=False, indent=2)


@mcp.tool()
def get_instance(id_or_name: str) -> str:
    """按实例 ID 或名称返回单台实例的完整信息（含 cloud/region/profile/ssh_alias 等）。"""
    inst = _find_instance(id_or_name)
    return json.dumps(inst, ensure_ascii=False, indent=2) if inst else _target_error(id_or_name)


@mcp.tool()
def query_domains() -> str:
    """列出所有域名（阿里云 + AWS Route53），含到期日期与剩余天数（按注册商/临期排序）。"""
    return json.dumps(_load().get("domains", []), ensure_ascii=False, indent=2)


@mcp.tool()
def query_databases() -> str:
    """列出所有托管数据库（AWS RDS 等），含 engine/class/storage/state/endpoint/multi_az 等。数据来自上次 sync 快照。"""
    return json.dumps(_load().get("databases", []), ensure_ascii=False, indent=2)


@mcp.tool()
def query_storage() -> str:
    """列出所有对象存储桶（AWS S3 / 阿里云 OSS 等），含 cloud/account/region/created。数据来自上次 sync 快照。"""
    return json.dumps(_load().get("storage", []), ensure_ascii=False, indent=2)


@mcp.tool()
def query_serverless() -> str:
    """列出所有 Serverless 函数（AWS Lambda 等），含 runtime/memory/timeout/code_mb/last_modified。数据来自上次 sync 快照。"""
    return json.dumps(_load().get("serverless", []), ensure_ascii=False, indent=2)


@mcp.tool()
def query_cdn() -> str:
    """列出所有 CDN 分布（AWS CloudFront，仅 global 分区），含 id/domain/aliases/origin/status/enabled/cert/price_class/last_modified/account。数据来自上次 sync 快照。"""
    return json.dumps(_load().get("cdn", []), ensure_ascii=False, indent=2)


@mcp.tool()
def query_iam() -> str:
    """列出所有 IAM 用户，含 account/created/last_used/key_count/oldest_key_days（用于排查闲置用户与陈旧密钥）。数据来自上次 sync 快照。"""
    return json.dumps(_load().get("iam", []), ensure_ascii=False, indent=2)


@mcp.tool()
def query_apikeys() -> str:
    """列出 AI 密钥元数据：GCP Cloud API Keys（AI Studio 的 Gemini key / Vertex AI Express key）+ AWS Bedrock 长期 API key（IAM 服务专用凭证，含 Claude Platform on AWS key；仅全球区），含 account/purpose/unrestricted/service_account(绑定身份)/key_type/status/created/age_days/expires。只有元数据、没有密钥串。数据来自上次 sync 快照。"""
    return json.dumps(_load().get("apikeys", []), ensure_ascii=False, indent=2)


@mcp.tool()
def query_messaging() -> str:
    """列出消息/事件资源（SQS 队列 / SNS 主题 / SES 身份 / EventBridge 规则），含 kind/account/region/detail。数据来自上次 sync 快照。"""
    return json.dumps(_load().get("messaging", []), ensure_ascii=False, indent=2)


@mcp.tool()
def query_billing() -> str:
    """列出各账号月度成本明细，含 cloud/account/service/month/amount/currency/period_start/partial。AWS 经 Cost Explorer 纳入（global 分区币种 USD、china 分区 aws-cn 币种 CNY），火山引擎经 billing ListBillOverviewByProd 纳入（CNY，应付金额）；币种不折算，按 currency 区分。数据来自上次 sync 快照。"""
    return json.dumps(_load().get("billing", []), ensure_ascii=False, indent=2)


@mcp.tool()
def instance_status(id_or_name: str, cloud: str | None = None, account: str | None = None, region: str | None = None) -> str:
    """实时查询单台实例状态（按云调用对应 CLI 的 Describe）。用于重启后验证。"""
    inst = _find_instance(id_or_name, cloud, account, region)
    if not inst:
        return _target_error(id_or_name, cloud, account, region)
    p = PROVIDERS.get(inst["cloud"])
    if not p:
        return f"未知 cloud: {inst['cloud']}"
    from .reboot_cli import _argv_for
    command, error = _argv_for(p, inst, True)
    if error:
        return error
    ok, out = _run(command)
    return out


# --------------------------------------------------------------------------
# 操作类工具
# --------------------------------------------------------------------------
@mcp.tool()
def reboot_instance(id_or_name: str, cloud: str | None = None, account: str | None = None, region: str | None = None, dry_run: bool = False) -> str:
    """重启一台实例（按 cloud 自动选 aws/tccli/aliyun/ve/gcloud/az/oci）。重启低风险可逆，直接执行。
    重启后建议过 ~40 秒调 instance_status 验证。"""
    inst = _find_instance(id_or_name, cloud, account, region)
    if not inst:
        return _target_error(id_or_name, cloud, account, region)
    p = PROVIDERS.get(inst["cloud"])
    if not p:
        return f"未知 cloud: {inst['cloud']}"
    from . import resources
    from .reboot_cli import _argv_for
    command, error = _argv_for(p, inst, False)
    if error:
        return error
    if dry_run:
        return json.dumps({**resources.plan(inst, "instances", "reboot", command), "dry_run": True}, ensure_ascii=False)
    ok, out = _run(command)
    ops_log.record("codex", "reboot", target=inst.get("name"), cloud=inst["cloud"],
                   detail=inst["id"], result="ok" if ok else "fail", account=resources.account_of(inst))
    status = "已发送重启" if ok else "重启失败"
    return f"[{status}] {inst.get('name')} ({inst['id']}, {inst['cloud']}/{inst['region']})\n{out}"


@mcp.tool()
def schedule_task(instance: str, schedule: str, confirm: bool = False, dry_run: bool = False) -> str:
    """创建定时重启实例的计划任务（云端到点执行，本机不常驻）。支持 AWS / 腾讯 / 阿里。
    instance=实例名或 ID；schedule=一次性本地时间 ISO（如 2026-06-06T22:30:00，按北京时间）
    或循环的 5 段标准 crontab（分 时 日 月 周，如 '30 22 * * *'=每天 22:30）。
    三云均为云 API 重启（AWS 直调，腾讯/阿里经 Lambda 调各自 API）。未接入的账号会如实拒绝。"""
    inst = _find_instance(instance)
    if not inst:
        return _target_error(instance)
    if not schedule_ops.is_supported(inst.get("cloud"), inst.get("profile")):
        return schedule_ops.unsupported_reason(inst.get("cloud"), inst.get("profile"))
    from .resources import plan
    operation = {**plan(inst, "instances", "schedule"), "schedule": schedule}
    if dry_run or not confirm:
        return json.dumps({**operation, "dry_run": dry_run,
                           "message": "预览；向用户确认后传 confirm=true 执行"}, ensure_ascii=False)
    sched = schedule.strip()
    one_off = bool(re.match(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", sched))
    ok, res = schedule_ops.create_reboot(
        inst["cloud"], inst["profile"], inst["region"], inst["id"], inst["name"],
        when=sched if one_off else None, crontab=None if one_off else sched)
    kind = "一次性" if one_off else "循环"
    ops_log.record("codex", "schedule_task", target=inst["name"], cloud=inst["cloud"],
                   detail=f"{kind}重启 @ {sched}", result="ok" if ok else "fail", account=inst.get("profile"))
    if not ok:
        return annotate(f"创建定时任务失败: {res}")
    return f"已创建{kind}定时重启 {inst['name']} ({inst['id']}, {inst['cloud']}) @ {sched} [{schedule_ops.DISPLAY_TZ}]，标识 {res}"


@mcp.tool()
def list_scheduled_tasks() -> str:
    """列出所有计划任务（定时重启），含目标实例 / 时间或循环表达式 / 状态。"""
    return json.dumps(schedule_ops.list_all(), ensure_ascii=False, indent=2)


@mcp.tool()
def cancel_scheduled_task(name: str, confirm: bool = False, dry_run: bool = False) -> str:
    """取消计划任务。先预览；向用户确认后传 confirm=true。可传完整资源引用。"""
    from . import resources
    # Preview uses cached inventory, without credentials or cloud calls.
    rows = _load().get("scheduled", []) if dry_run or not confirm else schedule_ops.list_all()
    row, candidates = resources.resolve(name, rows, "scheduled")
    if not row:
        return ("目标不唯一：\n" + "\n".join(resources.reference(r, "scheduled") for r in candidates)
                if candidates else "未找到计划任务；请先刷新计划任务清单。")
    operation = resources.plan(row, "scheduled", "cancel")
    if dry_run or not confirm:
        return json.dumps({**operation, "dry_run": dry_run, "message": "确认后传 confirm=true 执行"}, ensure_ascii=False)
    ok, out = schedule_ops.delete_row(row)
    ops_log.record("codex", "cancel_task", target=row.get("instance", name),
                   cloud=row.get("cloud", ""), detail=row.get("name", name), result="ok" if ok else "fail",
                   account=resources.account_of(row))
    return f"已取消计划任务 {row['name']}" if ok else annotate(f"取消失败: {out}")


@mcp.tool()
def run_ssh(ssh_alias: str, command: str, confirm: bool = False, dry_run: bool = False) -> str:
    """经 SSH 在实例上执行命令。ssh_alias 用 list_instances 返回的 ssh_alias（以 inventory 实际别名为准）。
    只读直接执行；写操作先预览，向用户确认后传 confirm=true。dry_run 不连接。"""
    read_only = is_read_only_ssh(command)
    if dry_run or (not read_only and not confirm):
        return json.dumps({"action": "ssh", "target": ssh_alias, "command": command,
                           "read_only": read_only, "confirmation_required": not read_only,
                           "dry_run": dry_run, "message": "写操作须向用户确认后传 confirm=true"}, ensure_ascii=False)
    ok, out = _ssh(ssh_alias, command)
    if not is_read_only_ssh(command):   # SSH 写操作记入操作日志
        ops_log.record("codex", "ssh", target=ssh_alias, detail=command,
                       result="ok" if ok else "fail")
    if not ok:
        return f"[SSH 失败] {out or '(连接失败，无输出)'}"
    return out or "(无输出)"


@mcp.tool()
def resolve_proxy_upstream(ssh_alias: str) -> str:
    """解析某台运行 v2ray/xray 的机器的出站上游 IP，并反查归属实例。只取地址/端口，不读取密钥。"""
    ok, out = _ssh(ssh_alias, 'for d in xray v2ray; do F=/usr/local/etc/$d/config.json; '
                              '[ -r "$F" ] && echo "$F" && break; done')
    if not ok:
        return f"[SSH 失败] 无法连接 {ssh_alias}：{out or '(连接失败)'}"
    path = next((p for p in out.strip().splitlines() if p.startswith("/")), "")
    if not path:
        return "未找到 v2ray/xray 配置（/usr/local/etc/{xray,v2ray}/config.json 都不可读）"
    ok, raw = _ssh(ssh_alias, f"cat {path}")
    if not ok:
        return f"[SSH 失败] 读取 {path} 失败：{raw or '(连接失败)'}"
    try:
        cfg = json.loads(raw)
    except json.JSONDecodeError:
        return f"读到配置但解析失败（{path}）"
    addrs = []
    for o in cfg.get("outbounds", []):
        s = o.get("settings", {}) or {}
        for v in (s.get("vnext") or []) + (s.get("servers") or []):
            if v.get("address"):
                addrs.append((v["address"], v.get("port")))
    if not addrs:
        return f"配置 {path} 里没找到 outbound 上游地址"
    ip, port = addrs[0]
    owner = next((i for i in _load().get("instances", []) if i.get("public_ip") == ip), None)
    res = {"host": ssh_alias, "config": path, "upstream_ip": ip, "upstream_port": port,
           "resolved_instance": ({"name": owner["name"], "id": owner["id"], "cloud": owner["cloud"],
                                  "region": owner["region_display"]} if owner else "未在清单中找到（可能未纳管）")}
    return json.dumps(res, ensure_ascii=False, indent=2)


@mcp.tool()
def sync() -> str:
    """全量同步：重扫所有云的实例 + 域名，刷新 工作区 data.json 与 inventory。返回同步报告。"""
    ok, out = _run([sys.executable, "-m", "noc_ops.sync"], timeout=600)
    if "===== 同步报告 =====" in out:
        out = out.split("===== 同步报告 =====", 1)[1].strip()
    return out[:4000]


# --------------------------------------------------------------------------
# Runbook（运维剧本）：跨客户端共享的知识层，可读可写
# --------------------------------------------------------------------------
@mcp.tool()
def list_runbooks() -> str:
    """列出所有运维剧本（runbook）及其标题。干活前先看有没有可复用的经验。"""
    out = []
    for f in sorted(RUNBOOKS.glob("*.md")):
        title = ""
        try:
            for line in f.read_text().splitlines():
                if line.strip():
                    title = line.lstrip("# ").strip()
                    break
        except OSError:
            pass
        out.append({"name": f.name, "title": title})
    return json.dumps(out, ensure_ascii=False, indent=2)


@mcp.tool()
def read_runbook(name: str) -> str:
    """读取一个 runbook 的完整内容（markdown）。name 取自 list_runbooks。"""
    safe = name if name.endswith(".md") else name + ".md"
    target = (RUNBOOKS / safe).resolve()
    if RUNBOOKS.resolve() not in target.parents or not target.is_file():
        return f"未找到 runbook: {name}"
    return target.read_text()


@mcp.tool()
def write_runbook(name: str, content: str) -> str:
    """新建或覆盖一个 runbook（沉淀运维经验）。name 用 kebab-case；content 是完整 markdown。
    建议结构参考 _TEMPLATE.md：触发意图 / 目标解析 / 执行步骤 / 是否写操作 / 注意事项。"""
    safe = re.sub(r"[^a-zA-Z0-9_.-]", "-", name)
    if not safe.endswith(".md"):
        safe += ".md"
    target = (RUNBOOKS / safe).resolve()
    if RUNBOOKS.resolve() not in target.parents:
        return f"非法路径: {name}"
    existed = target.is_file()
    target.write_text(content)
    # 写完顺手把新剧本登记进 NOC 机读索引（data.json）——纯本地零网络刷新，不触云。
    idx = ""
    try:
        from . import sync
        if sync.refresh_index(quiet=True) == 0:
            idx = "，已刷新 NOC 机读索引"
    except Exception:
        pass
    return f"{'已覆盖' if existed else '已新建'} runbooks/{safe}{idx}"


# --------------------------------------------------------------------------
# 通用 CLI 透传（覆盖白名单外的运维），带安全分类
# --------------------------------------------------------------------------
@mcp.tool()
def cloud_cli(cloud: str, command: str, confirm: bool = False) -> str:
    """执行任意云 CLI 命令（覆盖结构化工具未涵盖的运维）。cloud=aws|tencent|aliyun|volcengine|gcp|azure|oracle，
    command 是 CLI 二进制之后的参数（各云仍需自带 --profile/--region/--RegionId/--project/--subscription）。
    安全：只读 + 实例重启直接执行；其余写操作必须显式 confirm=true 才执行。
    例：cloud_cli("azure","vm list --subscription <S> -o json")；
        cloud_cli("aws","ec2 create-tags --resources i-x --tags Key=k,Value=v --profile P --region R", confirm=True)"""
    p = PROVIDERS.get(cloud)
    if not p or not p.cli:                       # 有 CLI 的云才能走 cloud_cli（单一来源=注册表，加云自动跟上）
        valid = " / ".join(c for c, pr in PROVIDERS.items() if pr.cli)
        return f"未知或无 CLI 的 cloud: {cloud}（应为 {valid}）"
    cmd = command.strip()
    if cmd.startswith(p.cli + " "):
        cmd = cmd[len(p.cli) + 1:]
    if not (p.is_read(cmd) or p.is_reboot(cmd) or confirm):
        return (f"⚠️ 这是写操作（非只读/非重启），出于安全未执行。\n"
                f"确认无误请带 confirm=true 重试。命令：{p.cli} {cmd}")
    try:
        argv = [p.cli, *shlex.split(cmd)]
    except ValueError as e:
        return f"命令解析失败: {e}"
    ok, out = _run(argv)
    if not p.is_read(cmd):   # 写操作（含重启）记入操作日志
        ops_log.record("codex", "cli", cloud=cloud, detail=cmd, result="ok" if ok else "fail")
    return out or "(无输出)"


def main():
    """`ops-mcp` 入口：给 Codex 等 MCP 客户端用（stdio）。"""
    paths.ensure_workspace()
    mcp.run()


if __name__ == "__main__":
    main()
