"use strict";

// API 응답 값은 모두 외부 문자열로 보고 textContent로만 화면에 넣는다.

class ApiError extends Error {
  constructor(code, message) {
    super(message);
    this.code = code;
  }
}

async function fetchJson(path) {
  let response;
  try {
    response = await fetch(path, { headers: { Accept: "application/json" } });
  } catch {
    throw new ApiError("NETWORK_ERROR", "서버에 연결하지 못했습니다. uvicorn이 실행 중인지 확인하세요.");
  }

  let body = null;
  try {
    body = await response.json();
  } catch {
    // JSON이 아닌 응답은 아래에서 상태 코드로 처리한다.
  }

  if (!response.ok) {
    const detail = body && body.detail;
    if (detail && typeof detail === "object" && detail.code) {
      throw new ApiError(detail.code, detail.message || "요청이 실패했습니다.");
    }
    throw new ApiError(`HTTP_${response.status}`, "요청이 실패했습니다.");
  }
  if (body === null) {
    throw new ApiError("INVALID_RESPONSE", "서버 응답을 해석하지 못했습니다.");
  }
  return body;
}

// ---- 공통 표시 ----

function el(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined && text !== null) node.textContent = String(text);
  if (className) node.className = className;
  return node;
}

function setLoading(statusEl, tableEl) {
  statusEl.className = "status";
  statusEl.textContent = "불러오는 중…";
  if (tableEl) tableEl.hidden = true;
}

function setEmpty(statusEl, tableEl, message) {
  statusEl.className = "status";
  statusEl.textContent = message;
  if (tableEl) tableEl.hidden = true;
}

function setError(statusEl, tableEl, err) {
  statusEl.className = "status error";
  statusEl.replaceChildren(el("span", err.code || "ERROR", "code"), el("span", err.message));
  if (tableEl) tableEl.hidden = true;
}

function showTable(statusEl, tableEl, rows) {
  statusEl.className = "status";
  statusEl.textContent = "";
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
  } else {
    node.textContent = String(value);
  }
}

// ---- 리소스별 로더 ----

async function loadContext() {
  const status = document.getElementById("context-status");
  const info = document.getElementById("context-info");
  info.hidden = true;
  setLoading(status);
  try {
    const data = await fetchJson("/api/context");
    document.getElementById("ctx-account").textContent = data.account_id;
    document.getElementById("ctx-arn").textContent = data.caller_arn;
    document.getElementById("ctx-region").textContent = data.region;
    status.textContent = "";
    info.hidden = false;
  } catch (err) {
    setError(status, null, err);
  }
}

async function loadEc2() {
  const status = document.getElementById("ec2-status");
  const table = document.getElementById("ec2-table");
  setLoading(status, table);
  setCount("count-ec2", "loading");
  try {
    const data = await fetchJson("/api/ec2");
    setCount("count-ec2", "ok", data.count);
    if (data.items.length === 0) {
      setEmpty(status, table, "EC2 인스턴스가 없습니다.");
      return;
    }
    const rows = data.items.map((item) => {
      const tr = document.createElement("tr");
      const state = cell(item.state);
      if (item.state) state.classList.add(`state-${item.state}`);
      tr.append(
        cell(item.name),
        cell(item.instance_id, "mono"),
        state,
        cell(item.instance_type),
        cell(item.private_ip, "mono"),
        cell(item.public_ip, "mono"),
      );
      return tr;
    });
    showTable(status, table, rows);
  } catch (err) {
    setCount("count-ec2", "error", err);
    setError(status, table, err);
  }
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
    setCount("count-s3", "error", err);
    setError(status, table, err);
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
    setCount("count-sg", "error", err);
    setError(status, table, err);
  }
}

// ---- 조회 버튼 ----

async function refreshAll() {
  const button = document.getElementById("refresh-btn");
  button.disabled = true;
  try {
    await Promise.all([loadContext(), loadEc2(), loadS3(), loadSecurityGroups()]);
    document.getElementById("last-fetched").textContent = new Date().toLocaleString("ko-KR");
  } finally {
    button.disabled = false;
  }
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

refreshAll();
