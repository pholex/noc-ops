"""Read tokens without ever putting their values into argv or config files."""
import os
import re
import shutil
import subprocess


def token(account, cloud):
    env = account.get("token_env", "")
    if env:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", env):
            return ""
        value = os.environ.get(env, "").strip()
        if value:
            return value
    if not shutil.which("security"):
        return ""
    try:
        r = subprocess.run(["security", "find-generic-password", "-s", account.get("keychain_service", "noc-" + cloud),
                            "-a", account.get("account", cloud), "-w"],
                           capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL)
        return r.stdout.strip() if r.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""
