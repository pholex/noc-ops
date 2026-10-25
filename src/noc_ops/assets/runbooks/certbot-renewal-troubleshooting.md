# Let's Encrypt 证书过期 / 自动续期排障

## 触发意图
- "xxx.域名 打不开 / 证书过期了 / 浏览器报不安全"
- "帮我续一下 xxx 的证书"
- "证书怎么又过期了 / 自动续期是不是坏了"

## 目标解析
先确定证书归属与服务入口：
- `dig +short <域名>` → 指向单台 EC2 则证书在那台机器上（nginx + certbot）；指向 ALB/CloudFront 则是 ACM 证书，走另一套（本剧本针对**机器上的 certbot**）。
- SSH 进去后，`sudo certbot certificates` 看全量证书及到期状态（`VALID` / `INVALID: EXPIRED`）。

## 执行步骤

### 1.（只读）盘清证书现状，区分"在用证 vs 孤儿证"
```bash
ssh <alias> '
  sudo certbot certificates 2>/dev/null | grep -E "Certificate Name|Domains|Expiry"
  # 每个 vhost 实际用的是哪张证（关键：certbot 管的 ≠ vhost 在用的）
  sudo grep -rnE "ssl_certificate " /etc/nginx/conf.d/*.conf
'
```
**重点判断**：certbot 列出的证书，未必是 nginx 真正在加载的。常见三类：
- **在用证**：某 vhost 的 `ssl_certificate` 指向 `/etc/letsencrypt/live/<域名>/` → 过期就真的挂，必须续。
- **孤儿证**：vhost 改用了别的证（如手动放的**通配证** `/etc/nginx/ssl/example.com.pem`），certbot 里那张同名证已没人用 → 过期无影响，可删。
- **死证**：对应服务/vhost 已下线（容器删了、vhost 改名 .disabled）→ 删。

### 2.（写操作，需确认）续在用证 / 删无用证
续单张（nginx 插件签发的，80 口要可达）：
```bash
sudo certbot renew --cert-name <域名> --no-random-sleep-on-renew && sudo systemctl reload nginx
```
删孤儿/死证（**删前必须确认没有活动 vhost 引用它的 live 路径**）：
```bash
# 守卫检查：只看 *.conf（.bak/.disabled 不被 nginx 加载，可忽略）
sudo grep -rnE "ssl_certificate .*/live/<域名>/" /etc/nginx/conf.d/*.conf
sudo certbot delete --cert-name <域名> --non-interactive
```
**删证顺序**：若证书被某 vhost 引用、而你要连 vhost 一起下线 → **先撤 vhost（改名 `.disabled` 可回滚）→ `nginx -t` 严格判退出码 → reload → 再删证**。否则删证后下次 reload 会因找不到证而失败。

### 3.（只读）验证：分清"修好了" vs "另有问题"
```bash
for h in <域名...>; do
  code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 12 https://$h/)
  exp=$(curl -sv --max-time 10 https://$h/ 2>&1 | grep -i "expire date" | sed "s/.*expire date: //")
  echo "$h -> HTTP $code | 证书到期 $exp"
done
```
- 公网失败但 `curl -H "Host: <域名>" https://127.0.0.1/ -k` 本地 200 → 后端没问题，是证书/对外层问题。
- `certificate has expired` → 续证；其它 TLS 错误另查。

## ⚠️ 自动续期为什么会"集体失效"——逐一排雷（治本的核心）
单续一张只是治标。证书集体过期 = **续期调度根本没在跑**，必须找出哪一环断了：

1. **调度器是否启用**：
   ```bash
   systemctl is-enabled --quiet certbot.timer && echo timer-on || echo timer-off
   systemctl is-active crond && sudo ls /etc/cron.d/ | grep -i certbot
   ```
   两套机制（systemd `certbot.timer` / `cron.d`）至少要有一个真正生效。
2. **`/etc/cron.d/` 文件格式坑（高频真凶）**：`/etc/cron.d/` 里的行**必须有用户字段**：`分 时 日 月 周 用户 命令`。少写 `root` 会让 cron 把命令首词当用户名 → 用户不存在 → **任务静默不执行**（journal 里查不到 CROND 跑过它就是证据）。逐字节核对、对照系统自带文件：
   ```bash
   sudo cat -A /etc/cron.d/certbot-renew
   sudo grep -hE "^[0-9*]" /etc/cron.d/0hourly   # 系统范例：必有 root 字段
   sudo journalctl -t CROND --since "7 days ago" | grep -i certbot   # 空=从没跑过
   ```
   修复（补 `root`，留备份，reload crond）：
   ```bash
   sudo cp -a /etc/cron.d/certbot-renew /etc/cron.d/certbot-renew.bak.$(date +%Y%m%d)
   echo "0 0,12 * * * root /usr/local/bin/certbot renew --quiet && systemctl reload nginx" | sudo tee /etc/cron.d/certbot-renew
   sudo systemctl reload crond
   ```
3. **`authenticator = manual` 的毒丸证**：DNS 挑战的证书无法非交互续期，会污染 `certbot renew`（全量）使其报错退出 → 后面的 `&& systemctl reload nginx` 被跳过。`grep -l "authenticator = manual" /etc/letsencrypt/renewal/*.conf` 找出来；不需要的（如生产走 ACM/ALB 的遗留证）直接 `certbot delete`。
4. **验证治本**：
   ```bash
   sudo certbot renew --dry-run --no-random-sleep-on-renew   # 期望 "all simulated renewals succeeded"
   ```

## 是否写操作
是。续证 / 删证 / 改 cron / 撤 vhost 均为写操作 → 先回显计划+影响面请用户确认。均可回滚（证可重签、cron 与 vhost 留了 `.bak`/`.disabled`）。审计与验证只读。

## 注意事项
- **`certbot renew` 非交互默认插入最长 ~8 分钟随机延迟**（错峰，LE 推荐）；手动跑/排障时会被当成"卡住"被超时杀掉，且**被杀的进程仍在后台持锁**，下次报 `Another instance of Certbot is already running`。手动续/验证一律加 `--no-random-sleep-on-renew`；cron 里则保留随机延迟。被杀后清理：`sudo pkill -9 -f "[c]ertbot"; sudo rm -f /etc/letsencrypt/.certbot.lock /var/log/letsencrypt/.certbot.lock /var/lib/letsencrypt/.certbot.lock`（pkill 模式用 `[c]` 括号避免误杀自己的命令行）。
- 改 nginx 后 reload 前**务必 `nginx -t` 并严格判退出码**：`if sudo nginx -t; then sudo systemctl reload nginx; fi`——别用 `nginx -t 2>&1 | tail` 这种会吞掉退出码、导致带病 reload。
- 续期成功的判据看 certbot 输出 `Congratulations, all renewals succeeded`，再 `curl -sv` 复核 `expire date` 真的前移了。
- **典型故障链**：多张证集体过期，根因常是 `cron.d/certbot-renew` 缺 `root` 字段（从未执行）+ 某张 manual 遗留证毒丸拖累批量续期。处置：删无用证 → 补 cron 用户字段 → 续在用证 → `--dry-run` 验证。用通配证的 vhost 通常不受单张 certbot 证过期影响。
- 相关剧本：[[ec2-public-exposure-hardening]]（证书问题常在收口验证时连带发现）。
