"""把 AWS / SSH 操作分类为「只读」或「写操作」，用于权限闸门。

只读 = 自动执行；写操作 = 先向用户确认。
分类要保守：拿不准时一律当成写操作（需确认），宁可多问一次。
"""
import shlex

# AWS CLI 操作动词：以这些前缀开头的子命令视为只读
_READ_ONLY_AWS_PREFIXES = (
    "describe", "list", "get", "lookup", "search", "scan", "query",
    "head", "show", "view", "ls", "estimate", "preview", "test",
    "check", "validate", "filter", "batch-get", "select",
)

# 明确属于写/危险的动词（即便不在上面，也强制确认）——主要用于兜底
_WRITE_AWS_VERBS = (
    "terminate", "stop", "start", "reboot", "delete", "create", "run",
    "modify", "update", "put", "attach", "detach", "reset", "set",
    "send-command", "associate", "disassociate", "deregister", "register",
    "authorize", "revoke", "enable", "disable", "import", "restore",
    "cancel", "release", "allocate", "replace",
)


def is_read_only_aws(command: str) -> bool:
    """判断一条 aws CLI 命令是否只读。command 不含开头的 'aws'。

    AWS CLI 结构：aws <service> <operation> [args...]
    取 operation（第二个 token，跳过以 - 开头的全局选项）来判断。
    """
    try:
        tokens = shlex.split(command)
    except ValueError:
        return False  # 解析不了 → 当作写操作，去确认
    # 去掉开头可能误写的 "aws"
    if tokens and tokens[0] == "aws":
        tokens = tokens[1:]
    # 跳过全局选项（--region xxx / --profile xxx / --output json 等）
    positional = []
    skip_next = False
    for t in tokens:
        if skip_next:
            skip_next = False
            continue
        if t.startswith("-"):
            # --key=value 形式自带值；--key value 形式要吃掉下一个
            if "=" not in t:
                skip_next = True
            continue
        positional.append(t)
        if len(positional) >= 2:
            break
    if len(positional) < 2:
        return False  # 信息不足 → 确认
    operation = positional[1].lower()
    if operation.startswith(_READ_ONLY_AWS_PREFIXES):
        # ssm send-command 这类 send- 开头的不是只读，已在写动词里；这里再兜底
        if operation in _WRITE_AWS_VERBS:
            return False
        return True
    return False


# 腾讯云 tccli：结构 `tccli <service> <Action> [options]`，Action 是 PascalCase。
# 只读动作前缀（Describe/List/Get…）→ 自动；其余（Reboot/Stop/Modify…）→ 确认。
_READ_ONLY_TCCLI_PREFIXES = (
    "Describe", "Inquiry", "Inquire", "List", "Get", "Query",
    "Check", "Search", "Preview",
)


def is_read_only_tccli(command: str) -> bool:
    """判断一条 tccli 命令是否只读。command 不含开头的 'tccli'。保守：拿不准要确认。"""
    try:
        tokens = shlex.split(command)
    except ValueError:
        return False
    if tokens and tokens[0] == "tccli":
        tokens = tokens[1:]
    positional = []
    skip_next = False
    for t in tokens:
        if skip_next:
            skip_next = False
            continue
        if t.startswith("-"):
            if "=" not in t:
                skip_next = True
            continue
        positional.append(t)
        if len(positional) >= 2:
            break
    if len(positional) < 2:
        return False
    action = positional[1]  # 区分大小写的 PascalCase 动作名
    return action.startswith(_READ_ONLY_TCCLI_PREFIXES)


# 重启识别：用户授权"实例重启"免二次确认（低风险、可逆、高频）。
# 仅指云 API 的实例重启（aws reboot-instances / 腾讯 RebootInstances），
# 不含 SSH 里的 reboot/systemctl（那仍按写操作确认）。
def _positionals(command, cli):
    """取 CLI 命令的前两个位置参数（跳过 --opt 全局选项），用于识别 service/operation。"""
    try:
        tokens = shlex.split(command)
    except ValueError:
        return []
    if tokens and tokens[0] == cli:
        tokens = tokens[1:]
    out, skip = [], False
    for t in tokens:
        if skip:
            skip = False
            continue
        if t.startswith("-"):
            if "=" not in t:
                skip = True
            continue
        out.append(t)
        if len(out) >= 2:
            break
    return out


def is_reboot_aws(command: str) -> bool:
    p = _positionals(command, "aws")
    return len(p) >= 2 and p[1].lower().startswith("reboot")


def is_reboot_tccli(command: str) -> bool:
    p = _positionals(command, "tccli")
    return len(p) >= 2 and p[1].startswith("Reboot")


