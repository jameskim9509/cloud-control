"""Boto3 조회·제어와 AWS 오류 분류. 모든 함수는 동기 함수다."""

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
