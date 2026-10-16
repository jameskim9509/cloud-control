"use strict";

// API 응답 값은 모두 외부 문자열로 보고 textContent로만 화면에 넣는다.
// 앱 토큰은 아래 변수(메모리)에만 둔다. URL·localStorage·sessionStorage·console에 남기지 않는다.
let appToken = null;
// 로그아웃·재연결 뒤 늦게 도착한 응답이 화면을 다시 채우지 않도록 연결마다 번호를 올린다.
let session = 0;

// 조회 결과 저장소. 화면과 요약 숫자는 모두 여기서 계산한다.
// state: idle(아직 조회 안 함) | loading | ok | error
const results = {};
const RESULT_KEYS = ["context", "ec2", "s3", "sg", "actions"];
const RESULT_PATHS = {
  context: "/api/context",
  ec2: "/api/ec2",
  s3: "/api/s3",
  sg: "/api/security-groups",
  actions: "/api/actions",
};
// 성공하면 AWS 로그인이 유효하다고 볼 수 있는 조회 경로(/api/actions는 DB만 읽으므로 제외)
const AWS_READ_PATHS = new Set(["/api/context", "/api/ec2", "/api/s3", "/api/security-groups", "/api/security-checks"]);

const ACTION_LABEL = { start: "시작", stop: "중지" };
const PENDING_STATUSES = new Set(["REQUESTED", "UNKNOWN"]);

// EC2 화면 상태
let ec2Filter = "";
// 제어 요청이 접수됐지만 아직 새로고침하지 않은 인스턴스. 새로고침 전까지 버튼을 막는다.
const staleInstances = new Set();
// 제어 요청을 보내는 중이면 true. 버튼 중복 클릭을 막는다(서버 검사는 별도로 유지).
let actionBusy = false;
// 결과를 확정하지 못한 제어 요청. 같은 request_id로만 다시 보낸다. 새 키로 자동 재요청하지 않는다.
let lostRequest = null;
// 보안 점검 실행 번호. 늦게 도착한 이전 응답이 새 결과처럼 보이지 않게 한다.
let checkRun = 0;

class ApiError extends Error {
  constructor(code, message, status) {
    super(message);
    this.code = code;
    this.status = status;
  }
}

// 화면에 반영하지 않을 응답(로그아웃·재연결 뒤 도착했거나 401로 이미 로그아웃 처리됨)
class Ignored extends Error {}

const ERROR_HINTS = {
  AWS_LOGIN_REQUIRED: "터미널에서 aws login --profile lab을 실행한 뒤 다시 조회하세요. 앱 토큰은 정상입니다.",
  AWS_ACCESS_DENIED: "AWS 계정 권한을 확인하세요.",
  AWS_ACCOUNT_MISMATCH: "aws login 계정과 .env의 EXPECTED_ACCOUNT_ID가 같은지 확인하세요.",
  AWS_CONNECTION_ERROR: "네트워크 연결을 확인한 뒤 다시 조회하세요.",
  AWS_SCAN_INCOMPLETE: "조회 중 대상이 바뀌었거나 정보가 누락되었습니다. 다시 실행하세요.",
  DB_UNAVAILABLE: "MariaDB가 실행 중인지, .env의 DB 설정이 맞는지 확인하세요.",
  NETWORK_ERROR: "uvicorn 서버가 실행 중인지 확인하세요.",
  INVALID_STATE: "새로고침해서 현재 상태를 확인하세요.",
  ACTION_IN_PROGRESS: "실행 이력에서 REQUESTED·UNKNOWN 요청을 확인하세요.",
};

