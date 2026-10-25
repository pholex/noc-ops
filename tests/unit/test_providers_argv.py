"""Tier 1 单元测试：各云 provider 的 reboot/status argv 拼装 + 读写分类委托。"""
import pytest

from noc_ops.providers import PROVIDERS

pytestmark = pytest.mark.unit

AWS = {"id": "i-123", "profile": "111122223333", "region": "cn-northwest-1"}
ALI = {"id": "i-bp1c6", "region": "cn-hangzhou"}
TEN = {"id": "lhins-x", "region": "ap-nanjing"}


def test_aws_reboot_argv():
    assert PROVIDERS["aws"].reboot_argv(AWS) == [
        "aws", "ec2", "reboot-instances", "--instance-ids", "i-123",
        "--profile", "111122223333", "--region", "cn-northwest-1",
    ]


def test_aliyun_reboot_argv():
    a = PROVIDERS["aliyun"].reboot_argv(ALI)
    assert a[:4] == ["aliyun", "ecs", "RebootInstance", "--InstanceId"]
    assert "i-bp1c6" in a and "--RegionId" in a and "cn-hangzhou" in a
    assert a[a.index("--profile") + 1] == "acme"        # 阿里固定 profile


def test_tencent_reboot_argv():
    a = PROVIDERS["tencent"].reboot_argv(TEN)
    assert a[:3] == ["tccli", "lighthouse", "RebootInstances"]
    assert '["lhins-x"]' in a                              # InstanceIds 是 JSON 数组


def test_status_argv_shapes():
    assert PROVIDERS["aws"].status_argv(AWS)[:3] == ["aws", "ec2", "describe-instance-status"]
    assert PROVIDERS["aliyun"].status_argv(ALI)[:3] == ["aliyun", "ecs", "DescribeInstances"]
    assert PROVIDERS["tencent"].status_argv(TEN)[:3] == ["tccli", "lighthouse", "DescribeInstances"]


def test_classification_delegation():
    assert PROVIDERS["aws"].is_reboot("ec2 reboot-instances --instance-ids i-1")
    assert not PROVIDERS["aws"].is_read("ec2 stop-instances --instance-ids i-1")
    assert PROVIDERS["aliyun"].is_reboot("ecs RebootInstance --InstanceId i-1 --RegionId cn-hangzhou")
    assert PROVIDERS["tencent"].is_read("lighthouse DescribeInstances --region ap-nanjing")


def test_registry_clouds():
    assert set(PROVIDERS) == {"aws", "aliyun", "tencent", "volcengine", "gcp", "azure", "oracle",
                              "cloudflare", "supabase",
                              "local", "sangfor", "vmware", "synology"}
    assert PROVIDERS["aws"].cli == "aws"
    assert PROVIDERS["tencent"].cli == "tccli"
    assert PROVIDERS["aliyun"].cli == "aliyun"
    assert PROVIDERS["volcengine"].cli == "ve"
    assert PROVIDERS["gcp"].cli == "gcloud"
    assert PROVIDERS["azure"].cli == "az"
    assert PROVIDERS["oracle"].cli == "oci"
    # Cloudflare 只列域名、Supabase 只列数据库（无 CLI、无 compute）：list_instances 走基类默认空
    assert PROVIDERS["cloudflare"].list_instances() == []
    assert PROVIDERS["supabase"].list_instances() == []
    # 排序：公有云 IaaS 在前(AWS0 阿里1 腾讯2 火山3 GCP4 Azure5 Oracle6) → Cloudflare(7)
    # → 本地网络机器(sangfor/vmware/local 8-10) → Supabase(11) → synology(12)
    assert [PROVIDERS[c].order for c in ("aws", "aliyun", "tencent", "volcengine", "gcp", "azure", "oracle")] == list(range(7))
    assert PROVIDERS["cloudflare"].order == 7
    assert PROVIDERS["supabase"].order == 11


