"""Tier 1 单元测试：读/写/重启分类器（aws_safety）。纯函数、零依赖、秒级。"""
import pytest

from noc_ops.aws_safety import (
    is_read_only_aws, is_read_only_tccli, is_read_only_aliyun,
    is_reboot_aws, is_reboot_tccli, is_reboot_aliyun, is_read_only_ssh,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("cmd,expect", [
    ("ec2 describe-instances --region cn-northwest-1", True),
    ("--region cn-northwest-1 ec2 describe-instances", True),   # 全局选项在前
    ("cloudwatch get-metric-statistics", True),
    ("ec2 stop-instances --instance-ids i-123", False),
    ("ec2 terminate-instances --instance-ids i-1", False),
    ("ec2 reboot-instances --instance-ids i-1", False),         # 重启不算"只读"
    ("ssm send-command --document-name AWS-RunShellScript", False),
    ("s3api delete-bucket --bucket logs-archive", False),
    ("not-a-valid 'unterminated", False),                        # 解析失败 → 保守判写
])
def test_aws_readonly(cmd, expect):
    assert is_read_only_aws(cmd) is expect


@pytest.mark.parametrize("cmd,expect", [
    ("lighthouse DescribeInstances --region ap-nanjing", True),
    ("cvm DescribeInstances", True),
    ("--region ap-nanjing lighthouse DescribeInstances", True),
    ("lighthouse RebootInstances --InstanceIds lhins-x --region ap-nanjing", False),
    ("lighthouse StopInstances --InstanceIds lhins-x", False),
])
def test_tccli_readonly(cmd, expect):
    assert is_read_only_tccli(cmd) is expect


@pytest.mark.parametrize("cmd,expect", [
    ("ecs DescribeInstances --RegionId cn-hangzhou", True),
    ("ecs RebootInstance --InstanceId i-1 --RegionId cn-hangzhou", False),
    ("ecs DeleteInstance --InstanceId i-1", False),
])
def test_aliyun_readonly(cmd, expect):
    assert is_read_only_aliyun(cmd) is expect


@pytest.mark.parametrize("fn,cmd,expect", [
    (is_reboot_aws, "ec2 reboot-instances --instance-ids i-1", True),
    (is_reboot_aws, "ec2 stop-instances --instance-ids i-1", False),
    (is_reboot_aws, "ec2 describe-instances", False),
    (is_reboot_tccli, "lighthouse RebootInstances --InstanceIds lhins-x", True),
    (is_reboot_tccli, "lighthouse StopInstances --InstanceIds lhins-x", False),
    (is_reboot_aliyun, "ecs RebootInstance --InstanceId i-1 --RegionId cn-hangzhou", True),
    (is_reboot_aliyun, "ecs DescribeInstances", False),
])
def test_reboot_detection(fn, cmd, expect):
    assert fn(cmd) is expect


@pytest.mark.parametrize("cmd,expect", [
    ("df -h", True),
    ("systemctl status nginx", True),
    ("tail -n 100 /var/log/app.log", True),
    ("df -h && uptime && cat /proc/loadavg", True),     # 全只读复合 → 放行
    ("ps aux | grep nginx", True),                       # 只读管道 → 放行
    ("systemctl restart nginx", False),
    ("rm -rf /tmp/x", False),
    ("cat /etc/hosts > /tmp/x", False),                  # 重定向 = 写
    ("df -h && rm -rf /tmp/x", False),                   # 一段是写 → 确认
    ("free -m; systemctl restart nginx", False),
    ("sudo reboot", False),
])
def test_ssh_readonly(cmd, expect):
    assert is_read_only_ssh(cmd) is expect
