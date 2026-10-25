# 判断某个 S3 桶是否还在用（删前必查）

## 触发意图
- "xxx 这个 s3 bucket 还在用吗 / 是不是在用？"
- "这个桶能不能删 / 还有没有人往里写？"
- "某某 bucket 干啥用的、谁在写它"

## 目标解析
桶名本身常带强提示，按命名拆解归属：
- `*-123456789012` 等纯数字后缀 = **AWS 账号 ID**。
- `*-alb-logs-*` / `*-lb-logs-*` = ALB/ELB 访问日志；`cloudtrail*` = CloudTrail；`*-cf-logs*` = CloudFront；`*-backup*`、`*-tfstate*` 同理。
- 桶 region 用 `aws s3api get-bucket-location --bucket <B> --profile <P>` 确认（中国区账号会返回 `cn-north-1` / `cn-northwest-1`）。

判断"在用"分两端，**两端都要看**：
1. **还在不在写**（消费侧/新鲜度）：最新对象的时间。
2. **谁在写**（生产侧）：哪个服务把它当输出目标——这是最硬的证据，新鲜度只是表象。

## 执行步骤

### 1.（只读）桶基本信息 + 存量 + 最新对象
```bash
B=<桶名>; P=<账号 profile>; R=<region，中国区填 cn-northwest-1 等>
aws s3api get-bucket-location --bucket $B --profile $P --output text          # 确认 region
aws s3 ls s3://$B --recursive --region $R --profile $P --summarize | tail -3  # 对象数 + 总大小
aws s3 ls s3://$B --recursive --region $R --profile $P | tail -15             # 最新对象（key 含日期分区时，tail = 最近）
```
- **最新对象是近期/持续产出** → 活跃。日志类桶（ALB/CloudFront/CloudTrail）会按固定节奏投递（ALB 约每 5 分钟一批）。
- key 路径里能读出生产者：ALB 日志 = `…/AWSLogs/<account>/elasticloadbalancing/<region>/YYYY/MM/DD/…_app.<alb名>.<id>_…`，从中拿到 **ALB 名**。

### 2.（只读）反查生产者，确认它确实指向这个桶
ALB/ELB 访问日志：
```bash
ARN=$(aws elbv2 describe-load-balancers --profile $P --region $R \
  --query "LoadBalancers[?LoadBalancerName=='<alb名>'].LoadBalancerArn" --output text)
aws elbv2 describe-load-balancer-attributes --load-balancer-arn "$ARN" --profile $P --region $R \
  --query "Attributes[?starts_with(Key,'access_logs')]" --output table
# 看 access_logs.s3.enabled=true 且 access_logs.s3.bucket=<桶名>
```
其它常见生产者的反查入口：
- **CloudFront**：`aws cloudfront get-distribution-config --id <id>`，看 `Logging.Bucket`。
- **CloudTrail**：`aws cloudtrail describe-trails`，看 `S3BucketName`。
- **S3 服务端访问日志**：`aws s3api get-bucket-logging --bucket <源桶>`，看 `TargetBucket`。
- **没找到任何生产者，但最新对象很旧** → 大概率是历史遗留，可进入"清理"判断。

## 是否写操作
否，全程只读（get-bucket-location / s3 ls / describe-*）。**结论若是"删桶"则属高危写操作，必须先回显计划再确认。**

## 注意事项
- **AWS 中国区（china 分区）必带 `--region`**：`get-bucket-location` 能成功（会重定向），但 `s3 ls` 不带 region 会去错误端点，报 `NoSuchBucket` 假阴性——别被它误导成"桶不存在"。
- **在用 ≠ 不能动**：日志/备份桶要瘦身就**配生命周期规则**（`put-bucket-lifecycle-configuration`，如 30/90 天转 IA 或过期删除），**不要删桶**——删桶会让 ALB 日志投递、CloudTrail 写入等直接失败。
- 删桶前还要确认无引用：生产者已关闭、无其它服务/脚本读它、桶策略里没有别的 principal 在写。
- **示例**：一个 `*-alb-logs-*` 桶，反查发现某生产 ALB 的 `access_logs.s3.enabled=true` 且每 5 分钟投递 → **在用、不可删**；若要彻底下线，按「先停生产者 → 核实无引用 → 删桶」的顺序（见同目录 [[alb-access-log-decommission]]），顺序不能反，否则 ALB 日志投递会失败。
