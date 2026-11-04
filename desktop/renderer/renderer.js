const api = window.claudino;
const $ = (id) => document.getElementById(id);

function initials(text) {
  const words = (text || "").replace(/@.*/, "").split(/[\s._-]+/).filter(Boolean);
  return (words.slice(0, 2).map((w) => w[0]).join("") || "··").toUpperCase();
}

// Navigation
document.querySelectorAll(".nav").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".nav").forEach((b) => b.classList.toggle("active", b === btn));
    document.querySelectorAll(".page").forEach((p) => { p.hidden = p.id !== `page-${btn.dataset.page}`; });
  });
});

// Account (General) and sidebar profile: Slack name and photo once linked, else the Claude login
let slack = {};
let account = null;

function showAvatar(img, fallback, url, name) {
  fallback.textContent = initials(name);
  img.hidden = !url;
  fallback.hidden = Boolean(url);
  if (url) {
    img.src = url;
    img.onerror = () => { img.hidden = true; fallback.hidden = false; };
  }
}

function renderProfile() {
  const name = slack.name || account?.email || "";
  $("profile-name").textContent = name || "Sin vincular";
  showAvatar($("avatar-img"), $("avatar"), slack.avatar, name);
  $("account-avatar").textContent = initials(name);
  $("profile-org").textContent = account?.orgName || "Sin login de Claude";
  $("account-name").textContent = account ? (slack.name || account.email) : "Sin login de Claude";
  $("account-email").textContent = account ? `${account.email} · ${account.orgName}` : "Corré claude auth login en la terminal";
}

async function loadAccount() {
  try {
    const s = await api.auth();
    account = s.loggedIn ? s : null;
    if (account && account.authMethod !== "claude.ai") {
      $("account-hint").textContent = `Ojo: Claude Code usa ${account.authMethod}, no tu login de claude.ai; puede consumir créditos de API.`;
    }
  } catch (e) {
    account = null;
    $("account-email").textContent = e.message;
  }
  $("logout").disabled = !account;
  renderProfile();
}

$("open-usage").addEventListener("click", () => api.open("https://claude.ai/settings/usage"));
$("logout").addEventListener("click", async () => {
  if (await api.logout()) loadAccount();
});

api.info().then((info) => {
  $("launch-at-login").checked = info.launchAtLogin;
  $("version").textContent = `Versión ${info.version}`;
});
$("launch-at-login").addEventListener("change", (e) => api.setLaunchAtLogin(e.target.checked));
$("open-repo").addEventListener("click", () => api.open("https://github.com/macch-ai/claudino"));

// Connection
const HUB = {
  "no-config": ["Sin vincular", "Vinculá esta Mac con Slack para recibir tus pedidos.", "warn"],
  connecting: ["Conectando…", "", "warn"],
  connected: ["Conectada", "Tus pedidos de Slack corren en esta Mac.", "ok"],
  denied: ["Vínculo revocado", "hermes ya no reconoce esta Mac (¿la vinculaste en otra?). Volvé a vincularla.", "bad"],
  offline: ["Sin conexión", "No llego a claudino.macch.ai. ¿Hay internet? Reintentando… Tus pedidos esperan en cola.", "bad"],
};

const PAIR_RETRY = {
  expired: ["El código venció", "Pedí uno nuevo y mandalo en Slack dentro de 10 minutos."],
  unreachable: ["No llego a claudino.macch.ai", "Revisá tu conexión a internet y volvé a intentar."],
  busy: ["hermes está ocupado", "Hay demasiados códigos pendientes; probá en un minuto."],
  idle: ["Vincular con Slack", "Pedí un código y escribíselo a @claudino en Slack."],
};

let pairing = null;

function renderPairing(s) {
  pairing = s.pairing;
  const linked = s.status !== "no-config" && s.status !== "denied";
  $("slack-linked").hidden = !linked;
  $("slack-pair").hidden = linked;
  if (linked) {
    const p = s.profile;
    $("slack-name").textContent = p.name || p.user;
    $("slack-handle").textContent = [p.handle && `@${p.handle}`, p.user].filter(Boolean).join(" · ");
    showAvatar($("slack-avatar"), $("slack-initials"), p.avatar, p.name || p.user);
    return;
  }
  const waiting = pairing && (pairing.state === "waiting" || pairing.state === "requesting");
  $("pair-waiting").hidden = !waiting;
  $("pair-retry").hidden = waiting;
  if (waiting) {
    const login = Boolean(pairing.loginUrl);
    $("pair-login").hidden = !login && pairing.state === "waiting";
    $("login-slack").disabled = !login;
    $("pair-alt-label").textContent = login ? "O escribí esto en Slack, en cualquier canal donde esté claudino:"
      : "Escribí esto en cualquier canal donde esté claudino:";
    $("pair-command").textContent = `@claudino vincular ${pairing.code || "···-···"}`;
    $("copy-pair").disabled = !pairing.code;
    renderExpiry();
  } else {
    const [title, detail] = PAIR_RETRY[pairing?.state || "idle"];
    $("pair-retry-title").textContent = title;
    $("pair-retry-detail").textContent = detail;
  }
}

