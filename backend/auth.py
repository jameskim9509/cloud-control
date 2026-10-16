"""앱 토큰 인증. 모든 /api 경로에 공통 의존성으로 적용한다."""

import secrets

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from backend.errors import ApiError

# auto_error=False: 헤더 누락·다른 scheme·빈 값에서 FastAPI 기본 오류 대신 None을 받아 직접 처리한다.
bearer_scheme = HTTPBearer(auto_error=False)


def auth_required() -> ApiError:
    return ApiError(
        401,
        "APP_AUTH_REQUIRED",
        "앱 토큰이 없거나 올바르지 않습니다. 토큰을 다시 입력하세요.",
        headers={"WWW-Authenticate": "Bearer"},
    )


def require_app_token(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> None:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise auth_required()

    supplied = credentials.credentials.strip()
    if not supplied:
        raise auth_required()

    expected = request.app.state.settings.admin_api_token
    # str끼리 비교하면 비ASCII 입력에서 TypeError(500)가 나므로 bytes로 바꿔 비교한다.
    if not secrets.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8")):
        raise auth_required()
