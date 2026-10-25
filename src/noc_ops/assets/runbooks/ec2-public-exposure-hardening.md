# 收紧 EC2 公网暴露面（安全组收口）

## 触发意图
- "看下如何收一收 xxx 的公网暴露面 / 这台机器开了哪些公网口"
- "把没用的端口关掉 / 收紧安全组"
- "这台公网暴露面太大，帮我审一下"

## 目标解析
把口语目标（机器名）→ 实例 + 安全组（用你的资产清单/CMDB 按名称或 public_ip 定位实例 ID）：
```bash
ID=<instance-id>; P=<profile>; R=<region>
aws ec2 describe-instances --instance-ids $ID --profile $P --region $R \
  --query "Reservations[].Instances[].SecurityGroups" --output json
```

## 执行步骤（只读审计 → 收口 → 验证）

### 1.（只读）列出每个 SG 的入站规则，**带描述**
```bash
for sg in <sg-1> <sg-2>; do
  aws ec2 describe-security-groups --group-ids $sg --profile $P --region $R \
    --query "SecurityGroups[].IpPermissions[].{Proto:IpProtocol,From:FromPort,To:ToPort,CIDR:IpRanges[].CidrIp,Desc:IpRanges[].Description}" \
    --output json
done
```
重点挑出 `0.0.0.0/0`（及 `::/0`）的规则——那才是公网面。内网段（如 `172.16.0.0/20`）一般 OK。

### 2.（只读）上机器看**真正在听公网**的服务，与 SG 对齐
```bash
ssh <alias> '
  sudo ss -tlnp | awk "NR==1 || /0.0.0.0:|\[::\]:/"   # 绑 0.0.0.0/:: 的才对公网
  sudo docker ps --format "{{.Names}}\t{{.Ports}}"     # 容器端口映射（0.0.0.0:X 会绕过 nginx 直曝）
  sudo ufw status; sudo systemctl is-active firewalld fail2ban  # 主机层防护
'
```
把每个公网 SG 端口归类：
- **保留**：确有服务且应公开（典型 `80`/`443` 给 nginx、`22` 给 SSH）。
- **僵尸**：SG 开着但**没有进程在听**（旧 portainer/1panel/n8n 等停用残留）→ 关，零影响。
- **冗余**：服务已由 nginx 经 `443`+域名提供，却又单独开了容器端口（如 nocobase `13000`）→ 关，走域名访问（还能拿到 TLS）。
- **滥用口**：无认证、可被白嫖/放大的服务（如 iperf3 `5201`）→ 立即关。

### 3. ⚠️ 收口前**必须**确认 SG 挂在哪些实例上（共享 SG 是大坑）
```bash
aws ec2 describe-instances --filters "Name=instance.group-id,Values=<sg>" \
  --profile $P --region $R \
  --query "Reservations[].Instances[].{Name:Tags[?Key=='Name']|[0].Value,Id:InstanceId}" --output table
```
- **SG 只挂这一台** → 按本机监听情况放心删。
- **SG 被多台共享**（如名为 `default` 的 SG 常挂一整组）→ **"本机没在听的口" ≠ "没用"**，可能是同组别的机器在服务它！这种 SG 上的"僵尸"规则**不能凭本机判断删**，要么逐台核实，要么干脆不动。

### 4.（写操作，需确认）撤销规则
```bash
aws ec2 revoke-security-group-ingress --group-id <sg> --profile $P --region $R --ip-permissions \
  'IpProtocol=tcp,FromPort=5201,ToPort=5201,IpRanges=[{CidrIp=0.0.0.0/0}]' \
  'IpProtocol=tcp,FromPort=13000,ToPort=13000,IpRanges=[{CidrIp=0.0.0.0/0}]' \
  ...（每条一个 IpProtocol=... 段，可一次多条）
```
返回 `RevokedSecurityGroupRules` 会回显每条的 Description，正好二次核对删对了没。

### 5.（只读）验证：服务没误伤 + 口确实关了
```bash
# 域名服务仍在（走 443/nginx，本次未动）
for h in <域名...>; do echo "$h -> $(curl -s -o /dev/null -w '%{http_code}' --max-time 10 https://$h/)"; done
# 被关的直连端口应不可达
curl -s -o /dev/null -w "%{http_code}\n" --max-time 6 http://<公网IP>:<被关端口>/   # 预期 000
```
**验证踩坑——分清"我误伤的" vs "本来就坏的"**：
- 某域名 `curl` 返回 `000` 别急着回滚。先 `curl -v` 看失败原因；再上机器 `curl -H "Host: <域名>" https://127.0.0.1/ -k` 经本地 nginx 测后端。
- 若本地 200、公网失败且原因是 `certificate has expired` → 是**证书过期**（既有问题），跟你删 SG 无关（SG 不碰证书/443）。见 [[certbot-renewal-troubleshooting]]。

## 是否写操作
是。第 4 步 `revoke-security-group-ingress` 是写操作 → 先回显计划+范围请用户确认。其余（审计、验证）只读。撤销可逆（记下删了哪些端口/CIDR/描述，需要时 `authorize-security-group-ingress` 加回）。

## 注意事项
- **收口判断的黄金组合 = SG 规则 ∩ 实际监听 ∩ SG 挂载范围**，三者缺一会误判（只看 SG=可能删了别人在用的；只看本机=漏掉共享 SG 风险）。
- docker 容器端口映射成 `0.0.0.0:X` 会**绕过 nginx 与 SG 之外的任何应用层防护**；理想做法是 compose 里绑 `127.0.0.1:X:X`，仅供本机 nginx 反代——这样即便 SG 误开也不裸奔（纵深防御）。
- 公网直连的多面板机器建议补 `fail2ban`；SSH 尽量**白名单化**（22 口只放办公网段/个人出口 IP，规则描述写清归属，便于以后核对清理）。
- 顺带审证书到期：`sudo certbot certificates` 看哪些 `EXPIRED`（见 [[certbot-renewal-troubleshooting]]）。
- 一个典型收口结果：公网仅剩 `80/443`(Web) + `22`(白名单 SSH)；关掉无认证的 iperf3、绕过 TLS 的面板端口是实质收益，停用残留的 portainer/1panel/n8n 端口是零风险清理。
