"use strict";

// API 응답 값은 모두 외부 문자열로 보고 textContent로만 화면에 넣는다.
// 앱 토큰은 아래 변수(메모리)에만 둔다. URL·localStorage·sessionStorage·console에 남기지 않는다.
let appToken = null;
// 로그아웃 뒤 늦게 도착한 응답이 화면을 다시 채우지 않도록 연결마다 번호를 올린다.
let session = 0;
// 확인창에 보여 줄 계정·리전(/api/context 결과)
let currentContext = null;
// 마지막으로 받은 EC2 목록. 버튼 활성 상태를 다시 그릴 때 쓴다.
let ec2Items = [];
// 제어 요청을 보내는 중이면 true. 버튼 중복 클릭을 막는다(서버 검사는 별도로 유지).
let actionBusy = false;
// 응답을 받지 못한 제어 요청. 같은 request_id로만 다시 보낸다. 새 키로 자동 재요청하지 않는다.
let lostRequest = null;

const ACTION_LABEL = { start: "시작", stop: "중지" };

class ApiError extends Error {
  constructor(code, message, status) {
    super(message);
    this.code = code;
    this.status = status;
  }
}

class StaleResponse extends Error {}

// 응답을 받지 못했거나(서버 처리 여부 불명) 서버 쪽 일시 오류(5xx)인 경우.
// 이때는 같은 요청 키를 보관한다. 같은 키 재전송은 서버가 중복 실행하지 않는다.
function shouldKeepRequestKey(err) {
  if (!(err instanceof ApiError)) return false;
  return err.code === "NETWORK_ERROR" || err.code === "INVALID_RESPONSE" || err.status >= 500;
}

async function requestJson(path, { method = "GET", body } = {}) {
  const mySession = session;
  if (!appToken) {
    throw new ApiError("APP_AUTH_REQUIRED", "앱 토큰을 입력하세요.", 401);
  }

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
    if (mySession !== session) throw new StaleResponse();
    throw new ApiError("NETWORK_ERROR", "서버 응답을 받지 못했습니다. uvicorn이 실행 중인지 확인하세요.", 0);
  }

  let data = null;
  try {
    data = await response.json();
  } catch {
    // JSON이 아닌 응답은 아래에서 상태 코드로 처리한다.
  }
  if (mySession !== session) throw new StaleResponse();

  if (!response.ok) {
    const detail = data && data.detail;
    if (detail && typeof detail === "object" && detail.code) {
      throw new ApiError(detail.code, detail.message || "요청이 실패했습니다.", response.status);
    }
    throw new ApiError(`HTTP_${response.status}`, "요청이 실패했습니다.", response.status);
  }
  if (data === null) {
    throw new ApiError("INVALID_RESPONSE", "서버 응답을 해석하지 못했습니다.", response.status);
  }
  return { status: response.status, data };
}

async function fetchJson(path) {
  return (await requestJson(path)).data;
}

// ---- 공통 표시 ----

function el(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined && text !== null) node.textContent = String(text);
  if (className) node.className = className;
  return node;
}

function clearStatus(statusEl) {
  statusEl.className = "status";
  statusEl.textContent = "";
}

function setInfo(statusEl, message, extraClass) {
  statusEl.className = extraClass ? `status ${extraClass}` : "status";
  statusEl.textContent = message;
}

function setLoading(statusEl, tableEl) {
  setInfo(statusEl, "불러오는 중…");
  if (tableEl) tableEl.hidden = true;
}

function setEmpty(statusEl, tableEl, message) {
  setInfo(statusEl, message);
  if (tableEl) tableEl.hidden = true;
}

function setError(statusEl, tableEl, err) {
  statusEl.className = "status error";
  statusEl.replaceChildren(el("span", err.code || "ERROR", "code"), el("span", err.message));
  if (tableEl) tableEl.hidden = true;
}

function showTable(statusEl, tableEl, rows) {
  clearStatus(statusEl);
  tableEl.tBodies[0].replaceChildren(...rows);
  tableEl.hidden = false;
}

function cell(value, className) {
  if (value === null || value === undefined || value === "") {
    return el("td", "-", "null");
  }
  return el("td", value, className);
}

