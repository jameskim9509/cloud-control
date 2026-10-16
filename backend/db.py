"""DB 연결. FastAPI(main.py)를 import하지 않고 config와 모델만 사용한다."""

from collections.abc import Iterator

from sqlalchemy import Engine, URL, create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from backend.config import DbSettings
from backend.models import ActionLog, ActionStatus


def create_db_engine(db: DbSettings) -> Engine:
    url = URL.create(
        drivername="mysql+pymysql",
        username=db.user,
        password=db.password,
        host=db.host,
        port=db.port,
        database=db.name,
        query={"charset": "utf8mb4"},
    )
    # echo=False: SQL과 연결 정보를 로그에 남기지 않는다. URL의 repr은 암호를 ***로 가린다.
    return create_engine(
        url,
        pool_pre_ping=True,
        echo=False,
        connect_args={"connect_timeout": 5, "read_timeout": 10, "write_timeout": 10},
    )


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)


def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    """요청마다 새 세션을 열고 끝나면 닫는다. 세션을 요청 간에 공유하지 않는다."""
    session = factory()
    try:
        yield session
    finally:
        session.close()


def list_recent_actions(session: Session, limit: int = 50) -> list[ActionLog]:
    stmt = select(ActionLog).order_by(ActionLog.created_at.desc(), ActionLog.id.desc()).limit(limit)
    return list(session.scalars(stmt))


# ---- 제어 이력 저장 함수. 각 함수는 필요한 경우 직접 commit한다. ----


def find_action_by_request_id(session: Session, request_id: str) -> ActionLog | None:
    return session.scalars(select(ActionLog).where(ActionLog.request_id == request_id)).first()


def has_pending_action(session: Session, instance_id: str) -> bool:
    stmt = (
        select(ActionLog.id)
        .where(ActionLog.instance_id == instance_id, ActionLog.status.in_(ActionStatus.PENDING))
        .limit(1)
    )
    return session.scalars(stmt).first() is not None


def insert_requested_action(session: Session, *, request_id: str, instance_id: str, action: str) -> ActionLog:
    """AWS 호출 전에 REQUESTED를 commit한다. request_id 중복이면 IntegrityError가 난다."""
    log = ActionLog(request_id=request_id, instance_id=instance_id, action=action, status=ActionStatus.REQUESTED)
    session.add(log)
    session.commit()
    return log


def update_action_result(
    session: Session, action_id: str, *, status: str, aws_request_id: str | None, message: str | None
) -> None:
    log = session.get(ActionLog, action_id)
    if log is None:
        raise LookupError("이력을 찾을 수 없습니다.")
    log.status = status
    log.aws_request_id = aws_request_id
    log.message = message
    session.commit()
