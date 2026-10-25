# CloudWatch 告警配置与通知验证

## 触发意图

- “给 EC2 / RDS / Lambda 配置告警，异常时通知我”
- “告警显示 ALARM，但没有收到邮件，怎么排查？”
- “验证告警是否真的能通知，以及如何停用和清理”

## 目标解析

本剧本配置同账号、同区域的普通指标告警，通过标准 SNS topic 发通知。示例只配置 SNS 动作，不附加重启、停机、扩缩容或自动修复。

- 先通过 `ops find <目标> --cloud aws --json` 或工作区清单定位资源，再用 AWS API 核实 profile、账号、区域和唯一 ID。多条匹配必须让用户选择。
- EC2 使用 `InstanceId`；RDS 使用数据库实例的 `DBInstanceIdentifier`；Lambda 使用函数短名 `FunctionName`。下面的 RDS 存储示例针对普通 RDS 实例，不能直接套到 Aurora 集群。
- 确认通知对象、是否已有 SNS topic、指标采样频率、正常基线、阈值、允许延迟和缺失数据策略。阈值只是示例，需按业务调整。
- 需要 AWS CLI v2 和 Python 3。配置身份需要相关 CloudWatch 告警/指标查询权限及 SNS 管理/发布权限；邮件订阅还需收件人确认。不要为此直接授予管理员权限。
- 下面以 AWS 商业分区为例。跨账号、跨区域、复合告警和自动修复需要独立核对权限与行为。

## 是否写操作

查询资源、指标、告警与 SNS 配置为只读。创建/修改告警、topic、订阅和策略，发布测试通知/指标、启停动作、删除资源均为写操作，先展示完整计划并确认。

测试通知会实际发送给 topic 的订阅者，需确认收件范围与测试时段。不要对带有自动修复动作的生产告警执行 `set-alarm-state` 来测试。

## 执行步骤

### 1. 记录目标并准备本地目录

逐段执行，任何命令失败都先停止排查。执行目录保存配置、资源标识和订阅信息，不提交到公开仓库。

```bash
P='<profile>'
R='<region>'
KIND='ec2'                       # ec2 / rds / lambda
TARGET='<resource-id-or-name>'    # 实例 ID、DB 实例标识或函数短名，不填 ARN
PREFIX='noc-monitor'             # 本次告警名称前缀，仅用字母、数字、短横线
RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)"
umask 077
TASK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/noc-cw-XXXXXX")"
cd "$TASK_DIR"
aws --profile "$P" --region "$R" sts get-caller-identity --output json > identity.json
```

检查 `identity.json` 中的账号与角色符合预期。根据 `KIND` 执行对应查询确认目标存在：

```bash
case "$KIND" in
  ec2) aws --profile "$P" --region "$R" ec2 describe-instances --instance-ids "$TARGET" ;;
  rds) aws --profile "$P" --region "$R" rds describe-db-instances --db-instance-identifier "$TARGET" ;;
  lambda) aws --profile "$P" --region "$R" lambda get-function-configuration --function-name "$TARGET" ;;
  *) echo 'KIND must be ec2, rds or lambda' ;;
esac
```

### 2. 选择通知 topic，确认订阅

优先选用用户指定的同账号、同区域标准 topic，记录其 ARN。没有合适的 topic 时，先用 `sns list-topics` 核对名称未被占用，再确认创建：

```bash
aws --profile "$P" --region "$R" sns list-topics --output json
# 仅在确认新建后执行；create-topic 对已有名称会返回原 topic，不能据此认定是新建资源
TOPIC_NAME='<new-topic-name>'
TOPIC_ARN="$(aws --profile "$P" --region "$R" sns create-topic \
  --name "$TOPIC_NAME" --query TopicArn --output text)"
```

已有 topic 时直接设置 `TOPIC_ARN='<topic-arn>'`，不执行创建。读取并备份属性与订阅：

```bash
aws --profile "$P" --region "$R" sns get-topic-attributes \
  --topic-arn "$TOPIC_ARN" --output json > topic-before.json
aws --profile "$P" --region "$R" sns list-subscriptions-by-topic \
  --topic-arn "$TOPIC_ARN" --output json > subscriptions-before.json
```

