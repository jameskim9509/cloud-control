"""없는 테이블만 생성한다. 기존 테이블과 데이터는 건드리지 않는다.

실행: python -m backend.init_db
"""

import sys

from sqlalchemy import inspect, text
from sqlalchemy.exc import SQLAlchemyError

# 모든 모델을 import해야 Base.metadata에 테이블이 등록된다.
from backend import models  # noqa: F401
from backend.config import ConfigError, get_db_settings
from backend.db import create_db_engine
from backend.models import Base


def describe_db_error(exc: SQLAlchemyError) -> str:
    """암호·URL 없이 원인만 보여 준다. PyMySQL 오류는 (코드, 메시지) 형태다."""
    orig = getattr(exc, "orig", None)
    if orig is not None and orig.args:
        return f"{type(orig).__name__}: {' '.join(str(arg) for arg in orig.args)}"
    return type(exc).__name__


def main() -> int:
    try:
        db = get_db_settings()
    except ConfigError as exc:
        print(f"[init_db] 설정 오류: {exc}", file=sys.stderr)
        return 1

    engine = create_db_engine(db)
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        before = set(inspect(engine).get_table_names())
        Base.metadata.create_all(engine, checkfirst=True)
        after = set(inspect(engine).get_table_names())
    except SQLAlchemyError as exc:
        print(f"[init_db] DB 오류: {describe_db_error(exc)}", file=sys.stderr)
        return 1
    finally:
        engine.dispose()

    expected = set(Base.metadata.tables)
    created = sorted((after - before) & expected)
    existing = sorted(before & expected)
    print(f"[init_db] 새로 만든 테이블: {', '.join(created) or '없음'}")
    print(f"[init_db] 이미 있던 테이블(유지): {', '.join(existing) or '없음'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
