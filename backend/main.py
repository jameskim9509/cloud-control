"""FastAPI 진입점. 실행: uvicorn backend.main:app --host 127.0.0.1 --port 8001"""

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from backend.aws_service import AwsService, AwsServiceError
from backend.config import FRONTEND_DIR, get_settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 서버 시작 시 설정을 읽는다. 누락되면 변수 이름만 담긴 ConfigError로 시작이 중단된다.
    app.state.aws = AwsService(get_settings())
    yield


app = FastAPI(title="Cloud Control", lifespan=lifespan)


@app.exception_handler(AwsServiceError)
async def aws_error_handler(request: Request, exc: AwsServiceError):
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": {"code": exc.code, "message": exc.message}},
    )


def aws(request: Request) -> AwsService:
    return request.app.state.aws


# Boto3 호출은 동기이므로 일반 def로 선언해 스레드풀에서 실행되게 한다.
@app.get("/api/context")
def get_context(request: Request):
    return aws(request).get_context()


@app.get("/api/ec2")
def list_ec2(request: Request):
    items = aws(request).list_ec2()
    return {"items": items, "count": len(items)}


@app.get("/api/s3")
def list_s3(request: Request):
    items = aws(request).list_s3()
    return {"items": items, "count": len(items)}


@app.get("/api/security-groups")
def list_security_groups(request: Request):
    items = aws(request).list_security_groups()
    return {"items": items, "count": len(items)}


# 루트 전체가 아니라 frontend 폴더만 공개한다. API 경로 등록 뒤에 마운트해야 /api가 가려지지 않는다.
if FRONTEND_DIR.is_dir():
    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