核实 `FifoTopic` 不是 `true`，以及所有订阅者都允许收到本次测试。若 `KmsMasterKeyId` 非空，核对密钥允许 CloudWatch 服务发布加密消息所需的 `kms:GenerateDataKey*`、`kms:Decrypt`。AWS 托管密钥策略不能自行编辑；需要定制授权时使用合适的客户管理密钥，不通过关闭加密绕过错误。

仅在缺少目标订阅且用户确认后新增邮件订阅：

```bash
EMAIL='<recipient-email>'
aws --profile "$P" --region "$R" sns subscribe --topic-arn "$TOPIC_ARN" \
  --protocol email --notification-endpoint "$EMAIL" --return-subscription-arn
aws --profile "$P" --region "$R" sns list-subscriptions-by-topic \
  --topic-arn "$TOPIC_ARN" --output json
```

收件人必须点击确认邮件中的链接，直到订阅 ARN 不再是 `PendingConfirmation`。已有订阅还要检查其 `FilterPolicy` / `FilterPolicyScope`：纯文本测试消息和 CloudWatch JSON 通知可能命中过滤规则的不同分支，不能互相替代验证。

### 3. 生成告警计划，核实指标

以下默认配置每个目标生成两条告警，初始 `ActionsEnabled=false`。禁用动作期间仍会评估指标和改变告警状态。

| 目标 | 指标 / 统计量 | 示例阈值 | 周期与 M/N | 缺失数据 |
|---|---|---|---|---|
| EC2 | `CPUUtilization` / Average | ≥ 80% | 300 秒，2/3 | missing |
| EC2 | `StatusCheckFailed` / Maximum | ≥ 1 | 60 秒，2/3 | missing |
| RDS 实例 | `CPUUtilization` / Average | ≥ 80% | 300 秒，2/3 | missing |
| RDS 实例 | `FreeStorageSpace` / Minimum | ≤ 5 GiB，按字节配置 | 300 秒，2/3 | missing |
| Lambda | `Errors` / Sum | ≥ 1 | 300 秒，1/1 | notBreaching |
| Lambda | `Throttles` / Sum | ≥ 1 | 300 秒，1/1 | notBreaching |

M/N 表示 N 个评估周期中 M 个数据点越界；CloudWatch 会取更宽的评估范围处理缺失数据，不能把它视为精确倒计时。EC2 基础 CPU 指标通常按 5 分钟采样，不能仅把告警周期改为 60 秒就得到详细监控。内存与文件系统使用率需要安装并配置 CloudWatch Agent，核对 `CWAgent` 下的完整维度后另建告警。

Lambda 单次错误告警适合低频任务；高流量服务应评估错误率、最低请求量与 M/N，避免每个偶发错误都通知。`notBreaching` 不证明函数正常执行；定时任务还需单独检查漏执行。RDS 磁盘阈值要结合容量、增长速度和扩容耗时确定。

运行下面的本地生成器，不会访问 AWS：

