"""DB 모델. 시각은 모두 UTC이며 tzinfo 없는 DATETIME으로 저장한다."""

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, Index, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

TABLE_ARGS = {
    "mysql_engine": "InnoDB",
    "mysql_charset": "utf8mb4",
    "mysql_collate": "utf8mb4_unicode_ci",
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def new_uuid() -> str:
    return str(uuid.uuid4())


class ActionStatus:
    REQUESTED = "REQUESTED"  # DB에 기록했고 AWS 호출 전이거나 결과 미기록
    ACCEPTED = "ACCEPTED"  # AWS가 요청을 접수함
    FAILED = "FAILED"  # AWS가 명시적으로 거절했거나 요청이 전송되지 않음
    UNKNOWN = "UNKNOWN"  # 전송했지만 결과를 알 수 없음(타임아웃 등)

    # 이 상태의 이력이 있는 대상에는 새 제어를 받지 않는다.
    PENDING = (REQUESTED, UNKNOWN)


class Base(DeclarativeBase):
    pass


class ActionLog(Base):
    """EC2 시작·중지 요청 이력. AWS 호출 전에 기록하고 결과로 status를 갱신한다."""

    __tablename__ = "action_logs"
    __table_args__ = (
        Index("ix_action_logs_created_at", "created_at"),
        Index("ix_action_logs_instance_id", "instance_id"),
        TABLE_ARGS,
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    request_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    instance_id: Mapped[str] = mapped_column(String(32), nullable=False)
    action: Mapped[str] = mapped_column(String(16), nullable=False)  # start | stop
    status: Mapped[str] = mapped_column(String(16), nullable=False)  # ActionStatus 값
    aws_request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False, default=utcnow)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "request_id": self.request_id,
            "instance_id": self.instance_id,
            "action": self.action,
            "status": self.status,
            "aws_request_id": self.aws_request_id,
            # 저장값은 UTC이므로 응답에는 +00:00을 붙여 시간대를 명시한다.
            "created_at": self.created_at.replace(tzinfo=timezone.utc).isoformat(),
            "message": self.message,
        }
