"""EC2 제어 API 테스트. 실제 AWS와 실습 DB(MariaDB)는 호출하지 않는다.

- AWS: AwsService의 client 생성 함수를 mock으로 바꾼다.
- DB: 메모리 SQLite로 바꾸고, DB 실패는 저장 함수를 mock으로 바꿔 흉내 낸다.

실행: python -m unittest -v tests.test_actions
"""

import asyncio
import dataclasses
import json
import threading
import unittest
import uuid
from unittest import mock

from botocore.exceptions import ClientError, EndpointConnectionError, ReadTimeoutError
from sqlalchemy import create_engine, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.pool import StaticPool

from backend import db as store
from backend import ec2_control
from backend.aws_service import MUTATION_CONFIG, AwsService
from backend.config import DbSettings, Settings
from backend.db import create_session_factory
from backend.main import app
from backend.models import ActionLog, ActionStatus, Base

TOKEN = "test-token"
ACCOUNT = "123456789012"
ALLOWED = "i-0123456789abcdef0"
NOT_ALLOWED = "i-0fffffffffffffff0"


def make_settings() -> Settings:
    return Settings(
        aws_profile="test",
        aws_region="ap-northeast-2",
        expected_account_id=ACCOUNT,
        admin_api_token=TOKEN,
        allowed_instance_ids=frozenset({ALLOWED}),
        db=DbSettings(host="unused", port=3306, user="unused", password="unused", name="unused"),
    )


def call_api(method: str, path: str, body=None, token: str | None = TOKEN) -> tuple[int, dict]:
    """httpx 없이 ASGI 앱을 직접 호출한다. lifespan(.env 읽기)은 실행하지 않는다."""
    raw = b"" if body is None else json.dumps(body).encode()
    headers = [(b"content-type", b"application/json")]
    if token is not None:
        headers.append((b"authorization", f"Bearer {token}".encode()))
    messages = []
    sent = False

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": raw, "more_body": False}
        await asyncio.Event().wait()

    async def send(message):
        messages.append(message)

    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": method, "scheme": "http", "path": path, "raw_path": path.encode(),
        "query_string": b"", "root_path": "", "headers": headers,
        "client": ("127.0.0.1", 1), "server": ("127.0.0.1", 8001), "app": app,
    }
    asyncio.run(app(scope, receive, send))
    status = messages[0]["status"]
    payload = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
    return status, json.loads(payload)


