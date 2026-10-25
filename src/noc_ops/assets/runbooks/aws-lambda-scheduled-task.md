# 使用 EventBridge Scheduler + Lambda 创建定时任务

## 触发意图

- “每天检查一次云资源状态 / 每隔一段时间运行一段 Python”
- “用 Lambda 建一个定时任务，不依赖本机开机”
- “定时任务没执行、执行失败、重复执行，怎么排查或停用？”

## 目标解析

Scheduler 负责触发时间，Lambda 负责执行逻辑。本剧本用同账号、同区域的 EC2 状态检查演示完整流程：只读取指定实例，输出结构化日志，不启停实例。

- 从 `inventory.md` / `data.json` 的 `instances` 确定目标；也可用 `ops find <目标> --cloud aws --kind instances --json`。必须核实 profile、账号、区域和实例 ID；多条匹配时让用户选择。
- 确定任务频率、IANA 时区、输入、最大执行时间、重试范围及日志保留时间。本模板默认每天 UTC 09:00，创建后保持禁用。
- 只调用一个 Scheduler 支持的 AWS API 时，优先直接配置其通用目标；需要条件判断、数据处理或自定义 API 调用时使用 Lambda。已有 `noc` 定时重启能力可直接复用。
- 以下模板面向 AWS 商业分区；其他分区先核对服务、运行时和服务主体支持。需要 AWS CLI v2、Python 3（用于检查调用结果）以及已登录的 profile。
- 部署身份需要操作 CloudFormation、IAM、Lambda、Scheduler、CloudWatch Logs、SQS，并能 `iam:PassRole` 传递本栈角色；不要为跑示例直接附加管理员权限。运行身份与部署身份分开。

## 是否写操作

查询身份、实例状态、栈状态和日志为只读。创建/更新栈、手动调用函数、启用/禁用调度、删除资源属于写操作：执行前回显账号、区域、目标、时间表达式、时区、权限和资源清单，等待确认。确认建栈不等于确认启用；分步骤执行，不把全文当作一段脚本运行。

## 执行步骤

### 1. 核实身份与时间

在一个单独的本地目录保存模板和执行结果；其中可能包含运行环境信息，不提交到公开仓库。以下命令在同一终端逐步执行，任何一步失败都先停止排查。

```bash
P='<profile>'
R='<region>'
INSTANCE_ID='<instance-id>'
STACK='scheduled-resource-check'  # 新建栈名，控制在 50 字符内
EXPR='cron(0 9 * * ? *)'
SCHEDULE_TZ='Etc/UTC'             # 按需改为 Asia/Shanghai、Asia/Tokyo 等
aws --profile "$P" --region "$R" sts get-caller-identity
aws --profile "$P" --region "$R" ec2 describe-instances \
  --instance-ids "$INSTANCE_ID" \
  --query 'Reservations[].Instances[].{Id:InstanceId,State:State.Name}'
```

| 需求 | 表达式 | 注意 |
|---|---|---|
| 每 15 分钟 | `rate(15 minutes)` | 固定间隔；未设置开始时间时，启用后可能立即触发 |
| 每天当地时间 09:00 | `cron(0 9 * * ? *)` | AWS cron 有 6 段，日与星期字段其中一个用 `?` |
| 指定时刻执行一次 | `at(<YYYY-MM-DD>T<HH:MM:SS>)` | 换成所选时区内的未来时间，不带 `Z` 或偏移量 |

Scheduler 为分钟级精度，关闭弹性窗口也不是秒级准点。夏令时地区会出现跳过不存在的时间、重复小时只执行一次的行为；`rate(1 day)` 是 24 小时间隔，不等同于每日当地固定时刻。

### 2. 保存模板

将下列内容保存为 `scheduled-task.yaml`。资源包括函数、两个最小权限角色、调度组与调度、日志组，以及分别接收投递失败和执行失败的两个标准 SQS 队列。

