"""Boto3 조회·제어와 AWS 오류 분류. 모든 함수는 동기 함수다."""

from datetime import datetime, timezone

import boto3
from botocore.config import Config
from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    ConnectionError as BotoConnectionError,
    CredentialRetrievalError,
    HTTPClientError,
    LoginError,
    LoginInsufficientPermissions,
    NoCredentialsError,
    PartialCredentialsError,
    ProfileNotFound,
    SSOError,
    TokenRetrievalError,
)

from backend.config import Settings
from backend.errors import ApiError

BOTO_CONFIG = Config(
    connect_timeout=3,
    read_timeout=10,
    retries={"max_attempts": 2, "mode": "standard"},
)

# 변경(시작·중지) 전용: 자동 재시도 없이 1회만 보낸다. 재시도가 중복 실행으로 이어지지 않게 하기 위함.
MUTATION_CONFIG = Config(
    connect_timeout=3,
    read_timeout=10,
    retries={"total_max_attempts": 1, "mode": "standard"},
)

# 보안 그룹 점검 대상: 종료(terminated)·종료 중(shutting-down)이 아닌 실제 인스턴스
SCAN_INSTANCE_STATES = ["pending", "running", "stopping", "stopped"]
# describe_security_groups(GroupIds=...) 한 번에 보낼 최대 그룹 수
SG_BATCH_SIZE = 100
# 공개 접근을 점검할 관리용 포트
RISKY_TCP_PORTS = (22, 3389)
PUBLIC_CIDRS = {"0.0.0.0/0", "::/0"}

ACCESS_DENIED_CODES = {
    "AccessDenied",
    "AccessDeniedException",
    "UnauthorizedOperation",
    "AuthorizationError",
}
LOGIN_REQUIRED_CODES = {
    "ExpiredToken",
    "ExpiredTokenException",
    "RequestExpired",
    "InvalidClientTokenId",
    "UnrecognizedClientException",
    "AuthFailure",
    "InvalidToken",
    "SignatureDoesNotMatch",
}
LOGIN_REQUIRED_ERRORS = (
    LoginError,
    NoCredentialsError,
    PartialCredentialsError,
    CredentialRetrievalError,
    TokenRetrievalError,
    SSOError,
    ProfileNotFound,
)


def access_denied() -> ApiError:
    return ApiError(403, "AWS_ACCESS_DENIED", "AWS 권한이 부족합니다.")


def login_required() -> ApiError:
    return ApiError(
        503,
        "AWS_LOGIN_REQUIRED",
        "AWS 로그인이 필요하거나 만료되었습니다. 터미널에서 aws login을 다시 실행하세요.",
    )


def translate_error(exc: Exception) -> ApiError:
    """Boto3 예외를 API 오류로 바꾼다. 빈 목록으로 숨기지 않는다."""
    if isinstance(exc, ClientError):
        code = exc.response.get("Error", {}).get("Code", "")
        if code in ACCESS_DENIED_CODES:
            return access_denied()
        if code in LOGIN_REQUIRED_CODES:
            return login_required()
        if code.startswith("InvalidInstanceID"):
            return ApiError(404, "INSTANCE_NOT_FOUND", "인스턴스를 찾을 수 없습니다.")
        if code == "IncorrectInstanceState":
            return ApiError(409, "INVALID_STATE", "AWS가 현재 인스턴스 상태에서 이 작업을 거절했습니다.")
        return ApiError(502, "AWS_REQUEST_FAILED", f"AWS 요청이 실패했습니다. ({code or '알 수 없음'})")
    # LoginInsufficientPermissions는 LoginError의 하위 클래스이므로 먼저 검사한다.
    if isinstance(exc, LoginInsufficientPermissions):
        return access_denied()
    if isinstance(exc, LOGIN_REQUIRED_ERRORS):
        return login_required()
    if isinstance(exc, (BotoConnectionError, HTTPClientError)):
        return ApiError(503, "AWS_CONNECTION_ERROR", "AWS에 연결하지 못했습니다. 네트워크를 확인하세요.")
    if isinstance(exc, BotoCoreError):
        return ApiError(502, "AWS_REQUEST_FAILED", "AWS 요청 처리 중 오류가 발생했습니다.")
    raise exc


