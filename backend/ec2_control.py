"""EC2 시작·중지 처리. 학습용 단일 프로세스 앱을 전제로 한다.

순서: 허용 목록 → [lock] 중복 키 → 미확정 이력 → 계정 확인 → 최신 상태 → REQUESTED commit → AWS 1회 호출 → 결과 기록
자동 재시도, waiter, 상태 폴링은 하지 않는다.
"""

import logging
import threading

from botocore.exceptions import ClientError, ConnectionError as BotoConnectionError
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from backend import db as store
from backend.aws_service import LOGIN_REQUIRED_ERRORS, AwsService, translate_error
from backend.errors import ApiError, db_unavailable
from backend.models import ActionLog, ActionStatus

logger = logging.getLogger(__name__)

# 프로세스에서 한 번만 만든다. 모든 변경 진입점이 이 lock을 사용한다.
CONTROL_LOCK = threading.Lock()

REQUIRED_STATE = {"start": "stopped", "stop": "running"}


def _db(session: Session, func):
    """DB 실패는 503으로 바꾼다. 이 시점에는 아직 AWS 변경 호출 전이다."""
    try:
        return func()
    except SQLAlchemyError as exc:
        session.rollback()
        raise db_unavailable() from exc


def _replay(log: ActionLog, instance_id: str, action: str) -> tuple[int, dict]:
    """같은 request_id 재전송: 내용이 같으면 기존 결과를 돌려주고 다시 실행하지 않는다."""
    if log.instance_id != instance_id or log.action != action:
        raise ApiError(409, "IDEMPOTENCY_CONFLICT", "같은 요청 키가 다른 대상이나 작업에 이미 사용되었습니다.")
    return 200, {"action_id": log.id, "request_id": log.request_id, "status": log.status}


def classify_aws_error(exc: Exception) -> tuple[str, ApiError, str | None]:
    """AWS 변경 호출 예외를 (이력 상태, 응답 오류, AWS 요청 ID)로 분류한다."""
    if isinstance(exc, ClientError):
        # AWS가 응답으로 명시적으로 거절했다.
        aws_request_id = exc.response.get("ResponseMetadata", {}).get("RequestId")
        return ActionStatus.FAILED, translate_error(exc), aws_request_id
    if isinstance(exc, LOGIN_REQUIRED_ERRORS):
        # 자격 증명을 얻지 못해 요청을 보내지 못했다.
        return ActionStatus.FAILED, translate_error(exc), None
    if isinstance(exc, BotoConnectionError):
        # 연결 자체가 안 됐다(연결 거부·DNS·연결 timeout·TLS). 요청 본문은 전송되지 않았다.
        return ActionStatus.FAILED, translate_error(exc), None
    # 읽기 timeout·연결 끊김·예상하지 못한 오류: 전송됐을 수 있으므로 결과를 단정하지 않는다.
    error = ApiError(
        504,
        "AWS_RESULT_UNKNOWN",
        "AWS 처리 결과를 확인하지 못했습니다. 콘솔에서 인스턴스 상태를 확인하세요. 같은 요청 키로 다시 보내면 기록된 결과를 돌려줍니다.",
    )
    return ActionStatus.UNKNOWN, error, None


def _record_result(session: Session, action_id: str, status: str, aws_request_id: str | None, message: str | None) -> None:
    """AWS 호출 뒤 결과를 기록한다. 실패해도 REQUESTED를 지우거나 AWS를 다시 호출하지 않는다."""
    try:
        store.update_action_result(session, action_id, status=status, aws_request_id=aws_request_id, message=message)
    except Exception:
        try:
            session.rollback()
        except Exception:
            pass
        # 비밀값 없이 식별자만 남긴다. 이력은 REQUESTED로 남아 같은 대상의 새 제어를 막는다.
        logger.error("제어 결과 DB 기록 실패: action_id=%s status=%s (REQUESTED로 유지)", action_id, status)


def request_ec2_action(aws: AwsService, session: Session, instance_id: str, action: str, request_id: str) -> tuple[int, dict]:
    if action not in REQUIRED_STATE:
        raise ApiError(422, "VALIDATION_ERROR", "action은 start 또는 stop이어야 합니다.")

    # 서버 허용 목록. DB·AWS 호출 전에 거절한다.
    aws.ensure_controllable(instance_id)

    with CONTROL_LOCK:
        # 1) 중복 키 판정은 EC2 상태 검사보다 먼저 한다.
        existing = _db(session, lambda: store.find_action_by_request_id(session, request_id))
        if existing is not None:
            return _replay(existing, instance_id, action)

        # 2) 같은 대상에 결과가 확정되지 않은 이력이 있으면 거절한다.
        if _db(session, lambda: store.has_pending_action(session, instance_id)):
            raise ApiError(
                409,
                "ACTION_IN_PROGRESS",
                "이 인스턴스에 결과가 확정되지 않은 요청(REQUESTED/UNKNOWN)이 있어 새 제어를 받을 수 없습니다.",
            )

        # 3) 예상 계정·고정 리전 확인(불일치 403)과 최신 상태 확인(불일치 409).
        aws.get_context()
        state = aws.get_instance_state(instance_id)
        required = REQUIRED_STATE[action]
        if state != required:
            raise ApiError(
                409,
                "INVALID_STATE",
                f"현재 상태가 {state}입니다. {action}은 {required} 상태에서만 할 수 있습니다.",
            )

        # 4) REQUESTED를 commit한 뒤에만 AWS를 호출한다.
        try:
            log = store.insert_requested_action(session, request_id=request_id, instance_id=instance_id, action=action)
        except IntegrityError as exc:
            # request_id UNIQUE 충돌: 다른 경로에서 먼저 기록됐다. 그 기록을 기준으로 응답한다.
            session.rollback()
            existing = _db(session, lambda: store.find_action_by_request_id(session, request_id))
            if existing is not None:
                return _replay(existing, instance_id, action)
            raise db_unavailable() from exc
        except SQLAlchemyError as exc:
            session.rollback()
            raise db_unavailable() from exc
        action_id = log.id

        # 5) AWS 변경 호출은 정확히 1회. 재시도하지 않는다.
        try:
            result = aws.change_instance_state(instance_id, action)
        except Exception as exc:
            status, error, aws_request_id = classify_aws_error(exc)
            _record_result(session, action_id, status, aws_request_id, f"{error.code}: {error.message}")
            raise error from exc

        _record_result(
            session,
            action_id,
            ActionStatus.ACCEPTED,
            result.get("aws_request_id"),
            f"AWS 접수 (현재 상태: {result.get('current_state') or '알 수 없음'})",
        )
        return 202, {"action_id": action_id, "request_id": request_id, "status": ActionStatus.ACCEPTED}
