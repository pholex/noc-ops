# noc-ops —— 所有云账号和机房的机器，一张清单，一句话管

> One inventory and one local dashboard for everything in all your cloud accounts (AWS, Alibaba Cloud, Tencent Cloud, Volcengine, Google Cloud, Azure, Oracle Cloud, Cloudflare, Supabase) and in your own server room. Operate it in plain language from Claude Code, Codex, Gemini CLI or Kiro. Reads just run; changes are shown to you first. Nothing leaves your machine. macOS / Linux.

## 它是什么

你有好几家云、好几个账号、机房里还有一堆机器。想知道有多少台机器、哪些域名快到期、上个月花了多少，得一家家登控制台翻。

装上 `noc-ops`，这些都收进**一张清单**和**一个本机网页看板**。然后在 Claude Code 或 Codex 里直接说话：「重启 web-01」「哪些域名 30 天内到期」「宁夏有几台机器在跑」，它去办。

只查看的事它直接做；会改东西的操作，它先说清要做什么，你点头才动。

## 支持哪些云

| | 机器 | 域名 | 数据库 | 存储 | 函数 | CDN | 成本 | 重启 |
|---|:-:|:-:|:-:|:-:|:-:|:-:|:-:|:-:|
| AWS · 阿里云 · 腾讯云 · 火山引擎 · Azure · Google Cloud · Oracle Cloud | ✓ | ✓ | ✓ | ✓ | ✓ | ✓* | ✓† | ✓ |
| Cloudflare | | ✓ | | ✓ | ✓ | ✓ | | |
| Supabase | | | ✓ | ✓ | ✓ | | | 暂停 / 恢复 |
| 机房：VMware / 深信服 HCI | ✓ | | | | | | | ✓ |
| 机房：群晖、任何能 SSH 的机器 | ✓ | | | | | | | |

\* Oracle Cloud CDN 支持 Media Streams 的 OCI Edge / Akamai 分发配置盘点，仅用于视频分发。

† Google Cloud 成本读取已配置的 BigQuery 账单导出，按项目、服务、UTC 使用月份汇总抵扣后净成本。

各云细节与全部命令见 [docs/clouds.md](docs/clouds.md)。

## 安装

```bash
curl -fsSL https://raw.githubusercontent.com/pholex/noc-ops/main/scripts/install.sh | sh
```

安装脚本先检查 `uv`、`noc-ops` 和已有配置，再检查下表中的云 CLI。默认只补齐缺失项，已安装的不重装，已有配置和 skill 不重复刷新；全部就绪时直接结束。

缺失的云 CLI 会列入菜单，供你选择自行安装（默认）、按需自动安装，或输入 `q` 退出。自动安装前展示实际步骤并确认。Cloudflare / Supabase 无需 CLI；没有交互终端时跳过云 CLI 安装，仅补齐基础工具和配置。详见 [INSTALL.md](INSTALL.md)。

用到哪家云，就装哪家的命令行工具并登录一次，没配的云会自动跳过。安装命令是 macOS 的，Linux 点云的名字看官方说明：

| 云 | 安装 | 登录 |
|---|---|---|
| [AWS](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html) | `curl -fsSL https://awscli.amazonaws.com/v2/install.sh \| bash` | `aws configure` |
| [阿里云](https://help.aliyun.com/zh/cli/install-cli-on-macos) | `brew install aliyun-cli` | `aliyun configure` |
| [腾讯云](https://cloud.tencent.com/document/product/1278/46705) | `pip install tccli` | `tccli configure` |
| [腾讯云对象存储](https://cloud.tencent.com/document/product/436/63144)（可选） | 下载 `coscli` 二进制 | `coscli config init` |
| [火山引擎](https://github.com/volcengine/volcengine-cli) | `npm install -g @volcengine/cli` | `ve configure` |
| [火山引擎对象存储](https://docs.volcengine.com/docs/6349/152751)（可选） | 下载 `tosutil` 二进制 | `tosutil config -interactive` |
| [Google Cloud](https://docs.cloud.google.com/sdk/docs/install) | `brew install --cask gcloud-cli` | `gcloud auth login` |
| [Azure](https://learn.microsoft.com/cli/azure/install-azure-cli-macos) | `brew install azure-cli` | `az login` |
| [Oracle Cloud](https://docs.oracle.com/iaas/Content/API/SDKDocs/cliinstall.htm) | `brew install oci-cli` | `oci setup config` |
| Cloudflare / Supabase | 不用装 | API token 放系统钥匙串，见 [INSTALL.md](INSTALL.md) |
| 机房机器 | 不用装 | 能 SSH 上去即可 |

## 三个命令

```bash
ops init            # 一问一答，填上你的账号
ops sync            # 扫所有云，生成清单（有变化就再跑一次）
ops sync --local    # 只扫机房里的机器（人要在内网或 VPN 里）
```

看板：`ops noc`，浏览器打开 http://127.0.0.1:8765 ，按标签页看机器、域名、数据库、存储、函数、CDN、成本、操作日志等。

云资源采集失败（缺少 CLI、认证失败、超时或分页中断）时，保留该账号该类资源的旧数据；AWS 实例按账号与区域分别处理，Azure 成本按共享计费档案整体处理，避免多订阅重复统计。只有成功完成查询的空列表才会清空对应范围。看板的「采集状态」显示最近尝试、上次完整成功和是否沿用旧数据；历史数据没有成功时间记录时显示「尚无记录」。全量、按类型和按账号刷新使用相同规则。补充字段查询失败时，资源列表仍更新，并标记字段不全。

初始化支持九朵云：`ops init --cloud azure` 可只添加 Azure 订阅，重复运行会合并已有账号并备份配置。只在本机检查用 `ops doctor --offline`，按云验证用 `ops doctor --cloud aws`，自动化读取用 `ops doctor --json`；未配置的云不会因缺 CLI 而判失败。

用 `ops find web --cloud aws --account <profile> --region us-east-1 --json` 查资源。结果包含 `noc://…` 唯一引用和支持的操作，CLI 与 MCP 都使用相同匹配规则；重复 ID/同名目标不会取第一条。`ops reboot`、`ops db` 支持同样的三个筛选参数。`--dry-run --json` 输出操作计划且不调用云 API；重启延续免二次确认规则，数据库暂停/恢复及 MCP 定时任务需要确认。

版本查询：`ops update --check`；更新预览：`ops update --dry-run`；确认后更新：`ops update`。会保留 uv 的安装来源、extra、扩展与版本约束，并刷新 skill。发布与回退说明见 [RELEASE.md](RELEASE.md)，变更见 [CHANGELOG.md](CHANGELOG.md)。

## 内置运维剧本

公开包附带 [通用运维剧本](src/noc_ops/assets/runbooks/README.md)，包括 S3 使用核查、ALB 日志下线、EC2 安全组收口、证书续期排障、访问来源分析、[EventBridge Scheduler + Lambda 定时任务](src/noc_ops/assets/runbooks/aws-lambda-scheduled-task.md)，以及 [CloudWatch 告警配置与通知验证](src/noc_ops/assets/runbooks/aws-cloudwatch-alarm-notification.md)。工作区没有 Markdown 剧本时，初始化会复制这些文件；已有剧本时不会自动补齐或覆盖。

## 扩展与贡献

参见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 许可

Apache License 2.0。
