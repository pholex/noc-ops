"""Execute the dashboard's real billing formatter and overview with offline data."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.unit


def cycle(start="2026-12-20T00:00:00+00:00", amount=30, **changes):
    return {"cloud": "cloudflare", "account": "cf-demo", "cf_account_id": "demo-account",
            "service": "Workers Standard", "currency": "USD", "amount": amount,
            "period_start": start, "period_basis": "billing_cycle", "month": start[:7], **changes}


def render(rows):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is needed to execute dashboard JavaScript")
    html = (Path(__file__).parents[2] / "src/noc_ops/dashboard/index.html").read_text()
    helpers = html[html.index("const CUR_SYM="):html.index("const BILLING_COLS=")]
    overview = html[html.index("function renderOverview(){"):html.index("function renderCollectionHealth")]
    script = "const DATA={billing:" + json.dumps(rows) + "};\n" + """
const target={innerHTML:''};
const document={getElementById:id=>id==='overview'?target:null};
""" + helpers + overview + """
renderOverview();
console.log(JSON.stringify({html:target.innerHTML,summary:cloudflareBillingSummary(DATA.billing),
                           labels:DATA.billing.map(billingPeriodLabel)}));
"""
    result = subprocess.run([node, "-"], input=script, text=True, capture_output=True, check=True)
    return json.loads(result.stdout)


@pytest.mark.parametrize("month,start", [
    ("2026-10", "2026-09-20T00:00:00+00:00"),
    ("2027-01", "2026-12-20T00:00:00+00:00"),
])
def test_cross_month_cycles_are_separate_from_calendar_costs(month, start):
    previous = start[:7]
    result = render([
        {"cloud": "aws", "month": month, "currency": "USD", "amount": 100},
        {"cloud": "aws", "month": previous, "currency": "USD", "amount": 50},
        cycle(start),
    ])
    assert '<div class="ov-v">$100.00</div>' in result["html"]
    assert '上月 $50.00' in result["html"] and '上月 $80.00' not in result["html"]
    assert 'Cloudflare · 最近账期按量费用' in result["html"]
    assert start[:10] + ' 起' in result["html"] and '$30.00' in result["html"]
    assert result["labels"][-1] == start[:10] + ' 起'


def test_latest_cycles_keep_services_accounts_and_currencies_independent():
    result = render([
        cycle("2026-11-20T00:00:00Z", 999),  # older cycle for the same service
        cycle(),
        cycle("2027-01-02T00:00:00Z", 7, service="D1"),
        cycle("2026-12-20T00:00:00Z", 8, cf_account_id="second-account"),
        cycle("2026-12-20T00:00:00Z", 2, account="cf-other"),
        cycle("2026-11-20T00:00:00Z", 3, currency="EUR"),
        cycle("2027-01-02T00:00:00Z", -1, service="R2"),
    ])
    assert result["summary"] == {"hasLegacy": False, "periods": [
        {"start": "2027-01-02T00:00:00.000Z", "amounts": {"USD": 6}},
        {"start": "2026-12-20T00:00:00.000Z", "amounts": {"USD": 40}},
        {"start": "2026-11-20T00:00:00.000Z", "amounts": {"EUR": 3}},
    ]}
    assert '$999.00' not in result["html"]
    assert '<div class="ov-k">本月成本</div>\n      <div class="ov-v">—</div>' in result["html"]


def test_legacy_cache_never_claims_synthetic_month_start_is_a_cycle():
    old = {"cloud": "cloudflare", "month": "2026-12", "period_start": "2026-12-01",
           "amount": 900, "currency": "USD", "cost_basis": "按量费用 / 账期起始月"}
    result = render([old, cycle()])
    assert result["labels"][0] == '2026-12 (旧汇总)'
    assert result["summary"]["hasLegacy"]
    assert '刷新成本后显示账期' in result["html"]
    assert '$900.00' not in result["html"] and '2026-12-01 起' not in result["html"]
    assert render([old])["summary"] == {"periods": [], "hasLegacy": True}


def test_empty_and_calendar_only_data_do_not_show_cloudflare_card():
    for rows in ([], [{"cloud": "gcp", "month": "2027-01", "currency": "JPY", "amount": 300}]):
        result = render(rows)
        assert 'Cloudflare · 最近账期按量费用' not in result["html"]
        assert result["summary"] == {"periods": [], "hasLegacy": False}


def test_cycle_labels_preserve_utc_time_and_zero_cost():
    result = render([cycle("2027-01-01T00:30:00+01:00", 0)])
    assert result["labels"] == ['2026-12-31 23:30:00 起']
    assert '$0.00' in result["html"]