// 대시보드 카드: 로딩·오류·숫자를 구분해 표시한다. 실패를 0으로 표시하지 않는다.
function setCount(id, state, value) {
  const node = document.getElementById(id);
  node.className = "card-value";
  if (state === "loading") {
    node.classList.add("loading");
    node.textContent = "불러오는 중…";
  } else if (state === "error") {
    node.classList.add("error");
    node.textContent = `조회 실패 (${value.code || "ERROR"})`;
  } else if (state === "reset") {
    node.textContent = "-";
  } else {
    node.textContent = String(value);
  }
}

// 오류 공통 처리. 인증 실패면 입력 화면으로 돌려보낸다.
function handleLoadError(err, onError) {
  if (err instanceof StaleResponse) return;
  if (err instanceof ApiError && err.code === "APP_AUTH_REQUIRED") {
    logout(err);
    return;
  }
  onError(err);
}

// ---- 계정 ----

function renderContext(data) {
  currentContext = { account_id: data.account_id, region: data.region };
  document.getElementById("ctx-account").textContent = data.account_id;
  document.getElementById("ctx-arn").textContent = data.caller_arn;
  document.getElementById("ctx-region").textContent = data.region;
  clearStatus(document.getElementById("context-status"));
  document.getElementById("context-info").hidden = false;
}

async function loadContext() {
  const status = document.getElementById("context-status");
  document.getElementById("context-info").hidden = true;
  currentContext = null;
  setLoading(status);
  try {
    renderContext(await fetchJson("/api/context"));
  } catch (err) {
    handleLoadError(err, (e) => setError(status, null, e));
  }
}

// ---- EC2 현재 상태와 제어 버튼 ----

function actionButton(item, action) {
  const required = action === "start" ? "stopped" : "running";
  const lostHere = lostRequest !== null && lostRequest.instanceId === item.instance_id;
  const button = el("button", ACTION_LABEL[action], action === "stop" ? "danger small" : "small");
  button.type = "button";
  // 허용 대상이고 정확히 필요한 상태일 때만 활성화. pending/stopping 등 전환 상태는 둘 다 비활성.
  button.disabled = actionBusy || lostHere || !item.controllable || item.state !== required;
  button.addEventListener("click", () => startAction(item, action));
  return button;
}

function renderEc2Rows() {
  const status = document.getElementById("ec2-status");
  const table = document.getElementById("ec2-table");
  if (ec2Items.length === 0) {
    setEmpty(status, table, "EC2 인스턴스가 없습니다.");
    return;
  }
  const rows = ec2Items.map((item) => {
    const tr = document.createElement("tr");
    const state = cell(item.state);
    if (item.state) state.classList.add(`state-${item.state}`);
    const actions = document.createElement("td");
    if (item.controllable) {
      actions.append(actionButton(item, "start"), " ", actionButton(item, "stop"));
    } else {
      actions.textContent = "허용 안 됨";
      actions.className = "null";
    }
    tr.append(
      cell(item.name),
      cell(item.instance_id, "mono"),
      state,
      cell(item.instance_type),
      cell(item.private_ip, "mono"),
      cell(item.public_ip, "mono"),
      actions,
    );
    return tr;
  });
  showTable(status, table, rows);
}

async function loadEc2() {
  const status = document.getElementById("ec2-status");
  const table = document.getElementById("ec2-table");
  setLoading(status, table);
  setCount("count-ec2", "loading");
  ec2Items = [];
  try {
    const data = await fetchJson("/api/ec2");
    setCount("count-ec2", "ok", data.count);
    ec2Items = data.items;
    renderEc2Rows();
  } catch (err) {
    handleLoadError(err, (e) => {
      setCount("count-ec2", "error", e);
      setError(status, table, e);
    });
  }
}

// ---- 제어 이력 ----

function formatTime(iso) {
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? iso : date.toLocaleString("ko-KR");
}

async function loadActions() {
  const status = document.getElementById("actions-status");
  const table = document.getElementById("actions-table");
  setLoading(status, table);
  try {
    const data = await fetchJson("/api/actions");
    if (data.items.length === 0) {
      setEmpty(status, table, "제어 이력이 없습니다.");
      return;
    }
    const rows = data.items.map((item) => {
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
    });
    showTable(status, table, rows);
  } catch (err) {
    handleLoadError(err, (e) => setError(status, table, e));
  }
}

