"use strict";
const $ = id => document.getElementById(id);
const csrf = document.querySelector('meta[name="csrf-token"]').content;
const tabs = [...document.querySelectorAll('[role="tab"]')];
let jobId = null, state = null, uploading = false, starting = false, pollTimer = null;
let activeTab = "input", sampledAt = Date.now();
let completionShownFor = null;
let settings = {oc_configured: false};
let credentialsReady = false, credentialsDirty = false;
const credentialClientId = remembered("lawgo-credential-client") || crypto.randomUUID();
remember("lawgo-credential-client", credentialClientId);
let credentialRevision = Number(remembered("lawgo-credential-revision")) || 0;
const touchedCredentials = new Set();
const busy = () => starting || ["queued", "running"].includes(state?.status);
const number = n => Number(n || 0).toLocaleString("ko-KR");
function remembered(key) { try { return sessionStorage.getItem(key); } catch { return null; } }
function remember(key, value) { try { sessionStorage.setItem(key, value); } catch {} }
function notice(message = "") { $("notice").textContent = message; $("notice").hidden = !message; }
async function api(path, options = {}) {
  const headers = {...options.headers};
  if (options.method && options.method !== "GET") headers["X-CSRF-Token"] = csrf;
  if (options.body && !(options.body instanceof FormData)) { headers["Content-Type"] = "application/json"; options.body = JSON.stringify(options.body); }
  const response = await fetch(path, {...options, headers});
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "요청을 처리하지 못했습니다. 잠시 후 다시 시도해주세요.");
  return data;
}
function selectTab(name, focus = false) {
  activeTab = ["input", "progress", "download"].includes(name) ? name : "input";
  for (const tab of tabs) {
    const selected = tab.dataset.tab === activeTab;
    tab.setAttribute("aria-selected", String(selected)); tab.tabIndex = selected ? 0 : -1;
    $(tab.getAttribute("aria-controls")).hidden = !selected;
    if (selected && focus) tab.focus();
  }
  remember("lawgo-tab", activeTab);
  // Navigation only changes visibility. The worker and polling are independent.
}
tabs.forEach((tab, index) => {
  tab.addEventListener("click", () => selectTab(tab.dataset.tab));
  tab.addEventListener("keydown", event => {
    let next = index;
    if (event.key === "ArrowRight") next = (index + 1) % tabs.length;
    else if (event.key === "ArrowLeft") next = (index + tabs.length - 1) % tabs.length;
    else if (event.key === "Home") next = 0;
    else if (event.key === "End") next = tabs.length - 1;
    else return;
    event.preventDefault(); selectTab(tabs[next].dataset.tab, true);
  });
});
document.querySelectorAll("[data-go]").forEach(button => button.addEventListener("click", () => selectTab(button.dataset.go, true)));
function credentialValues() { return {api_key: $("api-key").value.trim(), law_oc: $("oc").value.trim()}; }
async function saveCredentials() {
  if (!credentialsReady || !credentialsDirty) return;
  const revision = credentialRevision = Math.max(credentialRevision + 1, Date.now() * 1000);
  remember("lawgo-credential-revision", String(revision));
  remember("lawgo-credential-pending", String(revision));
  const values = {...credentialValues(), client_id: credentialClientId, revision};
  credentialsDirty = false;
  $("credential-status").textContent = "API 키를 저장하고 있습니다…";
  try {
    await api("/api/credentials", {method: "POST", body: values, keepalive: true});
    if (revision === credentialRevision) {
      remember("lawgo-credential-pending", "");
      $("credential-status").textContent = "키가 저장되었습니다. 다음 실행 때 자동으로 불러옵니다.";
    }
  } catch (error) {
    if (revision === credentialRevision) { credentialsDirty = true; $("credential-status").textContent = error.message; }
  }
}
async function restoreCredentials() {
  try {
    const pending = remembered("lawgo-credential-pending");
    const query = pending ? `?client_id=${encodeURIComponent(credentialClientId)}&revision=${encodeURIComponent(pending)}` : "";
    const saved = await api(`/api/credentials${query}`, {headers: {"X-CSRF-Token": csrf}});
    if (!touchedCredentials.has("api-key")) $("api-key").value = saved.api_key || "";
    if (!touchedCredentials.has("oc")) $("oc").value = saved.law_oc || "";
    remember("lawgo-credential-pending", "");
  } catch (error) { $("credential-status").textContent = error.message; }
  finally { credentialsReady = true; if (credentialsDirty) saveCredentials(); refreshControls(); }
}
$("show-key").addEventListener("click", () => {
  const show = $("api-key").type === "password";
  $("api-key").type = show ? "text" : "password";
  $("show-key").textContent = show ? "숨기기" : "보기";
  $("show-key").setAttribute("aria-pressed", String(show));
  $("show-key").setAttribute("aria-label", show ? "API 키 숨기기" : "API 키 보기");
});
for (const id of ["api-key", "oc"]) {
  $(id).addEventListener("input", () => {
    touchedCredentials.add(id); credentialsDirty = true;
    saveCredentials();
    refreshControls();
  });
  $(id).addEventListener("change", () => { touchedCredentials.add(id); credentialsDirty = true; saveCredentials(); refreshControls(); });
}
document.addEventListener("visibilitychange", () => { if (document.visibilityState === "hidden") saveCredentials(); });
window.addEventListener("pagehide", saveCredentials);
function refreshControls() {
  const locked = busy() || uploading;
  $("file").disabled = locked; $("change-file").disabled = locked; $("sheet").disabled = locked || state?.status !== "ready";
  $("connection-fields").disabled = locked || !credentialsReady; $("dropzone").setAttribute("aria-disabled", String(locked));
  const canRun = jobId && state?.template_output && ["ready", "error", "paused"].includes(state.status);
  const credentials = credentialsReady && $("api-key").value.trim() && ($("oc").value.trim() || settings.oc_configured);
  $("start").disabled = locked || !canRun || !credentials;
  $("start").setAttribute("aria-busy", String(starting));
  $("start").textContent = starting ? "실행 요청 중…" : ["error", "paused"].includes(state?.status) ? "이어서 실행하기 →" : "실행하기 →";
  $("run-hint").textContent = uploading ? "파일을 확인하고 있습니다…" : busy() ? "실행 중입니다. 진행 상황 탭에서 확인할 수 있습니다." : state?.status === "done" ? "다른 파일을 올리면 새 작업을 시작할 수 있습니다." : canRun ? `${number(state.count || state.total)}건을 처리합니다. 탭 이동 중에도 계속 실행됩니다.` : "파일과 API 정보를 입력하면 실행할 수 있습니다.";
}
async function upload(file) {
  if (!file || busy() || uploading) return;
  if (!/\.xlsx$/i.test(file.name)) return notice(".xlsx 엑셀 파일을 선택해주세요.");
  if (file.size > 15 * 1024 * 1024) return notice("파일은 최대 15MB까지 올릴 수 있습니다.");
  uploading = true; refreshControls(); notice();
  try {
    const form = new FormData(); form.append("file", file);
    const data = await api("/api/upload", {method: "POST", body: form});
    clearTimeout(pollTimer); jobId = data.id; remember("lawgo-job", jobId);
    state = {...data, status: "ready", completed: 0, total: data.count, progress: 0, active_elapsed: 0};
    $("file-summary").hidden = false; $("file-name").textContent = data.filename;
    $("file-count").textContent = `${number(data.count)}건 · ${data.sheet}`;
    $("sheet").replaceChildren(...data.sheets.map(name => { const option = document.createElement("option"); option.value = name; option.textContent = name; option.selected = name === data.sheet; return option; }));
    $("sheet-field").hidden = data.sheets.length < 2; renderState();
    if (!data.template_output) notice("엑셀 파일을 다시 올려주세요. 자치단체와 법령명 열이 필요하며, 내용 열은 자동으로 추가됩니다.");
  } catch (error) { notice(error.message); }
  finally { uploading = false; $("file").value = ""; refreshControls(); }
}
$("file").addEventListener("change", event => upload(event.target.files[0]));
$("change-file").addEventListener("click", () => $("file").click());
for (const name of ["dragenter", "dragover"]) $("dropzone").addEventListener(name, event => { event.preventDefault(); if (!busy()) $("dropzone").classList.add("dragover"); });
for (const name of ["dragleave", "drop"]) $("dropzone").addEventListener(name, event => { event.preventDefault(); $("dropzone").classList.remove("dragover"); });
$("dropzone").addEventListener("drop", event => upload(event.dataTransfer.files[0]));
$("sheet").addEventListener("change", async () => {
  uploading = true; refreshControls();
  try { const data = await api(`/api/jobs/${jobId}/sheet`, {method: "POST", body: {sheet: $("sheet").value}}); Object.assign(state, data, {total: data.count}); $("file-count").textContent = `${number(data.count)}건 · ${data.sheet}`; notice(); renderState(); }
  catch (error) { notice(error.message); }
  finally { uploading = false; refreshControls(); }
});
$("run-form").addEventListener("submit", async event => {
  event.preventDefault(); if ($("start").disabled || !jobId) return;
  starting = true; refreshControls(); notice();
  const body = {oc: $("oc").value.trim(), api_key: $("api-key").value.trim(), limit: state.options?.limit || 0, suggest_missing: false};
  try {
    credentialsDirty = true; await saveCredentials();
    await api(`/api/jobs/${jobId}/start`, {method: "POST", body});
    state.status = "queued"; selectTab("progress"); renderState(); clearTimeout(pollTimer); await poll();
  } catch (error) { notice(error.message); }
  finally { body.api_key = ""; starting = false; refreshControls(); }
});
function duration(seconds) {
  const n = Math.max(0, Math.floor(Number(seconds) || 0));
  const hours = Math.floor(n / 3600), minutes = Math.floor(n % 3600 / 60), secs = n % 60;
  return `${hours ? `${String(hours).padStart(2, "0")}:` : ""}${String(minutes).padStart(2, "0")}:${String(secs).padStart(2, "0")}`;
}
function renderClock() {
  const extra = state?.status === "running" ? Math.max(0, (Date.now() - sampledAt) / 1000) : 0;
  $("elapsed").textContent = duration((state?.active_elapsed || 0) + extra);
  $("eta").textContent = state?.status === "done" ? "완료" : state?.eta_seconds != null && busy() ? `약 ${duration(Math.max(0, state.eta_seconds - extra))}` : busy() ? "계산 중" : "—";
}
function renderState() {
  const status = state?.status || "ready", running = ["running", "queued"].includes(status), done = status === "done";
  $("running-dot").hidden = !running; $("done-dot").hidden = !done;
  $("status-label").textContent = ({ready: "대기", queued: "실행 대기", running: "실행 중", paused: "일시 중단", error: "확인 필요", done: "완료"})[status];
  $("state-icon").className = `state-icon ${running ? "running" : done ? "done" : ""}`;
  $("progress-title").textContent = ({ready: jobId ? "실행할 준비가 되었습니다" : "아직 실행한 작업이 없습니다", queued: "작업 순서를 기다리고 있습니다", running: "법령정보를 채우고 있습니다", paused: "작업을 일시 중단했습니다", error: "작업을 이어가기 위한 확인이 필요합니다", done: "모든 항목의 처리가 끝났습니다"})[status];
  $("progress-message").textContent = state?.message || "입력·실행 탭에서 파일을 올리고 실행해주세요.";
  $("completed").textContent = number(state?.completed); $("total").textContent = number(state?.total);
  $("percent").textContent = `${state?.progress || 0}%`; $("progress-bar").value = state?.progress || 0;
  $("stop").hidden = !running; $("stop").disabled = !!state?.stop_requested; $("stop").textContent = state?.stop_requested ? "중단 요청됨" : "일시 중단";
  $("resume").hidden = !["paused", "error"].includes(status); $("go-input").hidden = running || ["paused", "error"].includes(status);
  $("progress-help").textContent = ["paused", "error"].includes(status) ? "완료한 행은 저장되어 있습니다. 저장된 키로 이후 행부터 이어갈 수 있습니다." : "탭을 이동해도 실행 중인 작업은 계속됩니다.";
  $("download-empty").hidden = done; $("download-ready").hidden = !done;
  $("download-title").textContent = running ? "처리 완료 후 다운로드할 수 있습니다" : "완료된 파일이 여기에 표시됩니다";
  $("partial-area").hidden = !state?.partial_available || !["paused", "error"].includes(status);
  if (jobId) {
    $("download").href = `/api/jobs/${jobId}/download/xlsx`; $("partial-download").href = `/api/jobs/${jobId}/download/partial`;
    $("result-name").textContent = (state.filename || "규제목록.xlsx").replace(/\.xlsx$/i, "") + "_완료.xlsx";
    $("result-count").textContent = `${number(state.completed)}건 처리 · 경과 시간 ${duration(state.active_elapsed)}`;
  }
  renderClock(); refreshControls();
  // Show the completed file once per job, including completion during a reload.
  // Afterwards the user can freely return to another tab without being redirected.
  if (done && jobId && completionShownFor !== jobId && remembered("lawgo-completion-shown") !== jobId) {
    completionShownFor = jobId;
    remember("lawgo-completion-shown", jobId);
    selectTab("download", true);
  }
}
async function poll() {
  if (!jobId) return;
  const requestedJob = jobId;
  try {
    const data = await api(`/api/jobs/${requestedJob}?compact=1`);
    if (jobId !== requestedJob) return;
    state = data; sampledAt = Date.now(); renderState();
    if (["queued", "running"].includes(data.status)) pollTimer = setTimeout(poll, 1200);
  } catch {
    if (requestedJob !== jobId) return;
    $("progress-message").textContent = "진행 상태 연결을 다시 확인하고 있습니다. 서버 작업은 계속됩니다.";
    pollTimer = setTimeout(poll, 3500);
  }
}
$("stop").addEventListener("click", async () => {
  $("stop").disabled = true;
  try { await api(`/api/jobs/${jobId}/stop`, {method: "POST"}); clearTimeout(pollTimer); await poll(); }
  catch (error) { notice(error.message); $("stop").disabled = false; }
});
$("resume").addEventListener("click", () => { selectTab("input"); $("api-key").focus(); });
async function init() {
  await restoreCredentials();
  try { settings = await api("/api/run-settings"); } catch (error) { notice(error.message); }
  $("oc").required = !settings.oc_configured;
  const saved = remembered("lawgo-job");
  if (saved) {
    try {
      state = await api(`/api/jobs/${saved}?compact=1`); jobId = saved; sampledAt = Date.now();
      $("file-summary").hidden = false; $("file-name").textContent = state.filename; $("file-count").textContent = `${number(state.count || state.total)}건`;
      selectTab(remembered("lawgo-tab") || "progress");
      if (busy()) pollTimer = setTimeout(poll, 500);
    } catch { remember("lawgo-job", ""); }
  }
  renderState();
}
setInterval(renderClock, 1000);
init();