```yaml
AWSTemplateFormatVersion: '2010-09-09'
Description: Scheduled read-only EC2 state check with separate delivery and execution failure queues.
Parameters:
  InstanceId:
    Type: AWS::EC2::Instance::Id
  ScheduleExpression:
    Type: String
    Default: 'cron(0 9 * * ? *)'
  ScheduleTimezone:
    Type: String
    Default: Etc/UTC
  ScheduleState:
    Type: String
    Default: DISABLED
    AllowedValues: [DISABLED, ENABLED]
Resources:
  DeliveryFailures:
    Type: AWS::SQS::Queue
    DeletionPolicy: Retain
    UpdateReplacePolicy: Retain
    Metadata:
      com.aws.cloudformation.Context:
        why: Preserve events that Scheduler could not deliver.
        must: [Keep separate from function execution failures.]
    Properties:
      SqsManagedSseEnabled: true
      MessageRetentionPeriod: 1209600
  ExecutionFailures:
    Type: AWS::SQS::Queue
    DeletionPolicy: Retain
    UpdateReplacePolicy: Retain
    Metadata:
      com.aws.cloudformation.Context:
        why: Preserve Lambda asynchronous failure records.
        must: [Retained messages still expire after 14 days.]
    Properties:
      SqsManagedSseEnabled: true
      MessageRetentionPeriod: 1209600
  QueueTransportPolicy:
    Type: AWS::SQS::QueuePolicy
    Properties:
      Queues: [!Ref DeliveryFailures, !Ref ExecutionFailures]
      PolicyDocument:
        Version: '2012-10-17'
        Statement:
          - Effect: Deny
            Principal: '*'
            Action: 'sqs:*'
            Resource: [!GetAtt DeliveryFailures.Arn, !GetAtt ExecutionFailures.Arn]
            Condition:
              Bool:
                aws:SecureTransport: 'false'
  FunctionLogs:
    Type: AWS::Logs::LogGroup
    DeletionPolicy: Retain
    UpdateReplacePolicy: Retain
    Metadata:
      com.aws.cloudformation.Context:
        why: Keep execution evidence independently of the function lifecycle.
        must: [Name must match the function; events expire after 14 days.]
    Properties:
      LogGroupName: !Sub '/aws/lambda/${AWS::StackName}-check'
      RetentionInDays: 14
  FunctionRole:
    Type: AWS::IAM::Role
    Metadata:
      com.aws.cloudformation.Context:
        why: Allow regional EC2 reads, function logs and failure record delivery.
        must: [No instance mutation permissions.]
    Properties:
      AssumeRolePolicyDocument:
        Version: '2012-10-17'
        Statement:
          - Effect: Allow
            Principal: {Service: lambda.amazonaws.com}
            Action: sts:AssumeRole
      Policies:
        - PolicyName: ReadStateAndReport
          PolicyDocument:
            Version: '2012-10-17'
            Statement:
              - Effect: Allow
                Action: ec2:DescribeInstances
                Resource: '*'
                Condition:
                  StringEquals:
                    aws:RequestedRegion: !Ref AWS::Region
              - Effect: Allow
                Action: [logs:CreateLogStream, logs:PutLogEvents]
                Resource: !GetAtt FunctionLogs.Arn
              - Effect: Allow
                Action: sqs:SendMessage
                Resource: !GetAtt ExecutionFailures.Arn
  CheckFunction:
    Type: AWS::Lambda::Function
    Metadata:
      com.aws.cloudformation.Context:
        why: Read a configured instance and emit its state as JSON.
        must: [Read-only handler tolerates duplicate invocations.]
    Properties:
      FunctionName: !Sub '${AWS::StackName}-check'
      Runtime: python3.12
      Handler: index.handler
      Role: !GetAtt FunctionRole.Arn
      Timeout: 60
      MemorySize: 128
      Environment:
        Variables:
          INSTANCE_ID: !Ref InstanceId
      Code:
        ZipFile: |
          import json
          import os
          import boto3
          from botocore.config import Config

          ec2 = boto3.client("ec2", config=Config(
              connect_timeout=3, read_timeout=5,
              retries={"mode": "standard", "total_max_attempts": 2},
          ))

          def handler(event, context):
              instance_id = os.environ["INSTANCE_ID"]
              result = ec2.describe_instances(InstanceIds=[instance_id])
              instances = [i for r in result["Reservations"] for i in r["Instances"]]
              if len(instances) != 1:
                  raise RuntimeError("Expected exactly one instance")
              record = {
                  "job": "ec2-state-check",
                  "scheduled_time": event.get("scheduled_time"),
                  "request_id": context.aws_request_id,
                  "instance_id": instance_id,
                  "state": instances[0]["State"]["Name"],
              }
              print(json.dumps(record))
              return record
  AsyncInvocation:
    Type: AWS::Lambda::EventInvokeConfig
    Properties:
      FunctionName: !Ref CheckFunction
      Qualifier: '$LATEST'
      MaximumEventAgeInSeconds: 3600
      MaximumRetryAttempts: 2
      DestinationConfig:
        OnFailure:
          Destination: !GetAtt ExecutionFailures.Arn
  ScheduleGroup:
    Type: AWS::Scheduler::ScheduleGroup
  SchedulerRole:
    Type: AWS::IAM::Role
    Metadata:
      com.aws.cloudformation.Context:
        why: Deliver scheduled events only to this function and its delivery queue.
        must: [Trust SourceArn is a schedule group ARN, not a schedule ARN.]
    Properties:
      AssumeRolePolicyDocument:
        Version: '2012-10-17'
        Statement:
          - Effect: Allow
            Principal: {Service: scheduler.amazonaws.com}
            Action: sts:AssumeRole
            Condition:
              StringEquals:
                aws:SourceAccount: !Ref AWS::AccountId
                aws:SourceArn: !GetAtt ScheduleGroup.Arn
      Policies:
        - PolicyName: DeliverScheduledCheck
          PolicyDocument:
            Version: '2012-10-17'
            Statement:
              - Effect: Allow
                Action: lambda:InvokeFunction
                Resource: !GetAtt CheckFunction.Arn
              - Effect: Allow
                Action: sqs:SendMessage
                Resource: !GetAtt DeliveryFailures.Arn
  Schedule:
    Type: AWS::Scheduler::Schedule
    DependsOn: [AsyncInvocation, QueueTransportPolicy]
    Properties:
      GroupName: !Ref ScheduleGroup
      ScheduleExpression: !Ref ScheduleExpression
      ScheduleExpressionTimezone: !Ref ScheduleTimezone
      State: !Ref ScheduleState
      FlexibleTimeWindow: {Mode: 'OFF'}
      Target:
        Arn: !GetAtt CheckFunction.Arn
        RoleArn: !GetAtt SchedulerRole.Arn
        Input: '{"scheduled_time":"<aws.scheduler.scheduled-time>"}'
        RetryPolicy:
          MaximumEventAgeInSeconds: 3600
          MaximumRetryAttempts: 2
        DeadLetterConfig:
          Arn: !GetAtt DeliveryFailures.Arn
Outputs:
  FunctionName:
    Value: !Ref CheckFunction
  LogGroup:
    Value: !Ref FunctionLogs
  ScheduleName:
    Value: !Ref Schedule
  ScheduleGroupName:
    Value: !Ref ScheduleGroup
  DeliveryQueueUrl:
    Value: !Ref DeliveryFailures
  ExecutionQueueUrl:
    Value: !Ref ExecutionFailures
```