# 阿里云 aliyun CLI：结构 `aliyun <product> <Action> [--opt]`，Action 是 PascalCase。
_READ_ONLY_ALIYUN_PREFIXES = ("Describe", "List", "Get", "Query", "Check")


def is_read_only_aliyun(command: str) -> bool:
    p = _positionals(command, "aliyun")
    return len(p) >= 2 and p[1].startswith(_READ_ONLY_ALIYUN_PREFIXES)


def is_reboot_aliyun(command: str) -> bool:
    p = _positionals(command, "aliyun")
    return len(p) >= 2 and p[1].startswith("Reboot")


# 火山引擎 ve CLI：结构 `ve <service> <Action> [--Param v]`，Action 是 PascalCase（与阿里云同款）。
_READ_ONLY_VE_PREFIXES = ("Describe", "List", "Get", "Query", "Check", "Search")


def is_read_only_ve(command: str) -> bool:
    p = _positionals(command, "ve")
    return len(p) >= 2 and p[1].startswith(_READ_ONLY_VE_PREFIXES)


def is_reboot_ve(command: str) -> bool:
    p = _positionals(command, "ve")
    return len(p) >= 2 and p[1].startswith("Reboot")


# Google Cloud gcloud：结构 `gcloud <名词路径...> <动词> [资源] [--flags]`，动词在名词路径之后
# （与 aws 的"第二个 token"不同）。例：compute instances list / compute instances reset my-vm /
# projects get-iam-policy P / storage buckets describe gs://x。动词=名词路径后第一个读/写动词 token。
_READ_ONLY_GCLOUD_PREFIXES = ("describe", "list", "get", "lookup", "search", "tail", "print", "export")
_WRITE_GCLOUD_VERBS = (
    "reset", "stop", "start", "suspend", "resume", "delete", "create", "update",
    "set-iam-policy", "add-iam-policy-binding", "remove-iam-policy-binding",
    "add-metadata", "remove-metadata", "set-machine-type", "attach-disk", "detach-disk",
    "disable", "enable", "undelete", "import", "deploy", "add-access-config", "delete-access-config",
)


def _gcloud_verb(command: str) -> str:
    """取 gcloud 命令的动作动词（名词路径后第一个匹配读前缀或写动词的位置参数）。找不到返回 ""。"""
    try:
        tokens = shlex.split(command)
    except ValueError:
        return ""
    if tokens and tokens[0] == "gcloud":
        tokens = tokens[1:]
    skip = False
    for t in tokens:
        if skip:
            skip = False
            continue
        if t.startswith("-"):
            if "=" not in t:
                skip = True            # --flag value 形式吃掉下一个（名词路径在 flag 前，不影响动词识别）
            continue
        if t.startswith(_READ_ONLY_GCLOUD_PREFIXES) or t in _WRITE_GCLOUD_VERBS:
            return t                    # 名词路径(compute/instances/projects…)不会命中，第一个命中即动词
    return ""


def is_read_only_gcp(command: str) -> bool:
    """判断一条 gcloud 命令是否只读。command 不含开头的 'gcloud'。保守：拿不准要确认。"""
    v = _gcloud_verb(command)
    return bool(v) and v.startswith(_READ_ONLY_GCLOUD_PREFIXES) and v not in _WRITE_GCLOUD_VERBS


def is_reboot_gcp(command: str) -> bool:
    """GCE 实例「重启」= `compute instances reset`（硬重启，无优雅重启 API）。"""
    return _gcloud_verb(command) == "reset"


# Azure CLI（az）：与 gcloud 同构——命令是 `az <组> <子组…> <动词> [位置参数] [--flags]`，
# 动词是「名词路径后第一个匹配读前缀或写动词的位置 token」。例：vm list / vm restart my-vm /
# network dns zone list / storage account list / role assignment list。
_READ_ONLY_AZ_PREFIXES = ("list", "show", "get", "check", "exists")
_WRITE_AZ_VERBS = (
    "create", "delete", "update", "restart", "start", "stop", "deallocate",
    "redeploy", "resize", "reimage", "capture", "generalize", "convert",
    "set", "add", "remove", "enable", "disable", "assign", "open-port",
    "run-command", "invoke", "import", "wait",
)


def _az_verb(command: str) -> str:
    """取 az 命令的动作动词（名词路径后第一个匹配读前缀或写动词的位置参数）。找不到返回 ""。"""
    try:
        tokens = shlex.split(command)
    except ValueError:
        return ""
    if tokens and tokens[0] == "az":
        tokens = tokens[1:]
    skip = False
    for t in tokens:
        if skip:
            skip = False
            continue
        if t.startswith("-"):
            if "=" not in t:
                skip = True            # --flag value 形式吃掉下一个（名词路径在 flag 前，不影响动词识别）
            continue
        if t.startswith(_READ_ONLY_AZ_PREFIXES) or t in _WRITE_AZ_VERBS:
            return t                    # 名词路径(vm/network/storage…)不会命中，第一个命中即动词
    return ""


