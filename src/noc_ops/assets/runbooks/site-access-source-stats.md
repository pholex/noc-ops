# 统计某站点的访问来源（源 IP / 地理归属）

## 触发意图
- "统计下 https://xxx 的访问来源有哪些 / 谁在访问"
- "看看 xxx 这个站点的流量都从哪来"
- "xxx 最近的访问 IP / 地区分布"

## 目标解析（先判断流量入口在哪——别想当然）
"看日志"之前必须先搞清这个域名的**流量入口**，日志位置完全取决于它：
```bash
dig +short <域名>            # 关键：解析结果是什么？
```
- 结果是 **ALB DNS**（`*.elb.amazonaws.com[.cn]`）→ 走 ALB，日志在 **S3 访问日志桶**（用 [[s3-bucket-usage-check]] 找桶；ALB 日志格式见下）。
- 结果是 **CloudFront**（`*.cloudfront.net`）→ 看 CloudFront 日志桶。
- 结果是**单个 EC2 公网 IP** → **不经任何 LB**，日志在那台机器的 **Web 服务器(nginx/caddy)**上，得 SSH 进去看。
  - 反查这个 IP 是哪台实例：在你的资产清单/CMDB 里按 `public_ip` 找 → 拿到 SSH 别名。

## 执行步骤

### A. 入口在 EC2 + nginx
1. SSH 进去，确认谁在服务该域名、access_log 在哪：
   ```bash
   ssh <alias> '
     sudo ss -tlnp | grep -E ":80 |:443 "                       # 谁在监听
     sudo docker ps                                              # 若是反代容器
     sudo grep -rl "<域名>" /etc/nginx /etc/caddy 2>/dev/null   # 哪个 vhost 配置
     sudo grep -nE "log_format|access_log" /etc/nginx/nginx.conf /etc/nginx/conf.d/
     sudo ls -la /var/log/nginx/
   '
   ```
2. **看 log_format 是否含 `$host`，以及该 access.log 是否多 vhost 混写**（决定怎么切流量）：
   - **混写且不记 `$host`**（默认 `main`/`combined` 格式就是这样）→ **用 Referer 切分**。nginx 默认日志的 `$request` 只有路径没有 host，但 `$http_referer` 里带 `https://<域名>/...`，SPA 的所有 API/资源请求都会带——`grep "<域名>"` 命中的就是 Referer 行。
   - 若 vhost 配了独立 `access_log /var/log/nginx/<域名>.access.log` → 直接读那个文件，无需 Referer 过滤。
   - 若 log_format 含 `$host` → 按 `$host` 字段精确过滤。
3. 确认真实客户端 IP：日志里 `$http_x_forwarded_for` 为 `"-"` 说明是直连，`$remote_addr`(第 1 字段)即真实 IP；若非空（前面还有 CDN/代理）要取 XFF 链首个。
4. 跨全部轮转日志聚合源 IP（`zgrep` 同时吃 .gz 和明文）：
   ```bash
   ssh <alias> 'sudo bash -c "
     L=/var/log/nginx/access.log*
     echo 总请求:; zgrep -h \"<域名>\" \$L 2>/dev/null | wc -l
     echo 去重IP:; zgrep -h \"<域名>\" \$L 2>/dev/null | awk \"{print \\\$1}\" | sort -u | wc -l
     echo TopIP:;  zgrep -h \"<域名>\" \$L 2>/dev/null | awk \"{print \\\$1}\" | sort | uniq -c | sort -rn | head -30
   "'
   ```

### B. 入口在 ALB（对照）
ALB 日志在 S3，按 [[s3-bucket-usage-check]] 定位/同步到本地后：客户端 IP = 第 4 字段 `client:port`，host = 行内 `https://<域名>:443/` 或末尾的 `"<域名>"`(domain_name 字段)。
```bash
find . -name '*.log.gz' -print0 | xargs -0 gunzip -c \
  | grep '<域名>' | awk '{print $4}' | sed -E 's/:[0-9]+$//' | sort | uniq -c | sort -rn
```

### C. 把源 IP 变"来源"——地理/运营商归属
源 IP 列表本身不直观，对 Top IP（覆盖大头流量即可，无需全量）批量查归属：
```bash
# ip-api 免费批量，≤100/次、15次/分；字段按需裁剪
curl -s -X POST "http://ip-api.com/batch?fields=query,country,regionName,city,isp,as" \
  -H "Content-Type: application/json" --data '["IP1","IP2",...]' \
  | python3 -c 'import sys,json;[print(r["query"],r.get("country"),r.get("regionName"),r.get("city"),r.get("isp")) for r in json.load(sys.stdin)]'
```
判读要点：
- 流量是否**高度集中在少数 NAT 出口 IP**（单 IP 占大头）+ UA 是否同一类客户端 → 多半是**内部团队**而非公开散客。
- 同一城市/运营商的一片**动态 IP 池**应**按 /24 或运营商合并**理解，别当成很多独立访客。
- **长尾的 1–2 次境外云主机 IP**（Linode/DO/M247/各 VPS 段）基本是**扫描器/爬虫**，不是真实用户——可单列为噪声。

## 是否写操作
否，全只读（dig / SSH 读日志 / 第三方 IP 归属查询）。
- 注意：IP 归属查询会把**客户端 IP 发给第三方(ip-api)**——属公开 IP、低敏感，但心里有数；要纯离线可改 `dig -x <ip>` 反查 PTR（信息少些）。

## 注意事项
- **先 dig 定入口**是整条链的命门——入口判错，会去错误的地方翻日志，得到 0 命中的假象。
- nginx 默认日志**不记 `$host`**，多站点混写时**只能靠 Referer 切**；首屏直开页(Referer 为 `-`)会漏，对"源 IP 排行"无影响，但"独立访客数"会略偏低——报告里说明口径。
- `zgrep`/`zcat` 跨 .gz+明文统一处理；`access.log*` 通配能一把吃下当天+所有轮转归档；轮转保留期决定可统计的时间窗。
- 顺带留意 `error.log` 体积：突然变大常是境外扫描在打，公网直连的多面板机器建议收口（见 [[ec2-public-exposure-hardening]]）。
- **示例判读**：某自托管站点近 ~10 天数万次请求、数百去重 IP，但 ~90% 来自少数几个 NAT 出口 + 同一类客户端 UA → 实为内部团队自用；其余长尾境外云主机 IP 为扫描噪声。
