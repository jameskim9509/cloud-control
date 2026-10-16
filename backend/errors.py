"""API 오류 형식. 본문은 {"detail": {"code": ..., "message": ...}}로 통일한다."""


class ApiError(Exception):
    def __init__(self, status_code: int, code: str, message: str, headers: dict[str, str] | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.headers = headers


def db_unavailable() -> ApiError:
    # DB 오류 원인에는 호스트·계정 정보가 섞일 수 있어 응답에는 넣지 않는다.
    return ApiError(503, "DB_UNAVAILABLE", "이력 DB에 연결하지 못했습니다. MariaDB 상태와 DB 설정을 확인하세요.")