// ---- 제어 요청 ----

// 확인창. 취소하면 false를 돌려주고 POST를 보내지 않는다.
function confirmAction(item, action) {
  const dialog = document.getElementById("confirm-dialog");
  document.getElementById("confirm-account").textContent = currentContext.account_id;
  document.getElementById("confirm-region").textContent = currentContext.region;
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
  if (!currentContext) {
    setError(actionStatus, null, new ApiError("CONTEXT_REQUIRED", "계정 정보를 먼저 조회한 뒤 다시 시도하세요."));
    return;
  }
  if (!window.crypto || typeof window.crypto.randomUUID !== "function") {
    setError(actionStatus, null, new ApiError("UNSUPPORTED_BROWSER", "이 브라우저는 요청 키 생성을 지원하지 않습니다. http://127.0.0.1:8001 로 접속하세요."));
    return;
  }
  actionBusy = true; // 확인창이 열린 동안에도 다른 버튼을 막는다
  renderEc2Rows();
  let approved = false;
  try {
    approved = await confirmAction(item, action);
  } finally {
    if (!approved) {
      actionBusy = false;
      renderEc2Rows();
    }
  }
  if (!approved) return;

  // 승인 1회당 요청 키 1개. 응답이 유실되면 이 키를 그대로 보관한다.
  const pending = { instanceId: item.instance_id, name: item.name, action, requestId: crypto.randomUUID() };
  await submitAction(pending);
}

async function submitAction(pending) {
  const actionStatus = document.getElementById("action-status");
  const mySession = session;
  actionBusy = true;
  renderEc2Rows();
  setInfo(actionStatus, `${pending.name} ${ACTION_LABEL[pending.action]} 요청 중…`);
  try {
    const { status, data } = await requestJson(`/api/ec2/${encodeURIComponent(pending.instanceId)}/actions`, {
      method: "POST",
      body: { action: pending.action, request_id: pending.requestId },
    });
    setLostRequest(null);
    const prefix = status === 202 ? "접수됨" : "이미 처리된 요청";
    setInfo(actionStatus, `${prefix}: ${pending.name} ${ACTION_LABEL[pending.action]} → ${data.status}`, "ok");
  } catch (err) {
    if (err instanceof StaleResponse) return;
    if (err instanceof ApiError && err.code === "APP_AUTH_REQUIRED") {
      logout(err);
      return;
    }
    // 서버가 처리했을 수 있으면 같은 키를 보관하고 사용자가 직접 재전송하게 한다(자동 재요청 없음).
    setLostRequest(shouldKeepRequestKey(err) ? pending : null);
    setError(actionStatus, null, err);
  } finally {
    if (mySession === session) {
      actionBusy = false;
      renderEc2Rows();
      // 결과 반영을 위해 한 번만 다시 읽는다(반복 폴링 아님).
      await Promise.all([loadEc2(), loadActions()]);
    }
  }
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

// ---- 목록 묶음 ----

function loadLists() {
  return Promise.all([loadEc2(), loadS3(), loadSecurityGroups(), loadActions()]);
}

async function loadS3() {
  const status = document.getElementById("s3-status");
  const table = document.getElementById("s3-table");
  setLoading(status, table);
  setCount("count-s3", "loading");
  try {
    const data = await fetchJson("/api/s3");
    setCount("count-s3", "ok", data.count);
    if (data.items.length === 0) {
      setEmpty(status, table, "S3 버킷이 없습니다.");
      return;
    }
    const rows = data.items.map((item) => {
      const tr = document.createElement("tr");
      tr.append(cell(item.name, "mono"));
      return tr;
    });
    showTable(status, table, rows);
  } catch (err) {
    handleLoadError(err, (e) => {
      setCount("count-s3", "error", e);
      setError(status, table, e);
    });
  }
}

function formatProtocol(protocol) {
  if (protocol === "-1") return "전체";
  return String(protocol).toUpperCase();
}

function formatPorts(perm) {
  if (perm.IpProtocol === "-1" || perm.FromPort === undefined) return "전체";
  if (perm.FromPort === perm.ToPort) return String(perm.FromPort);
  return `${perm.FromPort}-${perm.ToPort}`;
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

async function loadSecurityGroups() {
  const status = document.getElementById("sg-status");
  const table = document.getElementById("sg-table");
  setLoading(status, table);
  setCount("count-sg", "loading");
  try {
    const data = await fetchJson("/api/security-groups");
    setCount("count-sg", "ok", data.count);
    if (data.items.length === 0) {
      setEmpty(status, table, "보안 그룹이 없습니다.");
      return;
    }
    const rows = [];
    for (const sg of data.items) {
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
          cell(formatPorts(perm), "mono"),
          cell(formatSources(perm), "mono"),
        );
        rows.push(tr);
      }
    }
    showTable(status, table, rows);
  } catch (err) {
    handleLoadError(err, (e) => {
      setCount("count-sg", "error", e);
      setError(status, table, e);
    });
  }
}