`ec2:DescribeInstances` 不支持按单个实例 ARN 限权，所以使用 `Resource: '*'` 并限制区域；指定实例 ID 是代码约束，不是 IAM 隔离。其余允许权限分别限定到本函数、日志组和队列。Scheduler 调用同账号函数使用执行角色，不需要套用旧 EventBridge Rule 的 `events.amazonaws.com` 调用授权。

内联 Python 使用 Lambda 运行时自带的 boto3；有额外依赖或需要锁定 SDK 版本时，改用打包 ZIP/镜像并一起管理依赖。此例不配置 VPC：通过 AWS API 查询 EC2 状态不需要连接实例所在子网。

### 3. 预览并创建禁用状态的任务

先确认目标区域支持模板运行时。首次创建使用新栈名；已有同名栈时先查明归属，不覆盖。下面会创建变更集和待创建栈记录，但不会创建函数或启用任务。

```bash
aws --profile "$P" --region "$R" cloudformation validate-template \
  --template-body file://scheduled-task.yaml
python3 - "$INSTANCE_ID" "$EXPR" "$SCHEDULE_TZ" <<'PY'
import json
import sys
from pathlib import Path
values = dict(zip(("InstanceId", "ScheduleExpression", "ScheduleTimezone"), sys.argv[1:]))
values["ScheduleState"] = "DISABLED"
Path("parameters.json").write_text(json.dumps([
    {"ParameterKey": key, "ParameterValue": value} for key, value in values.items()
]))
PY
CHANGESET="create-$(date -u +%Y%m%dT%H%M%SZ)"
aws --profile "$P" --region "$R" cloudformation create-change-set \
  --stack-name "$STACK" --change-set-name "$CHANGESET" --change-set-type CREATE \
  --template-body file://scheduled-task.yaml --capabilities CAPABILITY_IAM \
  --parameters file://parameters.json
aws --profile "$P" --region "$R" cloudformation wait change-set-create-complete \
  --stack-name "$STACK" --change-set-name "$CHANGESET"
aws --profile "$P" --region "$R" cloudformation describe-change-set \
  --stack-name "$STACK" --change-set-name "$CHANGESET"
```

