# Cloud Control

AWS 계정의 리소스를 웹에서 **조회**하고, 미리 지정한 EC2만 **시작·중지**하고, 실제 EC2에 연결된 **보안 그룹 규칙을 점검**하는 학습용 앱입니다.

| 기능 | 내용 |
|---|---|
| 조회 | EC2 목록, S3 버킷, 서울 리전(ap-northeast-2) 보안 그룹 |
| 제어 | `.env` 허용 목록에 있는 EC2의 시작·중지만 (요청 이력은 MariaDB에 기록) |
| 점검 | 실제 EC2의 모든 네트워크 인터페이스(ENI)에 연결된 보안 그룹에서 0.0.0.0/0·::/0에 열린 TCP 22·3389 규칙 탐지 |

## 사용 범위

- **단일 프로세스, localhost 전용 학습용 앱**입니다. 서버는 `127.0.0.1:8001`에서 uvicorn 프로세스 하나로 실행합니다.
- 중복 제어를 막는 lock이 프로세스 안에만 있으므로 워커를 여러 개 띄우거나 여러 서버를 동시에 실행하면 안 됩니다.
- 외부 공개, HTTPS, 다중 사용자를 고려하지 않았습니다. `--host 0.0.0.0`으로 열지 마세요.
- AWS 자격 증명은 학생 본인 IAM 사용자의 임시 세션(`aws login --profile lab`)을 그대로 씁니다. 앱이 AWS 권한을 추가하거나 바꾸지 않습니다.

### 지원하지 않는 기능

| 구분 | 지원하지 않음 | 대신 할 일 |
|---|---|---|
| 생성·삭제 | EC2·S3·보안 그룹 생성, EC2 종료(terminate), 강제 중지 | AWS 콘솔·CLI 사용 |
| 자동 수정 | 보안 그룹 규칙 삭제·변경, 점검 결과 자동 조치 | 점검 결과를 보고 콘솔에서 직접 수정 |
| 예약·자동화 | 예약 시작·중지, 자동 재시도, 상태 자동 폴링(waiter) | 화면의 새로고침 버튼 |
| 배포 | 외부 서버 배포, 다중 프로세스·다중 사용자 | 로컬(WSL)에서만 실행 |
| 계정 관리 | 아이디·비밀번호 로그인, JWT 발급, 사용자 관리 | 고정 앱 토큰 사용(아래 참고) |
| 기록 | 보안 점검 결과 저장 | 점검할 때마다 새로 조회 |

## 실행 순서 (WSL Ubuntu + Windows 브라우저)

아래 명령은 모두 WSL 터미널에서 실행합니다. 프로젝트 경로는 `~/vibe-lab/cloud-control` 기준입니다.

### 1. 프로젝트 폴더와 가상환경
```bash
cd ~/vibe-lab/cloud-control
python3 -m venv .venv            # 처음 한 번
source .venv/bin/activate
pip install -r requirements.txt  # 처음 한 번, 또는 requirements.txt가 바뀌었을 때
```

### 2. MariaDB 시작
```bash
sudo systemctl start mariadb     # systemd가 없는 WSL이면: sudo service mariadb start
systemctl is-active mariadb      # active 확인
```

DB와 계정은 처음 한 번 만듭니다. `<DB 암호>`는 직접 정하고 `.env`에만 적습니다.
```bash
sudo mariadb <<'SQL'
CREATE DATABASE IF NOT EXISTS clouddb CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER IF NOT EXISTS 'cloudapp'@'127.0.0.1' IDENTIFIED BY '<DB 암호>';
GRANT ALL PRIVILEGES ON clouddb.* TO 'cloudapp'@'127.0.0.1';
SQL
```

### 3. AWS 로그인
```bash
aws login --profile lab
aws sts get-caller-identity --profile lab   # Account가 .env의 EXPECTED_ACCOUNT_ID와 같은지 확인
```
세션이 만료되면 웹 상단에 `AWS_LOGIN_REQUIRED` 안내가 뜹니다. 이 명령을 다시 실행한 뒤 웹에서 **전체 조회**를 누르면 됩니다. 서버를 재시작할 필요는 없습니다.

