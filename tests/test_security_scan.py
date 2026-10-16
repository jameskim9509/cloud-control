"""보안 그룹 점검(scan_attached_security_groups) 테스트. 실제 AWS는 호출하지 않는다.

실행: python -m unittest -v tests.test_security_scan
"""

import unittest
from unittest import mock

from botocore.exceptions import ClientError, EndpointConnectionError, NoCredentialsError

from backend import aws_service
from backend.aws_service import AwsService, inspect_group
from backend.config import DbSettings, Settings
from backend.errors import ApiError

ACCOUNT = "123456789012"


def make_settings(allowed=frozenset()) -> Settings:
    return Settings(
        aws_profile="test",
        aws_region="ap-northeast-2",
        expected_account_id=ACCOUNT,
        admin_api_token="t",
        allowed_instance_ids=allowed,
        db=DbSettings(host="unused", port=3306, user="unused", password="unused", name="unused"),
    )


def instance(instance_id, enis, state="running", name=None):
    inst = {"InstanceId": instance_id, "State": {"Name": state}, "NetworkInterfaces": enis}
    if name:
        inst["Tags"] = [{"Key": "Name", "Value": name}]
    return inst


def eni(eni_id, *group_ids):
    return {"NetworkInterfaceId": eni_id, "Groups": [{"GroupId": g, "GroupName": g} for g in group_ids]}


def sg(group_id, *perms):
    return {"GroupId": group_id, "GroupName": f"name-{group_id}", "VpcId": "vpc-1", "IpPermissions": list(perms)}


def client_error(code, op="DescribeSecurityGroups"):
    return ClientError({"Error": {"Code": code, "Message": "x"}}, op)