```bash
python3 - "$KIND" "$TARGET" "$PREFIX" "$TOPIC_ARN" "$R" "$RUN_ID" <<'PY'
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

kind, target, prefix, topic, region, run_id = sys.argv[1:]
identity = json.loads(Path("identity.json").read_text())
account = identity["Account"]
parts = topic.split(":", 5)
if (len(parts) != 6 or parts[:3] != ["arn", "aws", "sns"]
        or parts[3:5] != [region, account] or parts[5].endswith(".fifo")):
    raise SystemExit("Use a standard SNS topic in the same commercial account and region")
if not re.fullmatch(r"[A-Za-z0-9_-]+", target) or not re.fullmatch(r"[A-Za-z0-9-]+", prefix):
    raise SystemExit("Use a resource short name and an ASCII alarm prefix")
specs = {
    "ec2": ("AWS/EC2", "InstanceId", [
        ("CPUUtilization", "Average", 80, 300, 2, 3, "missing", "GreaterThanOrEqualToThreshold"),
        ("StatusCheckFailed", "Maximum", 1, 60, 2, 3, "missing", "GreaterThanOrEqualToThreshold")]),
    "rds": ("AWS/RDS", "DBInstanceIdentifier", [
        ("CPUUtilization", "Average", 80, 300, 2, 3, "missing", "GreaterThanOrEqualToThreshold"),
        ("FreeStorageSpace", "Minimum", 5 * 1024**3, 300, 2, 3, "missing", "LessThanOrEqualToThreshold")]),
    "lambda": ("AWS/Lambda", "FunctionName", [
        ("Errors", "Sum", 1, 300, 1, 1, "notBreaching", "GreaterThanOrEqualToThreshold"),
        ("Throttles", "Sum", 1, 300, 1, 1, "notBreaching", "GreaterThanOrEqualToThreshold")]),
}
if kind not in specs:
    raise SystemExit("Unknown resource kind")
namespace, dimension, metrics = specs[kind]
names = []
now = datetime.now(timezone.utc)
for number, (metric, stat, threshold, period, m, n, missing, comparison) in enumerate(metrics, 1):
    name = f"{prefix}-{kind}-{target}-{metric}"
    if len(name) > 255:
        raise SystemExit("Alarm name is too long")
    dimensions = [{"Name": dimension, "Value": target}]
    alarm = {
        "AlarmName": name, "AlarmDescription": f"{kind} {metric} notification",
        "ActionsEnabled": False, "AlarmActions": [topic], "OKActions": [topic],
        "InsufficientDataActions": [], "Namespace": namespace, "MetricName": metric,
        "Dimensions": dimensions, "Statistic": stat, "Period": period,
        "EvaluationPeriods": n, "DatapointsToAlarm": m, "Threshold": threshold,
        "ComparisonOperator": comparison, "TreatMissingData": missing,
    }
    query = {
        "Namespace": namespace, "MetricName": metric, "Dimensions": dimensions,
        "StartTime": (now - timedelta(hours=1)).isoformat(), "EndTime": now.isoformat(),
        "Period": period, "Statistics": [stat],
    }
    Path(f"alarm-{number}.json").write_text(json.dumps(alarm, indent=2))
    Path(f"metric-{number}.json").write_text(json.dumps(query, indent=2))
    names.append(name)
test_name = f"{prefix}-notification-test-{run_id}"
if len(test_name) > 255:
    raise SystemExit("Test alarm name is too long")
Path("alarm-names.json").write_text(json.dumps({"AlarmNames": names}))
Path("notification-test.json").write_text(json.dumps({
    "AlarmName": test_name, "AlarmDescription": "Temporary notification validation",
    "ActionsEnabled": False, "AlarmActions": [topic], "OKActions": [topic],
    "InsufficientDataActions": [], "Namespace": "Noc/NotificationTest",
    "MetricName": "Signal", "Dimensions": [{"Name": "RunId", "Value": run_id}],
    "Statistic": "Maximum", "Period": 60, "EvaluationPeriods": 1, "DatapointsToAlarm": 1,
    "Threshold": 1, "ComparisonOperator": "GreaterThanOrEqualToThreshold",
    "TreatMissingData": "missing",
}, indent=2))
Path("planned-alarm-arns.json").write_text(json.dumps([
    f"arn:aws:cloudwatch:{region}:{account}:alarm:{name}" for name in names + [test_name]
]))
print("Created two disabled alarm plans, metric queries and a notification test plan")
PY
aws --profile "$P" --region "$R" cloudwatch get-metric-statistics \
  --cli-input-json file://metric-1.json --output json
aws --profile "$P" --region "$R" cloudwatch get-metric-statistics \
  --cli-input-json file://metric-2.json --output json
aws --profile "$P" --region "$R" cloudwatch describe-alarms \
  --alarm-types MetricAlarm CompositeAlarm --cli-input-json file://alarm-names.json \
  --output json > alarms-before.json
```

核实最近数据的单位、维度、统计量与基线。不要随意加 `Unit` 过滤：指定了错误单位也会查不到数据。长时间不活跃的指标可能不出现在 `list-metrics`；空结果也可能是维度错误、采集未启用或 Lambda 尚无错误，不能一律判正常。Lambda 稀疏错误指标可结合相同维度的 `Invocations` 和函数执行日志核验。