检查变更集的状态、资源、IAM 权限和成本范围；失败时查看 `StatusReason` 及 CloudFormation 事件，解决后重新预览。用户确认具体变更后执行：

```bash
aws --profile "$P" --region "$R" cloudformation execute-change-set \
  --stack-name "$STACK" --change-set-name "$CHANGESET"
aws --profile "$P" --region "$R" cloudformation wait stack-create-complete \
  --stack-name "$STACK"
aws --profile "$P" --region "$R" cloudformation describe-stacks \
  --stack-name "$STACK" --query 'Stacks[0].Outputs' --output json > stack-outputs.json
```

### 4. 手动测试，再确认启用

先保存输出中的日志组和两个队列 URL，后续清理需要用到。确认测试函数仍是只读示例，然后调用：

```bash
FUNCTION_NAME="${STACK}-check"
aws --profile "$P" --region "$R" lambda invoke \
  --function-name "$FUNCTION_NAME" --invocation-type RequestResponse --output json \
  --cli-binary-format raw-in-base64-out --payload '{"scheduled_time":"manual"}' \
  invocation-result.json > invocation-meta.json
python3 - <<'PY'
import json
from pathlib import Path
meta = json.loads(Path("invocation-meta.json").read_text())
if meta.get("StatusCode") != 200 or meta.get("FunctionError"):
    raise SystemExit("Invocation failed; inspect invocation-result.json locally")
result = json.loads(Path("invocation-result.json").read_text())
if result.get("job") != "ec2-state-check" or not result.get("state"):
    raise SystemExit("Unexpected function result")
print("Function check passed")
PY
aws --profile "$P" --region "$R" logs tail "/aws/lambda/$FUNCTION_NAME" --since 10m
```

CLI 返回成功不代表函数成功，必须检查 `FunctionError` 和响应内容；这次同步调用只验证函数，不验证调度或异步失败路径。

回显表达式、时区和下一次预期执行时间，获得启用确认后，创建 UPDATE 变更集。保留其他参数，避免启用时改动目标：

```bash
CHANGESET="enable-$(date -u +%Y%m%dT%H%M%SZ)"
aws --profile "$P" --region "$R" cloudformation create-change-set \
  --stack-name "$STACK" --change-set-name "$CHANGESET" --change-set-type UPDATE \
  --use-previous-template --capabilities CAPABILITY_IAM \
  --parameters 'ParameterKey=InstanceId,UsePreviousValue=true' \
    'ParameterKey=ScheduleExpression,UsePreviousValue=true' \
    'ParameterKey=ScheduleTimezone,UsePreviousValue=true' \
    'ParameterKey=ScheduleState,ParameterValue=ENABLED'
aws --profile "$P" --region "$R" cloudformation wait change-set-create-complete \
  --stack-name "$STACK" --change-set-name "$CHANGESET"
aws --profile "$P" --region "$R" cloudformation describe-change-set \
  --stack-name "$STACK" --change-set-name "$CHANGESET"
```

确认变更集仅包含预期变更后执行，并等待更新完成：

```bash
aws --profile "$P" --region "$R" cloudformation execute-change-set \
  --stack-name "$STACK" --change-set-name "$CHANGESET"
aws --profile "$P" --region "$R" cloudformation wait stack-update-complete \
  --stack-name "$STACK"
```

用输出中的 ScheduleName 和 ScheduleGroupName 执行 `aws scheduler get-schedule --name <name> --group-name <group> --profile <profile> --region <region>`，核对 `State`、表达式、时区及目标。等到一次真实触发后，确认日志中的 `scheduled_time` 不是 `manual`，且实例状态与预期一致。长周期任务可先在独立测试栈用 `rate(5 minutes)` 验证，再按正式周期创建任务。

### 5. 监控、重试与排障

Scheduler 异步调用 Lambda。两个阶段分别重试；不能用“Scheduler 已投递”判断业务成功，也不能只配置 Scheduler DLQ 就认为函数错误已被收集。

| 现象 | 检查项 |
|---|---|
| 没有触发 | 调度是否启用、区域/组名、时区/表达式、一次性时间是否已过去 |
| Scheduler 无法投递 | Scheduler 执行角色信任策略、`lambda:InvokeFunction`、投递失败队列及 Scheduler 错误指标 |
| 函数启动后报错 | CloudWatch Logs、Lambda `Errors` / `Throttles` / `Duration`、执行角色的 EC2 权限 |
| 异步事件最终失败 | 执行失败队列、`AsyncEventsDropped`、`DestinationDeliveryFailures`；确认函数角色能写队列 |
| 重复执行 | 调度重试、Lambda 异步重试、函数运行时间是否超过调度间隔；不要承诺恰好执行一次 |

