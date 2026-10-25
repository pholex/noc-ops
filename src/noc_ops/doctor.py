"""Diagnose configured accounts only. Offline checks never contact a cloud API."""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

from . import paths, credentials, __version__
from .cloud_config import CLOUDS, accounts, configured, validate_account
from .setup_clients import SKILL_NAME


def check(name, status, detail="", hint="", **scope):
    return dict(name=name, status=status, detail=detail, hint=hint, **scope)


def credential_command(cloud, a):
    if cloud == "aws":
        return ["aws", "sts", "get-caller-identity", "--profile", a["profile"], "--output", "json"]
    if cloud == "aliyun":
        return ["aliyun", "sts", "GetCallerIdentity", "--profile", a["profile"]]
    if cloud == "tencent":
        return ["tccli", "sts", "GetCallerIdentity", "--output", "json"]
    if cloud == "volcengine":
        return ["ve", "sts", "GetCallerIdentity", "--region", next(iter(a.get("regions", {})), "cn-beijing")]
    if cloud == "gcp":
        return ["gcloud", "projects", "describe", a["project"], "--format=json", "--quiet"] + (
            ["--configuration", a["configuration"]] if a.get("configuration") else [])
    if cloud == "azure":
        return ["az", "rest", "--method", "GET", "--url",
                f"https://management.azure.com/subscriptions/{a['subscription']}?api-version=2022-12-01",
                "--subscription", a["subscription"], "--output", "json", "--only-show-errors"]
    if cloud == "oracle":
        return ["oci", "iam", "region", "list", "--profile", a["profile"], "--output", "json"]
    return None


def probe(cloud, account):
    """Return (ok, sanitized description); do not echo credentials or raw responses."""
    try:
        if cloud in ("cloudflare", "supabase"):
            token = credentials.token(account, cloud)
            if not token:
                return False, "未找到 token（检查环境变量名或 Keychain）"
            if cloud == "cloudflare":
                from .providers.cloudflare import CloudflareProvider
                data = CloudflareProvider()._get(token, "/user/tokens/verify", quiet=True)
                return bool(data and (data.get("result") or {}).get("status") == "active"), "API token 验证"
            from .providers.supabase import SupabaseProvider
            ok, data = SupabaseProvider()._call(token, "/v1/projects", quiet=True)
            return bool(ok and isinstance(data, list)), "Management API 项目列表访问"
        command = credential_command(cloud, account)
        result = subprocess.run(command, capture_output=True, text=True, timeout=20,
                                stdin=subprocess.DEVNULL, env={**os.environ, "AWS_PAGER": "",
                                "AZURE_EXTENSION_USE_DYNAMIC_INSTALL": "no"})
        if result.returncode:
            return False, f"身份或项目访问检查失败（退出码 {result.returncode}）"
        raw = result.stdout.strip()
        data = json.loads(raw[raw.index("{"):] if cloud == "volcengine" else raw)
        if not isinstance(data, dict) or not data or data.get("Error") or data.get("error") or data.get("error_code"):
            return False, "身份接口返回错误或无有效数据"
        if data.get("Success") is False or (data.get("ResponseMetadata") or {}).get("Error"):
            return False, "身份接口返回业务错误"
        body = data.get("Response", data.get("Result", data))
        if not isinstance(body, dict) or body.get("Error"):
            return False, "身份接口返回业务错误"
        actual = body.get("Account") or body.get("AccountId")
        expected = account.get("account") or (account.get("profile") if cloud == "aws" and str(account.get("profile", "")).isdigit() else None)
        if expected and str(expected).isdigit() and actual is not None and str(actual) != str(expected):
            return False, "当前 CLI 身份与配置账号不一致"
        if cloud in ("aws", "aliyun", "tencent", "volcengine") and actual is None:
            return False, "身份响应缺少账号标识"
        return True, "身份或项目访问可用（不代表所有资源权限均已验证）"
    except subprocess.TimeoutExpired:
        return False, "身份检查超时（20 秒）"
    except (OSError, ValueError, TypeError):
        return False, "身份检查失败或响应不可解析"


def secret_fields(data, prefix=""):
    bad = []
    for key, value in data.items():
        path = f"{prefix}.{key}" if prefix else key
        if (re.search(r"(^|_)(password|secret|access_key|api_key|token)$", key.lower())
                or key.lower() in ("aws_access_key_id", "aws_secret_access_key", "private_key", "secret_key", "client_secret")) and key != "token_env":
            bad.append(path)
        if isinstance(value, str) and "-----BEGIN " in value:
            bad.append(path)
        if isinstance(value, dict):
            bad.extend(secret_fields(value, path))
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    bad.extend(secret_fields(item, path))
    return bad


