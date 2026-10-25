#!/usr/bin/env python3
"""ops noc —— NOC 运维看板本地服务。

读取 工作区 data.json（由 ops sync 生成）渲染只读看板。
- GET  /                首页（index.html）
- GET  /data.json       看板数据
- POST /api/refresh     触发一次全量 ops sync（重扫所有云、重探新机器），完成后前端重新拉 data.json
- POST /api/refresh?kind=<资源键>   只刷某一类资源（看板按 tab 单刷，省时，不全量重扫）

用法：python server.py [--port 8765] [--no-open]
"""
import json
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from html import escape as html_escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from .. import deploy_config
from .. import ops_log
from .. import paths

HERE = Path(__file__).resolve().parent        # 包内 dashboard 资产目录（index.html / vendor / favicon）
DATA = paths.data_json_path()                  # 工作区数据
INDEX = HERE / "index.html"                    # 包内静态资产
RUNBOOKS = paths.runbooks_dir()                # 工作区 runbooks

# 全量 sync 约需 1-2 分钟；加锁防止多标签页/连点并发跑多个 sync 抢写同一个 data.json。
_SYNC_LOCK = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # 安静
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        body = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            if INDEX.exists():
                self._send(200, INDEX.read_bytes(), "text/html; charset=utf-8")
            else:
                self._send(500, "index.html 缺失", "text/plain; charset=utf-8")
        elif self.path.startswith("/data.json"):
            if DATA.exists():
                self._send(200, DATA.read_bytes())
            else:
                self._send(200, json.dumps({"generated_at": None, "totals": {}, "instances": [],
                                            "error": "尚无 data.json，请先运行 ops sync"}))
        elif self.path.startswith("/ops-log.json"):
            # 轻量：直接读 .state/ops-log.jsonl（实时），不跑全量 sync，供日志 tab 自动刷新
            self._send(200, json.dumps(ops_log.tail(), ensure_ascii=False))
        elif self.path.startswith("/ops-config.json"):
            self._send_ops_config()         # 部署配置（非密），供「纳管范围」tab
        elif self.path.startswith("/api/meta"):
            # noc **支持**的云（能力声明）——单一来源=Provider 注册表，加一朵云自动反映，标题不漂移。
            # 回传 cloud(=CSS 配色类 cloud-<id>) + display(显示名)，供标题按品牌色渲染。
            from ..providers import PROVIDERS
            clouds = [{"cloud": p.cloud, "display": p.display}
                      for p in sorted(PROVIDERS.values(), key=lambda x: x.order)]
            self._send(200, json.dumps({"supported_clouds": clouds}, ensure_ascii=False))
        elif self.path.startswith("/runbook"):
            self._serve_runbook()
        else:
            self._serve_static(self.path.lstrip("/").split("?")[0])

    def _send_ops_config(self):
        """ops-config.toml 的 JSON 视图（非密），供「纳管范围」tab。tuple 键 dict 转 list。"""
        def kv_list(d, *names):    # {(profile,region): val|tuple} -> [{profile,region,<names>}]
            rows = []
            for (p, r), v in d.items():
                row = {"profile": p, "region": r}
                vals = v if isinstance(v, (tuple, list)) else (v,)
                for n, x in zip(names, vals):
                    row[n] = x
                rows.append(row)
            return rows
        s = deploy_config.scheduler()
        cfg = {
            "accounts": deploy_config.aws_accounts(),
            "scan_regions_global": deploy_config.scan_regions("global"),
            "scan_regions_china": deploy_config.scan_regions("china"),
            "route53_profiles": deploy_config.route53_profiles(),
            "alias_prefix": kv_list(deploy_config.aws_alias_prefix(), "prefix"),
            "jumps": kv_list(deploy_config.aws_jumps(), "alias", "instance"),
            "aliyun": deploy_config.cloud_accounts("aliyun"),   # 多账号 → list
            "tencent": deploy_config.cloud_accounts("tencent"),
            "volcengine": deploy_config.cloud_accounts("volcengine"),   # 多账号 → list，account=主账号 ID
            # 非 AWS 各云：统一把 account 字段归一成"与 data.json 资源 account 对齐的标识"，纳管范围面板按 account 读。
            "gcp": [{**a, "account": a.get("project")} for a in deploy_config.cloud_accounts("gcp")],
            # Azure：account=订阅 id（data.json 里 Azure 资源的 account 也是订阅 id）
            "azure": [{**a, "account": a.get("subscription")} for a in deploy_config.cloud_accounts("azure")],
            # Oracle：account=展示名（租户名 <tenancy-name>；与 data.json 里 OCI 资源的 account 对齐），回退 profile
            "oracle": [{**a, "account": a.get("account") or a.get("profile")} for a in deploy_config.cloud_accounts("oracle")],
            "cloudflare": deploy_config.cloud_accounts("cloudflare"),   # DNS/域名，无密钥（token 在 Keychain）
            "supabase": deploy_config.cloud_accounts("supabase"),       # 托管 Postgres 项目，无密钥（PAT 在 Keychain）
            "scheduler": {
                "timezone": s.get("timezone"), "display_tz": s.get("display_tz"),
                "sched_accounts": deploy_config.sched_accounts(),
                "dispatch": deploy_config.dispatch(),
                "dispatch_clouds": [list(t) for t in deploy_config.dispatch_clouds()],
            },
        }
        self._send(200, json.dumps(cfg, ensure_ascii=False, default=str))

    def _serve_runbook(self):
        # /runbook?name=<kebab> —— 只读展示某个 runbook 的 markdown，限定在 runbooks/ 内
        name = (parse_qs(urlparse(self.path).query).get("name") or [""])[0]
        if not re.fullmatch(r"[a-z0-9-]+", name):
            return self._send(400, "bad runbook name", "text/plain; charset=utf-8")
        target = (RUNBOOKS / f"{name}.md").resolve()
        if RUNBOOKS not in target.parents or not target.is_file():
            return self._send(404, "runbook not found", "text/plain; charset=utf-8")
        md = target.read_text(encoding="utf-8")
        page = (
            "<!doctype html><meta charset=utf-8>"
            "<meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>{html_escape(name)} · runbook</title><style>"
            "body{margin:0;background:#0d1117;color:#c9d1d9;"
            "font:14px/1.7 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}"
            "pre{white-space:pre-wrap;word-wrap:break-word;max-width:920px;margin:0 auto;padding:32px 24px}"
            "@media(prefers-color-scheme:light){body{background:#fff;color:#1f2328}}"
            f"</style><pre>{html_escape(md)}</pre>")
        self._send(200, page, "text/html; charset=utf-8")

    _CTYPES = {".js": "text/javascript", ".css": "text/css", ".json": "application/json",
               ".svg": "image/svg+xml", ".png": "image/png", ".woff2": "font/woff2", ".map": "application/json"}

    def _serve_static(self, rel):
        # 仅允许 noc 目录内的文件，防路径穿越
        target = (HERE / rel).resolve()
        if HERE not in target.parents or not target.is_file():
            return self._send(404, "not found", "text/plain; charset=utf-8")
        ctype = self._CTYPES.get(target.suffix, "application/octet-stream")
        self._send(200, target.read_bytes(), ctype)

    def do_POST(self):
        if self.path.startswith("/api/refresh"):
            # ?kind=<资源键> → 只刷该 tab（ops sync --kind=...，不全量重扫）；无 kind → 全量
            kind = (parse_qs(urlparse(self.path).query).get("kind") or [""])[0]
            if kind and not re.fullmatch(r"[a-z_]+", kind):
                return self._send(400, "bad kind", "text/plain; charset=utf-8")
            if not _SYNC_LOCK.acquire(blocking=False):
                return self._send(200, json.dumps({"ok": False, "report": "",
                                                   "stderr": "已有同步在进行，请稍候再试"}))
            try:
                cmd = [sys.executable, "-m", "noc_ops.sync"] + ([f"--kind={kind}"] if kind else [])
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
                ok = r.returncode == 0
                # 取报告里 ===== 之后的部分
                rep = r.stdout
                if "===== 同步报告 =====" in rep:
                    rep = rep.split("===== 同步报告 =====", 1)[1].strip()
                self._send(200, json.dumps({"ok": ok, "report": rep[:4000],
                                            "stderr": r.stderr[-2000:] if not ok else ""}))
            except subprocess.TimeoutExpired:
                self._send(200, json.dumps({"ok": False, "report": "", "stderr": "sync 超时(600s)"}))
            finally:
                _SYNC_LOCK.release()
        elif self.path == "/api/terminal":
            length = int(self.headers.get("Content-Length") or 0)
            try:
                iid = json.loads(self.rfile.read(length) or b"{}").get("instance_id", "")
            except (json.JSONDecodeError, ValueError):
                iid = ""
            self._send(200, json.dumps(_launch_terminal(iid), ensure_ascii=False))
        else:
            self._send(404, "not found", "text/plain; charset=utf-8")


