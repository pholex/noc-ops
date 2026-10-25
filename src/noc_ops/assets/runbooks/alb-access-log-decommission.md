# 停用 ALB 访问日志并删除其日志桶（下线）

## 触发意图
- "xxx ALB 可以关闭访问日志了，然后删掉日志桶"
- "不要 ALB 访问日志了 / 把 ALB 日志和桶清理掉"
- "这个 alb-logs 桶下线"

## 目标解析
- ALB：拿到名字 → ARN。`aws elbv2 describe-load-balancers --profile <P> --region <R> --query "LoadBalancers[?LoadBalancerName=='<名>'].LoadBalancerArn" --output text`
- 桶：通常就是 `access_logs.s3.bucket` 属性里的那个；先 `describe-load-balancer-attributes` 确认。
- **分区/区域**：AWS 中国区账号务必带 `--region`（如 `cn-northwest-1`），否则 s3 操作会假报 `NoSuchBucket`（见 [[s3-bucket-usage-check]]）。

## 执行步骤（顺序不可颠倒：先停生产者，再删桶）

### 1.（写操作，需确认）停用 ALB 访问日志
```bash
ARN="<alb-arn>"; P=<profile>; R=<region>
aws elbv2 modify-load-balancer-attributes --load-balancer-arn "$ARN" --profile $P --region $R \
  --attributes Key=access_logs.s3.enabled,Value=false \
  --query "Attributes[?starts_with(Key,'access_logs')]" --output table
# 期望 access_logs.s3.enabled=false（bucket/prefix 值会保留，但已不再写入）
```

### 2.（只读）核实**没有别的生产者**还在写这个桶（删桶前的命门）
```bash
# 同区域所有 ALB 的日志目标，确认没有第二个指向此桶
for a in $(aws elbv2 describe-load-balancers --profile $P --region $R --query "LoadBalancers[].LoadBalancerArn" --output text); do
  name=$(echo "$a" | sed -E 's#.*loadbalancer/app/([^/]+)/.*#\1#')
  en=$(aws elbv2 describe-load-balancer-attributes --load-balancer-arn "$a" --profile $P --region $R --query "Attributes[?Key=='access_logs.s3.enabled'].Value|[0]" --output text)
  bk=$(aws elbv2 describe-load-balancer-attributes --load-balancer-arn "$a" --profile $P --region $R --query "Attributes[?Key=='access_logs.s3.bucket'].Value|[0]" --output text)
  echo "$name: enabled=$en bucket=$bk"
done
```
- 全部 `enabled=false` 或 `bucket` 指向别处 → 安全。
- 还有别的服务可能写（CloudTrail/CloudFront/S3 server log 等）→ 按 [[s3-bucket-usage-check]] 反查；有引用就别删。

### 3.（高危写操作，需确认）删桶——先留档，再 `--force`
```bash
B=<桶名>
echo "删除前对象量(留档):"; aws s3 ls s3://$B --recursive --region $R --profile $P --summarize | tail -2
aws s3 rb s3://$B --force --region $R --profile $P     # --force = 先清空所有对象再删桶
aws s3api head-bucket --bucket $B --region $R --profile $P 2>&1 | tail -1 || echo "桶已删除(404)"
```
若桶开了**版本控制**，`rb --force` 删不掉历史版本 → 需 `delete-objects` 带 version-id 清理，或先 `put-bucket-versioning Suspended` 再批量删（ALB 日志桶一般无版本控制，通常 `--force` 即可）。

## 是否写操作
是，且**删桶不可逆**。三步里第 1、3 步是写操作：动手前回显计划（停哪个 ALB 的日志、删哪个桶、销毁多少对象），用户明确同意后执行。第 2 步只读。第 1 步可回滚（重新 `enabled=true`）；**第 3 步删桶不可回滚**——执行前务必完成第 2 步核实。

## 注意事项
- **顺序铁律：先停日志（第 1 步）再删桶（第 3 步）**。反了会导致 ALB 日志投递写一个不存在的桶而失败（ALB 会持续报投递错误）。
- 删桶 ≠ 唯一选择：若只是想省空间而非彻底下线，配**生命周期规则**过期老日志即可（见 [[s3-bucket-usage-check]]）。本剧本针对的是"确定不再要这些日志"的彻底下线。
- 删完记得更新你的资产清单 / 监控（存储桶计数应 -1）。
- 相关剧本：[[s3-bucket-usage-check]]（判断桶是否在用 / 反查生产者）、[[ec2-public-exposure-hardening]]（ALB 日志桶常在审计公网暴露面时被关注到）。
