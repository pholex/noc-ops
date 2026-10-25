"""Small TOML writer for configuration values; parse before replacing a config."""
import json
import math
from datetime import date, datetime, time


def quote(value):
    return json.dumps(str(value), ensure_ascii=False)


def scalar(value):
    if isinstance(value, str):
        return quote(value)
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            return "nan" if math.isnan(value) else "inf" if value > 0 else "-inf"
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(scalar(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{quote(k)} = {scalar(v)}" for k, v in value.items()) + " }"
    raise ValueError(f"不支持的配置类型：{type(value).__name__}")


def dumps(config):
    lines = ["# 由 ops init 生成；凭证请留在云 CLI、环境变量或系统钥匙串中。"]
    def table(data, prefix=(), array=False):
        if prefix:
            title = ".".join(quote(k) for k in prefix)
            lines.extend(["", ("[[" if array else "[") + title + ("]]" if array else "]")])
        for key, value in data.items():
            if not isinstance(value, dict) and not (isinstance(value, list) and value and all(isinstance(v, dict) for v in value)):
                lines.append(f"{quote(key)} = {scalar(value)}")
        for key, value in data.items():
            if isinstance(value, dict):
                table(value, (*prefix, key))
            elif isinstance(value, list) and value and all(isinstance(v, dict) for v in value):
                for item in value:
                    table(item, (*prefix, key), True)
    table(config)
    return "\n".join(lines) + "\n"