检查 `alarms-before.json`：若有同名告警，先确认归属及现有配置，再决定更新或改名。本剧本的创建步骤要求两个名称均未占用。`put-metric-alarm` 会完整覆盖已有告警配置，不能把它当作局部补丁；由 CloudFormation/Terraform 管理的告警应修改其源配置。

### 4. 给 CloudWatch 配置受限的 topic 发布权限

以下本地操作保留现有 topic 策略，只添加当前账号、当前区域和计划告警 ARN 的发布授权。复用共享 topic 时先与其管理者确认；如果由 IaC 管理，应把对应声明合入其源配置。

```bash
python3 - "$TOPIC_ARN" "$RUN_ID" <<'PY'
import json
import sys
from pathlib import Path
topic, run_id = sys.argv[1:]
attributes = json.loads(Path("topic-before.json").read_text())["Attributes"]
if attributes["TopicArn"] != topic or attributes.get("FifoTopic") == "true":
    raise SystemExit("Topic does not match the approved standard topic")
policy = json.loads(attributes["Policy"])
Path("topic-policy-before.json").write_text(json.dumps(policy, indent=2))
statements = policy.get("Statement", [])
if isinstance(statements, dict):
    statements = [statements]
sid = "NocCloudWatch" + run_id
if any(s.get("Sid") == sid for s in statements):
    raise SystemExit("Statement already exists; review before changing it")
statements.append({
    "Sid": sid, "Effect": "Allow", "Principal": {"Service": "cloudwatch.amazonaws.com"},
    "Action": "sns:Publish", "Resource": topic,
    "Condition": {
        "StringEquals": {"aws:SourceAccount": topic.split(":")[4]},
        "ArnEquals": {"aws:SourceArn": json.loads(Path("planned-alarm-arns.json").read_text())},
    },
})
policy["Statement"] = statements
Path("topic-policy.json").write_text(json.dumps(policy, indent=2))
PY
```

对比 `topic-policy-before.json` 与 `topic-policy.json`，确认只增加预期声明；应用前重新读取策略，若其他人已修改则重新合并，不覆盖并发变更。确认后执行：

```bash
aws --profile "$P" --region "$R" sns set-topic-attributes \
  --topic-arn "$TOPIC_ARN" --attribute-name Policy --attribute-value file://topic-policy.json
```

此策略不替代 KMS key policy，也不自动给当前操作者授予 `sns:Publish`。若组织策略要求加密，新 topic 应先按要求配置客户管理密钥及发布权限，再测试。

### 5. 验证 SNS 到收件人的通知

确认测试消息的收件范围后执行：

```bash
aws --profile "$P" --region "$R" sns publish --topic-arn "$TOPIC_ARN" \
  --subject 'CloudWatch notification channel test' \
  --message "Notification channel test: $RUN_ID" --output json
```

记录返回的 MessageId，收件人确认收到对应 `RUN_ID` 的消息；只返回 MessageId 不代表最终送达。这一步验证的是“当前操作者 → SNS → 收件人”，尚未证明 CloudWatch 服务能发布，也未验证阈值计算。共享 topic 的过滤策略如有意拒收纯文本消息，记录原因并继续核对实际 CloudWatch 通知格式，不随意删除过滤规则。

### 6. 用隔离的指标验证告警评估与通知

测试只向 `Noc/NotificationTest` 写自定义指标，不伪造 `AWS/*` 指标、不压测生产资源。每次测试使用独立 `RunId`，会产生一个自定义指标的费用；勿高频重复生成。测试前核对名称未占用和三个动作列表只包含已确认的 SNS topic。

```bash
TEST_ALARM="$(python3 -c 'import json; print(json.load(open("notification-test.json"))["AlarmName"])')"
aws --profile "$P" --region "$R" cloudwatch describe-alarms \
  --alarm-names "$TEST_ALARM" --alarm-types MetricAlarm CompositeAlarm --output json
# 仅在确认名称未占用后创建
aws --profile "$P" --region "$R" cloudwatch put-metric-alarm \
  --cli-input-json file://notification-test.json
aws --profile "$P" --region "$R" cloudwatch enable-alarm-actions --alarm-names "$TEST_ALARM"
aws --profile "$P" --region "$R" cloudwatch put-metric-data \
  --namespace Noc/NotificationTest \
  --metric-data "MetricName=Signal,Dimensions=[{Name=RunId,Value=$RUN_ID}],Value=0,Unit=Count"
aws --profile "$P" --region "$R" cloudwatch describe-alarms \
  --alarm-names "$TEST_ALARM" --output json
```

