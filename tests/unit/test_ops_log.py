"""Tier 1：操作日志 account 反查/拆分/补入。

不碰真云：构造一个临时 data.json 作清单，验证 record() 写入 account（跨账号拆条）、
backfill 补历史、幂等，以及 cloud 同名消歧。
"""
import json

import pytest

from noc_ops import ops_log

pytestmark = pytest.mark.unit


def _ws(monkeypatch, tmp_path, data):
    """搭一个临时工作区，写入 data.json 作为反查清单，并隔离 NOC_HOME + 索引缓存。"""
    (tmp_path / "data.json").write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setenv("NOC_HOME", str(tmp_path))
    ops_log._IDX_CACHE["key"] = None        # 清索引缓存，避免跨用例串
    return tmp_path / ".state" / "ops-log.jsonl"


DATA = {
    "instances": [
        {"name": "sample-web", "id": "i-080", "profile": "111", "cloud": "aws"},
        {"name": "srv-hz", "id": "i-bp1", "profile": "999", "cloud": "aliyun"},
    ],
    "storage": [
        {"name": "leu", "account": "222", "cloud": "aws"},
        {"name": "orf", "account": "333", "cloud": "aws"},
        {"name": "orf", "account": "999", "cloud": "aliyun"},   # 跨云同名
    ],
    "databases": [], "serverless": [], "iam": [], "messaging": [], "domains": [],
}


def test_resolve_single_account(monkeypatch, tmp_path):
    _ws(monkeypatch, tmp_path, DATA)
    g = ops_log._resolve_account_groups("sample-web", "aws", ops_log._account_index())
    assert dict(g) == {"111": "sample-web"}


def test_resolve_bucket_object_and_unknown(monkeypatch, tmp_path):
    _ws(monkeypatch, tmp_path, DATA)
    idx = ops_log._account_index()
    assert dict(ops_log._resolve_account_groups("leu/index.html", "aws", idx)) == {"222": "leu/index.html"}
    assert dict(ops_log._resolve_account_groups("mystery", "aws", idx)) == {"": "mystery"}
    assert dict(ops_log._resolve_account_groups("", "", idx)) == {"": ""}


def test_resolve_cloud_disambiguation(monkeypatch, tmp_path):
    _ws(monkeypatch, tmp_path, DATA)
    idx = ops_log._account_index()
    assert dict(ops_log._resolve_account_groups("orf", "aws", idx)) == {"333": "orf"}
    assert dict(ops_log._resolve_account_groups("orf", "aliyun", idx)) == {"999": "orf"}


def test_resolve_multi_account_split(monkeypatch, tmp_path):
    _ws(monkeypatch, tmp_path, DATA)
    idx = ops_log._account_index()
    g = ops_log._resolve_account_groups("sample-web, leu, mystery", "aws", idx)
    assert dict(g) == {"111": "sample-web", "222": "leu", "": "mystery"}


def test_record_writes_account_and_splits(monkeypatch, tmp_path):
    log = _ws(monkeypatch, tmp_path, DATA)
    ops_log.record("cli", "reboot", target="sample-web", cloud="aws")
    ops_log.record("cli", "tag", target="sample-web leu", cloud="aws")   # 跨账号 → 拆 2 条
    rows = [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines()]
    assert rows[0]["account"] == "111" and rows[0]["target"] == "sample-web"
    # 第二条拆成 111(sample-web) + 222(leu)
    split = {r["account"]: r["target"] for r in rows[1:]}
    assert split == {"111": "sample-web", "222": "leu"}


def test_record_explicit_account_no_split(monkeypatch, tmp_path):
    log = _ws(monkeypatch, tmp_path, DATA)
    ops_log.record("cli", "x", target="a b c", cloud="aws", account="777")
    rows = [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1 and rows[0]["account"] == "777" and rows[0]["target"] == "a b c"


def test_backfill_idempotent(monkeypatch, tmp_path):
    log = _ws(monkeypatch, tmp_path, DATA)
    log.parent.mkdir(parents=True, exist_ok=True)
    # 旧格式（无 account 字段）：含可反查、跨账号、不可反查三种
    old = [
        {"ts": "t", "source": "agent", "action": "reboot", "target": "sample-web", "cloud": "aws", "result": "ok"},
        {"ts": "t", "source": "cli", "action": "tag", "target": "sample-web leu", "cloud": "aws", "result": "ok"},
        {"ts": "t", "source": "cli", "action": "cli", "target": "", "cloud": "aws", "result": "ok"},
    ]
    log.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in old) + "\n", encoding="utf-8")
    filled = ops_log.backfill_accounts()
    rows = [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines()]
    assert filled == 3                       # sample-web + (sample-web,leu) 三个非空
    assert len(rows) == 4                    # 第二条拆成 2 → 共 4 行
    assert all("account" in r for r in rows)
    accts = sorted(r["account"] for r in rows)
    assert accts == ["", "111", "111", "222"]
    # 幂等：再跑不变
    before = log.read_text(encoding="utf-8")
    assert ops_log.backfill_accounts() == 0
    assert log.read_text(encoding="utf-8") == before