// ---- 공통 API 함수 ----
// 모든 요청이 이 함수를 거친다: 토큰 헤더 추가, 응답 해석, 401 로그아웃, AWS 로그인 안내.
async function api(path, { method = "GET", body } = {}) {
  const mySession = session;
  if (!appToken) throw new Ignored();

  const headers = { Accept: "application/json", Authorization: `Bearer ${appToken}` };
  if (body !== undefined) headers["Content-Type"] = "application/json";

  let response;
  try {
    response = await fetch(path, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch {
    if (mySession !== session) throw new Ignored();
    throw new ApiError("NETWORK_ERROR", "서버 응답을 받지 못했습니다.", 0);
  }

  let data = null;
  try {
    data = await response.json();
  } catch {
    // JSON이 아닌 응답은 아래에서 상태 코드로 처리한다.
  }
  if (mySession !== session) throw new Ignored();

  if (!response.ok) {
    const detail = data && data.detail;
    const err = detail && typeof detail === "object" && detail.code
      ? new ApiError(detail.code, detail.message || "요청이 실패했습니다.", response.status)
      : new ApiError(`HTTP_${response.status}`, "요청이 실패했습니다.", response.status);
    if (response.status === 401) {
      // 앱 토큰 문제. AWS 로그인 만료(503 AWS_LOGIN_REQUIRED)와 구분해 입력 화면으로 돌아간다.
      logout(err);
      throw new Ignored();
    }
    if (err.code === "AWS_LOGIN_REQUIRED") setAwsLoginBanner(true);
    throw err;
  }
  if (data === null) {
    throw new ApiError("INVALID_RESPONSE", "서버 응답을 해석하지 못했습니다.", response.status);
  }
  if (method === "GET" && AWS_READ_PATHS.has(path)) setAwsLoginBanner(false);
  return { status: response.status, data };
}

function setAwsLoginBanner(show) {
  document.getElementById("aws-login-banner").hidden = !show;
}

// ---- 공통 표시 ----

function el(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined && text !== null) node.textContent = String(text);
  if (className) node.className = className;
  return node;
}

function setInfo(statusEl, message, extraClass) {
  statusEl.className = extraClass ? `status ${extraClass}` : "status";
  statusEl.textContent = message;
}

function clearStatus(statusEl) {
  setInfo(statusEl, "");
}

function showError(statusEl, err) {
  statusEl.className = "status error";
  statusEl.replaceChildren(el("span", err.code || "ERROR", "code"), el("span", err.message));
  const hint = ERROR_HINTS[err.code];
  if (hint) statusEl.append(el("div", hint, "hint"));
}

function cell(value, className) {
  if (value === null || value === undefined || value === "") {
    return el("td", "-", "null");
  }
  return el("td", value, className);
}

function stateCell(state) {
  const td = cell(state);
  if (state) td.classList.add(`state-${state}`);
  return td;
}

function fillTable(table, rows) {
  table.tBodies[0].replaceChildren(...rows);
  table.hidden = rows.length === 0;
}

// 결과 상태(loading/error)를 안내 영역에 표시한다. ok면 false를 돌려 호출자가 내용을 그리게 한다.
// ok가 아니면 표 내용을 숨기는 데 그치지 않고 지운다(이전 데이터가 DOM에 남지 않게).
function renderResultStatus(result, statusEl, table) {
  if (result && result.state === "ok") {
    clearStatus(statusEl);
    return false;
  }
  fillTable(table, []);
  if (result && result.state === "loading") {
    setInfo(statusEl, "불러오는 중…");
  } else if (result && result.state === "error") {
    showError(statusEl, result.error);
  } else {
    clearStatus(statusEl);
  }
  return true;
}

function formatTime(iso) {
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? iso : date.toLocaleString("ko-KR");
}

// ---- 조회 ----

async function load(key) {
  results[key] = { state: "loading" };
  render(key);
  try {
    const { data } = await api(RESULT_PATHS[key]);
    results[key] = { state: "ok", data };
    if (key === "ec2") staleInstances.clear();
  } catch (err) {
    if (err instanceof Ignored) return;
    results[key] = { state: "error", error: err };
  }
  render(key);
}

function render(key) {
  if (key === "context") renderContext();
  if (key === "ec2") renderEc2();
  if (key === "s3") renderS3();
  if (key === "sg") renderSg();
  if (key === "actions") renderActions();
  renderDashboard();
}

function resetResults() {
  for (const key of RESULT_KEYS) {
    results[key] = { state: "idle" };
  }
}

// ---- 대시보드 ----

function renderContext() {
  const result = results.context;
  const status = document.getElementById("context-status");
  const info = document.getElementById("context-info");
  info.hidden = true;
  for (const id of ["ctx-account", "ctx-arn", "ctx-region"]) {
    document.getElementById(id).textContent = "";
  }
  if (result.state === "loading") {
    setInfo(status, "불러오는 중…");
  } else if (result.state === "error") {
    showError(status, result.error);
  } else if (result.state === "ok") {
    clearStatus(status);
    document.getElementById("ctx-account").textContent = result.data.account_id;
    document.getElementById("ctx-arn").textContent = result.data.caller_arn;
    document.getElementById("ctx-region").textContent = result.data.region;
    info.hidden = false;
  } else {
    clearStatus(status);
  }
}

// 요약 카드: 조회 성공 시 실제 값, 실패 시 '확인 불가'(0으로 표시하지 않음)
function setSummary(id, result, compute) {
  const value = document.getElementById(id);
  const sub = document.getElementById(`${id}-sub`);
  value.className = "card-value";
  sub.textContent = "";
  if (!result || result.state === "idle") {
    value.textContent = "-";
  } else if (result.state === "loading") {
    value.classList.add("loading");
    value.textContent = "불러오는 중…";
  } else if (result.state === "error") {
    value.classList.add("error");
    value.textContent = "확인 불가";
    sub.textContent = result.error.code || "ERROR";
  } else {
    value.textContent = String(compute(result.data));
  }
}

function renderDashboard() {
  const countState = (state) => (data) => data.items.filter((item) => item.state === state).length;
  setSummary("sum-ec2-total", results.ec2, (data) => data.items.length);
  setSummary("sum-ec2-running", results.ec2, countState("running"));
  setSummary("sum-ec2-stopped", results.ec2, countState("stopped"));
  setSummary("sum-s3", results.s3, (data) => data.items.length);
  setSummary("sum-sg", results.sg, (data) => data.items.length);
  setSummary("sum-pending", results.actions, (data) => data.items.filter((item) => PENDING_STATUSES.has(item.status)).length);
}

// ---- EC2 ----

function actionButton(item, action) {
  const required = action === "start" ? "stopped" : "running";
  const lostHere = lostRequest !== null && lostRequest.instanceId === item.instance_id;
  const button = el("button", ACTION_LABEL[action], action === "stop" ? "small danger" : "small");
  button.type = "button";
  // 허용 대상이고 정확히 필요한 상태일 때만 활성화. pending/stopping 등 전환 상태는 둘 다 비활성.
  button.disabled =
    actionBusy || lostHere || staleInstances.has(item.instance_id) || !item.controllable || item.state !== required;
  button.addEventListener("click", () => startAction(item, action));
  return button;
}

function controlCell(item) {
  const td = document.createElement("td");
  if (!item.controllable) {
    td.textContent = "허용 안 됨";
    td.className = "null";
    return td;
  }
  td.append(actionButton(item, "start"), " ", actionButton(item, "stop"));
  if (staleInstances.has(item.instance_id)) {
    td.append(el("div", "요청 접수됨 — 새로고침으로 상태 확인", "small-text"));
  }
  return td;
}

function renderEc2() {
  const result = results.ec2;
  const status = document.getElementById("ec2-status");
  const table = document.getElementById("ec2-table");
  const countText = document.getElementById("ec2-filter-count");
  countText.textContent = "";
  renderEc2Mini();
  if (renderResultStatus(result, status, table)) return;

  const all = result.data.items;
  // 필터는 이미 받은 목록에만 적용한다(서버 재조회 없음).
  const shown = ec2Filter ? all.filter((item) => item.state === ec2Filter) : all;
  countText.textContent = ec2Filter ? `표시 ${shown.length}개 / 전체 ${all.length}개` : `전체 ${all.length}개`;
  if (all.length === 0) {
    setInfo(status, "EC2 인스턴스가 없습니다.");
  } else if (shown.length === 0) {
    setInfo(status, `상태가 ${ec2Filter}인 인스턴스가 없습니다.`);
  }
  fillTable(
    table,
    shown.map((item) => {
      const tr = document.createElement("tr");
      tr.append(
        cell(item.name),
        cell(item.instance_id, "mono"),
        stateCell(item.state),
        cell(item.instance_type),
        cell(item.private_ip, "mono"),
        cell(item.public_ip, "mono"),
        controlCell(item),
      );
      return tr;
    }),
  );
}

// 실행 이력 탭 옆에 보여 주는 EC2 현재 상태(같은 조회 결과 사용)
function renderEc2Mini() {
  const result = results.ec2;
  const status = document.getElementById("ec2-mini-status");
  const table = document.getElementById("ec2-mini-table");
  if (renderResultStatus(result, status, table)) return;
  if (result.data.items.length === 0) setInfo(status, "EC2 인스턴스가 없습니다.");
  fillTable(
    table,
    result.data.items.map((item) => {
      const tr = document.createElement("tr");
      tr.append(
        cell(item.name),
        cell(item.instance_id, "mono"),
        stateCell(item.state),
        item.controllable ? el("td", "허용", "allowed") : el("td", "불가", "null"),
      );
      return tr;
    }),
  );
}

// ---- S3 / 보안 그룹 ----

function renderS3() {
  const result = results.s3;
  const status = document.getElementById("s3-status");
  const table = document.getElementById("s3-table");
  if (renderResultStatus(result, status, table)) return;
  if (result.data.items.length === 0) setInfo(status, "S3 버킷이 없습니다.");
  fillTable(
    table,
    result.data.items.map((item) => {
      const tr = document.createElement("tr");
      tr.append(cell(item.name, "mono"));
      return tr;
    }),
  );
}

function formatProtocol(protocol) {
  if (protocol === "-1") return "전체 프로토콜";
  return String(protocol).toUpperCase();
}

function formatPortRange(protocol, fromPort, toPort) {
  if (protocol === "-1" || fromPort === null || fromPort === undefined) return "전체";
  if (fromPort === toPort) return String(fromPort);
  return `${fromPort}-${toPort}`;
}

function formatSources(perm) {
  const sources = [
    ...(perm.IpRanges || []).map((r) => r.CidrIp),
    ...(perm.Ipv6Ranges || []).map((r) => r.CidrIpv6),
    ...(perm.PrefixListIds || []).map((p) => p.PrefixListId),
    ...(perm.UserIdGroupPairs || []).map((g) => g.GroupId),
  ];
  return sources.length ? sources.join(", ") : "-";
}

function renderSg() {
  const result = results.sg;
  const status = document.getElementById("sg-status");
  const table = document.getElementById("sg-table");
  if (renderResultStatus(result, status, table)) return;
  if (result.data.items.length === 0) setInfo(status, "보안 그룹이 없습니다.");
  const rows = [];
  for (const sg of result.data.items) {
    const perms = sg.IpPermissions || [];
    if (perms.length === 0) {
      const tr = document.createElement("tr");
      tr.append(cell(sg.GroupId, "mono"), cell(sg.GroupName), el("td", "인바운드 규칙 없음", "null"), cell(null), cell(null));
      rows.push(tr);
      continue;
    }
    for (const perm of perms) {
      const tr = document.createElement("tr");
      tr.append(
        cell(sg.GroupId, "mono"),
        cell(sg.GroupName),
        cell(formatProtocol(perm.IpProtocol)),
        cell(formatPortRange(perm.IpProtocol, perm.FromPort, perm.ToPort), "mono"),
        cell(formatSources(perm), "mono"),
      );
      rows.push(tr);
    }
  }
  fillTable(table, rows);
}

// ---- 실행 이력 ----

function renderActions() {
  const result = results.actions;
  const status = document.getElementById("actions-status");
  const table = document.getElementById("actions-table");
  if (renderResultStatus(result, status, table)) return;
  if (result.data.items.length === 0) setInfo(status, "제어 이력이 없습니다.");
  fillTable(
    table,
    result.data.items.map((item) => {
      const tr = document.createElement("tr");
      tr.append(
        cell(formatTime(item.created_at)),
        cell(item.instance_id, "mono"),
        cell(ACTION_LABEL[item.action] || item.action),
        cell(item.status, `action-${String(item.status).toLowerCase()}`),
        cell(item.message, "wrap"),
        cell(item.request_id, "mono small-text"),
      );
      return tr;
    }),
  );
}

// ---- EC2 제어 ----

// 확인창. 취소하면 false를 돌려주고 POST를 보내지 않는다.
function confirmAction(item, action, context) {
  const dialog = document.getElementById("confirm-dialog");
  document.getElementById("confirm-account").textContent = context.account_id;
  document.getElementById("confirm-region").textContent = context.region;
  document.getElementById("confirm-name").textContent = item.name;
  document.getElementById("confirm-id").textContent = item.instance_id;
  document.getElementById("confirm-action").textContent = `${ACTION_LABEL[action]} (${action})`;
  dialog.returnValue = "cancel";
  return new Promise((resolve) => {
    dialog.addEventListener("close", () => resolve(dialog.returnValue === "ok"), { once: true });
    dialog.showModal();
  });
}

async function startAction(item, action) {
  const actionStatus = document.getElementById("action-status");
  if (actionBusy) return;
  if (results.context.state !== "ok") {
    showError(actionStatus, new ApiError("CONTEXT_REQUIRED", "계정 정보를 조회하지 못해 확인창을 열 수 없습니다. 새로고침 후 다시 시도하세요."));
    return;
  }
  if (!window.crypto || typeof window.crypto.randomUUID !== "function") {
    showError(actionStatus, new ApiError("UNSUPPORTED_BROWSER", "이 브라우저는 요청 키 생성을 지원하지 않습니다. http://127.0.0.1:8001 로 접속하세요."));
    return;
  }
  actionBusy = true; // 확인창이 열린 동안에도 다른 버튼을 막는다
  renderEc2();
  let approved = false;
  try {
    approved = await confirmAction(item, action, results.context.data);
  } finally {
    if (!approved) {
      actionBusy = false;
      renderEc2();
    }
  }
  if (!approved) return;

  // 승인 1회당 요청 키 1개. 결과를 확정하지 못하면 이 키를 그대로 보관한다.
  await submitAction({ instanceId: item.instance_id, name: item.name, action, requestId: crypto.randomUUID() });
}

// 응답을 받지 못했거나(서버 처리 여부 불명) 서버 쪽 오류(5xx)면 같은 요청 키를 보관한다.
function shouldKeepRequestKey(err) {
  if (!(err instanceof ApiError)) return false;
  return err.code === "NETWORK_ERROR" || err.code === "INVALID_RESPONSE" || err.status >= 500;
}

async function submitAction(pending) {
  const actionStatus = document.getElementById("action-status");
  const label = `${pending.name} ${ACTION_LABEL[pending.action]}`;
  actionBusy = true;
  renderEc2();
  setInfo(actionStatus, `${label} 요청 중…`);
  try {
    const { status, data } = await api(`/api/ec2/${encodeURIComponent(pending.instanceId)}/actions`, {
      method: "POST",
      body: { action: pending.action, request_id: pending.requestId },
    });
    setLostRequest(null);
    staleInstances.add(pending.instanceId);
    const head = status === 202 ? "AWS 요청 접수" : `이미 처리된 요청 (이력 상태: ${data.status})`;
    setInfo(actionStatus, `${head} — 현재 상태는 새로고침으로 확인 (${label})`, "ok");
  } catch (err) {
    if (err instanceof Ignored) return;
    // 서버가 처리했을 수 있으면 같은 키를 보관하고 사용자가 직접 재전송하게 한다(자동 재요청 없음).
    setLostRequest(shouldKeepRequestKey(err) ? pending : null);
    showError(actionStatus, err);
  } finally {
    actionBusy = false;
  }
  renderEc2();
  // 이력(DB)만 다시 읽는다. EC2 상태는 사용자가 새로고침으로 확인한다.
  await load("actions");
}

function setLostRequest(pending) {
  lostRequest = pending;
  const box = document.getElementById("lost-request");
  if (!pending) {
    box.hidden = true;
    return;
  }
  document.getElementById("lost-request-text").textContent =
    `결과를 확정하지 못한 요청: ${pending.name} (${pending.instanceId}) ${ACTION_LABEL[pending.action]}, 요청 키 ${pending.requestId}. ` +
    "서버가 이미 처리했을 수 있습니다. 같은 키로 다시 보내면 중복 실행 없이 기록된 결과를 받습니다.";
  box.hidden = false;
}

// ---- 보안 점검 ----

function clearCheckResult() {
  document.getElementById("check-result").hidden = true;
  for (const id of ["check-table", "check-groups-table"]) {
    fillTable(document.getElementById(id), []);
  }
  for (const id of ["check-time", "check-instance-count", "check-group-count", "check-finding-count"]) {
    document.getElementById(id).textContent = "";
  }
  clearStatus(document.getElementById("check-summary"));
}

function connectionCells(conn) {
  return [cell(conn.instance_name), cell(conn.instance_id, "mono"), stateCell(conn.state), cell(conn.network_interface_id, "mono")];
}

function renderCheckResult(data) {
  document.getElementById("check-time").textContent = `${formatTime(data.checked_at)} (UTC ${data.checked_at})`;
  document.getElementById("check-instance-count").textContent = String(data.checked_instance_count);
  document.getElementById("check-group-count").textContent = String(data.checked_group_count);
  document.getElementById("check-finding-count").textContent = String(data.finding_count);

  const summary = document.getElementById("check-summary");
  if (data.checked_group_count === 0) {
    setInfo(summary, "점검 대상 EC2 연결 보안 그룹 없음");
  } else if (data.findings.length === 0) {
    setInfo(summary, "이번 점검 조건에 해당하는 규칙 없음", "ok");
  } else {
    setInfo(summary, `공개 관리 포트 규칙: 검토 필요 (${data.finding_count}건)`, "error");
  }

  // 한 규칙을 여러 인스턴스가 쓰면 연결 대상마다 한 줄로 펼친다(발견 건수는 그대로).
  const rows = [];
  for (const finding of data.findings) {
    const connections = finding.connections.length ? finding.connections : [{}];
    for (const conn of connections) {
      const tr = document.createElement("tr");
      tr.append(
        ...connectionCells(conn),
        cell(finding.group_id, "mono"),
        cell(finding.group_name),
        cell(finding.ip_version),
        cell(finding.cidr, "mono"),
        cell(formatProtocol(finding.ip_protocol)),
        cell(formatPortRange(finding.ip_protocol, finding.from_port, finding.to_port), "mono"),
        cell(finding.management_ports.join(", "), "mono"),
      );
      rows.push(tr);
    }
  }
  fillTable(document.getElementById("check-table"), rows);

  const groupRows = [];
  for (const group of data.checked_groups) {
    for (const conn of group.connections) {
      const tr = document.createElement("tr");
      tr.append(cell(group.group_id, "mono"), cell(group.group_name), ...connectionCells(conn));
      groupRows.push(tr);
    }
  }
  fillTable(document.getElementById("check-groups-table"), groupRows);
  document.getElementById("check-result").hidden = false;
}

async function runSecurityCheck() {
  const button = document.getElementById("check-run-btn");
  const status = document.getElementById("check-status");
  const myRun = ++checkRun;
  button.disabled = true;
  // 이전 결과를 먼저 지운다. 실패하면 이전 결과 대신 오류만 보인다.
  clearCheckResult();
  setInfo(status, "AWS에서 EC2와 보안 그룹을 조회하는 중…");
  try {
    const { data } = await api("/api/security-checks");
    if (myRun !== checkRun) return;
    clearStatus(status);
    renderCheckResult(data);
  } catch (err) {
    if (err instanceof Ignored || myRun !== checkRun) return;
    // 권한 부족·로그인 만료·통신 실패·불완전 조회는 '발견 없음'이 아니라 점검 실패로 보여 준다.
    showError(status, err);
    status.append(el("div", "점검을 완료하지 못했습니다. 발견이 없는 것이 아니라 확인하지 못한 상태입니다.", "hint"));
  } finally {
    if (myRun === checkRun) button.disabled = false;
  }
}

function resetCheck() {
  checkRun += 1;
  clearCheckResult();
  setInfo(document.getElementById("check-status"), "점검 실행 버튼을 누르면 AWS에서 새로 조회합니다.");
  document.getElementById("check-run-btn").disabled = false;
}

// ---- 새로고침 버튼 ----

async function withButton(id, work) {
  const button = document.getElementById(id);
  button.disabled = true;
  try {
    await work();
  } finally {
    button.disabled = false;
  }
}

function refreshAll() {
  return withButton("refresh-btn", async () => {
    await Promise.all(RESULT_KEYS.map(load));
    if (appToken) document.getElementById("last-fetched").textContent = new Date().toLocaleString("ko-KR");
  });
}

// ---- 연결 / 로그아웃 ----

function showView(loggedIn) {
  document.getElementById("login-view").hidden = loggedIn;
  document.getElementById("app-view").hidden = !loggedIn;
  document.getElementById("app-actions").hidden = !loggedIn;
}

async function connect() {
  const input = document.getElementById("token-input");
  const button = document.getElementById("connect-btn");
  const status = document.getElementById("login-status");

  const token = input.value.trim();
  input.value = "";
  if (!token) {
    showError(status, new ApiError("APP_AUTH_REQUIRED", "토큰을 입력하세요."));
    return;
  }

  session += 1;
  appToken = token;
  button.disabled = true;
  setInfo(status, "확인 중…");
  try {
    // /api/context로 토큰을 검증한다. 401이면 api()가 로그아웃 처리하고 입력 화면에 오류를 표시한다.
    await api("/api/context");
  } catch (err) {
    if (err instanceof Ignored) {
      button.disabled = false;
      return;
    }
    if (err.code === "NETWORK_ERROR" || err.code === "INVALID_RESPONSE") {
      // 서버에 닿지 못해 토큰을 확인하지 못했다.
      appToken = null;
      session += 1;
      showError(status, err);
      button.disabled = false;
      return;
    }
    // 그 밖의 오류(AWS_LOGIN_REQUIRED 등)는 토큰 검증을 통과한 뒤 발생한 것이므로 연결은 유지한다.
  }
  button.disabled = false;
  clearStatus(status);
  showView(true);
  selectTab("dashboard");
  await refreshAll();
}

// err가 있으면 401 등으로 강제 로그아웃된 것이므로 입력 화면에 이유를 보여 준다.
function logout(err) {
  appToken = null;
  session += 1;
  resetResults();
  staleInstances.clear();
  actionBusy = false;
  setLostRequest(null);
  for (const key of RESULT_KEYS) render(key);
  resetCheck();
  clearStatus(document.getElementById("action-status"));
  document.getElementById("last-fetched").textContent = "-";
  setAwsLoginBanner(false);
  const dialog = document.getElementById("confirm-dialog");
  if (dialog.open) dialog.close("cancel");

  showView(false);
  const status = document.getElementById("login-status");
  if (err instanceof ApiError) {
    showError(status, err);
  } else {
    clearStatus(status);
  }
  document.getElementById("token-input").focus();
}

// ---- 탭 ----

function selectTab(name) {
  for (const tab of document.querySelectorAll(".tabs [data-tab]")) {
    const active = tab.dataset.tab === name;
    tab.setAttribute("aria-selected", String(active));
    document.getElementById(`tab-${tab.dataset.tab}`).hidden = !active;
  }
}

// ---- 이벤트 ----

document.querySelector(".tabs").addEventListener("click", (event) => {
  const tab = event.target.closest("[data-tab]");
  if (tab) selectTab(tab.dataset.tab);
});
document.getElementById("refresh-btn").addEventListener("click", refreshAll);
document.getElementById("ec2-refresh-btn").addEventListener("click", () =>
  withButton("ec2-refresh-btn", () => Promise.all([load("context"), load("ec2")])),
);
document.getElementById("actions-refresh-btn").addEventListener("click", () =>
  withButton("actions-refresh-btn", () => Promise.all([load("actions"), load("ec2")])),
);
document.getElementById("ec2-filter").addEventListener("change", (event) => {
  ec2Filter = event.target.value;
  renderEc2();
});
document.getElementById("check-run-btn").addEventListener("click", runSecurityCheck);
document.getElementById("connect-btn").addEventListener("click", connect);
document.getElementById("logout-btn").addEventListener("click", () => logout());
document.getElementById("token-input").addEventListener("keydown", (event) => {
  if (event.key === "Enter") connect();
});
document.getElementById("lost-resend-btn").addEventListener("click", () => {
  if (lostRequest && !actionBusy) submitAction(lostRequest);
});
document.getElementById("lost-discard-btn").addEventListener("click", () => {
  setLostRequest(null);
  renderEc2();
});

resetResults();
for (const key of RESULT_KEYS) render(key);
showView(false);
