"""ops init —— 交互式配置向导：问答式生成 <workspace>/ops-config.toml。

按需选择九朵云，发现 AWS profile 或输入各云非密账号元数据。
按身份合并已有账号，保留其它配置；预览后确认、备份并原子写入。

安全：只问/只写**非密**信息（账号 ID / 区域 / profile 名 / ssh 用户名 / key 文件名）；
绝不碰 secret（云凭证仍在 ~/.aws、~/.tccli、~/.aliyun 各自管）。
"""
import argparse
import copy
import tempfile
import tomllib
from datetime import datetime
import configparser
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import paths, toml_io
from .cloud_config import CLOUDS, accounts as cloud_accounts, configured, validate_account

AWS_DIR = Path.home() / ".aws"
COMMON_GLOBAL_REGIONS = ["us-east-1", "us-west-2", "ap-northeast-1", "ap-east-1"]
DEFAULT_CHINA_REGIONS = ["cn-northwest-1"]


# ---- 纯函数（便于单测）-------------------------------------------------------

def aws_profiles() -> list:
    """从 ~/.aws/config 与 ~/.aws/credentials 收集 profile 名（去重保序）。
    credentials 段名即 profile 名；config 段名为 `[default]` 或 `[profile <名>]`。"""
    names = []
    cred = configparser.ConfigParser()
    if (AWS_DIR / "credentials").exists():
        try:
            cred.read(AWS_DIR / "credentials")
            names += list(cred.sections())
        except configparser.Error:
            pass
    conf = configparser.ConfigParser()
    if (AWS_DIR / "config").exists():
        try:
            conf.read(AWS_DIR / "config")
            for s in conf.sections():
                if s == "default" or s.startswith("profile "):
                    names.append(s[len("profile "):] if s.startswith("profile ") else s)
        except configparser.Error:
            pass
    seen, out = set(), []
    for n in names:
        if n and n not in seen:
            seen.add(n)
            out.append(n)
    return out


def partition_from_arn(arn: str) -> str:
    """STS ARN → "china"（arn:aws-cn:…）| "global"（arn:aws:…）。"""
    parts = arn.split(":")
    return "china" if len(parts) > 1 and parts[1] == "aws-cn" else "global"


