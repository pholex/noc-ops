"""把云 CLI / SSH 的失败输出，用确定性正则表秒判成结构化「诊断」。

定位：LLM 大脑的**前置滤网**——常见报错（鉴权失败、凭证过期、限额、超时、找不到资源…）
用正则零 token、确定性地命中，直接给出「这是什么 + 怎么修」，把 LLM 留给真正模糊的场景。
错误模式覆盖云 CLI 与 SSH，诊断仅依赖输入文本。

用法：命令失败时把 stdout+stderr 丢给 `classify(text)` 拿 [Finding]，
或直接 `annotate(out)` 在原始输出后追加一段人/agent 都易读的诊断。命中不了就原样返回，绝不误报。

只读、纯字符串处理、无外部依赖、无副作用。
"""
import re
from dataclasses import dataclass


@dataclass
class Finding:
    code: str           # 速查码（runbook 里可检索）
    title: str          # 一句话定性
    severity: str       # info | warning | error
    recommendation: str # 怎么修
    evidence: str       # 命中的原文片段（截断）


# 每条： (正则, code, severity, title, 修复建议)
# 正则对 **小写化后的全文** 匹配；按从具体到宽泛排序，dedupe 后每个 code 只报一次。
# 覆盖 AWS / 腾讯 tccli / 阿里云 aliyun CLI + SSH 四个来源的高频报错。
_CHECKS = [
    # —— 凭证 / 鉴权（最高频，先判）——
    (r"the security token included in the request is expired|expiredtoken|expired token|token.*expired",
     "TOKEN_EXPIRED", "error", "凭证/会话令牌已过期",
     "刷新凭证后重试：AWS 走 SSO 重新登录或更新 ~/.aws/credentials；临时令牌（STS）需重新签发。"),
    (r"invalidclienttokenid|the security token included in the request is invalid|invalidaccesskeyid|"
     r"sdk\.invalidaccesskeyid|specified access key.*not.*exist|secretid.*not.*found|invalidsecretid",
     "BAD_ACCESS_KEY", "error", "AccessKey/SecretId 无效",
     "核对该 profile 的 AccessKeyId 是否填错/已删除：aws=~/.aws，aliyun=`aliyun configure`，tccli=`tccli configure`。"),
    (r"signaturedoesnotmatch|signature.*not.*match|incompletesignature",
     "BAD_SECRET", "error", "签名不匹配（SecretKey 错误或时钟偏移）",
     "多为 SecretAccessKey 填错；若 key 确认无误，检查本机时间是否漂移（NTP 同步）。"),
    (r"unable to locate credentials|you must specify a region|the config profile .* could not be found|"
     r"profile .* not found|unable to load credentials",
     "NO_CREDENTIALS", "error", "没找到凭证 / profile 不存在",
     "命令漏了 --profile/--region，或该 profile 未配置。对照 inventory.md 的账号，补全或先 configure。"),

    # —— 权限 ——
    (r"accessdenied|unauthorizedoperation|not authorized to perform|you are not authorized|"
     r"forbidden\.|nopermission|operation_denied|cam.*denied",
     "ACCESS_DENIED", "error", "权限不足（IAM/RAM/CAM 策略未授权）",
     "凭证有效但缺该操作的权限。补对应策略，或换一个有权限的 profile/角色；只读巡检可先用 describe 类确认范围。"),

    # —— 资源不存在 / 目标错 ——
    (r"invalidinstanceid\.notfound|does not exist|notfound|resourcenotfound|"
     r"invalid.*not found|no such|could not find",
     "NOT_FOUND", "warning", "目标资源不存在（ID/区域可能不对）",
     "实例/资源 ID 在该区域查不到——多为 region 选错或资源已删。先 list/describe 确认 ID 与所在 region 再操作。"),

    # —— 限额 / 限流 ——
    (r"\b(throttling|throttled|requestlimitexceeded|toomanyrequests|rate exceeded)\b",
     "THROTTLING", "warning", "被限流（请求过于频繁）",
     "云端在限流，非真错。退避几秒后重试；批量操作加间隔或减少并发。"),
    (r"limitexceeded|quotaexceeded|instancelimitexceeded|insufficient.*capacity|"
     r"vcpuslimitexceeded|exceeded.*quota|quota.*exceed",
     "QUOTA", "warning", "触达配额 / 容量上限",
     "换更小规格、换可用区/区域，或在目标区域提交配额提升工单后重试。"),

    # —— 网络 / 连通性（CLI endpoint + SSH 通用）——
    (r"could not connect to the endpoint url|endpoint.*could not|failed to establish.*connection|"
     r"connection.*refused|connect to .* port .* failed",
     "ENDPOINT_UNREACHABLE", "error", "连不上服务端点 / 端口",
     "检查本机网络与目标 region 是否写对（拼错的 region 会拼出不存在的 endpoint）；SSH 则确认安全组放行与服务在监听。"),
    (r"could not resolve hostname|name or service not known|temporary failure in name resolution",
     "DNS_FAIL", "error", "域名解析失败",
     "主机名/别名拼错或 DNS 不可用。核对 ~/.ssh/config 的 Host 与 inventory.md 的地址。"),
    (r"connection timed out|connect timed out|timed out|operation timed out|"
     r"etimedout|deadline exceeded|read timed out",
     "TIMEOUT", "warning", "操作超时",
     "网络慢或目标无响应。重试；SSH 超时优先查实例 running 状态、安全组放行你的源 IP、是否需走跳板机。"),

    # —— SSH 专属 ——
    (r"permission denied \(publickey|permission denied \(.*publickey|host key verification failed|"
     r"too many authentication failures|no supported authentication",
     "SSH_AUTH", "error", "SSH 认证失败（密钥/known_hosts 问题）",
     "确认私钥路径 ~/.ssh/<profile>/<KeyName>.pem 与登录用户名（ec2-user/ubuntu）正确；"
     "Host key verification failed 多为重装后指纹变了，清掉 known_hosts 对应行。"),
    (r"no route to host|network is unreachable",
     "NO_ROUTE", "error", "网络不可达（路由/安全组）",
     "目标 IP 不通：查实例是否 running、安全组/防火墙是否放行、内网实例是否需经跳板机。"),

    # —— 主机侧资源 ——
    (r"no space left on device|disk quota exceeded",
     "DISK_FULL", "error", "磁盘写满",
     "远程磁盘满了。先 `df -h` 定位，清日志/缓存或扩容；写操作前务必确认是哪个挂载点。"),
    (r"cannot allocate memory|out of memory|oom-kill|killed process",
     "OOM", "warning", "内存耗尽（可能触发 OOM-Kill）",
     "进程被 OOM 杀掉。`free -m` 看余量，重启服务或升配；排查是否有内存泄漏的进程。"),

    # —— 参数 / 用法 ——
    (r"invalidparametervalue|invalidparameter\b|malformedinput|validationerror|"
     r"missingparameter|invalid.*parameter|parameter.*invalid",
     "BAD_PARAM", "warning", "参数非法 / 缺参数",
     "命令参数拼写或取值有误。对照该云 CLI 文档与 inventory.md 的实际值（region/ID/profile）逐项核对。"),

    # —— DryRun 成功（这是「好消息」，非错误）——
    (r"request would have succeeded, but dryrun flag is set|dryrunoperation",
     "DRYRUN_OK", "info", "DryRun 通过（权限与参数都没问题）",
     "这是预演成功的信号——去掉 --dry-run 即可真正执行该写操作。"),
]