def scan_incomplete(reason: str) -> ApiError:
    """일부라도 확인하지 못하면 점검 전체를 실패로 돌려준다. 발견 없음으로 단정하지 않는다."""
    return ApiError(503, "AWS_SCAN_INCOMPLETE", f"보안 그룹 점검을 완료하지 못했습니다. 잠시 후 다시 조회하세요. ({reason})")


def _rule_covers_port(perm: dict, port: int) -> bool:
    protocol = str(perm.get("IpProtocol", ""))
    if protocol == "-1":  # 모든 프로토콜·포트
        return True
    if protocol not in ("tcp", "6"):
        return False
    from_port, to_port = perm.get("FromPort"), perm.get("ToPort")
    if from_port is None or to_port is None:
        return True  # 범위가 없으면 보수적으로 전체 포트로 본다
    return from_port <= port <= to_port


def inspect_group(group: dict) -> list[dict]:
    """AWS 원본 보안 그룹(GroupId/GroupName/IpPermissions)의 인바운드 규칙을 점검한다.

    발견 단위는 (인바운드 규칙, 공개 CIDR) 조합이다. 한 규칙이 22와 3389를 모두 열면
    management_ports=[22, 3389]인 한 건으로 센다.
    """
    findings = []
    for perm in group.get("IpPermissions", []):
        ports = [port for port in RISKY_TCP_PORTS if _rule_covers_port(perm, port)]
        if not ports:
            continue
        sources = [("IPv4", r.get("CidrIp")) for r in perm.get("IpRanges", [])]
        sources += [("IPv6", r.get("CidrIpv6")) for r in perm.get("Ipv6Ranges", [])]
        for ip_version, cidr in sources:
            if cidr not in PUBLIC_CIDRS:
                continue
            findings.append({
                "group_id": group.get("GroupId"),
                "group_name": group.get("GroupName"),
                "ip_version": ip_version,
                "cidr": cidr,
                "ip_protocol": perm.get("IpProtocol"),
                "from_port": perm.get("FromPort"),
                "to_port": perm.get("ToPort"),
                "management_ports": ports,
            })
    return findings


