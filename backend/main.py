"""FastAPI 진입점. 실행: uvicorn backend.main:app --host 127.0.0.1 --port 8001"""

from collections.abc import Iterator
from contextlib import asynccontextmanager
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import SQLAlchemyError
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from backend.auth import require_app_token
from backend.aws_service import AwsService
from backend.config import FRONTEND_DIR, get_settings
from backend.db import create_db_engine, create_session_factory, list_recent_actions, session_scope
from backend.ec2_control import request_ec2_action
from backend.errors import ApiError, db_unavailable


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 서버 시작 시 설정을 읽는다. ADMIN_API_TOKEN·DB_* 등이 없으면 변수 이름만 담긴 ConfigError로 시작이 중단된다.
    settings = get_settings()
    app.state.settings = settings
    app.state.aws = AwsService(settings)
    # 엔진(커넥션 풀)만 앱 전체가 공유하고, 세션은 요청마다 get_db에서 새로 연다.
    engine = create_db_engine(settings.db)
    app.state.session_factory = create_session_factory(engine)
    try:
        yield
    finally:
        engine.dispose()


app = FastAPI(title="Cloud Control", lifespan=lifespan)


@app.exception_handler(ApiError)
async def api_error_handler(request: Request, exc: ApiError):
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": {"code": exc.code, "message": exc.message}},
        headers=exc.headers,
    )


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    # 입력값은 되돌려 보내지 않고 문제가 된 필드 위치만 알려 준다.
    fields = sorted({".".join(str(part) for part in err.get("loc", ())[1:]) or "body" for err in exc.errors()})
    return JSONResponse(
        status_code=422,
        content={"detail": {"code": "VALIDATION_ERROR", "message": f"요청 형식이 올바르지 않습니다: {', '.join(fields)}"}},
    )


class ActionRequest(BaseModel):
    # 정의하지 않은 필드가 오면 422로 거절한다. 계정·리전·허용 목록은 요청으로 받지 않는다.
    model_config = ConfigDict(extra="forbid")

    action: Literal["start", "stop"]
    request_id: UUID


def aws(request: Request) -> AwsService:
    return request.app.state.aws


def get_db(request: Request) -> Iterator[Session]:
    yield from session_scope(request.app.state.session_factory)


# 모든 /api 경로는 이 라우터에 등록해 앱 토큰 인증을 공통으로 적용한다.
api = APIRouter(prefix="/api", dependencies=[Depends(require_app_token)])


# Boto3 호출은 동기이므로 일반 def로 선언해 스레드풀에서 실행되게 한다.
@api.get("/context")
def get_context(request: Request):
    return aws(request).get_context()


@api.get("/ec2")
def list_ec2(request: Request):
    items = aws(request).list_ec2()
    return {"items": items, "count": len(items)}


@api.get("/s3")
def list_s3(request: Request):
    items = aws(request).list_s3()
    return {"items": items, "count": len(items)}


@api.get("/security-groups")
def list_security_groups(request: Request):
    items = aws(request).list_security_groups()
    return {"items": items, "count": len(items)}


@api.get("/actions")
def list_actions(db: Session = Depends(get_db)):
    try:
        items = [log.to_dict() for log in list_recent_actions(db, limit=50)]
    except SQLAlchemyError as exc:
        raise db_unavailable() from exc
    return {"items": items, "count": len(items)}


# 변경 경로. 동기 Boto3·DB 호출이므로 일반 def로 둔다.
@api.post("/ec2/{instance_id}/actions")
def post_ec2_action(
    instance_id: str,
    body: ActionRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
):
    status_code, payload = request_ec2_action(aws(request), db, instance_id, body.action, str(body.request_id))
    response.status_code = status_code  # 최초 접수 202, 같은 요청 재전송 200
    return payload


app.include_router(api)

# 루트 전체가 아니라 frontend 폴더만 공개하고 인증은 요구하지 않는다(토큰 입력 화면을 열어야 하므로).
# API 경로 등록 뒤에 마운트해야 /api가 가려지지 않는다.
if FRONTEND_DIR.is_dir():
    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
