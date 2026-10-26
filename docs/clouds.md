# 各云支持细节

README 里的表格说的是「有没有」，这里说「具体能看到什么」。看板标签页：机器 · 域名 · 数据库 · 存储 · 函数 · CDN · 账号权限（IAM）· AI 密钥 · 消息/事件 · 成本 · 计划任务 · 剧本 · 操作日志 · 纳管范围。

| 云 | 机器 | 域名 | 数据库 | 存储 | 函数 | CDN | IAM | AI 密钥 | 消息·事件 | 成本 | 额度 | 重启 |
|---|:-:|:-:|:-:|:-:|:-:|:-:|:-:|:-:|:-:|:-:|:-:|:-:|
| AWS | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| 阿里云 | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | | | ✓ | ✓ | ✓ |
| 腾讯云 | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | | | ✓ | ✓ | ✓ |
| 火山引擎 | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | | | ✓ | ✓ | ✓ |
| Google Cloud | ✓ | ✓ | ✓ | ✓ | ✓ | Cloud CDN | ✓ | ✓ | | BigQuery 导出 | ✓ | ✓ |
| Azure | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | | | ✓ | ✓ | ✓ |
| Oracle Cloud | ✓ | ✓ | ✓ | ✓ | ✓ | Media Streams | ✓ | | | ✓ | | ✓ |
| Cloudflare | Containers | ✓ 只读 | D1 | ✓ | ✓ | ✓ | | | | 按量费用 | | |
| Supabase | | | ✓ | ✓ | ✓ | | | | | | | 暂停/恢复 |
| 内网虚拟化平台：VMware / 深信服 HCI | ✓ | | | | | | | | | | | ✓ |
| 内网其它主机：群晖、任何能 SSH 的物理机或虚机 | ✓ | | | | | | | | | | | |

## 机房里的机器

- vCenter / 深信服有管控接口，自动扫出虚机并可重启。深信服这版接口不认重启，命令会如实告诉你改走哪条路，只能查状态。
- 其它机器不分物理还是虚拟，在工作区 `local-servers.toml` 登记一行即可，`ops sync --local` 探 SSH 端口判断在线。
- 人不在内网时，Tailscale 组网上的机器用 `ops sync --tailscale` 刷。
- 机房机器不参与 `ops sync` 全量扫描，连不上不会被误判成「已删除」。

### Oracle Media Streams CDN

通过 `oci media-services` 按配置的隔间和区域盘点分发通道及 CDN 配置，支持 OCI Edge 和 Media Streams 内的 Akamai 配置。看板 CDN 页展示配置名称、类型、所属通道、域名、区域、生命周期与启用标志；通道域名不等于完整播放 URL。仅支持视频分发配置，不提供通用网站 CDN。

刷新单个账号：`ops sync --kind=cdn --account=<OCI profile 或账号名>`。需要读取分发通道与 CDN 配置的 IAM 权限；扫描不递归子隔间。列表查询失败时保留该账号旧数据，配置详情失败时显示已获取的基本字段并标记采集不完整。不会采集 Akamai 鉴权密钥或签名播放 URL。


## 各云的补充说明

- **Cloudflare**：Containers 容器实例进机器、D1（SQLite）进数据库。每个 zone 带状态、DNS 记录数与代理数、SSL 模式、套餐、NS；在 Cloudflare Registrar 注册的域名还有到期日。R2 桶进存储、Workers 脚本进函数、开了代理记录的 zone 进 CDN。只读，不做任何写操作。
- **Supabase**：每个项目带算力档位（vCPU / 内存）、库实际占用、PITR 与最近备份、网络访问限制、自定义域名。Storage 桶进存储（公开桶标 ⚠）、Edge Functions 进函数。唯一的写操作是 `ops db pause|resume` 暂停 / 恢复项目，可逆，先回显再确认。
- **火山引擎 / 腾讯云的对象存储**：TOS 桶和 COS 桶不在各自 CLI 里，分别另装官方 `tosutil`（`tosutil config -interactive`）和 `coscli`（`coscli config init`）配好即可纳管。
- **Azure 成本**：MCA 订阅的成本只在计费档案层级有数据，驱动直接查档案层级，覆盖该档案下的全部订阅。
- **AI 密钥**：只盘元数据（绑定的 API、服务账号、创建时间），永远不取密钥串。
- **额度**：指信用余额、免费额度到期、超额保护，写在 `inventory.md` 的「云账务 / 额度」一节，不单独开标签页。
- **定时重启**（看板「计划任务」）：用一个 AWS 账号里的 EventBridge + Lambda 做调度器，可以派发到其它云的实例。

