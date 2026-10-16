"""앱 런타임 설정. 서버나 init_db가 실행될 때 프로젝트 루트의 .env를 읽는다."""

import os
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT_DIR / ".env"
FRONTEND_DIR = ROOT_DIR / "frontend"

ALLOWED_REGION = "ap-northeast-2"
REQUIRED_APP_VARS = ("AWS_PROFILE", "AWS_REGION", "EXPECTED_ACCOUNT_ID", "ADMIN_API_TOKEN")
REQUIRED_DB_VARS = ("DB_HOST", "DB_PORT", "DB_USER", "DB_PASSWORD", "DB_NAME")


class ConfigError(RuntimeError):
    """설정 오류. 메시지에는 변수 이름만 담고 값은 담지 않는다."""


@dataclass(frozen=True)
class DbSettings:
    host: str
    port: int
    user: str
    # repr에서 빼서 설정 객체가 출력돼도 암호가 드러나지 않게 한다.
    password: str = field(repr=False)
    name: str


@dataclass(frozen=True)
class Settings:
    aws_profile: str
    aws_region: str
    expected_account_id: str
    # repr에서 빼서 Settings가 로그나 오류 메시지에 찍혀도 토큰이 드러나지 않게 한다.
    admin_api_token: str = field(repr=False)
    allowed_instance_ids: frozenset[str]
    db: DbSettings


def parse_id_list(raw: str) -> frozenset[str]:
    """쉼표로 나누고 앞뒤 공백과 빈 항목을 제거해 집합으로 만든다."""
    return frozenset(item.strip() for item in raw.split(",") if item.strip())


def _read_required(names: tuple[str, ...]) -> dict[str, str]:
    # import 시점이 아니라 처음 호출될 때(서버·init_db 실행 시) .env를 읽는다.
    load_dotenv(ENV_FILE)
    values = {name: os.getenv(name, "").strip() for name in names}
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise ConfigError(f"필수 설정이 없습니다: {', '.join(missing)} (.env.example 참고)")
    return values


@lru_cache
def get_db_settings() -> DbSettings:
    """DB 설정만 읽는다. init_db는 AWS·토큰 설정 없이도 실행할 수 있다."""
    values = _read_required(REQUIRED_DB_VARS)
    try:
        port = int(values["DB_PORT"])
    except ValueError:
        port = 0
    if not 1 <= port <= 65535:
        raise ConfigError("DB_PORT는 1~65535 사이의 숫자여야 합니다.")
    return DbSettings(
        host=values["DB_HOST"],
        port=port,
        user=values["DB_USER"],
        password=values["DB_PASSWORD"],
        name=values["DB_NAME"],
    )


@lru_cache
def get_settings() -> Settings:
    values = _read_required(REQUIRED_APP_VARS)

    if values["AWS_REGION"] != ALLOWED_REGION:
        raise ConfigError(f"AWS_REGION은 {ALLOWED_REGION}만 허용됩니다.")

    if not re.fullmatch(r"\d{12}", values["EXPECTED_ACCOUNT_ID"]):
        raise ConfigError("EXPECTED_ACCOUNT_ID는 12자리 숫자여야 합니다.")

    return Settings(
        aws_profile=values["AWS_PROFILE"],
        aws_region=values["AWS_REGION"],
        expected_account_id=values["EXPECTED_ACCOUNT_ID"],
        admin_api_token=values["ADMIN_API_TOKEN"],
        # 비어 있어도 된다. 그 경우 조회만 가능하고 모든 시작·중지가 거절된다.
        allowed_instance_ids=parse_id_list(os.getenv("ALLOWED_INSTANCE_IDS", "")),
        db=get_db_settings(),
    )