投入持续运行前，将 Lambda 错误/节流、两条队列的 `ApproximateNumberOfMessagesVisible`、失败记录投递异常接入现有 CloudWatch 告警与通知渠道。没有通知动作的告警只改变状态，不会通知人。两个队列不是函数触发源，不要自动绑定回原函数而形成重试循环。

本例只读，重复调用不会修改目标实例，但可能产生重复日志。改为写业务时，使用“任务标识 + 计划执行时间”等稳定键做持久化幂等检查；请求 ID 每次不同，不能用作跨重试幂等键。并发上限不是去重锁；有重叠风险时需单独设计锁或租约。

修改函数/模板前，保存当前模板、参数和代码版本，创建变更集审查。生产任务需要版本回退时，应发布 Lambda 版本并让调度指向 alias，同时将调用权限及异步配置绑定到该 alias。本例使用 `$LATEST`，不提供自动回退保证。

### 6. 停用与清理

1. 复用第 4 步 UPDATE 变更集流程，把名称改为 `disable-...`，将 `ScheduleState` 设为 `DISABLED`；审查并执行后确认调度已禁用。
2. 禁用不会取消已投递事件或正在运行的函数。检查日志、异步事件及队列，等待在途工作处理结束；紧急停止有副作用的业务需要函数侧停机检查。
3. 一次性任务完成后也要清理。本模板由 CloudFormation 管理，未配置完成后自动删除调度，以免栈产生漂移。
4. 确认栈内资源没有被其他任务使用，保存 `stack-outputs.json`、模板、参数及需要保留的日志/失败记录。获得删除确认后执行：

```bash
aws --profile "$P" --region "$R" cloudformation delete-stack --stack-name "$STACK"
aws --profile "$P" --region "$R" cloudformation wait stack-delete-complete \
  --stack-name "$STACK"
```

日志组和两个队列设置了 `Retain`，删除栈后仍然存在且可能继续计费；日志和消息仍按各自的 14 天保留期过期，`Retain` 不是永久归档。取证或迁移后，再单独确认是否删除这些保留资源。不要复用原栈名直接重建，否则保留的同名日志组可能导致创建失败。

每次写操作完成后按实际动作记账，例如：

```bash
ops log scheduled-task-create "$STACK" --cloud aws --source cli \
  --detail "region=$R; schedule=$EXPR; timezone=$SCHEDULE_TZ; state=DISABLED"
```

启用、修改、禁用和删除分别使用对应动作名，并如实记录结果；由客户端执行时使用实际客户端的 `--source`。

## 注意事项

- 调度输入、函数环境变量、日志与失败记录不放密钥。需要秘密时，用执行角色按需读取 Secrets Manager / Parameter Store，并限制到具体资源。
- Lambda 普通函数单次运行最长 15 分钟；长任务或复杂流程另选执行方式。此例超时为 60 秒，SDK 重试和网络超时也应计入预算。
- 模板没有设置并发预留，避免消耗账号预留额度；按频率、执行耗时、重试规模评估并发和费用。Lambda、Scheduler、日志、SQS 分别可能收费，不承诺免费。
- 示例资源仅查询运行状态，不等同于业务健康检查；停止状态会正常记录，业务告警条件需自行定义。
- 创建变更集不是离线检查，`validate-template` 也不验证业务逻辑或全部 IAM 权限。先在获准的测试环境验证，再部署实际任务。

## 官方文档

- [Scheduler 调用 Lambda](https://docs.aws.amazon.com/lambda/latest/dg/with-eventbridge-scheduler.html)
- [时间表达式、时区与夏令时](https://docs.aws.amazon.com/scheduler/latest/UserGuide/schedule-types.html)
- [Scheduler 执行角色信任范围](https://docs.aws.amazon.com/scheduler/latest/UserGuide/cross-service-confused-deputy-prevention.html)
- [Lambda 异步错误与重试](https://docs.aws.amazon.com/lambda/latest/dg/invocation-async-error-handling.html)
- [Lambda 异步失败记录](https://docs.aws.amazon.com/lambda/latest/dg/invocation-async-retain-records.html)
- [Lambda Python 依赖打包](https://docs.aws.amazon.com/lambda/latest/dg/python-package.html)

## 相关剧本

- [[aws-cloudwatch-alarm-notification]]
- [[ec2-public-exposure-hardening]]
- [[_TEMPLATE]]