# --------------------------------------------------------------------------
# 生命周期管理：start / stop / status / restart（用端口监听进程定位，免 PID 文件）
# --------------------------------------------------------------------------
def _parse(argv):
    sub = argv[0] if argv and not argv[0].startswith("-") else "start"
    port = 8765
    if "--port" in argv:
        try:
            port = int(argv[argv.index("--port") + 1])
        except (ValueError, IndexError):
            pass
    return sub, port, ("--no-open" in argv)


def _listeners_procfs(port):
    """Linux 兜底：无 lsof 时解析 /proc/net/tcp{,6} 找 LISTEN(0A) 的 socket inode，再扫 /proc/*/fd 反查 PID。
    纯 stdlib、无外部命令；容器/精简发行版（Debian slim 等）默认没有 lsof，靠这条路。"""
    inodes = set()
    for tbl in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(tbl) as f:
                next(f)                                  # 表头
                for line in f:
                    cols = line.split()
                    if len(cols) < 10 or cols[3] != "0A":
                        continue
                    if int(cols[1].rsplit(":", 1)[1], 16) == port:
                        inodes.add(cols[9])
        except (OSError, ValueError, StopIteration):
            continue
    if not inodes:
        return []
    pids = []
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        try:
            for fd in os.listdir(f"/proc/{d}/fd"):
                try:
                    tgt = os.readlink(f"/proc/{d}/fd/{fd}")
                except OSError:
                    continue
                if tgt.startswith("socket:[") and tgt[8:-1] in inodes:
                    pids.append(int(d))
                    break
        except OSError:
            continue
    return pids


