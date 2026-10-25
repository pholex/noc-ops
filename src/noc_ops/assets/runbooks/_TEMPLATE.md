# <剧本标题：一句话说清这是干什么的>

> 文件名用 kebab-case，如 `rds-storage-autoscaling-check.md`。写完跑 `ops sync --index` 让看板「剧本」页登记。

## 触发意图

用户大概会怎么说（agent 靠这些句子匹配到本剧本）：
- 「……」
- 「……」

## 目标解析

- 目标是哪类资源（实例 / 域名 / 数据库 / 桶 …），在 `data.json` 的哪个键下找。
- 按 名字 → ID → 唯一子串 的顺序匹配；匹配到多条**一律列出让用户挑**，不猜。

## 是否写操作

- [ ] 纯只读，可直接执行
- [ ] 含重启（免确认，自动记日志）
- [ ] 含其它写操作：执行前必须回显完整计划并等用户确认

## 执行步骤

1. 第一步做什么，用到的命令（占位符写成 `<profile>` `<region>` `<实例ID>`，不写真实值）
   ```bash
   aws ec2 describe-instances --profile <profile> --region <region> --instance-ids <实例ID>
   ```
2. 第二步 …
3. 收尾：写操作完成后 `ops log <action> <target> --cloud <cloud> --source <调用方>` 记一条。

## 注意事项

- 这朵云 / 这类资源的坑（分区差异、CLI 退出码为 0 但 JSON 里报错、分页 …）。
- 什么情况下应该停下来问用户，而不是继续。

## 相关剧本

- [[另一份剧本的文件名（不含 .md）]]
