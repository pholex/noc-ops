# 可分发运维剧本包（通用版）

这是一组**与具体环境无关**的运维剧本，可直接分发给其它企业用户使用。均为方法论 / 排障流程，命令用占位符（`<域名>`/`<P>`/`<region>` 等），不含任何真实账号 ID、主机名、IP、内部域名或自研工具。

## 剧本清单

| 剧本 | 用途 | 适用对象 |
|---|---|---|
| `s3-bucket-usage-check.md` | 删 S3 桶前判断它是否还在用（新鲜度 + 反查生产者） | 任何 AWS 用户 |
| `alb-access-log-decommission.md` | 停用 ALB 访问日志并安全删除其日志桶 | 任何 AWS 用户 |
| `ec2-public-exposure-hardening.md` | 收紧 EC2 公网暴露面（安全组收口） | 任何 AWS 用户 |
| `certbot-renewal-troubleshooting.md` | Let's Encrypt 证书过期 / 自动续期排障 | 任何 nginx + certbot 主机 |
| `site-access-source-stats.md` | 统计某站点的访问来源（源 IP / 地理归属） | 任何 nginx / ALB 站点 |
| `_TEMPLATE.md` | 沉淀新剧本时的骨架（触发意图 / 目标解析 / 是否写操作 / 执行步骤 / 注意事项） | 所有用户 |

## 约定
- 占位符：`<域名>` 站点域名、`<P>` AWS profile、`<R>`/`<region>` 区域、`<sg>` 安全组 ID、`<alias>` SSH 别名、`<桶名>` S3 桶名。
- 安全原则：只读操作直接做；写操作（删桶/改安全组/续删证书等）先回显计划再确认；不可逆操作（删桶）务必先核实无引用。
- 剧本间用 `[[名称]]` 互链，均指向本目录内的其它剧本。
