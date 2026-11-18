"""未启用的 opt-in 区域按「没资源」处理；其他失败仍算失败；无 SES 端点的区域跳过 SES。"""
import subprocess

import pytest

from noc_ops.providers import aws

pytestmark = pytest.mark.unit


def _fake_run(returncode, stderr="", stdout=""):
    def run(cmd, **kw):
        return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr=stderr)
    return run


def test_opt_in_region_not_enabled_is_empty_success(monkeypatch):
    err = "aws: [ERROR]: An error occurred (InvalidClientTokenId) when calling the ListQueues operation"
    monkeypatch.setattr(aws.subprocess, "run", _fake_run(254, err))
    assert aws._aws_json("p", "ap-east-2", ["sqs", "list-queues"]) == {}


def test_same_error_in_normal_region_is_failure(monkeypatch):
    err = "An error occurred (InvalidClientTokenId) when calling the ListQueues operation"
    monkeypatch.setattr(aws.subprocess, "run", _fake_run(254, err))
    assert aws._aws_json("p", "us-east-1", ["sqs", "list-queues"]) is None


def test_other_error_in_opt_in_region_is_failure(monkeypatch):
    monkeypatch.setattr(aws.subprocess, "run", _fake_run(255, "Could not connect to the endpoint URL"))
    assert aws._aws_json("p", "ap-east-2", ["sqs", "list-queues"]) is None


def test_ses_skipped_where_no_endpoint(monkeypatch):
    calls = []
    monkeypatch.setattr(aws, "_aws_json", lambda p, r, a: calls.append((r, a[0])) or {})
    aws.AwsProvider()._messaging_in("p", "ap-east-1")
    assert ("ap-east-1", "ses") not in calls and ("ap-east-1", "sqs") in calls


def test_describe_instances_opt_in_region_not_enabled_is_empty(monkeypatch):
    from noc_ops import sync
    err = "An error occurred (AuthFailure) when calling the DescribeInstances operation"
    monkeypatch.setattr(sync.subprocess, "run", _fake_run(254, err))
    assert sync.describe("p", "ap-east-2") == []
    assert sync.describe("p", "us-east-1") is None