等到读取到 `OK` 后，再写入越界值。数据和状态传播需要时间，未达到预期时先排查，不直接跳到下一步：

```bash
aws --profile "$P" --region "$R" cloudwatch put-metric-data \
  --namespace Noc/NotificationTest \
  --metric-data "MetricName=Signal,Dimensions=[{Name=RunId,Value=$RUN_ID}],Value=1,Unit=Count"
aws --profile "$P" --region "$R" cloudwatch describe-alarm-history \
  --alarm-name "$TEST_ALARM" --history-item-type StateUpdate --output json
aws --profile "$P" --region "$R" cloudwatch describe-alarm-history \
  --alarm-name "$TEST_ALARM" --history-item-type Action --output json
```

确认状态进入 `ALARM`、动作历史显示 SNS 发布成功，并由收件人确认消息中的告警名和区域。随后至少等到下一个完整分钟，再写 `Value=0`；必要时每分钟补一个 0，直到看到真实评估回到 `OK` 并收到恢复通知。同一分钟的 Maximum 仍可能保留之前的 1。

本测试用 `missing`，停止发指标后可能转为 `INSUFFICIENT_DATA`；这不是恢复成功。验收应保存状态历史、动作历史和收件确认，而不是只看某一刻控制台颜色。

`set-alarm-state` 只临时改变状态，可能立即触发动作，且很快被真实指标评估覆盖；它不能替代上述指标链路测试。不要通过降低生产阈值或触发真实故障来完成通知验证。

### 7. 创建真实告警，检查后启用

先检查并按实际基线调整 `alarm-1.json`、`alarm-2.json`。重新确认名称未占用后，创建禁用动作的告警：

```bash
aws --profile "$P" --region "$R" cloudwatch put-metric-alarm --cli-input-json file://alarm-1.json
aws --profile "$P" --region "$R" cloudwatch put-metric-alarm --cli-input-json file://alarm-2.json
aws --profile "$P" --region "$R" cloudwatch describe-alarms \
  --cli-input-json file://alarm-names.json --output json > alarms-created.json
```

核对返回的 namespace、完整维度、统计量、单位、周期、M/N、阈值和缺失数据策略，以及 `AlarmActions`、`OKActions`、`InsufficientDataActions`。确认只有预期 SNS 动作；观察真实指标评估结果后，获得启用确认：

```bash
aws --profile "$P" --region "$R" cloudwatch enable-alarm-actions \
  --cli-input-json file://alarm-names.json
aws --profile "$P" --region "$R" cloudwatch describe-alarms \
  --cli-input-json file://alarm-names.json --output json
```

SNS 通知通常在状态转换时触发，不会每个周期重复发送。禁用动作时已经发生的 `ALARM`，不能指望重新启用后补发；它仍需人工处理当前异常。临时指标测试证明通知链路有效，真实资源的指标、阈值及业务含义还需分别验证。

验收记录应包含：两条真实告警 ARN、目标与完整维度、阈值与 M/N、缺失策略、SNS topic、订阅确认状态、最近指标证据，以及测试的 `OK → ALARM → OK` 历史和收件确认。

### 8. 排障、修改与停用

| 现象 | 重点检查 |
|---|---|
| 一直 `INSUFFICIENT_DATA` | 账号/区域、namespace、所有维度、单位、采样频率、Agent 是否上传、资源是否停止 |
| Lambda 没有错误数据但告警为 OK | `notBreaching` 把缺失视作不越界；另看 Invocations 与日志，不能据此断言任务执行过 |
| 人工 SNS 消息可达，告警通知不可达 | CloudWatch 服务发布权限、SourceArn 是否包含实际告警、KMS key policy、订阅过滤规则 |
| ALARM 后没有动作历史 | `ActionsEnabled`、AlarmActions、是否真的发生了状态转换、是否在禁用期间已变为 ALARM |
| 动作成功但没收到消息 | 订阅确认、过滤规则、邮箱垃圾箱、SNS 投递失败指标；HTTP/SQS/Lambda 订阅需进一步检查端点 |
| 告警反复抖动 | 指标延迟、周期、M/N、阈值、缺失策略，按业务基线调整，不统一改成 notBreaching |
| 内存或磁盘使用率没有数据 | EC2 默认指标不提供这些主机指标；核对 Agent 与路径/设备等维度 |