# 预编译
_COMPILED = [(re.compile(pat), code, sev, title, rec) for pat, code, sev, title, rec in _CHECKS]


def _snippet(text: str, start: int, end: int, pad: int = 60) -> str:
    """取命中位置周边一小段原文作证据，压平空白并截断。"""
    s = max(0, start - pad)
    e = min(len(text), end + pad)
    frag = " ".join(text[s:e].split())
    if len(frag) > 200:
        frag = frag[:197] + "..."
    return frag


def classify(text: str) -> list[Finding]:
    """扫描失败输出，返回去重后的 [Finding]。命中不了返回 []，绝不误报。"""
    if not text:
        return []
    low = text.lower()
    seen = set()
    out = []
    for rx, code, sev, title, rec in _COMPILED:
        if code in seen:
            continue
        m = rx.search(low)
        if not m:
            continue
        seen.add(code)
        out.append(Finding(code=code, title=title, severity=sev,
                           recommendation=rec, evidence=_snippet(text, m.start(), m.end())))
    return out


_SEV_ICON = {"info": "ℹ️", "warning": "⚠️", "error": "❌"}


def format_findings(findings: list[Finding]) -> str:
    """把 [Finding] 渲染成一段人/agent 都易读的诊断；无 finding 返回空串。"""
    if not findings:
        return ""
    lines = ["🔎 诊断（确定性匹配，未耗 token）："]
    for f in findings:
        icon = _SEV_ICON.get(f.severity, "•")
        lines.append(f" {icon} [{f.code}] {f.title}")
        lines.append(f"    证据：{f.evidence}")
        lines.append(f"    建议：{f.recommendation}")
    return "\n".join(lines)