def _az_rest_method(command: str) -> str:
    """`az rest` 的 HTTP 方法（小写；省略 --method 时 az 默认 get）。不是 az rest 返回 ""。"""
    try:
        tokens = shlex.split(command)
    except ValueError:
        return ""
    if tokens and tokens[0] == "az":
        tokens = tokens[1:]
    if not tokens or tokens[0] != "rest":
        return ""
    for i, t in enumerate(tokens):
        if t in ("--method", "-m") and i + 1 < len(tokens):
            return tokens[i + 1].lower()
        if t.startswith("--method="):
            return t.split("=", 1)[1].lower()
    return "get"


def is_read_only_azure(command: str) -> bool:
    """判断一条 az 命令是否只读。command 不含开头的 'az'。保守：拿不准要确认。

    `az rest` 按 HTTP 方法判：GET 只读；POST/PUT/PATCH/DELETE 一律要确认——唯一例外是
    Cost Management 的 `…/Microsoft.CostManagement/query`：它是「查询」语义的 POST（接口设计如此，
    body 只有时间窗/分组，不改任何资源），驱动的 Azure 成本就靠它（2026-10-25 用户放开）。
    """
    method = _az_rest_method(command)
    if method:
        if method == "get":
            return True
        return method == "post" and "/Microsoft.CostManagement/query" in command
    v = _az_verb(command)
    return bool(v) and v.startswith(_READ_ONLY_AZ_PREFIXES) and v not in _WRITE_AZ_VERBS


def is_reboot_azure(command: str) -> bool:
    """Azure VM「重启」= `vm restart`（优雅重启）。"""
    return _az_verb(command) == "restart"


# OCI CLI（oci）：命令是 `oci <服务> <资源…> <动作> [--flags]`，动作是「名词路径后第一个匹配读前缀
# 或写动词的位置 token」。例：compute instance list / compute instance get / os bucket list /
# iam user list / db autonomous-database list。电源操作特殊——统一走 `compute instance action
# --action <VERB>`（SOFTRESET/RESET 重启、SOFTSTOP/STOP 停、START 启），故 action 本身是写动词，
# 是否「重启」再看 --action 的值。
_READ_ONLY_OCI_PREFIXES = ("list", "get", "request-summarized")     # list/get/list-vnics… 都覆盖；usage-api 的 request-summarized-usages 也是只读查询
_WRITE_OCI_VERBS = (
    "create", "update", "delete", "terminate", "action", "change-compartment",
    "attach", "detach", "add", "remove", "enable", "disable", "import", "export",
    "reset", "start", "stop", "restart", "rotate", "move", "launch", "capture", "upload",
)


def _oci_verb(command: str) -> str:
    """取 oci 命令的动作动词（名词路径后第一个匹配读前缀或写动词的位置参数）。找不到返回 ""。"""
    try:
        tokens = shlex.split(command)
    except ValueError:
        return ""
    if tokens and tokens[0] == "oci":
        tokens = tokens[1:]
    skip = False
    for t in tokens:
        if skip:
            skip = False
            continue
        if t.startswith("-"):
            if "=" not in t:
                skip = True            # --flag value 形式吃掉下一个（也顺带吃掉 --action SOFTRESET 的值）
            continue
        if t.startswith(_READ_ONLY_OCI_PREFIXES) or t in _WRITE_OCI_VERBS:
            return t                    # 名词路径(compute/instance/os/bucket…)不会命中，第一个命中即动词
    return ""


def _oci_action_value(command: str) -> str:
    """取 `oci ... instance action --action <VERB>` 里 --action 的值（大写）。无则返回 ""。"""
    try:
        tokens = shlex.split(command)
    except ValueError:
        return ""
    for i, t in enumerate(tokens):
        if t == "--action" and i + 1 < len(tokens):
            return tokens[i + 1].upper()
        if t.startswith("--action="):
            return t.split("=", 1)[1].upper()
    return ""


def is_read_only_oci(command: str) -> bool:
    """判断一条 oci 命令是否只读。command 不含开头的 'oci'。保守：拿不准要确认。"""
    v = _oci_verb(command)
    return bool(v) and v.startswith(_READ_ONLY_OCI_PREFIXES) and v not in _WRITE_OCI_VERBS


def is_reboot_oci(command: str) -> bool:
    """OCI 实例「重启」= `compute instance action --action SOFTRESET`（优雅）或 RESET（硬 power-cycle）。
    SOFTSTOP/STOP/START 不算重启（停/启需确认）。"""
    return _oci_verb(command) == "action" and _oci_action_value(command) in ("SOFTRESET", "RESET", "REBOOT")