def test_gcp_reboot_status_argv():
    # 第二阶段：GCE「重启」= compute instances reset（硬重启）；按实例名 + zone + 项目（非 id）
    g = PROVIDERS["gcp"]
    inst = {"name": "my-vm", "az": "us-central1-a", "profile": "my-proj", "id": "123"}
    assert g.reboot_argv(inst) == ["gcloud", "compute", "instances", "reset", "my-vm",
                                   "--zone", "us-central1-a", "--project", "my-proj"]
    assert g.status_argv(inst)[:5] == ["gcloud", "compute", "instances", "describe", "my-vm"]
    assert g.cloud == "gcp" and g.order == 4


def test_gcp_classification():
    # 读=自动；reset=重启(免确认)；stop/start=普通写(需确认)
    g = PROVIDERS["gcp"]
    assert g.is_read("compute instances list --project p")
    assert g.is_read("compute instances describe my-vm --zone z --project p")
    assert g.is_read("projects get-iam-policy p --format json")
    assert g.is_read("storage buckets list --project p")
    for write in ("compute instances reset my-vm --zone z --project p",
                  "compute instances stop my-vm --zone z --project p",
                  "compute instances start my-vm --zone z --project p",
                  "projects add-iam-policy-binding p --member allUsers --role roles/x"):
        assert not g.is_read(write), write
    assert g.is_reboot("compute instances reset my-vm --zone z --project p")
    assert not g.is_reboot("compute instances stop my-vm --zone z --project p")
    assert not g.is_reboot("compute instances list --project p")


def test_oracle_reboot_status_argv():
    # OCI「重启」= compute instance action --action SOFTRESET（优雅）；按 instance OCID + region + profile
    o = PROVIDERS["oracle"]
    inst = {"name": "web-1", "id": "ocid1.instance.oc1..example0001",
            "profile": "DEFAULT", "region": "us-ashburn-1"}
    a = o.reboot_argv(inst)
    assert a[:4] == ["oci", "compute", "instance", "action"]
    assert a[a.index("--action") + 1] == "SOFTRESET"
    assert a[a.index("--instance-id") + 1] == "ocid1.instance.oc1..example0001"
    assert a[a.index("--region") + 1] == "us-ashburn-1"
    assert o.status_argv(inst)[:4] == ["oci", "compute", "instance", "get"]
    assert o.cloud == "oracle" and o.order == 6


def test_oracle_classification():
    # 读=自动；action --action SOFTRESET/RESET=重启(免确认)；STOP/START/terminate=普通写(需确认)
    o = PROVIDERS["oracle"]
    for read in ("compute instance list --compartment-id c", "os bucket list -c c",
                 "iam user list", "compute instance list-vnics --instance-id i",
                 "db autonomous-database list --compartment-id c"):
        assert o.is_read(read), read
    for write in ("compute instance terminate --instance-id i",
                  "compute instance action --instance-id i --action STOP",
                  "compute instance action --instance-id i --action START"):
        assert not o.is_read(write), write
        assert not o.is_reboot(write), write
    assert o.is_reboot("compute instance action --instance-id i --action SOFTRESET")
    assert o.is_reboot("compute instance action --instance-id i --action RESET")
    assert not o.is_reboot("compute instance list --compartment-id c")


def test_gcp_parse_member():
    g = PROVIDERS["gcp"]
    assert g._parse_member("user:a@b.com") == ("user", "a@b.com", False)
    assert g._parse_member("serviceAccount:x@p.iam.gserviceaccount.com") == \
        ("serviceAccount", "x@p.iam.gserviceaccount.com", False)
    # 已删主体：剥 deleted: 前缀、去 ?uid 尾、标记 deleted=True（删 SA 后残留的"债"绑定）
    assert g._parse_member("deleted:serviceAccount:vertex@p.iam.gserviceaccount.com?uid=123") == \
        ("serviceAccount", "vertex@p.iam.gserviceaccount.com", True)


def test_gcp_google_managed_sa_filter():
    from noc_ops.providers.gcp import _is_google_managed_sa
    assert _is_google_managed_sa("service-123@compute-system.iam.gserviceaccount.com")
    assert _is_google_managed_sa("service-123@gcp-sa-pubsub.iam.gserviceaccount.com")
    assert _is_google_managed_sa("123@cloudservices.gserviceaccount.com")
    assert not _is_google_managed_sa("vertex-express@my-proj.iam.gserviceaccount.com")   # 自建 SA → 保留
    assert not _is_google_managed_sa("alice@example.com")