class ActionApiTest(unittest.TestCase):
    def setUp(self):
        self.settings = make_settings()
        self.engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
        Base.metadata.create_all(self.engine)
        self.session_factory = create_session_factory(self.engine)

        # AWS client 대체: 조회용(sts, ec2)과 변경용(ec2) client를 따로 둔다.
        self.sts = mock.MagicMock()
        self.sts.get_caller_identity.return_value = {"Account": ACCOUNT, "Arn": "arn:aws:iam::123456789012:user/test"}
        self.ec2_read = mock.MagicMock()
        self.set_state("stopped")
        self.ec2_mutation = mock.MagicMock()
        self.ec2_mutation.start_instances.return_value = {
            "StartingInstances": [{"InstanceId": ALLOWED, "CurrentState": {"Name": "pending"}}],
            "ResponseMetadata": {"RequestId": "aws-req-1"},
        }
        self.ec2_mutation.stop_instances.return_value = {
            "StoppingInstances": [{"InstanceId": ALLOWED, "CurrentState": {"Name": "stopping"}}],
            "ResponseMetadata": {"RequestId": "aws-req-2"},
        }
        patches = [
            mock.patch.object(AwsService, "_client", side_effect=lambda service: self.sts if service == "sts" else self.ec2_read),
            mock.patch.object(AwsService, "_mutation_client", return_value=self.ec2_mutation),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

        app.state.settings = self.settings
        app.state.aws = AwsService(self.settings)
        app.state.session_factory = self.session_factory

    def tearDown(self):
        self.engine.dispose()

    # ---- 도우미 ----

    def set_state(self, state: str):
        self.ec2_read.describe_instances.return_value = {
            "Reservations": [{"Instances": [{"InstanceId": ALLOWED, "State": {"Name": state}}]}]
        }

    def mutation_calls(self) -> int:
        return self.ec2_mutation.start_instances.call_count + self.ec2_mutation.stop_instances.call_count

    def rows(self) -> list[ActionLog]:
        with self.session_factory() as s:
            return list(s.scalars(select(ActionLog)))

    def post(self, action="start", request_id=None, instance_id=ALLOWED, token=TOKEN, body=None):
        if body is None:
            body = {"action": action, "request_id": request_id or str(uuid.uuid4())}
        return call_api("POST", f"/api/ec2/{instance_id}/actions", body, token=token)

    def add_log(self, status: str, instance_id=ALLOWED):
        with self.session_factory() as s:
            s.add(ActionLog(request_id=str(uuid.uuid4()), instance_id=instance_id, action="start", status=status))
            s.commit()

    # ---- 인증·허용 목록·입력 ----

    def test_invalid_token_makes_no_mutation_call(self):
        for token in (None, "wrong-token", ""):
            status, body = self.post(token=token)
            self.assertEqual(status, 401)
            self.assertEqual(body["detail"]["code"], "APP_AUTH_REQUIRED")
        self.assertEqual(self.mutation_calls(), 0)
        self.assertEqual(self.rows(), [])

    def test_not_allowed_instance_is_rejected_before_aws(self):
        status, body = self.post(instance_id=NOT_ALLOWED)
        self.assertEqual(status, 403)
        self.assertEqual(body["detail"]["code"], "INSTANCE_NOT_ALLOWED")
        self.assertEqual(self.mutation_calls(), 0)
        self.sts.get_caller_identity.assert_not_called()
        self.ec2_read.describe_instances.assert_not_called()
        self.assertEqual(self.rows(), [])

    def test_empty_allowlist_rejects_all_control(self):
        app.state.aws = AwsService(dataclasses.replace(self.settings, allowed_instance_ids=frozenset()))
        status, body = self.post()
        self.assertEqual(status, 403)
        self.assertEqual(body["detail"]["code"], "CONTROL_DISABLED")
        self.assertEqual(self.mutation_calls(), 0)

    def test_invalid_body_returns_422(self):
        cases = [
            {"action": "start", "request_id": str(uuid.uuid4()), "region": "us-east-1"},  # 추가 필드
            {"action": "start", "request_id": "not-a-uuid"},
            {"action": "terminate", "request_id": str(uuid.uuid4())},
            {"action": "start"},
        ]
        for body in cases:
            status, payload = self.post(body=body)
            self.assertEqual(status, 422, body)
            self.assertEqual(payload["detail"]["code"], "VALIDATION_ERROR")
        self.assertEqual(self.mutation_calls(), 0)

    def test_account_mismatch_returns_403(self):
        self.sts.get_caller_identity.return_value = {"Account": "999999999999", "Arn": "arn:aws:iam::999999999999:user/x"}
        status, body = self.post()
        self.assertEqual(status, 403)
        self.assertEqual(body["detail"]["code"], "AWS_ACCOUNT_MISMATCH")
        self.assertEqual(self.mutation_calls(), 0)
        self.assertEqual(self.rows(), [])

    # ---- 상태 검사 ----

    def test_state_mismatch_returns_409(self):
        for state, action in [("running", "start"), ("stopped", "stop"), ("pending", "start"), ("stopping", "stop")]:
            self.set_state(state)
            status, body = self.post(action=action)
            self.assertEqual(status, 409, (state, action))
            self.assertEqual(body["detail"]["code"], "INVALID_STATE")
        self.assertEqual(self.mutation_calls(), 0)
        self.assertEqual(self.rows(), [])

    def test_stop_from_running_is_accepted(self):
        self.set_state("running")
        status, body = self.post(action="stop")
        self.assertEqual(status, 202)
        self.assertEqual(self.ec2_mutation.stop_instances.call_count, 1)
        # 강제 중지(Force)를 쓰지 않는다.
        self.assertNotIn("Force", self.ec2_mutation.stop_instances.call_args.kwargs)

    # ---- DB 실패 ----

    def test_db_failure_makes_no_mutation_call(self):
        error = OperationalError("SELECT", {}, Exception("db down"))
        for target in ("find_action_by_request_id", "has_pending_action", "insert_requested_action"):
            with self.subTest(target=target), mock.patch.object(store, target, side_effect=error):
                status, body = self.post()
                self.assertEqual(status, 503)
                self.assertEqual(body["detail"]["code"], "DB_UNAVAILABLE")
        self.assertEqual(self.mutation_calls(), 0)

    # ---- 접수와 같은 키 재전송 ----

    def test_first_request_accepted_and_replay_does_not_call_aws_again(self):
        request_id = str(uuid.uuid4())
        status, first = self.post(request_id=request_id)
        self.assertEqual(status, 202)
        self.assertEqual(first["status"], "ACCEPTED")
        self.assertEqual(first["request_id"], request_id)

        # 상태가 pending으로 바뀌어도 중복 판정이 먼저이므로 409가 아니라 200이다.
        self.set_state("pending")
        for _ in range(3):
            status, again = self.post(request_id=request_id)
            self.assertEqual(status, 200)
            self.assertEqual(again, {"action_id": first["action_id"], "request_id": request_id, "status": "ACCEPTED"})

        self.assertEqual(self.mutation_calls(), 1)
        [row] = self.rows()
        self.assertEqual(row.status, ActionStatus.ACCEPTED)
        self.assertEqual(row.aws_request_id, "aws-req-1")

    def test_same_key_with_different_content_returns_409(self):
        request_id = str(uuid.uuid4())
        self.assertEqual(self.post(request_id=request_id)[0], 202)
        status, body = self.post(action="stop", request_id=request_id)
        self.assertEqual(status, 409)
        self.assertEqual(body["detail"]["code"], "IDEMPOTENCY_CONFLICT")
        self.assertEqual(self.mutation_calls(), 1)

    def test_unique_conflict_on_insert_returns_existing_record(self):
        # 조회 시점에는 없었지만 insert 직전에 다른 경로가 같은 키를 기록한 경우를 흉내 낸다.
        request_id = str(uuid.uuid4())
        with self.session_factory() as s:
            s.add(ActionLog(request_id=request_id, instance_id=ALLOWED, action="start", status=ActionStatus.ACCEPTED))
            s.commit()
        real_find = store.find_action_by_request_id
        calls = []

        def missing_first(session, rid):
            calls.append(rid)
            return None if len(calls) == 1 else real_find(session, rid)

        with mock.patch.object(store, "find_action_by_request_id", side_effect=missing_first):
            status, body = self.post(request_id=request_id)
        self.assertEqual(len(calls), 2)  # 첫 조회 + IntegrityError 뒤 재조회
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ACCEPTED")
        self.assertEqual(self.mutation_calls(), 0)

    # ---- 결과 불확실·거절 ----

    def test_timeout_records_unknown_and_never_retries(self):
        self.ec2_mutation.start_instances.side_effect = ReadTimeoutError(endpoint_url="https://ec2")
        request_id = str(uuid.uuid4())
        status, body = self.post(request_id=request_id)
        self.assertEqual(status, 504)
        self.assertEqual(body["detail"]["code"], "AWS_RESULT_UNKNOWN")
        self.assertEqual(self.mutation_calls(), 1)
        [row] = self.rows()
        self.assertEqual(row.status, ActionStatus.UNKNOWN)

        # 같은 키 재전송은 기록된 UNKNOWN을 돌려주고 AWS를 다시 부르지 않는다.
        status, again = self.post(request_id=request_id)
        self.assertEqual(status, 200)
        self.assertEqual(again["status"], "UNKNOWN")
        # 새 키로 다시 보내도 UNKNOWN 이력 때문에 거절된다.
        status, blocked = self.post()
        self.assertEqual(status, 409)
        self.assertEqual(blocked["detail"]["code"], "ACTION_IN_PROGRESS")
        self.assertEqual(self.mutation_calls(), 1)

    def test_explicit_rejection_records_failed(self):
        self.ec2_mutation.start_instances.side_effect = ClientError(
            {"Error": {"Code": "IncorrectInstanceState", "Message": "x"}, "ResponseMetadata": {"RequestId": "aws-req-9"}},
            "StartInstances",
        )
        status, body = self.post()
        self.assertEqual(status, 409)
        self.assertEqual(body["detail"]["code"], "INVALID_STATE")
        [row] = self.rows()
        self.assertEqual(row.status, ActionStatus.FAILED)
        self.assertEqual(row.aws_request_id, "aws-req-9")
        self.assertEqual(self.mutation_calls(), 1)

    def test_connection_refused_records_failed(self):
        self.ec2_mutation.start_instances.side_effect = EndpointConnectionError(endpoint_url="https://ec2")
        status, body = self.post()
        self.assertEqual(status, 503)
        self.assertEqual(body["detail"]["code"], "AWS_CONNECTION_ERROR")
        self.assertEqual(self.rows()[0].status, ActionStatus.FAILED)
        self.assertEqual(self.mutation_calls(), 1)

    def test_db_update_failure_after_accept_keeps_requested(self):
        with mock.patch.object(store, "update_action_result", side_effect=OperationalError("UPDATE", {}, Exception("db down"))):
            with self.assertLogs("backend.ec2_control", level="ERROR"):
                status, body = self.post()
        self.assertEqual(status, 202)
        self.assertEqual(body["status"], "ACCEPTED")
        [row] = self.rows()
        self.assertEqual(row.status, ActionStatus.REQUESTED)  # 삭제하지 않는다
        self.assertEqual(self.mutation_calls(), 1)  # 재실행하지 않는다

    # ---- 미확정 이력 차단 ----

    def test_pending_history_blocks_new_request(self):
        for pending in (ActionStatus.REQUESTED, ActionStatus.UNKNOWN):
            with self.subTest(status=pending):
                self.setUp()
                self.add_log(pending)
                status, body = self.post()
                self.assertEqual(status, 409)
                self.assertEqual(body["detail"]["code"], "ACTION_IN_PROGRESS")
                self.assertEqual(self.mutation_calls(), 0)
                # 상태 검사보다 먼저 거절하므로 AWS 조회도 하지 않는다.
                self.ec2_read.describe_instances.assert_not_called()

    def test_finished_history_does_not_block(self):
        self.add_log(ActionStatus.FAILED)
        self.add_log(ActionStatus.ACCEPTED)
        self.assertEqual(self.post()[0], 202)

    # ---- lock·client 설정 ----

    def test_lock_is_single_and_held_during_aws_call(self):
        self.assertIsInstance(ec2_control.CONTROL_LOCK, type(threading.Lock()))
        lock_before = ec2_control.CONTROL_LOCK
        held = []
        self.ec2_mutation.start_instances.side_effect = lambda **kw: (
            held.append(ec2_control.CONTROL_LOCK.locked()),
            {"StartingInstances": [], "ResponseMetadata": {"RequestId": "r"}},
        )[1]
        self.assertEqual(self.post()[0], 202)
        self.assertEqual(self.post()[0], 202)  # 이전 이력이 ACCEPTED이고 상태 mock이 stopped라 다시 접수된다
        self.assertIs(ec2_control.CONTROL_LOCK, lock_before)
        self.assertEqual(held, [True, True])
        self.assertFalse(ec2_control.CONTROL_LOCK.locked())

    def test_mutation_client_has_no_retry_and_timeouts(self):
        self.assertEqual(MUTATION_CONFIG.retries["total_max_attempts"], 1)
        self.assertIsNotNone(MUTATION_CONFIG.connect_timeout)
        self.assertIsNotNone(MUTATION_CONFIG.read_timeout)


if __name__ == "__main__":
    unittest.main()