function markFetched() {
  if (appToken) {
    document.getElementById("last-fetched").textContent = new Date().toLocaleString("ko-KR");
  }
}

// ---- 새로고침 버튼 ----

async function withButton(id, work) {
  const button = document.getElementById(id);
  button.disabled = true;
  try {
    await work();
    markFetched();
  } finally {
    button.disabled = false;
  }
}

function refreshAll() {
  return withButton("refresh-btn", () => Promise.all([loadContext(), loadLists()]));
}

function refreshControl() {
  return withButton("control-refresh-btn", () => Promise.all([loadContext(), loadEc2(), loadActions()]));
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
    setError(status, null, new ApiError("APP_AUTH_REQUIRED", "토큰을 입력하세요."));
    return;
  }

  session += 1;
  appToken = token;
  button.disabled = true;
  setLoading(status);
  try {
    const context = await fetchJson("/api/context");
    clearStatus(status);
    showView(true);
    selectTab("dashboard");
    renderContext(context);
    await loadLists();
    markFetched();
  } catch (err) {
    if (err instanceof StaleResponse) return;
    // 검증 실패: 토큰을 버리고 입력 화면에 오류를 표시한다(AWS_LOGIN_REQUIRED 등도 여기서 보인다).
    appToken = null;
    session += 1;
    setError(status, null, err);
  } finally {
    button.disabled = false;
  }
}

function resetData() {
  currentContext = null;
  ec2Items = [];
  actionBusy = false;
  setLostRequest(null);
  for (const id of ["ctx-account", "ctx-arn", "ctx-region"]) {
    document.getElementById(id).textContent = "";
  }
  document.getElementById("last-fetched").textContent = "-";
  document.getElementById("context-info").hidden = true;
  for (const name of ["context", "ec2", "s3", "sg", "actions", "action"]) {
    clearStatus(document.getElementById(`${name}-status`));
  }
  for (const name of ["ec2", "s3", "sg", "actions"]) {
    const table = document.getElementById(`${name}-table`);
    table.tBodies[0].replaceChildren();
    table.hidden = true;
  }
  for (const name of ["ec2", "s3", "sg"]) {
    setCount(`count-${name}`, "reset");
  }
  const dialog = document.getElementById("confirm-dialog");
  if (dialog.open) dialog.close("cancel");
}

// err가 있으면 인증 만료 등으로 강제 로그아웃된 것이므로 입력 화면에 이유를 보여 준다.
function logout(err) {
  appToken = null;
  session += 1;
  resetData();
  showView(false);
  const status = document.getElementById("login-status");
  if (err instanceof ApiError) {
    setError(status, null, err);
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

document.querySelector(".tabs").addEventListener("click", (event) => {
  const tab = event.target.closest("[data-tab]");
  if (tab) selectTab(tab.dataset.tab);
});
document.getElementById("refresh-btn").addEventListener("click", refreshAll);
document.getElementById("control-refresh-btn").addEventListener("click", refreshControl);
document.getElementById("connect-btn").addEventListener("click", connect);
document.getElementById("logout-btn").addEventListener("click", () => logout());
document.getElementById("token-input").addEventListener("keydown", (event) => {
  if (event.key === "Enter") connect();
});
document.getElementById("lost-resend-btn").addEventListener("click", () => {
  if (lostRequest && !actionBusy) submitAction(lostRequest);
});
document.getElementById("lost-discard-btn").addEventListener("click", () => setLostRequest(null));

showView(false);