def annotate(text: str) -> str:
    """便捷接线：命令失败的原始输出 → 原文 + 追加诊断（若命中）。未命中原样返回。"""
    diag = format_findings(classify(text))
    return f"{text}\n\n{diag}" if diag else text


if __name__ == "__main__":
    # 自测：每条覆盖一类真实报错，断言命中预期 code。
    cases = [
        ("An error occurred (ExpiredToken) when calling the DescribeInstances operation: "
         "The security token included in the request is expired", "TOKEN_EXPIRED"),
        ("An error occurred (InvalidClientTokenId): The security token included in the request is invalid",
         "BAD_ACCESS_KEY"),
        ("An error occurred (SignatureDoesNotMatch) when calling the operation", "BAD_SECRET"),
        ("Unable to locate credentials. You can configure credentials by running 'aws configure'.",
         "NO_CREDENTIALS"),
        ("An error occurred (UnauthorizedOperation): You are not authorized to perform this operation.",
         "ACCESS_DENIED"),
        ("An error occurred (InvalidInstanceID.NotFound): The instance ID 'i-x' does not exist",
         "NOT_FOUND"),
        ("An error occurred (RequestLimitExceeded): Request limit exceeded. Throttling", "THROTTLING"),
        ("An error occurred (InstanceLimitExceeded): You have requested more vCPU capacity quota",
         "QUOTA"),
        ("Could not connect to the endpoint URL: \"https://ec2.bad-region.amazonaws.com/\"",
         "ENDPOINT_UNREACHABLE"),
        ("ssh: Could not resolve hostname nx-prd-appX: Name or service not known", "DNS_FAIL"),
        ("ssh: connect to host 1.2.3.4 port 22: Connection timed out", "TIMEOUT"),
        ("ec2-user@host: Permission denied (publickey).", "SSH_AUTH"),
        ("ssh: connect to host 10.0.0.5 port 22: No route to host", "NO_ROUTE"),
        ("cp: error writing '/var/log/big': No space left on device", "DISK_FULL"),
        ("java: Cannot allocate memory", "OOM"),
        ("An error occurred (InvalidParameterValue): Value 'xx' at 'instanceId' failed", "BAD_PARAM"),
        ("Request would have succeeded, but DryRun flag is set.", "DRYRUN_OK"),
        ("instance is running normally, 3 running", None),  # 正常输出 → 不该误报
    ]
    ok = 0
    for text, expect in cases:
        codes = [f.code for f in classify(text)]
        hit = (expect in codes) if expect else (codes == [])
        ok += hit
        flag = "OK" if hit else "XX"
        print(f"[{flag}] expect={expect!r:20} got={codes} :: {text[:55]!r}")
    print(f"\n{ok}/{len(cases)} passed")