class AwsService:
    def __init__(self, settings: Settings):
        self.settings = settings

    def _session(self) -> boto3.Session:
        # 요청마다 Session을 만들어 스레드 간 공유를 피하고 재로그인 결과를 바로 반영한다.
        # 리전은 서버 설정(ap-northeast-2 고정)에서만 온다.
        return boto3.Session(
            profile_name=self.settings.aws_profile,
            region_name=self.settings.aws_region,
        )

    def _client(self, service: str):
        return self._session().client(service, config=BOTO_CONFIG)

    def _mutation_client(self, service: str):
        return self._session().client(service, config=MUTATION_CONFIG)

    def _call(self, func):
        try:
            return func()
        except ApiError:
            raise
        except Exception as exc:
            raise translate_error(exc) from exc

    def ensure_controllable(self, instance_id: str) -> None:
        """시작·중지 전에 호출한다. AWS를 호출하지 않고 서버 설정의 허용 목록만 본다."""
        if not self.settings.allowed_instance_ids:
            raise ApiError(403, "CONTROL_DISABLED", "제어 허용 목록이 비어 있어 시작·중지를 할 수 없습니다.")
        if instance_id not in self.settings.allowed_instance_ids:
            raise ApiError(403, "INSTANCE_NOT_ALLOWED", "제어가 허용되지 않은 인스턴스입니다.")

    def get_context(self) -> dict:
        """STS 신원을 확인하고 기대한 계정이 아니면 403으로 차단한다."""
        identity = self._call(lambda: self._client("sts").get_caller_identity())
        if identity["Account"] != self.settings.expected_account_id:
            raise ApiError(
                403,
                "AWS_ACCOUNT_MISMATCH",
                "로그인한 AWS 계정이 EXPECTED_ACCOUNT_ID와 다릅니다.",
            )
        return {
            "account_id": identity["Account"],
            "caller_arn": identity["Arn"],
            "region": self.settings.aws_region,
        }

    def get_instance_state(self, instance_id: str) -> str:
        """제어 직전에 최신 상태를 읽는다(조회용 client, 재시도 허용)."""

        def fetch():
            response = self._client("ec2").describe_instances(InstanceIds=[instance_id])
            for reservation in response.get("Reservations", []):
                for inst in reservation.get("Instances", []):
                    if inst["InstanceId"] == instance_id:
                        return inst["State"]["Name"]
            raise ApiError(404, "INSTANCE_NOT_FOUND", "인스턴스를 찾을 수 없습니다.")

        return self._call(fetch)

    def change_instance_state(self, instance_id: str, action: str) -> dict:
        """시작·중지를 1회 요청한다. 예외는 호출자가 FAILED/UNKNOWN으로 분류하도록 그대로 올린다.

        terminate와 강제 중지(Force)는 제공하지 않는다.
        """
        client = self._mutation_client("ec2")
        if action == "start":
            response = client.start_instances(InstanceIds=[instance_id])
            changes = response.get("StartingInstances", [])
        elif action == "stop":
            response = client.stop_instances(InstanceIds=[instance_id])
            changes = response.get("StoppingInstances", [])
        else:
            raise ValueError("지원하지 않는 작업입니다.")
        current = changes[0].get("CurrentState", {}).get("Name") if changes else None
        return {
            "aws_request_id": response.get("ResponseMetadata", {}).get("RequestId"),
            "current_state": current,
        }

    def list_ec2(self) -> list[dict]:
        self.get_context()

        def fetch():
            items = []
            paginator = self._client("ec2").get_paginator("describe_instances")
            for page in paginator.paginate():
                for reservation in page.get("Reservations", []):
                    for inst in reservation.get("Instances", []):
                        tags = {t["Key"]: t["Value"] for t in inst.get("Tags", [])}
                        items.append({
                            "instance_id": inst["InstanceId"],
                            "name": tags.get("Name") or inst["InstanceId"],
                            "state": inst.get("State", {}).get("Name"),
                            "instance_type": inst.get("InstanceType"),
                            "private_ip": inst.get("PrivateIpAddress"),
                            "public_ip": inst.get("PublicIpAddress"),
                            "controllable": inst["InstanceId"] in self.settings.allowed_instance_ids,
                        })
            return items

        return self._call(fetch)

    def list_s3(self) -> list[dict]:
        self.get_context()

        def fetch():
            paginator = self._client("s3").get_paginator("list_buckets")
            return [
                {"name": bucket["Name"]}
                for page in paginator.paginate()
                for bucket in page.get("Buckets", [])
            ]

        return self._call(fetch)

    def list_security_groups(self) -> list[dict]:
        self.get_context()

        def fetch():
            paginator = self._client("ec2").get_paginator("describe_security_groups")
            return [
                {
                    "GroupId": sg["GroupId"],
                    "GroupName": sg["GroupName"],
                    "IpPermissions": sg.get("IpPermissions", []),
                }
                for page in paginator.paginate()
                for sg in page.get("SecurityGroups", [])
            ]

        return self._call(fetch)

    def scan_attached_security_groups(self) -> dict:
        """실제 EC2(모든 ENI)에 연결된 보안 그룹만 조회해 inspect_group으로 점검한다.

        사용 API: DescribeInstances, DescribeSecurityGroups (계정 확인용 STS 제외).
        제어 허용 목록(ALLOWED_INSTANCE_IDS)은 점검 대상 필터로 쓰지 않는다.
        """
        self.get_context()

        def scan():
            ec2 = self._client("ec2")

            # 1) 실제 인스턴스의 모든 ENI에서 그룹 연결을 모은다.
            attachments: dict[str, list[dict]] = {}
            seen: set[tuple[str, str, str]] = set()
            instance_ids: set[str] = set()
            paginator = ec2.get_paginator("describe_instances")
            filters = [{"Name": "instance-state-name", "Values": SCAN_INSTANCE_STATES}]
            for page in paginator.paginate(Filters=filters):
                for reservation in page.get("Reservations", []):
                    for inst in reservation.get("Instances", []):
                        instance_id = inst.get("InstanceId")
                        if not instance_id:
                            raise scan_incomplete("인스턴스 ID가 없는 항목이 있습니다")
                        instance_ids.add(instance_id)
                        tags = {t["Key"]: t["Value"] for t in inst.get("Tags", [])}
                        name = tags.get("Name") or instance_id
                        state = inst.get("State", {}).get("Name")

                        # VPC 인스턴스는 ENI가 최소 1개, ENI는 보안 그룹이 최소 1개다. 없으면 정보 누락으로 본다.
                        interfaces = inst.get("NetworkInterfaces")
                        if not interfaces:
                            raise scan_incomplete(f"{instance_id}의 네트워크 인터페이스 정보가 없습니다")
                        for eni in interfaces:
                            eni_id = eni.get("NetworkInterfaceId")
                            groups = eni.get("Groups")
                            if not eni_id or not groups:
                                raise scan_incomplete(f"{instance_id}의 네트워크 인터페이스 보안 그룹 정보가 없습니다")
                            for group in groups:
                                group_id = group.get("GroupId")
                                if not group_id:
                                    raise scan_incomplete(f"{instance_id}의 보안 그룹 ID가 없습니다")
                                key = (instance_id, eni_id, group_id)
                                if key in seen:
                                    continue
                                seen.add(key)
                                attachments.setdefault(group_id, []).append({
                                    "instance_id": instance_id,
                                    "instance_name": name,
                                    "state": state,
                                    "network_interface_id": eni_id,
                                })

            # 연결된 그룹이 없으면 보안 그룹 조회를 하지 않는다(빈 GroupIds는 전체 조회가 되므로).
            if not attachments:
                return self._scan_result([], [], instance_ids)

            # 2) 고유 그룹 ID를 정렬해 100개씩 조회하고, 요청한 그룹이 모두 왔는지 확인한다.
            group_ids = sorted(attachments)
            described: dict[str, dict] = {}
            for start in range(0, len(group_ids), SG_BATCH_SIZE):
                batch = group_ids[start:start + SG_BATCH_SIZE]
                try:
                    response = ec2.describe_security_groups(GroupIds=batch)
                except ClientError as exc:
                    code = exc.response.get("Error", {}).get("Code", "")
                    if code.startswith("InvalidGroup"):
                        # 조회 도중 그룹이 삭제되는 등 대상이 바뀌었다.
                        raise scan_incomplete("조회 중 보안 그룹이 변경되었습니다") from exc
                    raise
                returned = {sg["GroupId"]: sg for sg in response.get("SecurityGroups", [])}
                if set(returned) != set(batch):
                    raise scan_incomplete("요청한 보안 그룹 일부가 조회되지 않았습니다")
                described.update(returned)

            # 3) 각 그룹을 정확히 한 번 점검한다. 발견에는 그룹의 연결 대상을 붙이되 인스턴스 수만큼 늘리지 않는다.
            findings: list[dict] = []
            checked_groups: list[dict] = []
            for group_id in group_ids:
                sg = described[group_id]
                connections = attachments[group_id]
                checked_groups.append({"group_id": group_id, "group_name": sg.get("GroupName"), "connections": connections})
                for finding in inspect_group(sg):
                    findings.append({**finding, "connections": connections})
            return self._scan_result(findings, checked_groups, instance_ids)

        return self._call(scan)

    @staticmethod
    def _scan_result(findings: list[dict], checked_groups: list[dict], instance_ids: set[str]) -> dict:
        """전체 조회가 성공했을 때만 호출된다. 실패는 모두 ApiError로 올라간다."""
        return {
            "findings": findings,
            "checked_groups": checked_groups,
            "checked_instance_count": len(instance_ids),
            "checked_group_count": len({g["group_id"] for g in checked_groups}),
            "finding_count": len(findings),
            "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        }