def _listeners(port):
    """端口上处于 LISTEN 的进程 PID 列表。优先 lsof（macOS/大多数 Linux），没有 lsof 时走 /proc。"""
    if shutil.which("lsof"):
        try:
            r = subprocess.run(["lsof", f"-tiTCP:{port}", "-sTCP:LISTEN"],
                               capture_output=True, text=True, timeout=10)
            return [int(x) for x in r.stdout.split() if x.strip().isdigit()]
        except Exception:
            return []
    if os.path.isdir("/proc/net"):
        return _listeners_procfs(port)
    return []


def _proc_cmd(pid):
    # Linux 直接读 /proc（精简镜像可能没有 procps 的 ps）
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            raw = f.read()
        if raw:
            return raw.replace(b"\0", b" ").decode(errors="replace").strip()
    except OSError:
        pass
    try:
        return subprocess.run(["ps", "-p", str(pid), "-o", "command="],
                             capture_output=True, text=True).stdout.strip()
    except Exception:
        return ""


def _is_ours(pid):
    """该监听进程是否是 noc 看板（防误杀端口上的其它服务）。
    匹配守护子进程(`-m noc_ops.…`→含 noc_ops)、venv 路径(noc-ops)、前台 `ops noc`；
    不用宽松的 'ops'&'noc' 各自子串，避免误判无关进程。"""
    c = _proc_cmd(pid).lower()
    return ("noc_ops" in c) or ("noc-ops" in c) or ("ops noc" in c)


def _free_port():
    """取一个空闲的 localhost 端口（小竞态可接受）。"""
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def _keychain_ssh_pw(alias: str) -> str:
    """从 Keychain 取 SSH 密码（service=noc-ssh, account=ssh_alias）。取不到返回空串。"""
    try:
        r = subprocess.run(
            ["security", "find-generic-password", "-s", "noc-ssh", "-a", alias, "-w"],
            capture_output=True, text=True, timeout=5)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


