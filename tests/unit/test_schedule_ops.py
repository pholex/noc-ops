"""Tier 1 单元测试：计划任务的纯函数（时间→cron/名称、支持判定）。"""
import pytest

from noc_ops import schedule_ops as s

pytestmark = pytest.mark.unit


def test_parse_crontab_ok():
    assert s._parse_crontab("30 22 * * *") == ["30", "22", "*", "*", "*"]


@pytest.mark.parametrize("bad", ["30 22 * *", "30 22 * * * *", "every-day"])
def test_parse_crontab_bad(bad):
    with pytest.raises(ValueError):
        s._parse_crontab(bad)


@pytest.mark.parametrize("dom,dow,exp", [
    ("*", "*", ("*", "?")),     # 都 * → DOW 让位为 ?
    ("1", "*", ("1", "?")),
    ("*", "1", ("?", "1")),
    ("1", "1", ("1", "?")),     # 都有值 → 以 DOM 为准（AWS 要求恰一个 ?）
])
def test_quartz_dom_dow(dom, dow, exp):
    assert s._quartz_dom_dow(dom, dow) == exp


@pytest.mark.parametrize("raw,exp", [
    ("sample-web", "sample-web"),
    ("sample resource!", "sample-resource"),
    ("@@@", "x"),               # 全非法 → 兜底 x
    ("UPPER", "upper"),
])
def test_sanitize(raw, exp):
    assert s._sanitize(raw) == exp


def test_make_name_oneshot():
    assert s.make_name("sample-web", "2026-06-06T22:30:00") == "noc-reboot-sample-web-202606062230"


def test_make_name_cron():
    assert s.make_name("sample-web") == "noc-reboot-sample-web-cron"


@pytest.mark.parametrize("cloud,profile,exp", [
    ("aws", "111122223333", True),
    ("aws", "123456789012", True),
    ("aws", "210987654321", True),
    ("aws", "000000000000", False),
    ("tencent", "100000000001", True),
    ("aliyun", "1234567890123456", True),
    ("tencent", "999", False),
    ("gcp", "x", False),
])
def test_is_supported(cloud, profile, exp):
    assert s.is_supported(cloud, profile) is exp


def test_unsupported_reason():
    assert s.unsupported_reason("aws", "111122223333") == ""           # 支持 → 空
    assert "未知 cloud" in s.unsupported_reason("gcp", "x")
    assert "没接入" in s.unsupported_reason("tencent", "999")
