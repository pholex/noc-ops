#!/usr/bin/env bash
# 容器内执行：模拟新用户按 INSTALL.md 分步安装；每步记录退出码，不中断。
# 预期：1/2/5/6 退出 0；3（无账号）与 4（缺云 CLI）退出 1 属正常；5 的 sync 必须不 Traceback。
step() { echo; echo "############ STEP: $1"; }
run()  { echo "\$ $*"; "$@"; rc=$?; echo "[exit=$rc]"; return 0; }
WHEEL="$(ls /home/tester/noc_ops-*.whl | head -1)"

step "0 环境"
run uname -a; run python3 --version; run uv --version; run node --version
step "1 uv tool install wheel"
run uv tool install "$WHEEL"
run which ops ops-mcp
run ops --help
step "2 ops setup --workspace ~/noc"
run ops setup --workspace /home/tester/noc
run ls -la /home/tester/noc
run ls -la /home/tester/.agents/skills
run ls -la /home/tester/.claude/skills
step "3 ops init（无云 CLI，空输入走默认；预期 exit 1：未配置任何账号）"
yes '' | timeout 60 ops init; echo "[exit=$?]"
step "4 ops doctor / --fix（预期 exit 1：缺云 CLI）"
run ops doctor
run ops doctor --fix
step "5 ops sync（无任何云 CLI：应跳过各云、正常收尾，exit 0）"
timeout 120 ops sync; echo "[exit=$?]"
step "6 ops noc（看板）启停"
timeout 30 ops noc start --no-open; echo "[exit=$?]"
sleep 2
run ops noc status
run curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8765/
run ops noc stop
step "7 其他只读子命令"
run ops netprobe --help
run ops license status
step "8 可选 extra / 扩展包未装时的提示（预期 exit 2 + 提示，而非 Traceback）"
timeout 5 ops-mcp </dev/null; echo "[exit=$?]"
timeout 5 ops "有几台机器"; echo "[exit=$?]"
step "9 工作区文件清单"
run find /home/tester/noc -maxdepth 2 -type f
run ls -la /home/tester/.ssh