function renderExpiry() {
  if (!pairing?.expiresAt) return void ($("pair-expiry").textContent = "Pidiendo código a hermes…");
  const s = Math.max(0, Math.round((pairing.expiresAt - Date.now()) / 1000));
  $("pair-expiry").textContent = `Vence en ${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}. La app se conecta sola cuando lo mandes.`;
}
setInterval(renderExpiry, 1000);

$("copy-pair").addEventListener("click", async () => {
  await navigator.clipboard.writeText(`vincular ${pairing.code}`);
  $("copy-pair").textContent = "Copiado";
  setTimeout(() => { $("copy-pair").textContent = "Copiar"; }, 1500);
});
$("new-code").addEventListener("click", () => api.pair());
$("login-slack").addEventListener("click", () => api.login());
$("unpair").addEventListener("click", () => api.unpair());
$("url-form").addEventListener("submit", (e) => {
  e.preventDefault();
  api.setHubUrl(e.target.elements.url.value.trim());
});

function timeAgo(ms) {
  const s = Math.round((Date.now() - ms) / 1000);
  if (s < 60) return `hace ${s}s`;
  if (s < 3600) return `hace ${Math.round(s / 60)} min`;
  return `hace ${Math.round(s / 3600)} h`;
}

function duration(t) {
  const s = Math.round(((t.endedAt || Date.now()) - t.startedAt) / 1000);
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${s % 60}s`;
}

const TASK_STATE = { running: ["En curso", "warn"], done: ["Respondida", "ok"], error: ["Respondió con error", "bad"], failed: ["Falló", "bad"] };

function renderTasks(tasks) {
  const list = $("tasks");
  list.replaceChildren();
  if (!tasks.length) {
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = "Todavía no corrió ningún pedido.";
    return list.append(empty);
  }
  for (const t of tasks) {
    const [label, cls] = TASK_STATE[t.state];
    const row = document.createElement("div");
    row.className = "task";
    const dot = document.createElement("span");
    dot.className = `status-dot ${cls}`;
    const body = document.createElement("div");
    body.className = "grow";
    const title = document.createElement("div");
    title.className = "name";
    title.textContent = `${t.label} · ${label} · ${duration(t)}`;
    const summary = document.createElement("div");
    summary.className = "summary";
    summary.textContent = t.summary || `Hilo ${t.key}`;
    body.append(title, summary);
    const when = document.createElement("span");
    when.className = "when";
    when.textContent = timeAgo(t.startedAt);
    row.append(dot, body, when);
    list.append(row);
  }
}

function renderHub(s) {
  const [title, detail, cls] = HUB[s.status];
  $("hub-title").textContent = title;
  $("hub-detail").textContent = detail;
  $("hub-dot").className = `status-dot ${cls}`;
  $("nav-dot").className = `dot ${cls}`;
  $("nav-running").hidden = !s.running;
  $("nav-running").textContent = s.running;
  renderTasks(s.tasks);
  renderPairing(s);
  const urlInput = $("url-form").elements.url;
  if (document.activeElement !== urlInput) urlInput.value = s.profile.url;
  slack = s.status === "no-config" ? {} : s.profile;
  renderProfile();
}

api.hubState().then(renderHub);
api.onHubState(renderHub);

// Test prompt
$("send").addEventListener("click", async () => {
  const out = $("out");
  $("send").disabled = true;
  out.hidden = false;
  out.textContent = "Pensando…";
  try {
    const r = await api.ask($("prompt").value);
    out.textContent = (r.isError ? "[error] " : "") + r.result;
  } catch (e) {
    out.textContent = e.message;
  } finally {
    $("send").disabled = false;
  }
});

// Automations
const DAYS = ["D", "L", "M", "M", "J", "V", "S"]; // 0 = domingo
const DAY_ORDER = [1, 2, 3, 4, 5, 6, 0];
const form = $("automation-form");
let selectedDays = new Set([1, 2, 3, 4, 5]);
let automationList = [];

function renderDays() {
  const box = $("days");
  box.replaceChildren();
  for (const d of DAY_ORDER) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = `day${selectedDays.has(d) ? " on" : ""}`;
    b.textContent = DAYS[d];
    b.addEventListener("click", () => {
      selectedDays.has(d) ? selectedDays.delete(d) : selectedDays.add(d);
      renderDays();
    });
    box.append(b);
  }
}

function syncKind() {
  const kind = form.elements.kind.value;
  $("daily-fields").hidden = kind !== "daily";
  $("interval-fields").hidden = kind !== "interval";
}
form.elements.kind.addEventListener("change", syncKind);

function scheduleText(s) {
  if (s.kind === "slack") return "Solo desde Slack";
  if (s.kind === "interval") return `Cada ${s.minutes} min`;
  const days = s.days.length === 7 ? "todos los días"
    : s.days.length === 5 && [1, 2, 3, 4, 5].every((d) => s.days.includes(d)) ? "lunes a viernes"
    : DAY_ORDER.filter((d) => s.days.includes(d)).map((d) => DAYS[d]).join(" ");
  return `${s.time}, ${days}`;
}

function when(ms) {
  if (!ms) return "";
  const d = new Date(ms);
  const time = d.toLocaleTimeString("es-AR", { hour: "2-digit", minute: "2-digit" });
  if (d.toDateString() === new Date().toDateString()) return `hoy ${time}`;
  return `${d.toLocaleDateString("es-AR", { weekday: "short", day: "numeric" })} ${time}`;
}

function openForm(a) {
  form.hidden = false;
  form.reset();
  form.elements.id.value = a?.id || "";
  form.elements.name.value = a?.name || "";
  form.elements.prompt.value = a?.prompt || "";
  form.elements.channel.value = a?.channel || "";
  form.elements.enabled.checked = a ? a.enabled : true;
  const s = a?.schedule || { kind: "daily", time: "09:00", days: [1, 2, 3, 4, 5] };
  form.elements.kind.value = s.kind;
  if (s.kind === "daily") {
    form.elements.time.value = s.time;
    selectedDays = new Set(s.days);
  }
  if (s.kind === "interval") form.elements.minutes.value = s.minutes;
  $("delete-automation").hidden = !a;
  syncKind();
  renderDays();
  form.elements.name.focus();
}

function renderAutomations(list) {
  automationList = list;
  const box = $("automations");
  box.replaceChildren();
  if (!list.length) {
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = "Todavía no hay automatizaciones.";
    return box.append(empty);
  }
  for (const a of list) {
    const row = document.createElement("div");
    row.className = "task automation";
    const dot = document.createElement("span");
    dot.className = `status-dot ${a.enabled ? "ok" : ""}`;
    const body = document.createElement("div");
    body.className = "grow";
    const title = document.createElement("div");
    title.className = "name";
    title.textContent = a.name;
    const summary = document.createElement("div");
    summary.className = "summary";
    const parts = [scheduleText(a.schedule)];
    if (!a.enabled) parts.push("desactivada");
    else if (a.nextRunAt) parts.push(`próxima: ${when(a.nextRunAt)}`);
    if (a.lastRunAt) parts.push(`última: ${when(a.lastRunAt)}`);
    summary.textContent = parts.join(" · ");
    body.append(title, summary);
    const actions = document.createElement("div");
    actions.className = "actions";
    if (a.channel) {
      const tag = document.createElement("span");
      tag.className = "tag";
      tag.textContent = "Slack";
      actions.append(tag);
    }
    row.append(dot, body, actions);
    row.addEventListener("click", () => openForm(a));
    box.append(row);
  }
}

$("new-automation").addEventListener("click", () => openForm(null));
$("cancel-automation").addEventListener("click", () => { form.hidden = true; });
$("delete-automation").addEventListener("click", async () => {
  const id = form.elements.id.value;
  const a = automationList.find((x) => x.id === id);
  if (!a || !confirm(`¿Borrar la automatización "${a.name}"?`)) return;
  renderAutomations(await api.deleteAutomation(id));
  form.hidden = true;
});

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = form.elements;
  const kind = f.kind.value;
  const schedule = kind === "slack" ? { kind }
    : kind === "interval" ? { kind, minutes: Math.max(1, Number(f.minutes.value) || 60) }
    : { kind, time: f.time.value || "09:00", days: [...selectedDays].sort() };
  if (kind === "daily" && !schedule.days.length) return alert("Elegí al menos un día.");
  const name = f.name.value.trim();
  if (automationList.some((x) => x.id !== f.id.value && x.name.trim().toLowerCase() === name.toLowerCase())) {
    return alert("Ya hay una automatización con ese nombre: Slack las busca por nombre.");
  }
  renderAutomations(await api.saveAutomation({
    id: f.id.value || undefined,
    name,
    prompt: f.prompt.value.trim(),
    schedule,
    channel: f.channel.value.trim() || undefined,
    enabled: f.enabled.checked,
  }));
  form.hidden = true;
});

api.automations().then(renderAutomations);
api.onAutomations(renderAutomations);
setInterval(() => api.automations().then(renderAutomations), 60_000);

loadAccount();
setInterval(() => api.hubState().then(renderHub), 30_000); // refresh durations and "hace …"
