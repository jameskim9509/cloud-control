"""Boto3 조회와 AWS 오류 분류. 모든 함수는 동기 함수다."""

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

BOTO_CONFIG = Config(
    connect_timeout=3,
    read_timeout=10,
    retries={"max_attempts": 2, "mode": "standard"},
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


class AwsServiceError(Exception):
    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def access_denied() -> AwsServiceError:
    return AwsServiceError(403, "AWS_ACCESS_DENIED", "AWS 권한이 부족합니다.")


def login_required() -> AwsServiceError:
    return AwsServiceError(
        503,
        "AWS_LOGIN_REQUIRED",
        "AWS 로그인이 필요하거나 만료되었습니다. 터미널에서 aws login을 다시 실행하세요.",
    )


def translate_error(exc: Exception) -> AwsServiceError:
    """Boto3 예외를 API 오류로 바꾼다. 빈 목록으로 숨기지 않는다."""
    if isinstance(exc, ClientError):
        code = exc.response.get("Error", {}).get("Code", "")
        if code in ACCESS_DENIED_CODES:
            return access_denied()
        if code in LOGIN_REQUIRED_CODES:
            return login_required()
        return AwsServiceError(502, "AWS_REQUEST_FAILED", f"AWS 요청이 실패했습니다. ({code or '알 수 없음'})")
    # LoginInsufficientPermissions는 LoginError의 하위 클래스이므로 먼저 검사한다.
    if isinstance(exc, LoginInsufficientPermissions):
        return access_denied()
    if isinstance(exc, LOGIN_REQUIRED_ERRORS):
        return login_required()
    if isinstance(exc, (BotoConnectionError, HTTPClientError)):
        return AwsServiceError(503, "AWS_CONNECTION_ERROR", "AWS에 연결하지 못했습니다. 네트워크를 확인하세요.")
    if isinstance(exc, BotoCoreError):
        return AwsServiceError(502, "AWS_REQUEST_FAILED", "AWS 요청 처리 중 오류가 발생했습니다.")
    raise exc


class AwsService:
    def __init__(self, settings: Settings):
        self.settings = settings

    def _client(self, service: str):
        # 요청마다 Session을 만들어 스레드 간 공유를 피하고 재로그인 결과를 바로 반영한다.
        session = boto3.Session(
            profile_name=self.settings.aws_profile,
            region_name=self.settings.aws_region,
        )
        return session.client(service, config=BOTO_CONFIG)

    def _call(self, func):
        try:
            return func()
        except AwsServiceError:
            raise
        except Exception as exc:
            raise translate_error(exc) from exc

    def get_context(self) -> dict:
        """STS 신원을 확인하고 기대한 계정이 아니면 403으로 차단한다."""
        identity = self._call(lambda: self._client("sts").get_caller_identity())
        if identity["Account"] != self.settings.expected_account_id:
            raise AwsServiceError(
                403,
                "AWS_ACCOUNT_MISMATCH",
                "로그인한 AWS 계정이 EXPECTED_ACCOUNT_ID와 다릅니다.",
            )
        return {
            "account_id": identity["Account"],
            "caller_arn": identity["Arn"],
            "region": self.settings.aws_region,
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