# 空闲时每 30s 发一次 SSH 层心跳，防中间 NAT/防火墙把静默连接掐掉；连丢 3 次（~90s）才判断线
_SSH_KEEPALIVE = ["ssh", "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=3"]


def _reap_unused_terminal(proc):
    """回收没用上的 ttyd。ttyd 只在浏览器连上后才拉起 ssh 子进程，有子进程=正在用，不杀；
    用完关标签页后 ttyd 因 -o 自行退出。"""
    if proc.poll() is not None:
        return
    try:
        in_use = subprocess.run(["pgrep", "-P", str(proc.pid)],
                                capture_output=True, timeout=5).returncode == 0
    except Exception:
        in_use = True          # 查不了宁可留着，也别误杀正在用的会话
    if not in_use:
        proc.kill()


def _launch_terminal(instance_id):
    """按需起一个一次性、localhost、带随机 token 的 ttyd 网页终端，SSH 到该实例。返回 {ok,url|error}。

    安全硬隔离：仅对 data.json 已知实例；ssh 别名来自可信 data.json，**list argv 不走 shell**（无注入）；
    只绑 127.0.0.1；临时端口；随机 token 当 HTTP basic auth；-O 挡跨源 WS；-o/-m 1 一次性单客户端；
    Timer 兜底回收 orphan。**注意：终端内命令不进 ops_log 审计**（exec 不经 agent 闸门，已知局限）。
    """
    if not shutil.which("ttyd"):
        return {"ok": False, "error": "本机未装 ttyd（brew install ttyd）"}
    try:
        data = json.loads(DATA.read_text())
    except (OSError, json.JSONDecodeError):
        return {"ok": False, "error": "data.json 不可读，请先 ops sync"}
    inst = next((i for i in data.get("instances", []) if i.get("id") == instance_id), None)
    if not inst:
        return {"ok": False, "error": f"未找到实例 {instance_id}"}
    alias = inst.get("ssh_alias")
    # 本地机无 alias，用 private_ip + ssh_user + ssh_port 直接构建 SSH argv
    if not alias:
        ip      = inst.get("private_ip")
        user    = inst.get("ssh_user")
        ssh_port = inst.get("ssh_port")
        if not ip or not user:
            return {"ok": False, "error": f"{inst.get('name', instance_id)} 无 SSH 别名且缺少 IP/用户名"}
        ssh_argv = _SSH_KEEPALIVE + ["-o", "StrictHostKeyChecking=no"]
        if ssh_port and int(ssh_port) != 22:
            ssh_argv += ["-p", str(ssh_port)]
        ssh_argv.append(f"{user}@{ip}")
        alias = f"{user}@{ip}"
    else:
        ssh_argv = _SSH_KEEPALIVE + [alias]

    # 若 Keychain 有该别名的 SSH 密码，用 sshpass -e 免密直连
    env = None
    pw = _keychain_ssh_pw(alias)
    if pw and shutil.which("sshpass"):
        ssh_argv = ["sshpass", "-e"] + ssh_argv
        env = {**os.environ, "SSHPASS": pw}

    port, token = _free_port(), secrets.token_urlsafe(16)
    proc = subprocess.Popen(
        ["ttyd", "-i", "127.0.0.1", "-p", str(port), "-c", f"noc:{token}",
         "-W", "-O", "-o", "-m", "1", "-t", f"titleFixed={inst.get('name', alias)}"] + ssh_argv,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env=env, start_new_session=True)
    # 等 ttyd 真正 listen 再返回，否则浏览器抢在绑定端口前打开会看到「无法访问」页
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return {"ok": False, "error": f"ttyd 启动即退出 (exit {proc.returncode})"}
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.1).close()
            break
        except OSError:
            time.sleep(0.05)
    else:
        proc.kill()
        return {"ok": False, "error": "ttyd 3 秒内未就绪，已回收"}
    threading.Timer(600, _reap_unused_terminal, args=(proc,)).start()   # 10 分钟没用上就杀
    return {"ok": True, "url": f"http://noc:{token}@127.0.0.1:{port}/", "alias": alias}