修改前备份 `describe-alarms` 返回配置；其输出包含只读字段，不能直接当作 `put-metric-alarm` 输入回灌。保留经过审查的原始输入文件，修改完整配置后再应用；IaC 管理的资源走原有变更流程。

临时停用通知时，确认告警范围后执行：

```bash
aws --profile "$P" --region "$R" cloudwatch disable-alarm-actions \
  --cli-input-json file://alarm-names.json
```

记录恢复时间与负责人。禁用动作不会停止状态评估；恢复用 `enable-alarm-actions`，并重新查看当前异常，不能把“无邮件”当成系统健康。

### 9. 清理测试资源与撤销配置

验证结束，保存测试证据后删除本次测试告警：

```bash
aws --profile "$P" --region "$R" cloudwatch disable-alarm-actions --alarm-names "$TEST_ALARM"
aws --profile "$P" --region "$R" cloudwatch delete-alarms --alarm-names "$TEST_ALARM"
```

停止向测试维度发数据。CloudWatch 自定义指标不能立即手动删除，将按服务保留规则过期；删除告警不会删除指标历史。

真实告警保留运行。只有用户明确要求撤销时，确认没有复合告警或其他系统引用后，禁用动作并用 `delete-alarms --cli-input-json file://alarm-names.json` 删除本次两条告警。

topic 策略中的本次声明保留真实告警 ARN，移除测试 ARN；全部撤销时才移除本次 Sid。重新读取当前策略并合并，保留其他人新增的声明，不直接用旧快照覆盖。共享 topic 和原有订阅不删除；本次新建的专用 topic 也要核实无其他发布者和订阅用途，才另行确认删除。保留 `topic-before.json`、变更后策略、告警输入与验收记录作为回退依据。

每次写操作完成后按实际动作记账，例如：

```bash
ops log cloudwatch-alarm-create "$TARGET" --cloud aws --source cli \
  --detail "region=$R; kind=$KIND; actions=disabled; prefix=$PREFIX"
```

测试、启用、修改、禁用和删除分别记录，结果与 `--source` 按实际情况填写。

## 注意事项

- 阈值、邮件地址、资源标识和执行结果留在用户本地；通知正文不要放凭证或敏感业务数据。
- 告警、自定义指标、SNS 和 KMS 可能分别产生费用；部署前按数量与频率核算，不承诺免费。
- 资源删除后应检查对应告警、订阅及策略是否仍需要；普通指标告警不会因为目标资源消失就自动删除。
- 本剧本只创建两条基础告警，不代表完整监控覆盖。Lambda 延迟可按 Duration 的 p95/p99 与超时预算配置；定时任务还应监控漏执行、异步事件丢弃及失败队列。

## 官方文档

- [CloudWatch 告警](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/CloudWatch_Alarms.html)
- [通知与 SNS 资源策略](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/Notify_Users_Alarm_Changes.html)
- [PutMetricAlarm 创建和更新语义](https://docs.aws.amazon.com/cli/latest/reference/cloudwatch/put-metric-alarm.html)
- [SetAlarmState 测试行为](https://docs.aws.amazon.com/cli/latest/reference/cloudwatch/set-alarm-state.html)
- [SNS 加密权限](https://docs.aws.amazon.com/sns/latest/dg/sns-key-management.html)
- [EC2 指标](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/viewing_metrics_with_cloudwatch.html)
- [RDS 指标](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/rds-metrics.html)
- [Lambda 指标](https://docs.aws.amazon.com/lambda/latest/dg/monitoring-metrics-types.html)

## 相关剧本

- [[aws-lambda-scheduled-task]]
- [[ec2-public-exposure-hardening]]