class ScanTest(unittest.TestCase):
    def setUp(self):
        self.sts = mock.MagicMock()
        self.sts.get_caller_identity.return_value = {"Account": ACCOUNT, "Arn": "arn:aws:iam::123456789012:user/t"}
        self.ec2 = mock.MagicMock()
        self.pages = [{"Reservations": []}]
        self.ec2.get_paginator.return_value.paginate.side_effect = lambda **kw: iter(self.pages)
        # 요청한 GroupIds만 돌려주는 기본 동작
        self.ec2.describe_security_groups.side_effect = lambda GroupIds: {"SecurityGroups": [sg(g) for g in GroupIds]}
        p = mock.patch.object(AwsService, "_client", side_effect=lambda svc: self.sts if svc == "sts" else self.ec2)
        p.start()
        self.addCleanup(p.stop)
        self.service = AwsService(make_settings())

    def set_instances(self, *instances):
        self.pages = [{"Reservations": [{"Instances": list(instances)}]}]

    def assert_api_error(self, status, code):
        with self.assertRaises(ApiError) as ctx:
            self.service.scan_attached_security_groups()
        self.assertEqual((ctx.exception.status_code, ctx.exception.code), (status, code))

    # ---- 조회 범위 ----

    def test_state_filter_is_applied(self):
        self.service.scan_attached_security_groups()
        self.ec2.get_paginator.assert_called_once_with("describe_instances")
        kwargs = self.ec2.get_paginator.return_value.paginate.call_args.kwargs
        self.assertEqual(
            kwargs["Filters"],
            [{"Name": "instance-state-name", "Values": ["pending", "running", "stopping", "stopped"]}],
        )

    def test_no_instances_skips_security_group_call(self):
        result = self.service.scan_attached_security_groups()
        checked_at = result.pop("checked_at")
        self.assertEqual(
            result,
            {"findings": [], "checked_groups": [], "checked_instance_count": 0, "checked_group_count": 0, "finding_count": 0},
        )
        self.assertRegex(checked_at, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        self.ec2.describe_security_groups.assert_not_called()

    def test_account_mismatch_blocks_scan(self):
        self.sts.get_caller_identity.return_value = {"Account": "999999999999", "Arn": "x"}
        self.assert_api_error(403, "AWS_ACCOUNT_MISMATCH")
        self.ec2.get_paginator.assert_not_called()

    def test_collects_all_enis_and_dedupes(self):
        self.set_instances(
            instance("i-1", [eni("eni-a", "sg-1", "sg-2"), eni("eni-b", "sg-2")], name="web"),
            instance("i-2", [eni("eni-c", "sg-1", "sg-1")], state="stopped"),  # 같은 ENI에 중복 그룹
        )
        result = self.service.scan_attached_security_groups()
        self.assertEqual(result["checked_instance_count"], 2)
        by_id = {g["group_id"]: g for g in result["checked_groups"]}
        self.assertEqual(sorted(by_id), ["sg-1", "sg-2"])
        self.assertEqual(by_id["sg-1"]["group_name"], "name-sg-1")
        self.assertEqual(
            by_id["sg-1"]["connections"],
            [
                {"instance_id": "i-1", "instance_name": "web", "state": "running", "network_interface_id": "eni-a"},
                {"instance_id": "i-2", "instance_name": "i-2", "state": "stopped", "network_interface_id": "eni-c"},
            ],
        )
        self.assertEqual([a["network_interface_id"] for a in by_id["sg-2"]["connections"]], ["eni-a", "eni-b"])
        self.ec2.describe_security_groups.assert_called_once_with(GroupIds=["sg-1", "sg-2"])

    def test_allowlist_is_not_used_as_scan_filter(self):
        self.service = AwsService(make_settings(allowed=frozenset({"i-other"})))
        self.set_instances(instance("i-1", [eni("eni-a", "sg-1")]))
        result = self.service.scan_attached_security_groups()
        self.assertEqual(result["checked_group_count"], 1)

    def test_batches_of_100_sorted(self):
        group_ids = [f"sg-{i:03d}" for i in range(250)]
        enis = [eni(f"eni-{i}", g) for i, g in enumerate(reversed(group_ids))]
        self.set_instances(instance("i-1", enis))
        result = self.service.scan_attached_security_groups()
        calls = [c.kwargs["GroupIds"] for c in self.ec2.describe_security_groups.call_args_list]
        self.assertEqual([len(c) for c in calls], [100, 100, 50])
        self.assertEqual(sum(calls, []), sorted(group_ids))
        self.assertEqual(result["checked_group_count"], 250)

    def test_each_group_inspected_once(self):
        self.set_instances(
            instance("i-1", [eni("eni-a", "sg-1", "sg-2")]),
            instance("i-2", [eni("eni-b", "sg-1")]),
        )
        with mock.patch.object(aws_service, "inspect_group", wraps=inspect_group) as spy:
            self.service.scan_attached_security_groups()
        self.assertEqual(sorted(c.args[0]["GroupId"] for c in spy.call_args_list), ["sg-1", "sg-2"])

    def test_instance_count_is_unique_ids(self):
        inst = instance("i-1", [eni("eni-a", "sg-1")])
        self.pages = [{"Reservations": [{"Instances": [inst]}]}, {"Reservations": [{"Instances": [inst]}]}]
        self.assertEqual(self.service.scan_attached_security_groups()["checked_instance_count"], 1)

    # ---- 발견 집계 ----

    def test_shared_group_counts_rule_once_with_all_connections(self):
        open_rule = {"IpProtocol": "tcp", "FromPort": 22, "ToPort": 3389, "IpRanges": [{"CidrIp": "0.0.0.0/0"}], "Ipv6Ranges": []}
        self.set_instances(*[instance(f"i-{n}", [eni(f"eni-{n}", "sg-open", "sg-safe")]) for n in range(3)])
        self.ec2.describe_security_groups.side_effect = lambda GroupIds: {
            "SecurityGroups": [sg("sg-open", open_rule), sg("sg-safe")]
        }
        result = self.service.scan_attached_security_groups()
        self.assertEqual(result["finding_count"], 1)
        self.assertEqual(len(result["findings"]), 1)
        [finding] = result["findings"]
        self.assertEqual(finding["management_ports"], [22, 3389])
        self.assertEqual(finding["group_id"], "sg-open")
        self.assertEqual([c["instance_id"] for c in finding["connections"]], ["i-0", "i-1", "i-2"])
        # 발견이 없는 그룹도 checked_groups에 포함된다.
        self.assertEqual([g["group_id"] for g in result["checked_groups"]], ["sg-open", "sg-safe"])
        self.assertEqual(result["checked_group_count"], 2)

    def test_group_is_passed_in_original_aws_shape(self):
        self.set_instances(instance("i-1", [eni("eni-a", "sg-1")]))
        with mock.patch.object(aws_service, "inspect_group", return_value=[]) as spy:
            self.service.scan_attached_security_groups()
        passed = spy.call_args.args[0]
        self.assertEqual(passed, sg("sg-1"))  # GroupId/GroupName/IpPermissions 원본 그대로

    # ---- 불완전 조회 ----

    def test_missing_network_interfaces_is_incomplete(self):
        for broken in (
            {"InstanceId": "i-1", "State": {"Name": "running"}},  # 키 없음
            {"State": {"Name": "running"}, "NetworkInterfaces": [eni("eni-a", "sg-1")]},  # 인스턴스 ID 없음
            instance("i-1", []),  # 빈 목록
            instance("i-1", [{"NetworkInterfaceId": "eni-a"}]),  # Groups 없음
            instance("i-1", [{"NetworkInterfaceId": "eni-a", "Groups": []}]),
            instance("i-1", [{"Groups": [{"GroupId": "sg-1"}]}]),  # ENI ID 없음
        ):
            with self.subTest(broken=broken):
                self.set_instances(broken)
                self.assert_api_error(503, "AWS_SCAN_INCOMPLETE")
        self.ec2.describe_security_groups.assert_not_called()

    def test_returned_group_set_mismatch_is_incomplete(self):
        self.set_instances(instance("i-1", [eni("eni-a", "sg-1", "sg-2")]))
        self.ec2.describe_security_groups.side_effect = lambda GroupIds: {"SecurityGroups": [sg("sg-1")]}
        self.assert_api_error(503, "AWS_SCAN_INCOMPLETE")

    def test_group_deleted_during_scan_is_incomplete(self):
        self.set_instances(instance("i-1", [eni("eni-a", "sg-1")]))
        self.ec2.describe_security_groups.side_effect = client_error("InvalidGroup.NotFound")
        self.assert_api_error(503, "AWS_SCAN_INCOMPLETE")

    def test_failure_in_later_batch_fails_whole_scan(self):
        self.set_instances(instance("i-1", [eni(f"eni-{i}", f"sg-{i:03d}") for i in range(150)]))
        first_batch = {"SecurityGroups": [sg(f"sg-{i:03d}") for i in range(100)]}
        self.ec2.describe_security_groups.side_effect = [first_batch, client_error("UnauthorizedOperation")]
        self.assert_api_error(403, "AWS_ACCESS_DENIED")

    # ---- AWS 오류 분류 ----

    def test_aws_errors_are_classified(self):
        self.set_instances(instance("i-1", [eni("eni-a", "sg-1")]))
        cases = [
            (client_error("UnauthorizedOperation", "DescribeInstances"), 403, "AWS_ACCESS_DENIED"),
            (client_error("ExpiredToken", "DescribeInstances"), 503, "AWS_LOGIN_REQUIRED"),
            (NoCredentialsError(), 503, "AWS_LOGIN_REQUIRED"),
            (EndpointConnectionError(endpoint_url="https://ec2"), 503, "AWS_CONNECTION_ERROR"),
        ]
        for exc, status, code in cases:
            with self.subTest(code=code, exc=type(exc).__name__):
                self.ec2.get_paginator.return_value.paginate.side_effect = exc
                self.assert_api_error(status, code)


class InspectGroupTest(unittest.TestCase):
    def rule(self, protocol, from_port=None, to_port=None, v4=(), v6=()):
        perm = {"IpProtocol": protocol, "IpRanges": [{"CidrIp": c} for c in v4], "Ipv6Ranges": [{"CidrIpv6": c} for c in v6]}
        if from_port is not None:
            perm["FromPort"], perm["ToPort"] = from_port, to_port
        return perm

    def found(self, *perms):
        return [(f["ip_version"], f["cidr"], f["management_ports"]) for f in inspect_group(sg("sg-1", *perms))]

    def test_detects_public_ssh_and_rdp(self):
        self.assertEqual(self.found(self.rule("tcp", 22, 22, v4=["0.0.0.0/0"])), [("IPv4", "0.0.0.0/0", [22])])
        self.assertEqual(self.found(self.rule("tcp", 3389, 3389, v6=["::/0"])), [("IPv6", "::/0", [3389])])

    def test_one_rule_covering_both_ports_is_one_finding_per_cidr(self):
        self.assertEqual(
            self.found(self.rule("tcp", 0, 65535, v4=["0.0.0.0/0"], v6=["::/0"])),
            [("IPv4", "0.0.0.0/0", [22, 3389]), ("IPv6", "::/0", [22, 3389])],
        )
        self.assertEqual(self.found(self.rule("-1", v4=["0.0.0.0/0"])), [("IPv4", "0.0.0.0/0", [22, 3389])])

    def test_finding_keeps_rule_fields(self):
        [f] = inspect_group(sg("sg-1", self.rule("tcp", 20, 30, v4=["0.0.0.0/0"])))
        self.assertEqual(
            f,
            {"group_id": "sg-1", "group_name": "name-sg-1", "ip_version": "IPv4", "cidr": "0.0.0.0/0",
             "ip_protocol": "tcp", "from_port": 20, "to_port": 30, "management_ports": [22]},
        )

    def test_ignores_non_public_or_other_ports_or_udp(self):
        self.assertEqual(self.found(self.rule("tcp", 22, 22, v4=["10.0.0.0/8", "203.0.113.5/32"])), [])
        self.assertEqual(self.found(self.rule("tcp", 80, 443, v4=["0.0.0.0/0"])), [])
        self.assertEqual(self.found(self.rule("udp", 0, 65535, v4=["0.0.0.0/0"])), [])
        self.assertEqual(self.found(), [])


class SecurityCheckApiTest(unittest.TestCase):
    """GET /api/security-checks에 기존 운영자 인증이 적용되는지 확인한다."""

    def setUp(self):
        from backend.main import app

        self.app = app
        self.scan = mock.MagicMock(return_value={"findings": []})
        app.state.settings = make_settings()
        app.state.aws = mock.MagicMock(scan_attached_security_groups=self.scan)

    def test_requires_token(self):
        from tests.test_actions import call_api

        for token in (None, "wrong"):
            status, body = call_api("GET", "/api/security-checks", token=token)
            self.assertEqual(status, 401)
            self.assertEqual(body["detail"]["code"], "APP_AUTH_REQUIRED")
        self.scan.assert_not_called()

    def test_each_request_scans_again(self):
        from tests.test_actions import call_api

        for _ in range(2):
            status, _ = call_api("GET", "/api/security-checks", token="t")
            self.assertEqual(status, 200)
        self.assertEqual(self.scan.call_count, 2)


if __name__ == "__main__":
    unittest.main()