def diagnose(*, offline=False, clouds=None):
    rows = []
    ws, cfg = paths.workspace_dir(), paths.deploy_config_path()
    rows.append(check("工作区", "ok" if ws.is_dir() and os.access(ws, os.W_OK) else "error", str(ws), "ops setup --workspace <可写目录>"))
    try:
        data = tomllib.loads(cfg.read_text())
        rows.append(check("部署配置", "ok", "配置可解析"))
    except (OSError, ValueError):
        rows.append(check("部署配置", "error", "配置缺失或 TOML 无法解析", "ops init"))
        return rows
    fields = secret_fields(data)
    rows.append(check("凭证存放", "error" if fields else "ok",
                      "发现不应存放在配置中的凭证字段（内容未显示）" if fields else "未发现凭证字段",
                      "将凭证移到云 CLI、环境变量或 Keychain" if fields else ""))
    count = 0
    for cloud, spec in CLOUDS.items():
        if clouds and cloud not in clouds:
            continue
        try:
            configured_accounts = accounts(data, cloud)
        except ValueError:
            rows.append(check(spec.label, "error", "账号列表格式错误", "ops init --cloud " + cloud))
            continue
        if not configured_accounts:
            rows.append(check(spec.label, "skipped", "未配置，无需安装或登录", cloud=cloud))
            continue
        count += len(configured_accounts)
        cli_ok = not spec.cli or shutil.which(spec.cli) is not None
        rows.append(check(spec.label + " CLI", "ok" if cli_ok else "error", spec.cli or "无需 CLI（API token）",
                          "安装对应云 CLI，再运行 ops doctor --cloud " + cloud, cloud=cloud))
        if cloud in ("tencent", "volcengine"):
            auxiliary = "coscli" if cloud == "tencent" else "tosutil"
            rows.append(check(auxiliary, "ok" if shutil.which(auxiliary) else "warning",
                              "对象存储工具；仅纳管 COS/TOS 时需要", "按需安装 " + auxiliary, cloud=cloud))
        for a in configured_accounts:
            label = str(a.get(spec.identity) or "未填写")
            scope = dict(cloud=cloud, account=label)
            errors = validate_account(cloud, a)
            if not spec.multiple and len(configured_accounts) > 1:
                errors.append("当前驱动只支持一个默认 CLI 账号")
            rows.append(check("账号配置", "error" if errors else "ok", "；".join(errors) or "必要字段齐全",
                              "ops init --cloud " + cloud, **scope))
            if errors or not cli_ok:
                rows.append(check("凭证与访问", "skipped", "先修复配置或 CLI", **scope))
            elif offline:
                rows.append(check("凭证与访问", "skipped", "离线模式未验证", spec.login, **scope))
            else:
                ok, detail = probe(cloud, a)
                rows.append(check("凭证与访问", "ok" if ok else "error", detail, spec.login, **scope))
    if not count:
        local = any(data.get(k) for k in ("local", "sangfor", "vmware", "synology")) or paths.workspace_dir().joinpath("local-servers.toml").exists()
        rows.append(check("纳管账号", "ok" if local else "warning", "仅配置本地网络机器" if local else "尚无云账号", "ops init"))
    skill = Path.home() / ".agents/skills" / SKILL_NAME / "SKILL.md"
    skill_ok = skill.exists() or (Path.home() / ".claude/skills" / SKILL_NAME / "SKILL.md").exists()
    from .update import skill_version
    installed_skill = skill_version()
    rows.append(check("客户端 skill", "ok" if skill_ok and installed_skill == __version__ else "warning",
                      f"工具 {__version__} / skill {installed_skill or '未标注或未安装'}", "ops setup"))
    rows.append(check("资源清单", "ok" if paths.data_json_path().exists() else "warning", "已生成" if paths.data_json_path().exists() else "尚未生成", "ops sync"))
    return rows


def main(argv=None):
    parser = argparse.ArgumentParser(description="按实际配置体检；只读验证身份，不执行资源变更。")
    parser.add_argument("--offline", action="store_true", help="只检查本机配置和依赖，不验证云凭证")
    parser.add_argument("--json", action="store_true", help="结构化输出")
    parser.add_argument("--cloud", action="append", choices=CLOUDS)
    parser.add_argument("--fix", action="store_true", help="创建工作区并刷新客户端 skill")
    args = parser.parse_args(argv)
    if args.fix:
        from contextlib import redirect_stdout
        from . import setup_clients
        with redirect_stdout(sys.stderr):
            paths.ensure_workspace()
            setup_clients.main([])
    rows = diagnose(offline=args.offline, clouds=args.cloud)
    ok = not any(r["status"] == "error" for r in rows)
    if args.json:
        print(json.dumps({"ok": ok, "offline": args.offline, "checks": rows}, ensure_ascii=False, indent=2))
    else:
        print("ops doctor —— " + ("本机离线检查（未验证云凭证）" if args.offline else "配置与云访问检查"))
        icons = {"ok": "✓", "warning": "!", "error": "✗", "skipped": "·"}
        for row in rows:
            scope = "/".join(str(row[k]) for k in ("cloud", "account") if row.get(k))
            print(f"  {icons[row['status']]} {scope + ' ' if scope else ''}{row['name']}：{row['detail']}")
            if row["status"] in ("error", "warning") and row["hint"]:
                print("    下一步：" + row["hint"])
        print("检查完成" if ok else "存在未通过项，请按提示处理。")
    return 0 if ok else 1