def _serve(port):
    """实际阻塞服务（前台模式 / 守护子进程都跑这个）。"""
    paths.ensure_workspace()
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


def _do_start(port, no_open, foreground=False):
    paths.ensure_workspace()
    mine = [p for p in _listeners(port) if _is_ours(p)]
    if mine:
        print(f"NOC 看板已在运行 (PID {mine[0]})：http://127.0.0.1:{port}/。重启用 `ops noc restart`。")
        return 0
    others = [p for p in _listeners(port) if not _is_ours(p)]
    if others:
        print(f"⚠️ 端口 {port} 被其它进程占用 (PID {others[0]}: {_proc_cmd(others[0])[:50]})；"
              f"换端口：`ops noc --port <N>`。")
        return 1
    if not DATA.exists():
        print("提示：还没有 data.json，先跑一次 `ops sync` 生成数据。")
    url = f"http://127.0.0.1:{port}/"
    if foreground:                       # ops noc -f：前台跑，占终端，Ctrl+C 退出
        print(f"NOC 运维看板已启动（前台）：{url}（Ctrl+C 退出）")
        if not no_open:
            webbrowser.open(url)
        try:
            _serve(port)
        except KeyboardInterrupt:
            print("\n已退出。")
        return 0
    # 默认：后台守护启动（detach，关终端不死，日志进 .state/noc.log）
    log = paths.state_dir() / "noc.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    logf = open(log, "a")
    subprocess.Popen([sys.executable, "-m", "noc_ops.dashboard.server", "_serve", "--port", str(port)],
                     stdout=logf, stderr=logf, stdin=subprocess.DEVNULL, start_new_session=True)
    logf.close()                         # 子进程已 dup 该 fd，父进程关掉自己这份（避免句柄泄露）
    for _ in range(25):                  # 等它起来（最多 ~5s）
        time.sleep(0.2)
        if _listeners(port):
            break
    mine = [p for p in _listeners(port) if _is_ours(p)]
    if mine:
        print(f"NOC 看板已后台启动：{url} (PID {mine[0]})。日志 {log}；管理：ops noc status / stop / restart。")
        if not no_open:
            webbrowser.open(url)
        return 0
    print(f"⚠️ 后台启动未确认（端口 {port} 未监听），见日志 {log}。")
    return 1


def _do_stop(port):
    pids = _listeners(port)
    if not pids:
        print(f"NOC 看板未在运行（端口 {port} 无监听）。")
        return 0
    killed = []
    for pid in pids:
        if _is_ours(pid):
            try:
                os.kill(pid, signal.SIGTERM)
                killed.append(pid)
            except ProcessLookupError:
                pass
        else:
            print(f"⚠️ 端口 {port} 被非 noc 进程占用 (PID {pid}: {_proc_cmd(pid)[:50]})，未动。")
    if killed:
        # 等被杀进程真正退出、端口释放（SIGTERM 通常瞬杀，给点余量让 restart 不撞端口）
        for _ in range(15):
            if not [p for p in _listeners(port) if _is_ours(p)]:
                break
            time.sleep(0.2)
        print(f"已停止 NOC 看板 (PID {', '.join(map(str, killed))})。")
    return 0


def _do_status(port):
    mine = [p for p in _listeners(port) if _is_ours(p)]
    if mine:
        print(f"NOC 看板运行中：http://127.0.0.1:{port}/ (PID {', '.join(map(str, mine))})")
    else:
        print(f"NOC 看板未运行（端口 {port}）。用 `ops noc` 启动。")
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    sub, port, no_open = _parse(argv)
    if sub == "_serve":            # 内部：守护子进程实际跑的阻塞服务
        _serve(port)
        return 0
    if sub == "stop":
        return _do_stop(port)
    if sub == "status":
        return _do_status(port)
    if sub == "restart":
        _do_stop(port)
        time.sleep(1)
        return _do_start(port, no_open)
    if sub == "start":
        fg = ("--foreground" in argv) or ("-f" in argv)
        return _do_start(port, no_open, foreground=fg)
    print(f"未知 noc 子命令: {sub}（可用: start / stop / status / restart）", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