### 4. `.env` 작성 (처음 한 번)
```bash
cp .env.example .env
```
편집기로 `.env`를 열어 값을 채웁니다. 변수 설명은 아래 [환경변수](#환경변수)를 보세요.

### 5. 테이블 생성 (처음 한 번)
```bash
python -m backend.init_db
```
없는 테이블만 만들고 기존 데이터는 건드리지 않습니다. 다시 실행해도 안전합니다. 설정이 틀리면 비밀값을 뺀 원인만 출력하고 실패로 종료합니다.

### 6. 서버 실행
```bash
uvicorn backend.main:app --host 127.0.0.1 --port 8001
```
필수 설정이 빠졌으면 서버가 시작되지 않고 **빠진 변수 이름만** 출력합니다.

### 7. Windows 브라우저에서 접속
Windows의 Chrome이나 Edge에서 **http://127.0.0.1:8001/** 을 엽니다. WSL2의 localhost 전달 기능으로 접속됩니다. 앱 토큰을 입력하고 **연결**을 누릅니다.

## 환경변수

`.env`는 프로젝트 루트에 두고 Git에 올리지 않습니다(`.gitignore` 등록). 서버와 `init_db`가 **실행될 때만** 읽습니다. 값을 바꾸면 서버를 재시작해야 반영됩니다. 예시는 `.env.example`에 있으며 실제 비밀값은 넣지 않습니다.

| 변수 | 필수 | 용도 |
|---|---|---|
| `AWS_PROFILE` | O | `aws login`에 쓴 AWS CLI 프로필 이름 |
| `AWS_REGION` | O | AWS 리전. 이 실습에서는 `ap-northeast-2`만 허용 |
| `EXPECTED_ACCOUNT_ID` | O | 기대하는 AWS 계정 ID(12자리). 로그인 계정과 다르면 모든 AWS 기능이 403으로 차단됨 |
| `ADMIN_API_TOKEN` | O | 웹·API 접속용 앱 토큰 |
| `ALLOWED_INSTANCE_IDS` | | 시작·중지를 허용할 EC2 인스턴스 ID(쉼표로 구분). 비우면 조회·점검만 가능 |
| `DB_HOST`, `DB_PORT` | O | MariaDB 주소와 포트 |
| `DB_USER`, `DB_PASSWORD` | O | MariaDB 계정과 암호 |
| `DB_NAME` | O | 데이터베이스 이름 |

### 앱 토큰 (`ADMIN_API_TOKEN`)
- 로그인할 때 자동으로 발급되는 JWT가 **아닙니다**. 실습자가 직접 만들어 `.env`에 적어 두는 **고정 문자열**입니다. 만료 시간도 사용자 구분도 없습니다.
- 생성 예: `python -c "import secrets; print(secrets.token_urlsafe(32))"`
- 바꾸려면 `.env`를 수정하고 서버를 재시작합니다. 열려 있던 브라우저는 다음 요청에서 401을 받고 토큰 입력 화면으로 돌아갑니다.
- 브라우저는 토큰을 탭 메모리에만 둡니다(URL·localStorage에 저장하지 않음). 페이지를 새로고침하면 다시 입력해야 합니다.
- 토큰을 아는 사람은 허용 목록의 EC2를 시작·중지할 수 있습니다. 대화·캡처·Git에 남기지 마세요.

### 제어 허용 목록 (`ALLOWED_INSTANCE_IDS`)
- 시작·중지할 수 있는 EC2를 서버가 정합니다. 웹 화면이나 API 요청으로는 바꿀 수 없습니다.
- 편집 방법: `.env`의 `ALLOWED_INSTANCE_IDS`를 고친 뒤 **서버를 재시작**합니다.
  ```
  ALLOWED_INSTANCE_IDS=i-0123456789abcdef0,i-0fedcba9876543210
  ```
- **목록에서 빼는 것은 EC2 삭제가 아닙니다.** 인스턴스는 AWS에 그대로 남아 계속 실행되고 과금됩니다. 이 앱에서 더는 시작·중지할 수 없게 될 뿐이며, 조회와 보안 점검에는 계속 나옵니다.
- 허용 목록은 제어만 제한합니다. 보안 점검 대상을 거르는 데는 쓰지 않습니다.

## 화면

| 탭 | 내용 |
|---|---|
| 대시보드 | 계정 ID, 호출자 ARN, 리전, 요약 숫자(EC2 전체·실행 중·중지됨, S3, 보안 그룹, 결과 미확정 제어). 조회에 실패한 항목은 0이 아니라 '확인 불가' |
| EC2 | 인스턴스 목록, 상태 필터(이미 조회한 목록만 거름), 시작·중지 버튼 |
| S3 | 버킷 목록 |
| 보안 그룹 | 서울 리전 보안 그룹과 인바운드 규칙 |
| 실행 이력 | 최근 50건 제어 이력과 EC2 현재 상태를 나란히 표시 |
| 보안 점검 | '점검 실행'을 누를 때마다 AWS에서 새로 조회해 점검 |

자동 갱신은 하지 않습니다. 최신 상태는 각 탭의 **새로고침**이나 상단의 **전체 조회**로 확인합니다.

## EC2 시작·중지

1. 허용 목록에 있는 인스턴스가 `stopped`이면 **시작**, `running`이면 **중지** 버튼만 활성화됩니다. `pending`·`stopping` 같은 전환 상태에서는 둘 다 비활성입니다.
2. 버튼을 누르면 계정·리전·이름·ID·작업을 보여 주는 확인창이 뜹니다. **취소**하면 요청을 보내지 않습니다.
3. **승인**하면 브라우저가 요청 키(UUID)를 하나 만들어 서버로 보냅니다. 서버는 다음 순서로 처리합니다.
   1. 허용 목록 확인
   2. 같은 요청 키가 이미 있는지 확인
   3. 같은 인스턴스에 결과 미확정 이력이 있는지 확인
   4. 계정 확인과 최신 상태 확인
   5. DB에 `REQUESTED`를 기록
   6. AWS를 **1회만** 호출하고 결과를 기록

### 이력 상태의 뜻

| 상태 | 뜻 |
|---|---|
| `REQUESTED` | DB에 기록했지만 AWS 결과를 아직 기록하지 못함 |
| `ACCEPTED` | **AWS가 요청을 접수함. 완료가 아님** |
| `FAILED` | AWS가 거절했거나 요청이 전송되지 않음 |
| `UNKNOWN` | 요청을 보냈지만 결과를 알 수 없음(응답 시간 초과 등) |

**`ACCEPTED`는 "시작됨"이나 "중지됨"이 아니라 요청이 접수됐다는 뜻입니다.** 인스턴스는 그 뒤 `pending → running`, `stopping → stopped`로 바뀌는 데 수십 초가 걸리고, 그 과정에서 실패할 수도 있습니다. 화면에 "AWS 요청 접수 — 현재 상태는 새로고침으로 확인"이 나오면 잠시 뒤 **새로고침**해서 실제 상태를 확인하세요.

### `UNKNOWN`이 나왔을 때
AWS가 요청을 처리했는지 알 수 없는 상태입니다. 앱은 자동으로 재시도하지 않고, 이 인스턴스에 대한 새 제어 요청을 거절합니다(`409 ACTION_IN_PROGRESS`). 이렇게 대응하세요.

1. AWS 콘솔이나 CLI로 실제 상태를 확인합니다.
   ```bash
   aws ec2 describe-instances --profile lab --region ap-northeast-2 --instance-ids <인스턴스 ID> \
     --query 'Reservations[].Instances[].State.Name' --output text
   ```
2. 웹의 **실행 이력** 탭에서 해당 이력의 요청 키를 확인합니다. 아래 명령으로 action_id(이력 ID)를 찾습니다.
   ```bash
   mariadb -h 127.0.0.1 -u cloudapp -p clouddb \
     -e "SELECT id, request_id, instance_id, action, status, created_at FROM action_logs WHERE status IN ('REQUESTED','UNKNOWN');"
   ```
3. 실제 상태를 확인한 뒤 이력을 정리합니다. 정리해야 새 제어가 가능합니다.
   ```bash
   mariadb -h 127.0.0.1 -u cloudapp -p clouddb \
     -e "UPDATE action_logs SET status='FAILED', message='콘솔 확인 후 수동 정리' WHERE id='<action_id>' AND status IN ('REQUESTED','UNKNOWN');"
   ```

브라우저가 서버 응답을 받지 못했을 때도 비슷하게 처리합니다. 화면은 같은 요청 키를 보관하고 **같은 요청 키로 다시 보내기** 버튼을 보여 줍니다. 같은 키로 다시 보내면 서버는 AWS를 다시 호출하지 않고 기록된 결과만 돌려줍니다. 새 키로 자동 재요청하지 않습니다.

`REQUESTED`가 오래 남아 있으면 AWS 접수 뒤 DB 기록이 실패했을 수 있습니다. 이때도 위와 같이 실제 상태를 확인한 뒤 정리합니다.

## 보안 그룹 점검

- **대상**: `pending`·`running`·`stopping`·`stopped` 상태 EC2의 **모든 ENI에 실제로 연결된** 보안 그룹만 점검합니다. 어느 EC2에도 연결되지 않은 그룹은 점검하지 않습니다. 이런 그룹은 '보안 그룹' 탭에서 볼 수 있습니다.
- **조건**: 인바운드 규칙이 0.0.0.0/0 또는 ::/0에서 TCP 22(SSH)나 3389(RDP)를 허용하는지 봅니다. 포트 범위가 22나 3389를 포함하는 경우와 '전체 프로토콜' 규칙도 해당합니다. Prefix List나 다른 보안 그룹을 소스로 쓰는 규칙은 공개로 보지 않습니다.
- **건수**: (인바운드 규칙, 공개 CIDR) 조합 하나가 1건입니다. 한 규칙이 22와 3389를 모두 열면 관리 포트 `22, 3389`인 1건으로 셉니다. 여러 인스턴스가 같은 그룹을 쓰면 표에서는 연결 대상별로 펼쳐 보여 주지만 건수는 늘지 않습니다.
- 규칙 기반 점검이며 **실제 침해 여부를 판정하지 않습니다.**

### 결과 구분

| 화면 표시 | 뜻 |
|---|---|
| **공개 관리 포트 규칙: 검토 필요 (N건)** | 연결된 그룹을 모두 점검했고 조건에 맞는 규칙이 N건 있음 |
| **이번 점검 조건에 해당하는 규칙 없음** (발견 없음) | 연결된 그룹을 모두 점검했고 조건에 맞는 규칙이 없음 |
| **점검 대상 EC2 연결 보안 그룹 없음** (대상 없음) | 점검할 EC2가 없어 그룹 조회를 하지 않음 |
| **오류 코드 + "점검을 완료하지 못했습니다"** (실패) | 권한 부족(`AWS_ACCESS_DENIED`), 로그인 만료(`AWS_LOGIN_REQUIRED`), 통신 실패(`AWS_CONNECTION_ERROR`), 조회 중 그룹 변경·정보 누락(`AWS_SCAN_INCOMPLETE`). **발견 없음이 아니라 확인하지 못한 상태**이며 이전 결과도 보여 주지 않음 |

점검 결과는 저장하지 않습니다. '점검 실행'을 누를 때마다 AWS에서 새로 조회합니다.

## API

모든 `/api` 경로는 `Authorization: Bearer <앱 토큰>` 헤더가 필요합니다. 정적 파일(`frontend/`)은 토큰 없이 열립니다.

| 메서드 | 경로 | 응답 |
|---|---|---|
| GET | `/api/context` | `account_id`, `caller_arn`, `region` |
| GET | `/api/ec2` | `items`(instance_id, name, state, instance_type, private_ip, public_ip, controllable), `count` |
| GET | `/api/s3` | `items[{name}]`, `count` |
| GET | `/api/security-groups` | `items`(GroupId, GroupName, IpPermissions), `count` |
| GET | `/api/actions` | 최근 50건 제어 이력 `items`, `count` |
| POST | `/api/ec2/{instance_id}/actions` | 본문 `{"action": "start" 또는 "stop", "request_id": "<UUID>"}`. 처음 접수하면 202, 같은 요청을 다시 보내면 200 |
| GET | `/api/security-checks` | `findings`, `checked_groups`, `checked_instance_count`, `checked_group_count`, `finding_count`, `checked_at` |

오류 본문은 항상 `{"detail": {"code": "...", "message": "..."}}` 형식입니다.

| 상태 | code | 뜻 |
|---|---|---|
| 401 | `APP_AUTH_REQUIRED` | 앱 토큰이 없거나 틀림. 웹은 토큰 입력 화면으로 돌아감 |
| 403 | `AWS_ACCOUNT_MISMATCH` | 로그인한 AWS 계정이 `EXPECTED_ACCOUNT_ID`와 다름 |
| 403 | `AWS_ACCESS_DENIED` | AWS 권한 부족 |
| 403 | `INSTANCE_NOT_ALLOWED`, `CONTROL_DISABLED` | 허용 목록 밖, 또는 허용 목록이 비어 있음. AWS를 호출하기 전에 차단 |
| 409 | `INVALID_STATE` | 현재 상태에서 할 수 없는 작업 |
| 409 | `ACTION_IN_PROGRESS` | 같은 인스턴스에 `REQUESTED`·`UNKNOWN` 이력이 있음 |
| 409 | `IDEMPOTENCY_CONFLICT` | 같은 요청 키를 다른 대상이나 작업에 사용 |
| 422 | `VALIDATION_ERROR` | 요청 본문 형식 오류(정의 밖 필드, 잘못된 UUID 등) |
| 503 | `AWS_LOGIN_REQUIRED` | AWS 로그인 만료. 앱 토큰 오류와 다르며 웹은 연결을 유지하고 안내를 표시 |
| 503 | `AWS_CONNECTION_ERROR` | AWS 통신 실패 |
| 503 | `AWS_SCAN_INCOMPLETE` | 점검 중 정보 누락이나 대상 변경 |
| 503 | `DB_UNAVAILABLE` | MariaDB 연결 실패 |
| 504 | `AWS_RESULT_UNKNOWN` | 제어 요청 결과를 알 수 없음. 이력은 `UNKNOWN` |

## 구조

```
backend/
  main.py          FastAPI 앱, API 경로, 오류 형식, frontend/ 정적 파일 제공
  config.py        .env 읽기와 검증
  auth.py          앱 토큰 인증
  aws_service.py   Boto3 조회·제어, AWS 오류 분류, 보안 그룹 점검(inspect_group)
  ec2_control.py   시작·중지 처리(허용 목록, 요청 키, lock, 이력)
  db.py, models.py MariaDB 연결과 action_logs 테이블
  init_db.py       없는 테이블만 생성
  errors.py        공통 오류 형식
frontend/          index.html, app.js, style.css
tests/             unittest (AWS는 mock, DB는 메모리 SQLite로 대체)
```

## 테스트

```bash
python -m unittest -v tests.test_actions tests.test_security_scan
```
실제 AWS와 실습 DB를 호출하지 않습니다.

## 확인 상태

### 직접 확인한 것

| 기능 | 확인 방법 |
|---|---|
| 조회 결과(EC2 9개, S3 8개, 보안 그룹 16개)가 실제와 일치 | 실제 계정에서 앱의 조회 코드 결과와 AWS CLI 결과를 비교(읽기 전용) |
| 보안 점검(대상 EC2 9개, 연결 9건, 공개 SSH 규칙 7건)이 실제와 일치 | 실제 계정에서 앱의 점검 결과와 AWS CLI 원본으로 따로 계산한 결과를 비교 |
| `init_db` 테이블 생성, 재실행 시 기존 테이블 유지, 설정 오류 시 비밀값 없이 실패 | 실제 MariaDB에 실행 |
| 이력 조회 0건 응답, DB 중단 시 503 | 실제 MariaDB와 닫힌 포트로 확인 |
| 인증(401), 허용 목록, 요청 키 중복 방지, 상태 검사, UNKNOWN·FAILED 기록, 점검 집계 규칙 | 단위 테스트 40개 (AWS mock, 메모리 SQLite) |
| 화면 동작: 토큰 입력과 401, 요약의 '확인 불가', 상태 필터, 확인창 취소·승인, 접수 안내, 보안 점검 표시, AWS 만료 안내, 로그아웃 시 데이터 삭제 | Windows Chrome에서 실제 페이지를 조작. 백엔드는 mock AWS와 메모리 DB 사용 |
| Windows 브라우저에서 WSL 서버 접속 | Windows에서 WSL의 localhost 포트로 접속 확인 |

PLAN.md 기준으로 A~D단계는 실습자가 확인 완료로 표시했습니다.

### 아직 검증하지 않은 것

- 이 README 기준 코드로 **실제 AWS의 EC2 시작·중지**를 웹에서 실행한 결과(ACCEPTED 기록, 상태 전환)
- 실제 `.env`·MariaDB·AWS 세션으로 서버를 띄우고 Windows 브라우저로 6개 탭 전체를 확인한 통합 동작
- 실제 AWS 세션이 만료됐을 때의 `AWS_LOGIN_REQUIRED` 표시(가짜 만료로만 확인)
- 실제 네트워크 장애로 생긴 `UNKNOWN`과 수동 정리 절차(단위 테스트로만 확인)
- 보안 그룹이 100개를 넘을 때의 묶음 조회(단위 테스트로만 확인)
