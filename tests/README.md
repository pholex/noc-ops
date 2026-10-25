# tests —— noc-ops 回归

公开仓只有 **Tier 1 单元测试**：离线、零 LLM、秒级、CI 友好。

```bash
uv run --extra dev pytest -m unit -q
```

## 结构

```
tests/
  unit/                     # 纯函数 / CLI 分发 / provider argv / sync 逻辑断言
    conftest.py               把 NOC_HOME 钉到匿名化夹具工作区，并把 .state 写盘改道到 tmp
  fixtures/workspace/
    ops-config.toml           匿名化的部署配置（账号 123456789012 类示例、IP 为 RFC 5737 段）
```

单测**不读任何真实工作区**：`conftest.py` 把 `NOC_HOME` 指到 `tests/fixtures/workspace/`，结构与真实配置一致但全是示例值。
改配置结构（加云、加字段）时同步改这份夹具。

## 加用例

- 新 provider / 新资源种类：在 `tests/unit/` 加 `test_<cloud>_*.py`，用 `monkeypatch` 替换 `subprocess.run`，断言 argv 拼装与解析，绝不真调云 CLI。
- 需要工作区文件（data.json 等）的用例：自己 `monkeypatch.setenv("NOC_HOME", tmp_path)` 并写入测试数据。

## 全新机器安装测试

不在 pytest 里，见 `scripts/fresh-install-test/run.sh`：在 Docker `python:3.12-slim` 里按 INSTALL.md 走一遍完整安装。