# SSH：默认所有 SSH 命令都需确认（远程执行风险高）。
# 仅当远程命令明显只读时才放行。
_READ_ONLY_SSH_PREFIXES = (
    "cat", "ls", "df", "du", "free", "top", "htop", "ps", "uptime",
    "whoami", "id", "hostname", "uname", "date", "tail", "head", "less",
    "grep", "find", "stat", "echo", "systemctl status", "journalctl",
    "docker ps", "docker logs", "netstat", "ss", "ip a", "ifconfig",
    "pwd", "env", "which", "lsblk", "mount", "vmstat", "iostat",
)


# 出现这些 token 一律视为写/危险，直接要确认（即便分段也不放行）
_SSH_DANGER_TOKENS = (
    ">", ">>", "rm ", "kill", "reboot", "shutdown", "tee", " dd ", "mkfs",
    "mv ", "chmod", "chown", "truncate", "sudo ", "systemctl restart",
    "systemctl stop", "systemctl start", "docker rm", "docker stop",
    "docker restart", "docker start", "apt ", "yum ", "pip install", "git ",
)


def is_read_only_ssh(command: str) -> bool:
    """判断一条要在远程主机执行的命令是否只读。保守：拿不准就要确认。

    支持复合命令：按 && / ; / | 拆段，**每一段**都必须以只读命令开头，
    且整条不含危险 token，才放行；否则要求确认。
    """
    cmd = command.strip()
    if not cmd:
        return False
    low = cmd.lower()
    if any(tok in low for tok in _SSH_DANGER_TOKENS):
        return False
    # 拆分复合命令的各段
    segments = []
    for part in cmd.replace("&&", "\n").replace("||", "\n").replace(";", "\n").replace("|", "\n").split("\n"):
        seg = part.strip().lower()
        if seg:
            segments.append(seg)
    if not segments:
        return False
    return all(seg.startswith(_READ_ONLY_SSH_PREFIXES) for seg in segments)


if __name__ == "__main__":
    # 简单自测
    tests_aws = [
        ("ec2 describe-instances --region cn-northwest-1", True),
        ("--region cn-northwest-1 ec2 describe-instances", True),
        ("ec2 stop-instances --instance-ids i-123", False),
        ("ssm send-command --document-name AWS-RunShellScript", False),
        ("ec2 reboot-instances --instance-ids i-1", False),
        ("cloudwatch get-metric-statistics", True),
    ]
    for cmd, expect in tests_aws:
        got = is_read_only_aws(cmd)
        print(f"[{'OK' if got == expect else 'XX'}] aws {cmd!r} -> {got}")
    tests_tccli = [
        ("lighthouse DescribeInstances --region ap-nanjing", True),
        ("lighthouse RebootInstances --InstanceIds lhins-x --region ap-nanjing", False),
        ("cvm DescribeInstances", True),
        ("lighthouse StopInstances --InstanceIds lhins-x", False),
        ("--region ap-nanjing lighthouse DescribeInstances", True),
    ]
    for cmd, expect in tests_tccli:
        got = is_read_only_tccli(cmd)
        print(f"[{'OK' if got == expect else 'XX'}] tccli {cmd!r} -> {got}")
    tests_reboot = [
        (is_reboot_aws, "ec2 reboot-instances --instance-ids i-1", True),
        (is_reboot_aws, "ec2 stop-instances --instance-ids i-1", False),
        (is_reboot_aws, "ec2 describe-instances", False),
        (is_reboot_tccli, "lighthouse RebootInstances --InstanceIds lhins-x", True),
        (is_reboot_tccli, "lighthouse StopInstances --InstanceIds lhins-x", False),
    ]
    for fn, cmd, expect in tests_reboot:
        got = fn(cmd)
        print(f"[{'OK' if got == expect else 'XX'}] {fn.__name__} {cmd!r} -> {got}")
    tests_ssh = [
        ("df -h", True),
        ("systemctl status nginx", True),
        ("systemctl restart nginx", False),
        ("rm -rf /tmp/x", False),
        ("tail -n 100 /var/log/app.log", True),
        ("cat /etc/hosts > /tmp/x", False),
        ("df -h && uptime && cat /proc/loadavg", True),   # 全只读复合 → 放行
        ("ps aux | grep nginx", True),                     # 只读管道 → 放行
        ("df -h && rm -rf /tmp/x", False),                 # 含写动词 → 确认
        ("free -m; systemctl restart nginx", False),       # 一段是写 → 确认
    ]
    for cmd, expect in tests_ssh:
        got = is_read_only_ssh(cmd)
        print(f"[{'OK' if got == expect else 'XX'}] ssh {cmd!r} -> {got}")