def caller_identity(profile: str):
    """`aws sts get-caller-identity --profile p` → (account_id, partition) 或 None。"""
    try:
        r = subprocess.run(
            ["aws", "sts", "get-caller-identity", "--profile", profile, "--output", "json"],
            capture_output=True, text=True, timeout=20,
            env={**os.environ, "AWS_PAGER": ""})
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    try:
        d = json.loads(r.stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(d, dict) or not d.get("Account") or not d.get("Arn"):
        return None
    return (d["Account"], partition_from_arn(d["Arn"]))


def _q(s) -> str:
    """TOML 双引号字符串。"""
    return toml_io.quote(s)


def _arr(xs) -> str:
    return "[" + ", ".join(_q(x) for x in xs) + "]"


def emit_toml(route53, regions_global, regions_china, accounts, aliyun, tencent) -> str:
    """把向导收集到的结构拼成 ops-config.toml 文本（与 ops-config.default.toml 同 schema）。"""
    L = ["# ops 部署配置 —— 由 `ops init` 生成。非密、可 git 跟踪、可移植。",
         "# 只放非密信息（账号 ID / 区域 / profile 名 / ssh 用户名 / key 文件名）；绝不放 secret。",
         "",
         "# ===== AWS =====",
         "[aws]",
         f"route53_profiles = {_arr(route53)}",
         f"scan_regions_global = {_arr(regions_global)}",
         f"scan_regions_china = {_arr(regions_china)}",
         ""]
    for a in accounts:
        L += ["[[aws.accounts]]",
              f"profile = {_q(a['profile'])}",
              f"partition = {_q(a['partition'])}",
              ""]
    # 别名前缀按区域昵称自动派生、跳板仅无公网内网实例需要——留空段，需要时手填
    L += ['[aws.alias_prefix]   # "<profile>/<region>" = "别名前缀"（留空=按区域昵称派生）',
          "",
          '[aws.jumps]          # "<profile>/<region>" = ["跳板别名", "跳板实例名"]（仅内网实例需要）',
          ""]
    if aliyun:
        L += ["# ===== 阿里云 ECS =====",
              "[aliyun]",
              f"profile = {_q(aliyun['profile'])}",
              f"account = {_q(aliyun['account'])}",
              f"ssh_user = {_q(aliyun['ssh_user'])}",
              f"ssh_key = {_q(aliyun['ssh_key'])}",
              "[aliyun.regions]",
              f"{_q(aliyun['region'])} = {_arr([aliyun['display'], aliyun['flag']])}",
              ""]
    if tencent:
        L += ["# ===== 腾讯云 Lighthouse =====",
              "[tencent]",
              f"account = {_q(tencent['account'])}",
              f"ssh_user = {_q(tencent['ssh_user'])}",
              f"ssh_key = {_q(tencent['ssh_key'])}",
              "[tencent.regions]",
              f"{_q(tencent['region'])} = {_arr([tencent['display'], tencent['flag']])}",
              ""]
    return "\n".join(L) + "\n"


# ---- 交互 -------------------------------------------------------------------

def _ask(prompt, default=""):
    s = input(f"{prompt}" + (f" [{default}]" if default else "") + ": ").strip()
    return s or default


def _yn(prompt, default=True):
    s = input(f"{prompt} [{'Y/n' if default else 'y/N'}]: ").strip().lower()
    return default if not s else s in ("y", "yes", "是")


def _manual_add(accounts):
    """手动逐个录入 AWS 账号（profile 发现不到/未选时的兜底）。"""
    while _yn("手动添加一个 AWS 账号？", default=not accounts):
        acct = _ask("  账号 ID 或 profile 名")
        if not acct:
            break
        part = "china" if _yn("  是中国区(aws-cn)账号？", default=False) else "global"
        accounts.append({"profile": acct, "partition": part})


def _collect_aws():
    """发现并选定纳管的 AWS 账号；返回 (accounts, route53)。"""
    accounts, route53 = [], []
    if not shutil.which("aws"):
        print("⚠️ 未找到 aws CLI（`brew install awscli`）；可手动输入账号 ID。")
    profiles = aws_profiles()
    if profiles:
        print(f"发现 {len(profiles)} 个 AWS profile：{', '.join(profiles)}\n")
        for p in profiles:
            ident = caller_identity(p)
            if not ident:
                print(f"  · {p}: 凭证无法验证，跳过（可稍后 `aws configure --profile {p}`）")
                continue
            acct, part = ident
            if _yn(f"  · profile {p} → 账号 {acct}（{part}）纳入纳管？"):
                accounts.append({"profile": p, "partition": part})
                if part == "global" and _yn("    该账号扫描 Route53 域名？", default=False):
                    route53.append(p)
    else:
        print("未发现 AWS profile。建议先 `aws configure --profile <名>`，或在此手动输入账号 ID。")
    # profile 发现不到、或一个都没选中 → 给手动录入兜底
    if not accounts:
        _manual_add(accounts)
    return accounts, route53


def _warn_ssh_keys(accounts):
    """提示每个账号目录下的私钥情况（约定 ~/.ssh/<账号>/<KeyName>.pem）。"""
    for a in accounts:
        d = Path.home() / ".ssh" / a["profile"]
        pems = sorted(d.glob("*.pem")) if d.exists() else []
        if pems:
            print(f"  🔑 {a['profile']}: {len(pems)} 个私钥（{d}）")
        else:
            print(f"  ⚠️ {a['profile']}: 未在 {d} 找到 .pem——sync 时部分实例可能缺私钥"
                  f"（约定 ~/.ssh/<账号>/<KeyName>.pem）")


def _collect_regions(accounts):
    has_global = any(a["partition"] == "global" for a in accounts)
    has_china = any(a["partition"] == "china" for a in accounts)
    rg, rc = [], []
    if has_global:
        s = _ask("global 扫描区域（逗号分隔）", ",".join(COMMON_GLOBAL_REGIONS))
        rg = [x.strip() for x in s.split(",") if x.strip()]
    if has_china:
        s = _ask("china 扫描区域（逗号分隔）", ",".join(DEFAULT_CHINA_REGIONS))
        rc = [x.strip() for x in s.split(",") if x.strip()]
    return rg, rc


def _collect_nonaws(name, label, ask_profile):
    """阿里/腾讯共用的小问答；返回 dict 或 None。"""
    if not _yn(f"配置{label}？", default=False):
        return None
    d = {}
    if ask_profile:
        d["profile"] = _ask(f"  {label} CLI profile 名", "default")
    d["account"] = _ask(f"  {label}账号 ID")
    d["ssh_user"] = _ask("  默认 SSH 用户名", "root" if name == "aliyun" else "ubuntu")
    d["ssh_key"] = _ask("  私钥文件名（~/.ssh/<账号>/<名>.pem，不含 .pem）", name)
    d["region"] = _ask("  区域 ID", "cn-hangzhou" if name == "aliyun" else "ap-nanjing")
    d["display"] = _ask("  区域显示名", "Hangzhou" if name == "aliyun" else "Nanjing")
    d["flag"] = _ask("  区域旗帜 emoji", "🇨🇳")
    return d


def _required(prompt, default=""):
    while True:
        value = _ask(prompt, default)
        if configured(value):
            return value
        print("  请输入有效值，不能留空或使用 <占位符>。")


def collect_cloud(cloud):
    spec = CLOUDS[cloud]
    rows = []
    while True:
        row = {key: _required(f"  {spec.label} {key}", "default" if key == "profile" else cloud if key == "account" and not spec.cli else "")
               for key in spec.required}
        if cloud == "gcp":
            row["configuration"] = _ask("  gcloud 命名配置（可留空）")
        if cloud == "oracle":
            row["account"] = _ask("  账号显示名", row["profile"])
        if spec.region or cloud == "oracle":
            region_ids = _ask("  扫描区域（逗号分隔；留空表示不扫描区域资源）", spec.region)
            row["regions"] = {r.strip(): [r.strip(), ""] for r in region_ids.split(",") if r.strip()}
            row["ssh_user"] = _ask("  SSH 用户名（可留空）")
            row["ssh_key"] = _ask("  SSH 私钥文件名（不含 .pem，可留空）")
        if not spec.cli:
            row["keychain_service"] = _ask("  macOS Keychain service", "noc-" + cloud)
            row["token_env"] = _ask("  token 所在的环境变量名（只填变量名，不填 token；可留空）")
            import re
            if row["token_env"] and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", row["token_env"]):
                raise ValueError("token_env 必须是环境变量名")
        errors = validate_account(cloud, row)
        if errors:
            raise ValueError("；".join(errors))
        rows.append(row)
        print(f"  凭证配置：{spec.login}")
        if not spec.multiple or not _yn(f"再添加一个{spec.label}账号？", False):
            break
    return rows


def merge_config(previous, additions):
    """Add/update selected identities, retaining all unrelated settings and accounts."""
    merged = copy.deepcopy(previous)
    # ops setup seeds an AWS example even for users who only configure another
    # cloud. Remove example identities, not real unselected accounts.
    def placeholder(value):
        return isinstance(value, str) and value.startswith("<") and value.endswith(">")
    for cloud, spec in CLOUDS.items():
        block = merged.get(cloud, {})
        if isinstance(block, dict) and isinstance(block.get("accounts"), list):
            block["accounts"] = [a for a in block["accounts"] if not placeholder(a.get(spec.identity))]
    aws = merged.get("aws", {})
    if "route53_profiles" in aws:
        aws["route53_profiles"] = [p for p in aws["route53_profiles"] if not placeholder(p)]
    scheduler = merged.get("scheduler", {})
    if isinstance(scheduler.get("accounts"), list):
        scheduler["accounts"] = [a for a in scheduler["accounts"] if not placeholder(a.get("profile"))]
    for cloud, block in additions.items():
        old = cloud_accounts(merged, cloud)
        # Drop the shipped placeholders, never real accounts.
        old = [copy.deepcopy(a) for a in old if configured(a.get(CLOUDS[cloud].identity))]
        for new in block.get("accounts", []):
            match = next((a for a in old if a.get(CLOUDS[cloud].identity) == new.get(CLOUDS[cloud].identity)), None)
            if match is None:
                if not CLOUDS[cloud].multiple and old:
                    raise ValueError(f"{CLOUDS[cloud].label} 当前只支持一个默认 CLI 账号；请先核对现有配置")
                old.append(copy.deepcopy(new))
            else:
                regions = {**match.get("regions", {}), **new.get("regions", {})}
                match.update(new)
                if regions:
                    match["regions"] = regions
        if cloud == "aws":
            target = merged.setdefault("aws", {})
            target["accounts"] = old
            for key, value in block.items():
                if key != "accounts":
                    target[key] = list(dict.fromkeys([x for x in target.get(key, []) if configured(x)] + value))
        else:
            # Normalize the legacy single-account table to the provider's accounts schema.
            target = merged.get(cloud, {})
            merged[cloud] = ({**target, "accounts": old} if "accounts" in target else {"accounts": old})
    return merged


def write_config(cfg, text):
    tomllib.loads(text)  # Validate before touching the current file.
    cfg.parent.mkdir(parents=True, exist_ok=True)
    if cfg.exists():
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        shutil.copy2(cfg, cfg.with_name(cfg.name + "." + stamp + ".bak"))
    fd, temporary = tempfile.mkstemp(prefix=".ops-config-", dir=cfg.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
        os.replace(temporary, cfg)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main(argv=None):
    parser = argparse.ArgumentParser(description="按所用云添加账号，保留已有配置；仅收集非密信息。")
    parser.add_argument("--cloud", action="append", choices=CLOUDS, help="要配置的云，可重复指定")
    parser.add_argument("--dry-run", action="store_true", help="预览新增配置，不写文件")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    cfg = paths.deploy_config_path()
    try:
        previous = tomllib.loads(cfg.read_text()) if cfg.exists() else {}
        print(f"ops init —— 工作区 {paths.workspace_dir()}")
        selected = args.cloud
        if not selected:
            names = list(CLOUDS)
            for i, name in enumerate(names, 1):
                print(f"  {i} {CLOUDS[name].label}")
            choice = _ask("选择要配置的云（序号，用逗号分隔；回车退出）")
            if not choice or choice.lower() == "q":
                print("未修改配置。")
                return 0
            selected = []
            for value in choice.split(","):
                if not value.strip().isdigit() or not 1 <= int(value) <= len(names):
                    raise ValueError("请选择菜单中的序号")
                selected.append(names[int(value) - 1])
        additions = {}
        for cloud in dict.fromkeys(selected):
            if cloud == "aws":
                rows, route53 = _collect_aws()
                if any(validate_account("aws", a) for a in rows):
                    raise ValueError("AWS 账号配置无效")
                rg, rc = _collect_regions(rows)
                additions[cloud] = {"accounts": rows, "route53_profiles": route53,
                                    "scan_regions_global": rg, "scan_regions_china": rc}
            else:
                additions[cloud] = {"accounts": collect_cloud(cloud)}
        merged = merge_config(previous, additions)
        text = toml_io.dumps(merged)
        if args.dry_run:
            print(toml_io.dumps(additions))
            print("预览完成，未写入配置。")
            return 0
        print("将合并账号：" + "、".join(f"{CLOUDS[c].label} {len(b['accounts'])} 个" for c, b in additions.items()))
        print("保留其它云、备注和计划任务；已有文件会备份，写入后 TOML 注释会重新生成。")
        if not _yn("写入配置？", False):
            print("已取消，未修改配置。")
            return 0
        write_config(cfg, text)
        from . import deploy_config
        deploy_config.reload()
        print(f"✅ 已保存 {cfg}。下一步：ops doctor → ops sync。")
        return 0
    except (EOFError, KeyboardInterrupt):
        print("\n已退出，未修改配置。")
        return 0
    except (OSError, ValueError) as exc:
        print(f"配置未写入：{exc}。请检查输入与现有 TOML。", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