### Cloudflare Containers 与 D1

使用现有账号标签与 API token。Containers 需要 `Workers Containers Read`，D1 需要 `D1 Read`，账号发现需要 `Account Settings Read`。可用 `ops sync --kind=instances --account=<标签>` 或 `ops sync --kind=databases --account=<标签>` 分别刷新；列表或分页失败会保留该标签的旧清单。

Containers 按应用列出实际容器实例，展示状态、当前部署区域、规格、所属应用和镜像。无实例的应用不计为机器，不提供 SSH 或重启。D1 展示名称、UUID、容量、创建日期和只读副本配置；详情失败仍保留基本信息并标记采集不完整。区域为 jurisdiction 限制（未设置时显示自动），不代表实时运行位置；API 未提供的状态与连接端点显示为空。不执行 SQL，也不采集容器环境变量。

接口说明：[Containers 实例](https://developers.cloudflare.com/api/resources/containers/subresources/applications/subresources/instances/methods/list/)、[D1 数据库](https://developers.cloudflare.com/api/resources/d1/subresources/database/methods/get/)。

### Cloudflare 成本

使用 [Billable Usage v1 API](https://developers.cloudflare.com/api/resources/billing/subresources/usage/methods/get_account_usage_v1/) 读取自助账号的按量费用，需要 `Billing Read`；账号发现需要 `Account Settings Read`。`ops sync --kind=billing --account=<标签>` 强制刷新，完整同步复用现有成本缓存策略；仅按账号同步时不查询成本。

查询近三个月起始的账期，按 Cloudflare 账号、产品、账期起始月与原币种汇总 `ContractedCost`，不累加累计金额字段。账期月不等于自然使用月，数据每日更新，API 未提供账期结束日，记录保留 `partial=true`。这不是完整发票，不含固定订阅费、税费与历史调整；不使用受限的 v2 企业接口。权限不足、账号未覆盖或响应异常时保留旧成本，只有成功的空结果才清空。

## 命令全集

日常只用 `ops init` / `ops sync` / `ops sync --local` / `ops noc`。其余见 `ops help`：

| 命令 | 作用 |
|---|---|
| `ops sync --kind=<类>` | 只刷一类资源（instances / domains / databases / storage / serverless / cdn / iam / apikeys / messaging / billing / credits） |
| `ops sync --account=<账号>` | 只刷一个云账号，可与 `--kind` 组合 |
| `ops sync --tailscale` | 只刷 Tailscale 组网上的机器 |
| `ops sync --probe=<实例>` | 只探一台机器的 SSH 登录用户名并回写配置 |
| `ops reboot <名字或 ID>` | 重启；`--status` 只查状态，`--dry-run` 只打印将执行的命令 |
| `ops db status\|pause\|resume <名字或 ref>` | 托管数据库项目查状态 / 暂停 / 恢复（目前仅 Supabase） |
| `ops netprobe` | 探测本机出口链路，纯只读 |
| `ops doctor [--fix]` | 体检；`--fix` 自动修可修项 |
| `ops setup [--workspace DIR] [--with-mcp]` | 装 / 刷新 skill；指定工作区；无 shell 的 GUI 客户端（Claude Desktop）接 MCP |
| `ops noc status \| stop \| restart` | 看板管理；`-f --port N` 前台换端口 |
| `ops log <action> [target]` | 手工记一条操作日志 |
| `ops --version` | 版本 |

## 安全规则

| 操作类型 | 行为 |
|---|---|
| 只读（describe / list / get、状态查询） | 自动执行 |
| 实例重启 | 直接执行，自动记审计日志 |
| 其它写操作（启停 / 暂停恢复 / 销毁 / 改配置、远程改动、删资源） | 先回显计划，等你确认才做 |

拿不准的一律按写操作对待。所有写操作记入 `<工作区>/.state/ops-log.jsonl`，看板「操作日志」页可看。凭证只住在各云 CLI 自己的配置和系统 Keychain 里，`ops` 不保存、不上传。

## 工作区

你的数据全部在工作区，默认 `~/.noc`，用 `NOC_HOME` 或 `ops setup --workspace` 改：

```
<workspace>/
├── ops-config.toml   # 你的账号、区域、SSH key 约定（ops init 生成）
├── local-servers.toml# 机房里手工登记的机器
├── inventory.md      # 人读清单，含云账务 / 额度一节
├── data.json         # 机读数据，看板与 skill 读它
├── runbooks/*.md     # 你沉淀的运维剧本
└── .state/           # 运行时状态：SSH 用户缓存、快照、操作日志、看板日志
```

升级 `noc-ops` 不碰工作区。秘密（API token、密码）不要写进 `ops-config.toml`，放系统 Keychain 或环境变量。

## 沉淀 runbook

1. 在 agent 客户端里把一件运维做完。
2. 让它「存成 runbook」，它会按 `runbooks/_TEMPLATE.md` 写一份。
3. 下次同类需求，agent 会先找到这份剧本照着执行。看板「剧本」页列出全部剧本。

## Google Cloud CDN 与成本

Cloud CDN 盘点开启 `enableCDN` 的 Backend Services 和 Backend Buckets，一项后端一行，显示缓存模式、后端来源和 URL Map 路由主机名（含通配符）。路由主机名不代表已验证的 DNS、TLS 或公网可达性。未挂路由的 CDN 后端仍列出；Media CDN 不在此范围。需要 `compute.backendServices.list`、`compute.backendBuckets.list`，主机名补充需要 `compute.urlMaps.list`。补充查询失败时保留后端列表并标记 partial；任一后端列表失败时沿用项目旧清单。

成本使用 Cloud Billing 的标准或详细 BigQuery 导出表，不支持 FOCUS 表。先在 Google Cloud 中开启账单导出，在工作区 `ops-config.toml` 对应的项目配置中填写：

```toml
[[gcp.accounts]]
project = "example-project"
configuration = "default"  # 可选，沿用该 gcloud 配置的身份
billing_export_table = "example-billing.costs.gcp_billing_export_v1_XXXXXX_XXXXXX_XXXXXX"
billing_location = "US"     # 与数据集位置一致
billing_query_project = "example-billing"  # 可选，默认用导出表所在项目，承担查询费用
billing_max_bytes = 1073741824              # 可选，单次查询默认上限 1 GiB
```

当前 gcloud 身份需要导出数据集上的 `roles/bigquery.dataViewer` 和查询项目上的 `roles/bigquery.jobUser`，并需已启用 BigQuery API。NOC 不自动开启 API 或账单导出，不保存访问令牌。

```bash
ops sync --account=example-project --kind=cdn
ops sync --account=example-project --kind=billing
```

成本窗口为本月及前两个月，按项目、服务、UTC 使用月份和原币种聚合 `cost + credits`；负数抵扣保留，多币种分列合计，不折算汇率。按 `project.id` 过滤，所以同一导出表可供多个项目使用，不会重复计入整张结算账号账单；未归属项目的费用不纳入。此口径不等于发票月份，账单导出也存在延迟和历史覆盖限制，空查询不能证明无费用。

BigQuery 查询可能计费；仅 `ops sync --kind=billing`（含看板成本刷新）执行，普通全量或账号刷新沿用 GCP 成本缓存。超过字节上限时查询失败并保留旧成本；未配置导出表显示“未配置”，权限不足、超时或分页失败也不会清空旧数据。

参考：[账单导出设置](https://docs.cloud.google.com/billing/docs/how-to/export-data-bigquery-setup)、[标准导出字段](https://docs.cloud.google.com/billing/docs/how-to/export-data-bigquery-tables/standard-usage)。
