"use strict";

const $ = selector => document.querySelector(selector);
const state = { user: null, stockLocations: null, lastCtoReference: null };
const activeRequests = new Set();
let transferData = null;
let ponObservation = null;
let scoreData = null;
let orderDraftTimer = null;
let modemPurchaseOptions = null;
let modemPhotoHash = "";
let tesseractLoader = null;
let returnTechnicians = null;
let materialMonitorTimer = null;
let materialMonitorRefreshing = false;
let stockDashboardTimer = null;
let stockDashboardRefreshing = false;

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, char => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[char]));
}

function number(value) {
  return new Intl.NumberFormat("pt-BR", { maximumFractionDigits: 2 }).format(Number(value) || 0);
}

function materialThumbnail(description, category = "") {
  const name = `${description || ""} ${category || ""}`.normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase();
  const assets = [
    [["miguel"], "miguelao", "Miguelão"],
    [["isolante"], "fita-isolante", "Fita isolante"],
    [["conector azul", "conector upc", "sc/upc", "conector sc upc"], "conector-azul", "Conector azul"],
    [["drop"], "drop", "Bobina de cabo drop"],
    [["conector", "adaptador sc", "sc/apc"], "conector", "Conector óptico"],
    [["cunha"], "cunha", "Cunha de ancoragem"],
    [["presilha", "grampo", "clip"], "presilha", "Presilha de cabo"],
    [["modem", "onu", "ont", "wifi", "wi-fi", "roteador", "repetidor", "repetidores", "mesh"], "modem", "Modem ou repetidor"],
  ];
  const found = name.includes("conector") && (name.includes("azul") || name.includes("upc"))
    ? assets.find(([, file]) => file === "conector-azul")
    : assets.find(([terms]) => terms.some(term => name.includes(term)));
  return found
    ? `<img class="inventory-material-image" src="/materials/${found[1]}.png" alt="${found[2]}" loading="lazy">`
    : '<span class="inventory-material-placeholder" aria-hidden="true">▦</span>';
}

function animateStockValue(element, nextValue) {
  if (!element) return;
  const next = Number(nextValue) || 0;
  const previous = Number(element.dataset.stockNumber);
  if (!Number.isFinite(previous) || previous === next) {
    element.dataset.stockNumber = String(next);
    element.textContent = `${number(next)}${element.dataset.stockSuffix || ""}`;
    return;
  }
  const started = performance.now();
  const duration = 560;
  const suffix = element.dataset.stockSuffix || "";
  element.classList.remove("stock-number-up", "stock-number-down");
  element.classList.add(next > previous ? "stock-number-up" : "stock-number-down");
  const draw = now => {
    const progress = Math.min(1, (now - started) / duration);
    const eased = 1 - Math.pow(1 - progress, 3);
    element.textContent = `${number(previous + ((next - previous) * eased))}${suffix}`;
    if (progress < 1) return window.requestAnimationFrame(draw);
    element.dataset.stockNumber = String(next);
    element.textContent = `${number(next)}${suffix}`;
    window.setTimeout(() => element.classList.remove("stock-number-up", "stock-number-down"), 500);
  };
  window.requestAnimationFrame(draw);
}

function stockDashboardValues(data) {
  const company = data.company_stock || {}, reconciliation = data.reconciliation || {}, today = data.today || {}, pending = data.pending || {};
  const items = company.items || [];
  const normalized = value => String(value || "").normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase();
  const totalFor = terms => items.filter(item => terms.some(term => normalized(item.description).includes(term))).reduce((sum, item) => sum + Number(item.quantity || 0), 0);
  const connectorUnits = (today.connectors || []).reduce((sum, item) => sum + Number(item.quantity || 0), 0);
  const pendingTotal = (pending.connector_returns || []).length + (pending.modem_returns || []).length + (pending.installation_alerts || []).length + (pending.cases || []).length;
  return {
    companyTotal: Number(company.total_units || 0), usedSinceSix: Number(reconciliation.used_since_six || 0), checkedStocks: Number(reconciliation.checked_stocks || 0), connectorUnits, connectorRows: (today.connectors || []).length, pendingTotal,
    modems: totalFor(["modem", "onu", "ont", "wifi", "roteador"]), drop: totalFor(["drop"]), connectors: totalFor(["conector"]), tools: totalFor(["ferramenta", "alicate", "furadeira", "maquina"]),
  };
}

function updateStockDashboard(data) {
  rememberStockAlerts(data);
  const values = stockDashboardValues(data);
  const fields = {
    "company-total": values.companyTotal, "used-since-six": values.usedSinceSix, "pending-total": values.pendingTotal, "connectors-os": values.connectorUnits,
    modems: values.modems, drop: values.drop, connectors: values.connectors, tools: values.tools,
  };
  Object.entries(fields).forEach(([key, value]) => animateStockValue(document.querySelector(`[data-stock-value="${key}"]`), value));
  const checked = $("[data-stock-note='checked-stocks']");
  if (checked) checked.textContent = `${number(values.checkedStocks)} estoque(s) conferido(s)`;
  const connectorRows = $("[data-stock-note='connector-rows']");
  if (connectorRows) connectorRows.textContent = `${number(values.connectorRows)} lançamento(s) hoje`;
  const timestamp = $("[data-stock-note='updated-at']");
  if (timestamp) timestamp.textContent = `Atualizado ${new Date().toLocaleTimeString("pt-BR", { hour: "2-digit", minute: "2-digit" })}`;
}

function stockDashboardStorageKey() {
  return `sgfibra-stock-dashboard-v1:${state.user?.usuario || "supervisor"}`;
}

function rememberStockAlerts(data) {
  state.stockAlerts = data.pending || {};
  try { sessionStorage.setItem(`${stockDashboardStorageKey()}:alerts`, JSON.stringify(state.stockAlerts)); } catch (_error) {}
  renderStockAttention(state.stockAlerts);
  bindStockAlerts();
}

function stockAttentionItems(pending = {}) {
  return [
    ["↩", `${number((pending.connector_returns || []).reduce((sum, item) => sum + Number(item.quantity || 0), 0))} conectores para devolver`, "conectores", (pending.connector_returns || []).length],
    ["◉", `${number((pending.modem_returns || []).length)} modem(ns) aguardando devolução`, "modens", (pending.modem_returns || []).length],
    ["!", `${number((pending.installation_alerts || []).length)} instalação(ões) para conferir`, "instalacoes", (pending.installation_alerts || []).length],
    ["≠", `${number((pending.cases || []).length)} divergência(s) aberta(s)`, "divergencias", (pending.cases || []).length],
  ].filter(([, , , count]) => count > 0);
}

function renderStockAttention(pending = {}) {
  const target = $(".stock-attention-list");
  if (!target) return;
  const items = stockAttentionItems(pending);
  target.innerHTML = items.map(([icon, label, kind]) => `<a href="#alertas-estoque/${kind}"><b>${icon}</b><span>${esc(label)}</span><i>›</i></a>`).join("") || '<article class="stock-attention-ok">✓ Nenhuma pendência ativa no momento.</article>';
}

function bindStockAlerts() {
  const list = $(".stock-attention-list");
  const heading = list?.previousElementSibling;
  let refresh = $("#stock-attention-refresh");
  if (!refresh && heading?.classList.contains("stock-section-heading")) {
    const actions = document.createElement("div");
    actions.className = "stock-heading-actions";
    actions.innerHTML = '<button id="stock-attention-refresh" type="button">↻ Atualizar</button><a href="#alertas-estoque/conectores">Gerenciar avisos ›</a>';
    heading.lastElementChild?.remove();
    heading.appendChild(actions);
    refresh = $("#stock-attention-refresh");
  }
  if (!refresh) return;
  refresh.onclick = async () => {
    if (stockDashboardRefreshing) return;
    stockDashboardRefreshing = true;
    refresh.disabled = true;
    refresh.textContent = "Atualizando…";
    try {
      const data = await api("/api/superadmin/material-monitor?refresh=1", { timeoutMs: 180000, concurrent: true });
      updateStockDashboard(data);
      saveStockDashboardScreen();
      notify("Alertas atualizados.");
    } catch (error) { notify(error.message); }
    finally { stockDashboardRefreshing = false; refresh.disabled = false; refresh.textContent = "↻ Atualizar"; }
  };
}

async function stockAlertsView(kind, pendingOverride = null) {
  if (state.user?.mode !== 'superadmin') return homeView();
  setPage('Alertas de estoque');
  const groups = {
    conectores: ['Conectores para devolver', 'connector_returns'],
    modens: ['Modens aguardando devolução', 'modem_returns'],
    instalacoes: ['Instalações para conferir', 'installation_alerts'],
    divergencias: ['Divergências abertas', 'cases'],
  };
  const group = groups[kind];
  if (!group) { location.hash = 'estoque'; return; }
  let pending = pendingOverride || state.stockAlerts;
  if (!pending) {
    try { pending = JSON.parse(sessionStorage.getItem(`${stockDashboardStorageKey()}:alerts`) || 'null'); } catch (_error) {}
  }
  if (!pending) {
    loading('Buscando os alertas salvos...');
    try {
      const data = await api('/api/superadmin/material-monitor', { timeoutMs: 180000 });
      rememberStockAlerts(data);
      pending = data.pending || {};
    } catch (error) { errorView(error); return; }
  }
  if (location.hash !== `#alertas-estoque/${kind}`) return;
  const items = pending[group[1]] || [];
  state.stockAlerts = pending;
  const field = (label, value) => value == null || value === '' ? '' : `<p><strong>${label}:</strong> ${esc(value)}</p>`;
  const quantity = kind === 'conectores' ? items.reduce((sum, item) => sum + Number(item.quantity || 0), 0) : items.length;
  const itemId = item => kind === 'conectores' ? item.source_id : kind === 'divergencias' ? item.id : item.os_id;
  $('#content').innerHTML = `<section class="hero"><div><small>ATENÇÃO · ESTOQUE</small><h1>${group[0]}</h1><p>${number(quantity)} ${kind === 'conectores' ? 'conector(es)' : 'alerta(s)'} · ${number(items.length)} registro(s) da última consulta</p></div></section><div class="toolbar stock-alert-toolbar"><a class="secondary button-link" href="#estoque">← Voltar ao estoque</a><button id="stock-alerts-refresh" class="secondary">↻ Atualizar alertas</button></div><p class="stock-alert-help">Depois de conferir um item, marque como validado. Ele sai da lista ativa, mas a confirmação continua registrada no histórico do app.</p><section class="launched-modem-list">${items.map(item => `<article class="card stock-alert-card"><h2>${esc(item.client || item.title || 'Cliente não informado')}</h2>${field('Técnico', item.technician || item.owner)}${field('OS', item.os_id ? `#${item.os_id}` : '')}${field('Material', item.product)}${field('Quantidade', item.quantity != null ? number(item.quantity) : '')}${field('GPON', item.serial || item.gpon)}${field('Tipo', item.os_type || item.return_type)}${field('Motivo', item.reason || item.description)}${field('Informação registrada', item.note || item.service_note || item.evidence)}${field('Data', item.used_at || item.closed_at)}<button class="secondary stock-alert-dismiss" data-alert-id="${esc(itemId(item))}">✓ Validado · retirar da lista</button></article>`).join('') || '<article class="card">Nenhum alerta neste grupo na última consulta.</article>'}</section>`;
  $('#stock-alerts-refresh').onclick = async event => {
    const button = event.currentTarget; button.disabled = true; button.textContent = 'Atualizando…';
    try { const data = await api('/api/superadmin/material-monitor?refresh=1', { timeoutMs: 180000 }); rememberStockAlerts(data); await stockAlertsView(kind); }
    catch (error) { notify(error.message); button.disabled = false; button.textContent = '↻ Atualizar alertas'; }
  };
  const toolbar = document.querySelector('.stock-alert-toolbar');
  toolbar.insertAdjacentHTML('beforeend', '<a class="secondary button-link" href="#alertas-estoque/conectores">Conectores</a><a class="secondary button-link" href="#alertas-estoque/modens">Modens</a><a class="secondary button-link" href="#alertas-estoque/instalacoes">Instalações</a><a class="secondary button-link" href="#alertas-estoque/divergencias">Divergências</a>');
  toolbar.insertAdjacentHTML('beforeend', '<button id="stock-select-all" class="secondary">Selecionar todos</button><button id="stock-remove-selected" class="primary" disabled>Retirar selecionados (0)</button>');
  const removeSelected = $('#stock-remove-selected');
  const updateSelection = () => {
    const count = document.querySelectorAll('.stock-alert-select:checked').length;
    removeSelected.disabled = count === 0;
    removeSelected.textContent = `Retirar selecionados (${count})`;
  };
  document.querySelectorAll('.stock-alert-card').forEach(card => {
    const id = card.querySelector('.stock-alert-dismiss').dataset.alertId;
    card.insertAdjacentHTML('afterbegin', `<label style="display:flex;gap:8px;align-items:center"><input class="stock-alert-select" style="width:auto" type="checkbox" value="${esc(id)}">Selecionar aviso</label>`);
    card.querySelector('.stock-alert-select').onchange = updateSelection;
  });
  $('#stock-select-all').onclick = () => {
    const boxes = [...document.querySelectorAll('.stock-alert-select')];
    const checked = boxes.some(box => !box.checked);
    boxes.forEach(box => box.checked = checked); updateSelection();
  };
  removeSelected.onclick = async () => {
    const ids = [...document.querySelectorAll('.stock-alert-select:checked')].map(box => box.value);
    if (!ids.length || !window.confirm(`Validar e retirar ${ids.length} aviso(s)? Para modens e conectores, isso confirma o recebimento da devolução. Prossiga apenas para itens já conferidos.`)) return;
    removeSelected.disabled = true;
    try {
      for (let offset=0; offset<ids.length; offset+=200) {
        const batch=ids.slice(offset,offset+200);
        if (kind==='conectores') await api('/api/superadmin/connector-returns/dismiss', {method:'POST',body:JSON.stringify({source_ids:batch})});
        else if (kind==='modens') await api('/api/superadmin/removals/dismiss', {method:'POST',body:JSON.stringify({order_ids:batch.map(Number)})});
        else if (kind==='instalacoes') await api('/api/superadmin/installations-without-materials', {method:'DELETE',body:JSON.stringify({order_ids:batch.map(Number)})});
        else for (const id of batch) await api(`/api/logistics/cases/${encodeURIComponent(id)}`, {method:'POST',body:JSON.stringify({message:'Validado pelo supervisor no painel de atenção.',status:'resolved'})});
        state.stockAlerts[group[1]] = state.stockAlerts[group[1]].filter(item=>!batch.includes(String(itemId(item))));
        persistStockAlertRemoval();
      }
      notify('Avisos validados e retirados da lista.');
    } catch(error) { notify(error.message); }
    await stockAlertsView(kind,state.stockAlerts);
  };
  document.querySelectorAll('.stock-alert-dismiss').forEach(button => button.onclick = async () => {
    const identifier = button.dataset.alertId;
    if (!identifier || button.disabled) return;
    button.disabled = true; button.textContent = 'Salvando…';
    try {
      if (kind === 'conectores') await api('/api/superadmin/connector-returns/dismiss', { method: 'POST', body: JSON.stringify({ source_ids: [identifier] }) });
      else if (kind === 'modens') await api('/api/superadmin/removals/dismiss', { method: 'POST', body: JSON.stringify({ order_ids: [Number(identifier)] }) });
      else if (kind === 'instalacoes') await api('/api/superadmin/installations-without-materials', { method: 'DELETE', body: JSON.stringify({ order_ids: [Number(identifier)] }) });
      else await api(`/api/logistics/cases/${encodeURIComponent(identifier)}`, { method: 'POST', body: JSON.stringify({ message: 'Validado pelo supervisor no painel de atenção.', status: 'resolved' }) });
      state.stockAlerts[group[1]] = (state.stockAlerts[group[1]] || []).filter(item => String(itemId(item)) !== String(identifier));
      persistStockAlertRemoval();
      try { sessionStorage.setItem(`${stockDashboardStorageKey()}:alerts`, JSON.stringify(state.stockAlerts)); } catch (_error) {}
      notify('Item validado e retirado da lista ativa.');
      // The mutation above has already been saved. Re-render from the local
      // snapshot instead of starting an unrelated SGP consultation: otherwise
      // a slow/failed refresh incorrectly looks like the validation failed.
      await stockAlertsView(kind, state.stockAlerts);
    } catch (error) { notify(error.message); button.disabled = false; button.textContent = '✓ Validado · retirar da lista'; }
  });
}

function persistStockAlertRemoval() {
  state.stockDashboardHtml = '';
  try {
    sessionStorage.removeItem(stockDashboardStorageKey());
    sessionStorage.setItem(`${stockDashboardStorageKey()}:alerts`, JSON.stringify(state.stockAlerts));
  } catch (_error) {}
}

function saveStockDashboardScreen() {
  if (!location.hash.startsWith("#estoque") || !$(".stock-mobile-hero")) return;
  state.stockDashboardHtml = $("#content").innerHTML;
  try { sessionStorage.setItem(stockDashboardStorageKey(), state.stockDashboardHtml); }
  catch (error) { console.warn("Não foi possível guardar a tela de estoque nesta sessão:", error.message); }
}

function loadStockDashboardScreen() {
  if (state.stockDashboardHtml) return state.stockDashboardHtml;
  try { return sessionStorage.getItem(stockDashboardStorageKey()) || ""; }
  catch (_error) { return ""; }
}

function money(value) {
  return new Intl.NumberFormat("pt-BR", { style: "currency", currency: "BRL" }).format(Number(value) || 0);
}

function notify(message) {
  const toast = $("#toast");
  toast.textContent = message;
  toast.classList.add("show");
  window.setTimeout(() => toast.classList.remove("show"), 2500);
}

async function api(path, options = {}) {
  const { timeoutMs = 0, concurrent = false, ...fetchOptions } = options;
  if (!concurrent) {
    activeRequests.forEach(active => active.abort());
    activeRequests.clear();
  }
  const controller = new AbortController();
  let timedOut = false;
  const timeout = timeoutMs > 0 ? window.setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, timeoutMs) : null;
  activeRequests.add(controller);
  try {
    const response = await fetch(path, {
      credentials: "same-origin",
      headers: fetchOptions.body instanceof FormData ? {} : { "Content-Type": "application/json" },
      cache: "no-store",
      ...fetchOptions,
      signal: controller.signal,
    });
    const data = response.status === 204 ? null : await response.json().catch(() => ({}));
    if (!response.ok) {
      const error = new Error(data.error || `Falha na consulta (${response.status})`);
      error.status = response.status;
      throw error;
    }
    return data;
  } catch (error) {
    if (timedOut) {
      throw new Error("O SGP demorou além do limite configurado para esta operação.");
    }
    if (controller.signal.aborted) {
      const cancelled = new Error("Consulta cancelada.");
      cancelled.name = "AbortError";
      throw cancelled;
    }
    throw error;
  } finally {
    if (timeout !== null) window.clearTimeout(timeout);
    activeRequests.delete(controller);
  }
}

function loading(text = "Consultando o SGP somente uma vez...") {
  $("#content").innerHTML = `<section class="loading"><span class="loading-indicator" aria-hidden="true"></span><span>${esc(text)}</span></section>`;
}

function errorView(error) {
  if (error.name === "AbortError") return;
  $("#content").innerHTML = `<section class="card empty"><h3>Não foi possível consultar</h3><p>${esc(error.message)}</p><button class="secondary" onclick="location.reload()">Tentar novamente</button></section>`;
}

function setPage(title) {
  activeRequests.forEach(controller => controller.abort());
  activeRequests.clear();
  $("#page-title").textContent = title;
  document.querySelectorAll("[data-route]").forEach(link => {
    link.classList.toggle("active", link.dataset.route === location.hash.slice(1));
  });
  setMenuOpen(false);
}

function returnObligationFormHtml(home = false) {
  return `<form id="return-obligation-form" class="card return-obligation-form ${home ? "home-return-form" : ""}"><div><small>${home ? "LANÇAMENTO RÁPIDO" : "EDITAR DEVOLUÇÃO"}</small><h2>Materiais que o técnico deve devolver</h2><p>Separe os modens pelo tipo da devolução.</p></div><label>Técnico<select id="return-technician" required><option value="">Carregando técnicos...</option></select></label><label>Conectores para devolver<input id="return-connectors" type="number" min="0" max="1000" value="0"></label><label class="wide">GPONs de troca<textarea id="return-modems-exchange" rows="4" placeholder="Um GPON por linha"></textarea></label><label class="wide">GPONs de retirada<textarea id="return-modems-removal" rows="4" placeholder="Um GPON por linha"></textarea></label><label class="wide">Observação<textarea id="return-note" rows="2" maxlength="500" placeholder="Informação opcional para o técnico"></textarea></label><button class="primary">Salvar no banco</button><p id="return-error" class="error"></p></form>`;
}

async function loadReturnTechnicianOptions(selected = "") {
  const select = $("#return-technician");
  if (!select) return;
  try {
    if (!returnTechnicians) {
      const data = await api("/api/superadmin/users");
      returnTechnicians = (data.users || []).filter(user => user.role === "technician").map(user => ({
        value: String(user.name || user.username || "").trim(), username: String(user.username || "").trim(),
      })).filter(item => item.value).sort((a, b) => a.value.localeCompare(b.value, "pt-BR"));
      for (const required of ["LEO-LIRA", "DavidR"]) {
        if (!returnTechnicians.some(item => item.value.toLocaleLowerCase() === required.toLocaleLowerCase() || item.username.toLocaleLowerCase() === required.toLocaleLowerCase())) {
          returnTechnicians.push({ value: required, username: required });
        }
      }
      returnTechnicians.sort((a, b) => a.value.localeCompare(b.value, "pt-BR"));
    }
    if (!document.body.contains(select)) return;
    const values = [...returnTechnicians];
    if (selected && !values.some(item => item.value.toLocaleLowerCase() === selected.toLocaleLowerCase())) values.unshift({ value: selected, username: "" });
    select.innerHTML = `<option value="">Selecione o técnico</option>${values.map(item => `<option value="${esc(item.value)}" ${item.value === selected ? "selected" : ""}>${esc(item.value)}${item.username && item.username !== item.value ? ` · ${esc(item.username)}` : ""}</option>`).join("")}`;
  } catch (error) {
    if (error.name !== "AbortError" && document.body.contains(select)) select.innerHTML = '<option value="">Não foi possível carregar os técnicos</option>';
  }
}

let liveLocationWatch=null;
let technicianShiftActive=false;
let shiftScheduleTimer=null;
function localSaoPauloHour(){return Number(new Intl.DateTimeFormat('pt-BR',{timeZone:'America/Sao_Paulo',hour:'2-digit',hour12:false}).format(new Date()));}
function stopLiveLocation(){if(liveLocationWatch!=null&&navigator.geolocation)navigator.geolocation.clearWatch(liveLocationWatch);liveLocationWatch=null;}
function startLiveLocation({silent=false}={}){
  if(state.user?.mode!=='technician'||!navigator.geolocation||!technicianShiftActive||localSaoPauloHour()>=19)return;
  if(liveLocationWatch!=null)return;
  liveLocationWatch=navigator.geolocation.watchPosition(
    position=>api('/api/technician/live-location',{method:'POST',concurrent:true,body:JSON.stringify({latitude:position.coords.latitude,longitude:position.coords.longitude,accuracy:position.coords.accuracy})}).then(result=>{if(result?.active===false){technicianShiftActive=false;stopLiveLocation();}}).catch(()=>{}),
    error=>{stopLiveLocation();if(!silent)notify(error.code===1?'Permita a localização para ativar seu expediente.':'Não foi possível obter sua localização.');},
    {enableHighAccuracy:true,maximumAge:15000,timeout:20000}
  );
}
function ensureShiftSchedule(){
  clearInterval(shiftScheduleTimer);
  shiftScheduleTimer=setInterval(()=>{
    if(state.user?.mode!=='technician')return;
    if(localSaoPauloHour()>=19&&technicianShiftActive){
      technicianShiftActive=false;
      stopLiveLocation();
      api('/api/technician/shift',{method:'POST',concurrent:true,body:JSON.stringify({active:false})}).catch(()=>{});
      if(location.hash==='#home'||!location.hash)homeView();
    }
  },30000);
}
async function syncTechnicianShift(){
  if(state.user?.mode!=='technician')return;
  try{const result=await api('/api/technician/shift',{concurrent:true});technicianShiftActive=result.active===true;if(technicianShiftActive)startLiveLocation({silent:true});}
  catch(_){technicianShiftActive=false;stopLiveLocation();}
  ensureShiftSchedule();
  if(location.hash==='#home'||!location.hash)homeView();
}
async function toggleTechnicianShift(){
  if(technicianShiftActive){
    technicianShiftActive=false;stopLiveLocation();
    await api('/api/technician/shift',{method:'POST',body:JSON.stringify({active:false})}).catch(()=>{});
    return homeView();
  }
  if(localSaoPauloHour()>=19)return;
  if(!navigator.geolocation)return notify('Este aparelho não permite compartilhar a localização.');
  navigator.geolocation.getCurrentPosition(async()=>{
    localStorage.setItem('ebf-location-authorized','1');
    try{const result=await api('/api/technician/shift',{method:'POST',body:JSON.stringify({active:true})});technicianShiftActive=result.active===true;if(technicianShiftActive)startLiveLocation({silent:true});homeView();}
    catch(error){notify(error.message);}
  },error=>notify(error.code===1?'Permita a localização para ativar seu expediente.':'Não foi possível obter sua localização.'),{enableHighAccuracy:true,maximumAge:0,timeout:20000});
}

function homeView() {
  stopPonObservation();
  setPage("Início");
  const name = state.user?.nome || state.user?.usuario || "Usuário";
  const technician = state.user?.mode === "technician";
  const office = state.user?.mode === "office";
  const supervisor = state.user?.mode === "superadmin";
  $("#content").innerHTML = `
    <section class="home-welcome">
      <span class="home-brand-mark" aria-hidden="true">SG</span>
      <small>SG FIBRA TÉCNICO</small>
      <h1>Olá, ${esc(name)}</h1>
      <p>Escolha uma ferramenta para começar. Nenhuma consulta ao SGP é feita nesta tela.</p>
    </section>
    <section class="home-actions">${technician ? `
      <a class="card" href="#ordens"><span>☷</span><div><h2>Ordens do dia</h2><p>Abertas, em execução, pendentes e encerradas hoje.</p></div><b>→</b></a>
      <a class="card" href="#pendencias-estoque"><span>▦</span><div><h2>Divergências de materiais</h2><p>Informe diferenças de saldo ou devolução e acompanhe a resposta.</p></div><b>→</b></a>
      <a class="card" href="#relatorios"><span>▥</span><div><h2>Relatórios</h2><p>Consulte seu resumo semanal de serviços e materiais.</p></div><b>→</b></a>
      <a class="card" href="#vincular-modem"><span>◉</span><div><h2>Vincular modem</h2><p>Escolha a OLT, localize o aparelho e vincule ao cliente.</p></div><b>→</b></a>
      <button class="card" id="live-location-toggle" type="button"><span>⌖</span><div><h2>Expediente e localização</h2><p>Ative o expediente para iniciar a localização em tempo real.</p></div><b>→</b></button>
    ` : `
      <a class="card" href="#rota"><span>◷</span><div><h2>Rota</h2><p>Acompanhe técnicos, instalações e serviços no mapa.</p></div><b>→</b></a>
      <a class="card" href="#vincular-modem"><span>◉</span><div><h2>Vincular modem</h2><p>Informe o ID do cliente, escolha a ONU encontrada na OLT e preencha somente a etiqueta.</p></div><b>→</b></a>
      <a class="card" href="#procurar-modem"><span>⌕</span><div><h2>Procurar modem</h2><p>Localize uma ONU em todas as OLTs pelos seis últimos caracteres.</p></div><b>→</b></a>
      ${supervisor ? `<a class="card" href="#relatorios"><span>▥</span><div><h2>Relatórios</h2><p>Análise de trocas e resumo semanal.</p></div><b>→</b></a>` : ""}
      ${supervisor ? `<a class="card" href="#estoque"><span>▦</span><div><h2>Estoque</h2><p>Saldo disponível, conferência de GPON e compra de modens.</p></div><b>→</b></a>` : ""}
      ${office ? "" : `<a class="card" href="#pontuacao"><span>★</span><div><h2>Pontuação</h2><p>Classificação, repetidas e devolução de pontos.</p></div><b>→</b></a>
      <a class="card" href="#acessos"><span>♙</span><div><h2>Acessos</h2><p>Autorizar técnicos, escritório, supervisores ou bloquear usuários.</p></div><b>→</b></a>`}
      <a class="card" href="#cto"><span>◫</span><div><h2>Portas ativas da CTO</h2><p>Conferir contratos ativos, slot, PON e identificar as portas da caixa.</p></div><b>→</b></a>
    `}</section>
    `;
  if(technician){const button=$("#live-location-toggle");const closed=localSaoPauloHour()>=19;button.innerHTML=`<span>⌖</span><div><h2>Expediente e localização</h2><p>${technicianShiftActive?'Expediente ativo · localização em tempo real.':closed?'Compartilhamento encerrado no horário programado.':'Ative o expediente para iniciar a localização.'}</p></div><b>${technicianShiftActive?'Encerrar':closed?'Encerrado':'Ativar'}</b>`;button.disabled=closed&&!technicianShiftActive;button.onclick=toggleTechnicianShift;}
}

async function tutorialsView() {
  setPage("Tutoriais"); loading("Carregando a Central de Tutoriais...");
  try {
    const data = await api('/api/tutorials', { concurrent: true });
    const items = data.items || [], admin = state.user?.mode === 'superadmin';
    const categories = [...new Set(items.map(item => item.category).filter(Boolean))];
    const cards = items.map(item => '<article class="card tutorial-card" data-search="' + esc((item.title+' '+item.summary+' '+item.keywords).toLowerCase()) + '"><div class="tutorial-thumb">' + (item.thumbnail_url ? '<img src="' + esc(item.thumbnail_url) + '" alt="" loading="lazy">' : '<span>▶</span>') + '</div><small>' + esc(item.category) + ' · ' + esc(item.duration || 'Duração não informada') + (item.status === 'draft' ? ' · RASCUNHO' : '') + '</small><h2>' + esc(item.title) + '</h2><p>' + esc(item.summary) + '</p><button class="secondary tutorial-open" data-id="' + item.id + '">Abrir tutorial</button>' + (admin ? '<button class="secondary tutorial-edit" data-id="' + item.id + '">Editar</button><button class="secondary tutorial-delete" data-id="' + item.id + '">Excluir</button>' : '') + '</article>').join('');
    $("#content").innerHTML = '<section class="hero"><div><small>CAPACITAÇÃO E PROCEDIMENTOS</small><h1>Central de Tutoriais</h1><p>Consulte procedimentos e materiais de treinamento da equipe.</p></div><b>▶</b></section><section class="card tutorial-toolbar"><input id="tutorial-search" type="search" placeholder="Pesquisar por título, descrição ou palavra-chave"><select id="tutorial-category"><option value="">Todas as categorias</option>' + categories.map(c => '<option>'+esc(c)+'</option>').join('') + '</select>' + (admin ? '<button id="tutorial-new" class="primary">+ Novo tutorial</button>' : '') + '</section><section id="tutorial-list" class="tutorial-grid">' + (cards || '<article class="card empty">Nenhum tutorial publicado ainda.</article>') + '</section>';
    const render = () => { const q = String($("#tutorial-search").value || '').toLowerCase().trim(), cat = $("#tutorial-category").value; let shown = 0; document.querySelectorAll('.tutorial-card').forEach(el => { const ok = (!q || el.dataset.search.includes(q)) && (!cat || el.querySelector('small').textContent.startsWith(cat)); el.classList.toggle('hidden', !ok); if (ok) shown++; }); if (!shown) $("#tutorial-list").innerHTML = '<article class="card empty">Nenhum tutorial corresponde à pesquisa.</article>'; };
    $("#tutorial-search").oninput = render; $("#tutorial-category").onchange = render;
    const byId = id => items.find(item => String(item.id) === String(id));
    document.querySelectorAll('.tutorial-open').forEach(btn => btn.onclick = () => tutorialDetailView(byId(btn.dataset.id)));
    if (admin) { $("#tutorial-new").onclick = () => tutorialEditorView(); document.querySelectorAll('.tutorial-edit').forEach(btn => btn.onclick = () => tutorialEditorView(byId(btn.dataset.id))); document.querySelectorAll('.tutorial-delete').forEach(btn => btn.onclick = async () => { if (!confirm('Excluir este tutorial?')) return; await api('/api/superadmin/tutorials/' + btn.dataset.id, {method:'DELETE'}); tutorialsView(); }); }
  } catch (error) { errorView(error); }
}
function tutorialYoutubeEmbed(url) {
  try {
    const parsed = new URL(url);
    const host = parsed.hostname.toLowerCase();
    let id = '';
    if (host === 'youtu.be') id = parsed.pathname.split('/').filter(Boolean)[0] || '';
    else if (host.endsWith('youtube.com')) id = parsed.searchParams.get('v') || parsed.pathname.match(/\/(?:embed|shorts|live)\/([^/?]+)/)?.[1] || '';
    return id ? 'https://www.youtube.com/embed/' + encodeURIComponent(id) + '?origin=' + encodeURIComponent(location.origin) + '&rel=0' : '';
  } catch (_) { return ''; }
}
function tutorialDetailView(item) {
  if (!item) return tutorialsView(); setPage(item.title);
  const youtube = item.video_url ? tutorialYoutubeEmbed(item.video_url) : '';
  const directVideo = /\.(mp4|webm|ogg|m3u8)(?:[?#].*)?$/i.test(item.video_url || '');
  const video = item.video_url ? (youtube ? '<iframe class="tutorial-video" src="' + esc(youtube) + '" title="' + esc(item.title) + '" referrerpolicy="strict-origin-when-cross-origin" allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture; web-share" allowfullscreen></iframe>' : /vimeo.com/i.test(item.video_url) ? '<iframe class="tutorial-video" src="' + esc(item.video_url) + '" title="' + esc(item.title) + '" referrerpolicy="strict-origin-when-cross-origin" allowfullscreen></iframe>' : directVideo ? '<video class="tutorial-video" controls src="' + esc(item.video_url) + '"></video>' : '<div class="tutorial-no-video"><strong>Este provedor não permite reprodução incorporada.</strong><p>Abra o conteúdo em uma nova aba para assistir.</p><a class="primary button-link" href="' + esc(item.video_url) + '" target="_blank" rel="noopener noreferrer">Abrir vídeo</a></div>') : '<div class="tutorial-no-video">Este tutorial ainda não possui vídeo.</div>';
  $("#content").innerHTML = '<section class="hero"><div><small>'+esc(item.category)+' · '+esc(item.duration || 'Duração não informada')+'</small><h1>'+esc(item.title)+'</h1><p>'+esc(item.summary)+'</p></div></section><article class="card tutorial-detail">'+video+'<h2>Sobre este procedimento</h2><p class="tutorial-content">'+esc(item.content).replace(/\n/g,'<br>')+'</p>'+(item.steps ? '<h2>Passo a passo</h2><p class="tutorial-content">'+esc(item.steps).replace(/\n/g,'<br>')+'</p>' : '')+(item.notes ? '<aside class="tutorial-notes"><strong>Observações importantes</strong><p>'+esc(item.notes).replace(/\n/g,'<br>')+'</p></aside>' : '')+'<button class="secondary" id="tutorial-back">← Voltar aos tutoriais</button></article>';
  $("#tutorial-back").onclick = tutorialsView;
}
function tutorialEditorView(item = {}) {
  setPage(item.id ? 'Editar tutorial' : 'Novo tutorial');
  const f = (label, name, value, type) => '<label>'+label+(type === 'textarea' ? '<textarea name="'+name+'" rows="4">'+esc(value)+'</textarea>' : '<input name="'+name+'" type="'+(type || 'text')+'" value="'+esc(value)+'">')+'</label>';
  $("#content").innerHTML = '<form id="tutorial-form" class="card tutorial-editor"><h1>'+(item.id ? 'Editar tutorial' : 'Novo tutorial')+'</h1>'+f('Título','title',item.title||'')+f('Descrição resumida','summary',item.summary||'','textarea')+f('Conteúdo completo','content',item.content||'','textarea')+f('Categoria','category',item.category||'Operação')+'<label>Vídeo local (MP4/WebM/OGG/M4V)<input id="tutorial-video-file" type="file" accept="video/mp4,video/webm,video/ogg,video/x-m4v"></label>'+f('URL de vídeo externo (opcional)','video_url',item.video_url||'','url')+f('URL da miniatura','thumbnail_url',item.thumbnail_url||'','url')+f('Duração aproximada','duration',item.duration||'')+f('Palavras-chave','keywords',item.keywords||'')+f('Passo a passo','steps',item.steps||'','textarea')+f('Observações e alertas','notes',item.notes||'','textarea')+'<label>Status<select name="status"><option value="draft">Rascunho</option><option value="published" '+(item.status === 'published' ? 'selected' : '')+'>Publicado</option></select></label><button class="primary">Salvar tutorial</button><button type="button" class="secondary" id="tutorial-cancel">Cancelar</button><p id="tutorial-form-error" class="error"></p></form>';
  $("#tutorial-cancel").onclick = tutorialsView;
  $("#tutorial-form").onsubmit = async event => { event.preventDefault(); const form = event.currentTarget, body = Object.fromEntries(new FormData(form)); try { const file = $("#tutorial-video-file").files[0]; if (file) { const upload = await api('/api/superadmin/tutorials/upload', {method:'POST', body:(() => { const data = new FormData(); data.append('file', file); return data; })()}); body.video_url = upload.url; } await api(item.id ? '/api/superadmin/tutorials/'+item.id : '/api/superadmin/tutorials', {method:item.id ? 'PUT' : 'POST', body:JSON.stringify(body)}); tutorialsView(); } catch (error) { $("#tutorial-form-error").textContent = error.message; } };
}
function reportsView() {
  setPage("Relatórios");
  const supervisor = state.user?.mode === "superadmin";
  $("#content").innerHTML = `<section class="hero"><div><small>CENTRAL DE CONSULTAS</small><h1>Relatórios</h1><p>Escolha qual análise deseja abrir.</p></div><b>▥</b></section><section class="folder-grid report-folder-grid">${supervisor ? `<a class="card folder-card" href="#analise-os"><b>1</b><h2>Análise de OS</h2><p>Identifique trocas de modem nas OS encerradas da data escolhida.</p></a><a class="card folder-card" href="#resumo-semanal"><b>2</b><h2>Resumo semanal</h2><p>Veja serviços e materiais usados por técnico.</p></a>` : `<a class="card folder-card" href="#resumo-semanal"><b>1</b><h2>Resumo semanal</h2><p>Veja seus serviços e materiais usados durante a semana.</p></a>`}</section>`;
}

async function materialUsageView(period = "day", selectedDate = localDateValue(), force = false) {
  if (state.user?.mode !== "superadmin") return homeView();
  setPage("Materiais usados");
  $("#content").innerHTML = `<section class="hero"><div><small>RELATÓRIO DE ESTOQUE</small><h1>Materiais usados</h1><p>Conectores lançados em OS e comodato, separados pela necessidade de devolução.</p></div><b>▦</b></section><form id="material-usage-filter" class="card toolbar"><label>Período<select id="material-usage-period"><option value="day" ${period === "day" ? "selected" : ""}>Dia</option><option value="month" ${period === "month" ? "selected" : ""}>Mês</option></select></label><label>Data<input id="material-usage-date" type="date" value="${esc(selectedDate)}" max="${localDateValue()}"></label><button class="primary">Consultar</button><a class="secondary button-link" href="#estoque">Voltar</a></form><div id="material-usage-results" class="card empty">Escolha o período e clique em Consultar.</div>`;
  $("#material-usage-filter").onsubmit = event => { event.preventDefault(); materialUsageView($("#material-usage-period").value, $("#material-usage-date").value, true); };
  if (!force) return;
  const target = $("#material-usage-results");
  target.textContent = "Consultando conectores e tipos de OS no SGP...";
  try {
    const query = new URLSearchParams({ period, date: selectedDate, refresh: "1" });
    const data = await api(`/api/superadmin/material-usage?${query}`, { timeoutMs: 180000 });
    const report = data.connectors || {};
    const rows = report.connectors || [];
    const counts = report.connector_counts || {};
    const groups = {
      return: rows.filter(item => item.classification === "return"),
      exempt: rows.filter(item => item.classification === "exempt"),
      review: rows.filter(item => item.classification === "review"),
    };
    const connectorCard = item => `<article class="card launched-modem-card"><h2>${esc(item.client || "Cliente não identificado")}</h2><p><strong>Técnico:</strong> ${esc(item.technician || "Não identificado")}</p><p><strong>Tipo da OS:</strong> ${esc(item.os_type || "Não informado")}</p><p><strong>Conectores:</strong> ${number(item.quantity)} · ${esc(item.product || "Conector")}</p><small>OS #${number(item.os_id)} · ${esc(item.used_at || "")}</small></article>`;
    const section = (title, quantity, items, empty) => `<section class="section-title"><div><h2>${title}</h2><p>${number(quantity)} conector(es) · ${number(items.length)} lançamento(s)</p></div></section><section class="launched-modem-list">${items.map(connectorCard).join("") || `<article class="card">${empty}</article>`}</section>`;
    target.className = "material-usage-results";
    target.innerHTML = `${report.incomplete ? `<section class="card"><strong>Consulta incompleta</strong><p>Não foi possível consultar as OS: ${esc((report.failed_orders || []).join(", "))}. Estes lançamentos permanecem a conferir. Consulte novamente.</p></section>` : ""}<a class="secondary button-link" href="#pendencias-estoque">Registrar / acompanhar divergência</a><section class="metrics"><article class="card bad"><small>DEVOLVER</small><b>${number(counts.return || 0)}</b></article><article class="card good"><small>DISPENSADOS</small><b>${number(counts.exempt || 0)}</b></article><article class="card"><small>A CONFERIR</small><b>${number(counts.review || 0)}</b></article></section><section class="section-title"><div><h2>Conectores ${period === "day" ? "do dia" : "do mês"}</h2><p>${esc(data.start)} a ${esc(data.end)} · OS e comodato</p></div></section><h2>Devolução por técnico</h2><section class="launched-modem-list">${(report.technicians || []).filter(item => item.return > 0).map(item => `<article class="card launched-modem-card"><h2>${esc(item.technician)}</h2><p><strong>${number(item.return)}</strong> conector(es) para devolver</p></article>`).join("") || '<article class="card">Nenhum conector para devolver.</article>'}</section>${section("Devem ser devolvidos", counts.return || 0, groups.return, "Nenhum conector para devolver.")}${section("Não precisam ser devolvidos", counts.exempt || 0, groups.exempt, "Nenhum conector dispensado.")}${section("A conferir", counts.review || 0, groups.review, "Nenhum lançamento pendente de classificação.")}`;
  } catch (error) { target.className = "card empty"; target.textContent = `Não foi possível concluir a consulta: ${error.message}`; }
}

function routeFolderView() {
  if (!["superadmin", "office"].includes(state.user?.mode)) return homeView();
  setPage("Rota");
  const visitCard=state.user?.mode==="superadmin"?`<a class="card folder-card" href="#acompanhamento-instalacoes"><b>4</b><h2>Acompanhamento das instalações</h2><p>Veja deslocamentos, mensagens do cliente e endereços IP registrados.</p></a>`:"";
  $("#content").innerHTML = `<section class="hero"><div><small>OPERAÇÃO EM CAMPO</small><h1>Rota</h1><p>Escolha a ferramenta que deseja abrir.</p></div><b>◷</b></section><section class="folder-grid"><a class="card folder-card" href="#acompanhar-rota"><b>1</b><h2>Acompanhar rota</h2><p>Veja a agenda, o tempo em atendimento e as notificações dos técnicos.</p></a><a class="card folder-card" href="#mapa-servicos"><b>2</b><h2>Mapa de serviços</h2><p>Compare a posição dos técnicos com as ocorrências abertas do dia.</p></a><a class="card folder-card" href="#puxar-rompimento"><b>3</b><h2>Puxar rompimento</h2><p>Selecione OLT, Slot e PON e quantifique os clientes online e offline.</p></a>${visitCard}</section>`;
}

let supervisorVisitTrackingTimer=null;
function supervisorVisitTrackingStop(){if(supervisorVisitTrackingTimer)clearInterval(supervisorVisitTrackingTimer);supervisorVisitTrackingTimer=null;}
async function supervisorVisitTrackingView(){
  supervisorVisitTrackingStop();
  if(state.user?.mode!=="superadmin")return homeView();
  setPage("Acompanhamento das instalações");
  const load=async()=>{
    const root=$("#visit-tracking-results");if(!root)return;
    try{
      const data=await api("/api/superadmin/visit-tracking",{timeoutMs:20000});
      const sessions=data.sessions||[],active=sessions.filter(item=>item.state==="traveling"),arrived=sessions.filter(item=>item.state==="arrived");
      const label={ready:"Aguardando GPS",traveling:"Em deslocamento",arrived:"Chegou ao endereço",finished:"Atendimento encerrado",paused:"Pausado",cancelled:"Revogado"};
      root.innerHTML=`<section class="metrics"><article class="card"><small>DESLOCAMENTOS ATIVOS</small><b>${active.length}</b></article><article class="card"><small>CHEGADAS</small><b>${arrived.length}</b></article><article class="card"><small>VISITAS NO HISTÓRICO</small><b>${sessions.length}</b></article></section><section class="visit-tracking-list">${sessions.map(item=>`<article class="card visit-tracking-card"><header><div><small>OS #${number(item.order_id)} · ${esc(item.technician||"Técnico")}</small><h2>${esc(item.client||"Cliente")}</h2></div><b class="visit-state ${esc(item.state)}">${esc(label[item.state]||item.state)}</b></header><p class="muted">Iniciado ${item.created_at?new Date(item.created_at*1000).toLocaleString("pt-BR"):"—"}${item.arrived_at?` · Chegada ${new Date(item.arrived_at*1000).toLocaleString("pt-BR")}`:""}</p>${item.location?`<p><strong>Última posição:</strong> ${Number(item.location.latitude).toFixed(5)}, ${Number(item.location.longitude).toFixed(5)} · ${new Date(item.location.updated_at*1000).toLocaleTimeString("pt-BR")}</p>`:`<p class="muted">Sem localização recente compartilhada.</p>`}<h3>Mensagens e IP de origem</h3><div class="visit-message-list">${(item.messages||[]).map(message=>`<article><header><b>${esc(message.side==="client"?"Cliente":"Técnico")}</b><time>${message.created_at?new Date(message.created_at*1000).toLocaleString("pt-BR"):"—"}</time></header><p>${esc(message.text||"")}</p><small>IP público registrado: ${esc(message.ip||"Não disponível (mensagem antiga ou IP não informado pelo proxy)")}</small></article>`).join("")||'<p class="muted">Nenhuma mensagem nesta visita.</p>'}</div></article>`).join("")||'<article class="card empty">Nenhum acompanhamento de instalação ativo ou recente.</article>'}<p class="muted">Mensagens e endereços IP ficam disponíveis durante o prazo temporário da visita. Atualização automática a cada 10 segundos. O IP público pode ser compartilhado por vários usuários (NAT).</p></section>`;
    }catch(error){root.innerHTML=`<article class="card empty error">Não foi possível carregar os acompanhamentos: ${esc(error.message)}</article>`;}
  };
  $("#content").innerHTML=`<section class="section-title"><div><small>SUPERVISÃO</small><h1>Acompanhamento das instalações</h1><p>Leia o andamento e o chat das visitas. Esta tela é somente para supervisores.</p></div><button id="refresh-visit-tracking" class="secondary">Atualizar</button></section><div id="visit-tracking-results" class="loading">Carregando visitas…</div>`;
  $("#refresh-visit-tracking").onclick=load;
  await load();supervisorVisitTrackingTimer=setInterval(()=>{if(location.hash.startsWith("#acompanhamento-instalacoes"))load();},10000);
}

async function inventoryFolderView() {
  if (state.user?.mode !== "superadmin") return homeView();
  stockDashboardStop();
  setPage("Estoque");
  const savedScreen = loadStockDashboardScreen();
  if (savedScreen) {
    $("#content").innerHTML = savedScreen;
    bindStockAlerts();
    stockDashboardStart(true);
    return;
  }
  $("#content").innerHTML = '<section class="card loading">Carregando o resumo de materiais...</section>';
  try {
    const data = await api("/api/superadmin/material-monitor", { timeoutMs: 180000 });
    const pending = data.pending || {};
    const values = stockDashboardValues(data);
    const attention = stockAttentionItems(pending);
    const stockValue = (key, value, suffix = "") => `<b data-stock-value="${key}" data-stock-number="${Number(value) || 0}" data-stock-suffix="${suffix}">${number(value)}${suffix}</b>`;
    const category = (icon, label, key, quantity, suffix, href) => `<a class="stock-category-card" href="${href}"><b>${icon}</b><span>${esc(label)}</span><strong data-stock-value="${key}" data-stock-number="${Number(quantity) || 0}" data-stock-suffix="${suffix}">${number(quantity)}${suffix}</strong><small>Ver detalhes →</small></a>`;
    const tool = (icon, title, note, href) => `<a class="stock-tool-card" href="${href}"><b>${icon}</b><span><strong>${esc(title)}</strong><small>${esc(note)}</small></span><i>›</i></a>`;
    $("#content").innerHTML = `<section class="stock-mobile-hero"><div><small>CONTROLE DE MATERIAIS</small><h1>Estoque</h1><p>Saldo, uso e conferência da operação em um só lugar.</p></div><a class="stock-refresh" href="#monitor-materiais" title="Abrir monitor ao vivo">◉</a></section><section class="stock-overview-metrics"><article><small>ESTOQUE EMPRESA</small>${stockValue("company-total", values.companyTotal)}<span>saldo somado</span></article><article><small>SAÍDAS DESDE 06H</small>${stockValue("used-since-six", values.usedSinceSix)}<span data-stock-note="checked-stocks">${number(values.checkedStocks)} estoque(s) conferido(s)</span></article><article class="${values.pendingTotal ? "alert" : "ok"}"><small>ATENÇÃO</small>${stockValue("pending-total", values.pendingTotal)}<span>pendência(s) ativa(s)</span></article><article><small>CONECTORES EM OS</small>${stockValue("connectors-os", values.connectorUnits)}<span data-stock-note="connector-rows">${number(values.connectorRows)} lançamento(s) hoje</span></article></section><p class="stock-auto-update" data-stock-note="updated-at">Atualizado agora · nova consulta automática a cada 2 minutos</p><section class="stock-section-heading"><div><h2>Categorias</h2><p>Saldo disponível na Empresa</p></div><a href="#saldo-estoque">Ver todos ›</a></section><section class="stock-category-grid">${category("◉", "Modens", "modems", values.modems, " un", "#saldo-estoque")}${category("〰", "Drop", "drop", values.drop, " m", "#saldo-estoque")}${category("⌁", "Conectores", "connectors", values.connectors, " un", "#materiais-usados")}${category("⌕", "Ferramentas", "tools", values.tools, " un", "#saldo-estoque")}</section><section class="stock-section-heading"><div><h2>Atenção</h2><p>Itens que precisam de análise</p></div><a href="#pendencias-estoque">Ver todas ›</a></section><section class="stock-attention-list">${attention.map(([icon, label, href]) => `<a href="${href}"><b>${icon}</b><span>${esc(label)}</span><i>›</i></a>`).join("") || '<article class="stock-attention-ok">✓ Nenhuma pendência ativa no momento.</article>'}</section><section class="stock-section-heading"><div><h2>Ferramentas de estoque</h2><p>Consultas e controles operacionais</p></div></section><section class="stock-tools-grid">${tool("◉", "Monitor de materiais", "Visão ao vivo de saldos, OS e pendências", "#monitor-materiais")}${tool("▦", "Saldo disponível", "Filtrar por estoque, categoria e referência", "#saldo-estoque")}${tool("✓", "Conferir GPON", "Verificar se a ONU já está vinculada", "#conferir-gpon")}${tool("+", "Comprar modem", "Cadastrar referências no estoque Empresa", "#comprar-modem")}${tool("⌁", "Materiais usados", "Conectores lançados e devoluções", "#materiais-usados")}${tool("▣", "Modem lançado", "Auditar vínculo do GPON com o cliente", "#modem-lancado")}${tool("≡", "Conferência de saldo", "Entradas, saídas e diferenças", "#conferencia-saldo")}${tool("!", "Divergências", "Anotações e decisões de conferência", "#pendencias-estoque")}</section><section class="stock-section-heading"><div><h2>Movimentações recentes</h2><p>Registros feitos pelo app</p></div><a href="#historico-logistica">Ver todas ›</a></section><section class="stock-recent-list">${(data.events || []).slice(0, 5).map(item => `<article><b>◌</b><span><strong>${esc(item.detail?.action || item.detail?.message || "Atualização de materiais")}</strong><small>${esc(item.actor || "Sistema")} · ${esc(new Date(item.created_at * 1000).toLocaleString("pt-BR"))}</small></span><i>›</i></article>`).join("") || '<article><span><strong>Sem movimentações registradas</strong><small>As próximas operações realizadas pelo app aparecerão aqui.</small></span></article>'}</section>`;
    rememberStockAlerts(data);
    saveStockDashboardScreen();
    stockDashboardStart(Boolean(data.saved_snapshot));
  } catch (error) { errorView(error); }
}

function stockDashboardStart(refreshNow = false) {
  const refresh = async () => {
    if (stockDashboardRefreshing || !location.hash.startsWith("#estoque")) return;
    stockDashboardRefreshing = true;
    try {
      const data = await api("/api/superadmin/material-monitor?refresh=1", { timeoutMs: 180000, concurrent: true });
      updateStockDashboard(data);
      window.setTimeout(saveStockDashboardScreen, 700);
    } catch (error) {
      if (error.name !== "AbortError") console.warn("Atualização do estoque falhou:", error.message);
    } finally { stockDashboardRefreshing = false; }
  };
  if (refreshNow) window.setTimeout(refresh, 0);
  stockDashboardTimer = window.setInterval(refresh, 120000);
}

function stockDashboardStop() {
  if (stockDashboardTimer) window.clearInterval(stockDashboardTimer);
  stockDashboardTimer = null;
  stockDashboardRefreshing = false;
}

function materialMonitorStop() {
  if (materialMonitorTimer) window.clearInterval(materialMonitorTimer);
  materialMonitorTimer = null;
  materialMonitorRefreshing = false;
}

async function materialMonitorView(force = false, selectedDate = "") {
  if (state.user?.mode !== "superadmin") return homeView();
  materialMonitorStop(); setPage("Análise de materiais");
  const today = new Intl.DateTimeFormat("en-CA", { timeZone: "America/Sao_Paulo" }).format(new Date());
  const day = selectedDate || sessionStorage.getItem("material-analysis-day") || today;
  const status = value => value === "correct" ? ["CORRETO", "good"] : value === "divergence" ? ["DIVERGÊNCIA", "bad"] : ["ANALISAR", ""];
  const render = data => {
    const rows = data.items || [], totals = rows.reduce((all, row) => { all[row.status] = (all[row.status] || 0) + 1; return all; }, {}), returnRows = rows.filter(row => row.connector_return?.required), returnUnits = returnRows.reduce((sum, row) => sum + Number(row.connector_return?.quantity || 0), 0), modemReturns = rows.filter(row => row.modem_history?.return_required);
    const card = item => { const [label, css] = status(item.status), modem = item.modem_history || {}, connector = item.connector_return || {}; return `<button class="card launched-modem-card material-analysis-card ${css}" data-order-id="${esc(String(item.os_id))}" data-technician="${esc(item.technician || "Não identificado")}" data-status="${esc(item.status || "needs_review")}" data-return="${connector.required ? Number(connector.quantity) : 0}"><header><div><small>OS #${number(item.os_id)} · ${label}</small><h2>${esc(item.client || "Cliente não informado")}</h2></div><b>${item.status === "correct" ? "✓" : "!"}</b></header><p><strong>${esc(item.service || "Serviço não informado")}</strong></p><p>Técnico: ${esc(item.technician || "Não identificado")}</p><p><strong>Esperado:</strong> ${esc((item.expected || []).map(rule => `${rule.minimum} ${rule.material}`).join(", ") || "Sem regra automática")}</p><p><strong>Lançado:</strong> ${esc((item.launched || []).map(material => `${material.quantity} ${material.name}${material.reference ? ` (${material.reference})` : ""}`).join(", ") || "Nenhum material")}</p>${modem.status && modem.status !== "not_applicable" ? `<p><strong>Histórico do modem:</strong> ${esc(modem.message || "Aguardando conferência")}</p>` : ""}${connector.required ? `<p class="bad"><strong>Devolver:</strong> ${number(connector.quantity)} conector(es) de corretiva.</p>` : ""}<small>${esc(item.reason || "Toque para abrir a análise completa")}</small></button>`; };
    const technicians = [...new Set(rows.map(item => item.technician || "Não identificado"))].sort((a, b) => a.localeCompare(b, "pt-BR"));
    $("#content").innerHTML = `<section class="hero"><div><small>CONTROLE DE MATERIAIS</small><h1>Análise diária de OS</h1><p>Compare o serviço encerrado, o lançamento do técnico e o contexto do cliente em um só lugar.</p></div><b>✓</b></section><section class="card toolbar"><label>Data das OS encerradas <input id="material-analysis-date" type="date" value="${esc(day)}"></label><label>Técnico <input id="material-analysis-technician" list="material-analysis-technicians" placeholder="Todos · digite um nome" autocomplete="off"><datalist id="material-analysis-technicians">${technicians.map(name => `<option value="${esc(name)}"></option>`).join("")}</datalist></label><button id="material-analysis-search" class="primary">Consultar OS</button><button id="material-analysis-refresh" class="secondary">Atualizar no SGP</button><a class="secondary button-link" href="#estoque">Voltar ao estoque</a></section><section class="metrics"><article class="card"><small>OS EXIBIDAS</small><b id="material-count-all">${number(data.closed_orders)}</b><p>${esc(data.date)}</p></article><article class="card good"><small>CORRETAS</small><b id="material-count-correct">${number(totals.correct)}</b><p>Regras atendidas</p></article><article class="card bad"><small>DIVERGÊNCIAS</small><b id="material-count-divergence">${number(totals.divergence)}</b><p>Material esperado ausente</p></article><article class="card"><small>EM ANÁLISE</small><b id="material-count-review">${number(totals.needs_review)}</b><p>Sem regra ou contexto pendente</p></article></section><section id="material-return-summary" class="card ${returnUnits ? "bad" : "good"}"><strong>Resumo de devolução:</strong> ${returnUnits ? `${number(returnUnits)} conector(es) em ${number(returnRows.length)} OS corretiva(s) devem retornar ao estoque.` : "Nenhum conector de corretiva pendente de devolução nesta data."}</section><section class="card"><strong>Como funciona:</strong> a regra de materiais é aplicada pelo sistema. ${data.ai_available ? "Ao abrir uma OS, a IA também lê as informações escritas pelo técnico e apresenta uma segunda análise." : "A análise detalhada permanece disponível sem IA."} Toque em um card para abrir o cliente, sinal, quedas e materiais.</section><section class="section-title"><div><small>OS ENCERRADAS</small><h2>Resultado da data selecionada</h2><p id="material-filter-result">${rows.length ? "Todos os serviços abaixo foram carregados para análise." : "Nenhuma OS encerrada encontrada nessa data."}</p></div></section><section class="launched-modem-list">${rows.map(card).join("") || '<article class="card">Nenhum serviço encerrado para esta data.</article>'}</section>`;
    const updateReturnSummary = selected => { const connectors = selected.reduce((sum, row) => sum + Number(row.connector_return?.required ? row.connector_return.quantity : 0), 0), connectorOrders = selected.filter(row => row.connector_return?.required).length, modems = selected.filter(row => row.modem_history?.return_required).length, summary = $("#material-return-summary"); summary.classList.toggle("bad", Boolean(connectors || modems)); summary.classList.toggle("good", !connectors && !modems); summary.innerHTML = `<strong>Resumo de devolução:</strong> ${number(connectors)} conector(es) em ${number(connectorOrders)} OS corretiva(s) e ${number(modems)} modem(ns) devem retornar ao estoque.`; };
    updateReturnSummary(rows);
    $("#material-analysis-search").onclick = () => { const value = $("#material-analysis-date").value; sessionStorage.setItem("material-analysis-day", value); materialMonitorView(false, value); };
    $("#material-analysis-refresh").onclick = () => materialMonitorView(true, $("#material-analysis-date").value);
    $("#material-analysis-technician").oninput = event => {
      const query = event.target.value.trim().toLocaleLowerCase("pt-BR"); let visible = 0, correct = 0, divergent = 0, review = 0, returns = 0, returnOrders = 0;
      document.querySelectorAll(".material-analysis-card").forEach(item => { const show = !query || item.dataset.technician.toLocaleLowerCase("pt-BR").includes(query); item.hidden = !show; if (!show) return; visible += 1; if (item.dataset.status === "correct") correct += 1; else if (item.dataset.status === "divergence") divergent += 1; else review += 1; const quantity = Number(item.dataset.return || 0); if (quantity) { returns += quantity; returnOrders += 1; } });
      $("#material-count-all").textContent = number(visible); $("#material-count-correct").textContent = number(correct); $("#material-count-divergence").textContent = number(divergent); $("#material-count-review").textContent = number(review); $("#material-filter-result").textContent = query ? `${visible} OS encontrada(s) para o filtro informado.` : "Todos os serviços abaixo foram carregados para análise."; updateReturnSummary(rows.filter(row => !query || String(row.technician || "Não identificado").toLocaleLowerCase("pt-BR").includes(query)));
    };
    document.querySelectorAll(".material-analysis-card").forEach(button => button.onclick = () => materialAnalysisDetailView(Number(button.dataset.orderId), data.date, data.ai_available, rows.find(row => String(row.os_id) === button.dataset.orderId)));
  };
  $("#content").innerHTML = '<section class="card loading">Consultando as OS encerradas e os materiais lançados...</section>';
  try { render(await api(`/api/superadmin/material-analysis?date=${encodeURIComponent(day)}${force ? "&refresh=1" : ""}`, { timeoutMs: 180000 })); }
  catch (error) { errorView(error); }
}

async function materialAnalysisDetailView(orderId, selectedDate, aiAvailable, summary = {}) {
  if (!Number.isSafeInteger(orderId) || orderId <= 0) { notify("Número de OS inválido. Atualize a lista e tente novamente."); return; }
  setPage(`Análise da OS #${orderId}`); loading("Carregando contexto do cliente e da OS...");
  try {
    const data = await api(`/api/superadmin/material-analysis/${orderId}`, { timeoutMs: 120000 }), order = data.order || {}, signal = data.signal || {}, outages = data.outages || {};
    $("#content").innerHTML = `<section class="card toolbar"><button id="material-analysis-back" class="secondary">← Voltar para a lista</button>${aiAvailable ? `<button id="material-ai-review" class="primary">Analisar atendimento com IA</button>` : ""}</section><section class="hero"><div><small>OS #${number(orderId)}</small><h1>${esc(order.client || "Cliente")}</h1><p>${esc(order.service || "Serviço não informado")} · Técnico: ${esc(order.technician || "Não identificado")}</p></div><b>⌁</b></section><section class="metrics"><article class="card"><small>SINAL ATUAL</small><b>${esc(signal.current ?? data.onu?.rx ?? "—")}</b><p>${signal.outside_average ? "Fora da média da PON" : "Comparação disponível quando houver PON"}</p></article><article class="card"><small>QUEDAS · 30 DIAS</small><b>${outages.total == null ? "—" : number(outages.total)}</b><p>${esc(outages.message || "Extrato de tráfego")}</p></article><article class="card"><small>GPON / ONU</small><b>${esc(data.onu?.gpon || "—")}</b><p>Contrato ${esc(order.contract_id || "—")}</p></article></section><section class="card"><small>INFORMAÇÕES DA OS</small><h2>${esc(order.performed || order.service || "—")}</h2><p>${esc(order.content || "Sem observação registrada.")}</p><p>${esc(order.address || "Endereço não informado")}</p></section><section class="section-title"><div><small>MATERIAIS LANÇADOS</small><h2>Itens associados à OS</h2></div></section><section class="launched-modem-list">${(data.materials || []).map(item => `<article class="card"><h2>${esc(item.produto || item.product || item.descricao || "Material")}</h2><b>${number(item.quantidade || item.quantity || 0)}</b><p>${esc(item.referencia || "Sem referência")}</p></article>`).join("") || '<article class="card">Nenhum material lançado nesta OS.</article>'}</section><section class="section-title"><div><small>HISTÓRICO DE QUEDAS</small><h2>Últimas desconexões</h2></div></section><section class="launched-modem-list">${(outages.items || []).slice(0, 10).map(item => `<article class="card"><strong>${esc(item.started_at || "")}</strong><p>${esc(item.duration || "Duração não informada")}</p></article>`).join("") || `<article class="card">${esc(outages.message || "Nenhuma queda encontrada no período.")}</article>`}</section>`;
    const evidence = document.createElement("section"); evidence.className = "card"; evidence.innerHTML = `<h2>Conferência dos materiais e devoluções</h2><p>${esc(summary.reason || "Confira os materiais e a análise da IA abaixo.")}</p><p>${esc(summary.modem_history?.message || "")}</p>${summary.modem_history?.old_gpon ? `<p>Modem para devolver: ${esc(summary.modem_history.old_gpon)}</p>` : ""}${summary.connector_return?.required ? `<p>Devolver ${number(summary.connector_return.quantity)} conector(es).</p>` : ""}${(data.warnings || []).map(warning => `<p class="bad">${esc(warning)}</p>`).join("")}`; $("#content").appendChild(evidence);
    $("#material-analysis-back").onclick = () => materialMonitorView(false, selectedDate);
    const ai = $("#material-ai-review"); if (ai) { const resultBox = document.createElement("section"); resultBox.id = "material-ai-result"; resultBox.className = "card"; resultBox.innerHTML = "<strong>Análise da IA</strong><p>Pronta para ler as informações do técnico. A consulta é iniciada somente ao tocar no botão.</p>"; ai.closest(".toolbar").insertAdjacentElement("afterend", resultBox); ai.onclick = async () => { ai.disabled = true; ai.textContent = "Analisando informações..."; resultBox.innerHTML = "<strong>Análise da IA</strong><p>Lendo serviço, observações, materiais e dados técnicos...</p>"; try { const force = ai.dataset.reviewed === "1" ? "&refresh=1" : ""; const result = await api(`/api/superadmin/material-analysis/${orderId}/ai?date=${encodeURIComponent(selectedDate)}${force}`, { method: "POST", timeoutMs: 180000 }), analysis = result.analysis || {}; ai.dataset.reviewed = "1"; resultBox.className = `card ${analysis.status === "correct" ? "good" : analysis.status === "divergence" ? "bad" : ""}`; resultBox.innerHTML = `<strong>${esc(analysis.summary || "Análise concluída")}</strong>${(analysis.findings || []).map(item => `<p>• ${esc(item)}</p>`).join("")}<p><strong>Ação recomendada:</strong> ${esc(analysis.recommended_action || "Revisão humana.")}</p>`; } catch (error) { resultBox.className = "card bad"; resultBox.innerHTML = `<strong>Não foi possível concluir a análise da IA</strong><p>${esc(error.message || "Tente novamente.")}</p>`; } finally { ai.disabled = false; ai.textContent = ai.dataset.reviewed === "1" ? "Analisar novamente com IA" : "Analisar atendimento com IA"; } }; }
  } catch (error) { errorView(error); }
}

async function logisticsAuditView(offset = 0) {
  if (state.user?.mode !== "superadmin") return homeView();
  setPage("Histórico de logística");
  loading();
  try {
    const data = await api(`/api/logistics/audit?offset=${Number(offset) || 0}`);
    $("#content").innerHTML = `<section class="card"><h1>Histórico de logística</h1><p>Registros das operações feitas pelo app a partir desta atualização. Alterações feitas diretamente no SGP não aparecem neste histórico.</p><a class="secondary button-link" href="#pendencias-estoque">Voltar às divergências</a></section>${data.items.map(item => `<article class="card"><small>${esc(new Date(item.created_at * 1000).toLocaleString('pt-BR'))} · ${esc(item.actor)}</small><h2>${esc(item.detail.action || item.detail.message || 'Análise de divergência')}</h2><details><summary>Ver dados registrados</summary><pre style="white-space:pre-wrap;overflow-wrap:anywhere">${esc(JSON.stringify(item.detail, null, 2))}</pre></details></article>`).join('') || '<section class="card">Nenhum registro ainda.</section>'}<div class="toolbar"><button id="audit-prev" class="secondary" ${offset ? '' : 'disabled'}>Mais recentes</button><button id="audit-next" class="secondary" ${data.more ? '' : 'disabled'}>Mais antigos</button></div>`;
    $("#audit-prev").onclick = () => logisticsAuditView(Math.max(0, offset - 100));
    $("#audit-next").onclick = () => logisticsAuditView(offset + 100);
  } catch (error) { errorView(error); }
}

async function logisticsCasesView() {
  if (!["technician", "superadmin"].includes(state.user?.mode)) return homeView();
  setPage("Divergências de materiais");
  loading();
  try {
    const data = await api("/api/logistics/cases");
    const supervisor = state.user.mode === "superadmin";
    const labels = { open: "Pendente", review: "Em análise", resolved: "Resolvido" };
    $("#content").innerHTML = `<section class="hero"><div><small>LOGÍSTICA INTEGRADA</small><h1>Divergências de materiais</h1><p>Registre diferenças de saldo, uso ou devolução. O histórico não altera automaticamente o SGP.</p></div></section><div class="toolbar"><a class="secondary button-link" href="${supervisor ? '#estoque' : '#home'}">Voltar</a>${supervisor ? '<a class="secondary button-link" href="#conferencia-saldo">Conferir saldo</a><a class="secondary button-link" href="#materiais-usados">Conectores usados</a>' : ''}</div><form id="logistics-new" class="card"><h2>Informar divergência</h2><label>Material, OS ou estoque<input name="subject" required minlength="3" maxlength="160"></label><label>O que precisa ser conferido?<textarea name="message" required minlength="5" maxlength="2000" placeholder="Informe a data, quantidade e o saldo esperado. Não envie senhas."></textarea></label><button class="primary">Enviar para análise</button><p class="logistics-result" role="status"></p></form><section class="launched-modem-list">${(data.items || []).map(item => `<article class="card"><small>${esc(labels[item.status] || item.status)} · ${esc(item.owner)}</small><h2>${esc(item.subject)}</h2><details><summary>Ver histórico (${item.events.length})</summary>${item.events.map(entry => `<p><strong>${esc(entry.actor)}</strong> · ${esc(new Date(entry.created_at * 1000).toLocaleString('pt-BR'))}<br>${esc(entry.message || '')}</p>`).join('')}</details><form data-case="${esc(item.id)}"><label>Resposta / justificativa<textarea name="message" required minlength="5" maxlength="2000"></textarea></label>${supervisor ? `<label>Situação<select name="status"><option value="review">Em análise</option><option value="resolved">Resolvido</option><option value="open">Pendente</option></select></label>` : ''}<button class="secondary">${supervisor ? 'Salvar análise' : 'Enviar informação / reabrir'}</button><p class="logistics-result" role="status"></p></form></article>`).join('') || '<article class="card">Nenhuma divergência registrada.</article>'}</section>`;
    if (supervisor) $("#content .toolbar").insertAdjacentHTML("beforeend", '<a class="secondary button-link" href="#historico-logistica">Histórico de operações</a>');
    $("#content").querySelectorAll("form").forEach(form => form.onsubmit = async event => {
      event.preventDefault();
      const button = form.querySelector("button");
      button.disabled = true;
      try {
        await api(`/api/logistics/cases${form.dataset.case ? '/' + encodeURIComponent(form.dataset.case) : ''}`, { method: "POST", body: JSON.stringify(Object.fromEntries(new FormData(form))) });
        await logisticsCasesView();
      } catch (error) { form.querySelector('.logistics-result').textContent = error.message; }
      finally { button.disabled = false; }
    });
  } catch (error) { errorView(error); }
}

async function stockReconciliationView() {
  if (state.user?.mode !== "superadmin") return homeView();
  setPage("Conferência de saldo");
  if (!state.stockLocations) {
    loading("Carregando estoques...");
    try { state.stockLocations = await api("/api/admin/stock"); }
    catch (error) { return errorView(error); }
  }
  const locations = state.stockLocations?.locations || [];
  $("#content").innerHTML = `<section class="hero"><div><small>LOGÍSTICA DE MATERIAIS</small><h1>Conferência de saldo</h1><p>Saldo às 06h, entradas e saídas até a última consulta.</p></div><b>▦</b></section><form id="stock-reconciliation-form" class="card toolbar"><label>Estoque<select id="stock-reconciliation-location" required><option value="">Escolha um estoque</option>${locations.map(item => `<option value="${Number(item.id)}">${esc(item.descricao)}</option>`).join("")}</select></label><button class="primary">Consultar e conferir</button><button id="stock-reconciliation-general" type="button" class="secondary">Consulta geral</button><button id="stock-reconciliation-saved" type="button" class="secondary">Última consulta salva</button><a class="secondary button-link" href="#estoque">Voltar</a></form><section id="stock-reconciliation-result" class="card empty">Escolha um estoque ou use a consulta geral. Os resultados ficam salvos por até 4 dias.</section>`;
  const target = $("#stock-reconciliation-result");
  const quantity = value => value == null ? "—" : number(value);
  const timestamp = value => value ? new Date(value * 1000).toLocaleString("pt-BR") : "—";
  const flowLabels = { entries: "Entrada", returns: "Devolução", uses: "Saída para cliente", transfers_out: "Transferência de saída", review: "A conferir" };
  const flowClasses = { entries: "stock-flow-in", returns: "stock-flow-return", uses: "stock-flow-out", transfers_out: "stock-flow-transfer", review: "stock-flow-review" };
  const detailBlock = (data, saved = false) => {
    const openingStatus = data.opening_unknown ? "Abertura não identificada" : data.opening_calculated ? "Abertura calculada" : "Abertura baseada em saldo anterior";
    const note = data.opening_unknown ? "Não foi possível calcular o saldo das 06h com segurança. Confira os movimentos pendentes." : data.opening_calculated ? "Saldo das 06h calculado a partir do saldo atual e de todos os movimentos encontrados; não é uma leitura histórica direta do SGP e não comprova ausência de diferenças." : "Saldo das 06h reconstruído a partir de uma consulta anterior ao dia.";
    const items = data.items || [];
    const movements = data.movements || [];
    return `<section class="card"><h2>${esc(data.location || "Estoque")}</h2><p>Das ${esc(timestamp(data.baseline_at))} até ${esc(timestamp(data.observed_at))} · ${number(movements.length)} movimentação(ões)${saved ? " · consulta salva" : ""}</p><strong>${esc(openingStatus)}</strong><p>${esc(note)}</p></section>${data.incomplete ? `<section class="card"><h2>Conferência incompleta</h2><p>${esc(data.message || "Há movimentações não identificadas.")}</p></section>` : ""}<section class="card removal-table-scroll"><table class="removal-table stock-reconciliation-table"><thead><tr><th>Material</th><th>Saldo 06h</th><th>Entradas</th><th>Devoluções</th><th>Usos</th><th>Transferências</th><th>Esperado</th><th>Atual</th><th>Diferença</th></tr></thead><tbody>${items.map(item => `<tr><td>${esc(item.product)}</td><td>${quantity(item.initial)}</td><td class="stock-flow-in">+${number(item.entries)}</td><td class="stock-flow-return">+${number(item.returns)}</td><td class="stock-flow-out">−${number(item.uses)}</td><td class="stock-flow-transfer">−${number(item.transfers_out)}</td><td>${quantity(item.expected)}</td><td>${number(item.current)}</td><td class="${item.difference ? "stock-flow-out" : ""}">${quantity(item.difference)}</td></tr>`).join("") || '<tr><td colspan="9">Nenhum material encontrado.</td></tr>'}</tbody></table></section><section class="section-title"><div><h2>Movimentações</h2><p>Entradas em verde, devoluções em azul, saídas em vermelho e transferências em laranja.</p></div></section><section class="stock-movement-list">${movements.map(item => `<article class="card stock-movement ${flowClasses[item.flow] || "stock-flow-review"}"><header><strong>${esc(item.product || "Material")}</strong><b>${esc(flowLabels[item.flow] || flowLabels.review)} · ${number(item.quantity)}</b></header><p>${esc(item.origin || "Origem não informada")} → ${esc(item.destination || "Destino não informado")}</p><small>${esc(item.at || "")} · ${esc(item.movement_type || "")}</small></article>`).join("") || '<article class="card">Nenhuma movimentação no período.</article>'}</section>${(data.unmatched_movements || []).length ? `<section class="card"><h2>A conferir (${number(data.unmatched_movements.length)})</h2><p>Estes lançamentos não foram classificados automaticamente; o saldo pode não fechar.</p></section>` : ""}`;
  };
  const renderDetail = (data, saved = false) => {
    target.className = "stock-reconciliation-result";
    target.innerHTML = `${data.outdated ? '<section class="card"><strong>Consulta histórica: houve alterações de estoque no app após esta leitura. Clique em Consultar e conferir para atualizar.</strong></section>' : ""}<a class="secondary button-link" href="#pendencias-estoque">Registrar divergência</a>${detailBlock(data, saved)}`;
  };
  const renderGeneral = async data => {
    target.className = "stock-reconciliation-result";
    const label = item => item.error ? "Falha na consulta" : item.incomplete ? "Abertura pendente" : item.opening_unknown ? "Abertura desconhecida" : item.opening_calculated ? "Abertura calculada" : item.balanced ? "Saldo conferido" : "Verificar diferenças";
    const needsDetails = (data.items || []).filter(item => !item.error && !item.detail);
    if (needsDetails.length) {
      target.innerHTML = '<section class="card loading">Abrindo todos os detalhes salvos...</section>';
      const details = await Promise.all(needsDetails.map(item => api(`/api/superadmin/stock-reconciliation?location=${encodeURIComponent(item.id)}&saved=1`, { concurrent: true }).catch(error => ({ error: error.message }))));
      needsDetails.forEach((item, index) => { item.detail = details[index]; });
    }
    const cards = (data.items || []).map(item => item.error || item.detail?.error ? `<section class="card bad"><h2>${esc(item.location || "Estoque")}</h2><p>${esc(item.error || item.detail.error)}</p></section>` : `<article class="stock-general-detail"><header class="card"><small>${esc(label(item))}</small><h2>${esc(item.location || "Estoque")}</h2><p>${number(item.movement_count || 0)} movimentação(ões) · ${number(item.differences || 0)} diferença(s) · ${number(item.pending || 0)} pendente(s)</p></header>${detailBlock(item.detail, true)}</article>`).join("") || '<article class="card">Nenhum estoque encontrado.</article>';
    target.innerHTML = `${data.outdated ? '<section class="card"><strong>Houve alterações após a consulta salva. Faça uma nova consulta geral para atualizar.</strong></section>' : ""}<section class="card"><h2>Consulta geral completa</h2><p>${number(data.total)} estoque(s) consultado(s) em ${esc(timestamp(data.observed_at))}. Abaixo estão todos os detalhes já carregados; não é preciso abrir técnico por técnico.</p></section>${cards}`;
  };
  $("#stock-reconciliation-form").onsubmit = async event => {
    event.preventDefault();
    const button = event.currentTarget.querySelector("button");
    button.disabled = true;
    target.className = "card loading";
    target.textContent = "Consultando saldo e movimentações no SGP...";
    try {
      const data = await api(`/api/superadmin/stock-reconciliation?location=${encodeURIComponent($("#stock-reconciliation-location").value)}`, { timeoutMs: 180000 });
      renderDetail(data);
    } catch (error) { target.className = "card empty"; target.textContent = `Conferência não concluída: ${error.message}`; }
    finally { button.disabled = false; }
  };
  $("#stock-reconciliation-general").onclick = async () => {
    const button = $("#stock-reconciliation-general");
    button.disabled = true;
    target.className = "card loading";
    target.textContent = "Consultando todos os estoques no SGP. Isso pode levar alguns minutos...";
    try { await renderGeneral(await api("/api/superadmin/stock-reconciliation/general", { timeoutMs: 600000 })); }
    catch (error) { target.className = "card empty"; target.textContent = `Consulta geral não concluída: ${error.message}`; }
    finally { button.disabled = false; }
  };
  $("#stock-reconciliation-saved").onclick = async () => {
    target.className = "card loading";
    target.textContent = "Carregando última consulta salva...";
    try { await renderGeneral(await api("/api/superadmin/stock-reconciliation/general?saved=1")); }
    catch (error) { target.className = "card empty"; target.textContent = `Nenhuma consulta salva disponível: ${error.message}`; }
  };
}

async function inventoryOnuCheck(gpon, target) {
  target.textContent = "Consultando ONU ID no SGP...";
  try {
    const result = await api(`/api/superadmin/inventory/onu?gpon=${encodeURIComponent(gpon)}`, { timeoutMs: 70000 });
    target.innerHTML = `<strong>${result.installed ? "Já cadastrado como ONU" : "Nenhuma ONU vinculada encontrada"}</strong><br><small>ONU ID consultado: ${esc(result.searched_onu_id)}</small>${(result.items || []).map(item => `<p>Contrato ${esc(item.contract_id || "—")} · ${esc(item.client || "Cliente não informado")} · ${esc(item.olt || "OLT não informada")}</p>`).join("")}`;
    return true;
  } catch (error) { target.textContent = `Conferência não concluída: ${error.message}`; return false; }
}

async function inventoryCheckAll(button) {
  const card = button.closest(".inventory-card");
  const rows = [...card.querySelectorAll(".inventory-reference-row")];
  let completed = 0, failed = 0, consecutiveFailures = 0;
  button.disabled = true;
  try {
    for (const row of rows) {
      if (!card.isConnected) break;
      button.textContent = `Verificando ${completed + failed + 1}/${rows.length}...`;
      const ok = await inventoryOnuCheck(row.querySelector(".inventory-check").dataset.gpon, row.querySelector(".inventory-check-result"));
      if (ok) { completed++; consecutiveFailures = 0; }
      else { failed++; consecutiveFailures++; }
      if (consecutiveFailures >= 3) break;
      await new Promise(resolve => setTimeout(resolve, 250));
    }
  } finally {
    button.disabled = false;
    button.textContent = "Verificar vínculo em todos";
    const status = card.querySelector(".inventory-check-all-status");
    if (status) status.textContent = `${completed} conferido(s)${failed ? ` · ${failed} falha(s)${consecutiveFailures >= 3 ? "; interrompido após três falhas seguidas" : "; tente novamente"}` : ""}`;
  }
}

function inventoryGponView() {
  setPage("Conferir GPON");
  $("#content").innerHTML = `<section class="hero"><div><small>ONU ID</small><h1>Conferir GPON</h1><p>ZTEGD, ZTEGC e HWTC são convertidos para o número lido pela OLT.</p></div><b>◉</b></section><form id="inventory-gpon-form" class="card toolbar"><label>GPON / serial<input id="inventory-gpon" required autocomplete="off" placeholder="Ex.: ZTEGD42B013E"></label><button class="primary">Verificar vínculo</button><a class="secondary button-link" href="#estoque">Voltar</a></form><section id="inventory-gpon-result" class="card stock-intro">Informe uma referência para consultar.</section>`;
  $("#inventory-gpon-form").onsubmit = event => { event.preventDefault(); inventoryOnuCheck($("#inventory-gpon").value, $("#inventory-gpon-result")); };
}

async function inventoryBalanceView(_locationId = "", refresh = false) {
  setPage("Saldo disponível");
  loading("Consultando saldo disponível no SGP...");
  try {
    const params = new URLSearchParams();
    if (refresh) params.set("refresh", "1");
    const data = await api(`/api/superadmin/inventory?${params}`, { timeoutMs: 90000 });
    const items = data.items || [];
    const normalize = value => String(value || "").normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase();
    const locations = [...new Set(items.map(item => String(item.location || "").trim()).filter(Boolean))].sort((a, b) => a.localeCompare(b, "pt-BR"));
    const categories = [...new Set(items.map(item => String(item.category || "Material").trim()).filter(Boolean))].sort((a, b) => a.localeCompare(b, "pt-BR"));
    $("#content").innerHTML = `<section class="hero"><div><small>SALDO DO SGP</small><h1>Materiais disponíveis</h1><p>${items.length} tipos carregados. Os filtros abaixo não fazem nova consulta.</p></div><b>▦</b></section>
      <section class="card inventory-filters"><div class="inventory-filter-grid"><label class="inventory-location-field">Local de estoque<input id="inventory-location-search" type="search" autocomplete="off" placeholder="Digite o nome do estoque"><span id="inventory-location-suggestions" class="inventory-suggestions hidden"></span></label><label>Categoria<select id="inventory-category"><option value="">Todas as categorias</option>${categories.map(category => `<option value="${esc(category)}">${esc(category)}</option>`).join("")}</select></label><label>Material<input id="inventory-material-search" type="search" placeholder="Pesquisar material ou GPON"></label></div><div class="inventory-filter-actions"><button id="inventory-clear" class="secondary">Limpar filtros</button><button id="inventory-refresh" class="secondary">Atualizar saldo no SGP</button><a class="secondary button-link" href="#estoque">Voltar</a></div></section>
      <div class="inventory-list-head"><strong id="inventory-count"></strong><span id="inventory-total"></span></div><section id="inventory-list" class="inventory-list"></section>`;
    const render = () => {
      const location = normalize($("#inventory-location-search").value.trim());
      const category = normalize($("#inventory-category").value);
      const material = normalize($("#inventory-material-search").value.trim());
      const filtered = items.filter(item => (!location || normalize(item.location).includes(location))
        && (!category || normalize(item.category || "Material") === category)
        && (!material || normalize(item.description).includes(material) || (item.references || []).some(ref => normalize(ref.referencia).includes(material))));
      $("#inventory-count").textContent = `${filtered.length} ${filtered.length === 1 ? "material encontrado" : "materiais encontrados"}`;
      $("#inventory-total").textContent = `${number(filtered.reduce((sum, item) => sum + Number(item.quantity || 0), 0))} unidades no saldo exibido`;
      $("#inventory-list").innerHTML = filtered.map(item => `<article class="card inventory-card">${materialThumbnail(item.description, item.category)}<div class="inventory-card-main"><div class="inventory-card-tags"><span>${esc(item.category || "Material")}</span><span>${esc(item.location || "Estoque")}</span></div><h3>${esc(item.description)}</h3><p>${(item.references || []).length ? `${item.references.length} referência(s) cadastrada(s)` : "Sem referência individual"}</p></div><div class="inventory-card-quantity"><small>SALDO</small><strong>${number(item.quantity)}</strong></div>${(item.references || []).length ? `<details class="inventory-card-references"><summary>Ver referências <span>⌄</span></summary><div>${item.references.map(ref => `<section class="inventory-reference-row"><code>${esc(ref.referencia)}</code><div class="inventory-reference-actions"><button class="secondary inventory-check" data-gpon="${esc(ref.referencia)}">Verificar vínculo</button>${ref.id ? `<a class="secondary button-link" target="_blank" rel="noopener noreferrer" href="https://foxfibrasp.sgp.net.br/admin/djadmin/?extra=estoque2/produtoreferenciaupdate/${Number(ref.id)}/change">Editar no SGP</a>` : ""}</div><span class="inventory-check-result"></span></section>`).join("")}</div></details>` : ""}</article>`).join("") || '<article class="card stock-intro">Nenhum material corresponde aos filtros. Tente outra busca ou limpe os filtros.</article>';
      document.querySelectorAll(".inventory-card-references").forEach(details => {
        const toolbar = document.createElement("div");
        toolbar.className = "inventory-check-all-toolbar";
        toolbar.innerHTML = '<button type="button" class="secondary inventory-check-all">Verificar vínculo em todos</button><span class="inventory-check-all-status"></span>';
        details.querySelector("div").prepend(toolbar);
      });
      document.querySelectorAll(".inventory-check").forEach(button => button.onclick = () => inventoryOnuCheck(button.dataset.gpon, button.closest(".inventory-reference-row").querySelector(".inventory-check-result")));
      document.querySelectorAll(".inventory-check-all").forEach(button => button.onclick = () => inventoryCheckAll(button));
    };
    const suggestions = $("#inventory-location-suggestions");
    $("#inventory-location-search").oninput = event => {
      const query = normalize(event.target.value.trim());
      const matches = query.length >= 2 ? locations.filter(name => normalize(name).includes(query)).slice(0, 8) : [];
      suggestions.innerHTML = matches.map(name => `<button type="button" data-location="${esc(name)}">${esc(name)}</button>`).join("");
      suggestions.classList.toggle("hidden", !matches.length);
      suggestions.querySelectorAll("button").forEach(button => button.onclick = () => {
        $("#inventory-location-search").value = button.dataset.location;
        suggestions.classList.add("hidden"); render();
      });
      render();
    };
    $("#inventory-location-search").onblur = () => window.setTimeout(() => suggestions.classList.add("hidden"), 150);
    $("#inventory-category").onchange = render;
    $("#inventory-material-search").oninput = render;
    $("#inventory-clear").onclick = () => { $("#inventory-location-search").value = ""; $("#inventory-category").value = ""; $("#inventory-material-search").value = ""; suggestions.classList.add("hidden"); render(); };
    $("#inventory-refresh").onclick = () => inventoryBalanceView("", true);
    render();
  } catch (error) { errorView(error); }
}

async function modemExchangeView(selectedDate = localDateValue(), force = false) {
  if (state.user?.mode !== "superadmin") return homeView();
  setPage("Análise de OS");
  $("#content").innerHTML = `<section class="hero"><div><small>MODEM DE TROCA E RETIRADA</small><h1>Análise de OS</h1><p>OS encerradas e agendadas para a data escolhida.</p></div><b>⇄</b></section><form id="exchange-filter" class="card report-date-filter"><label>Data das OS<input id="exchange-date" type="date" value="${esc(selectedDate)}" max="${localDateValue()}" required></label><button class="primary">Analisar OS</button><a class="secondary button-link" href="#relatorios">Voltar aos relatórios</a></form><div id="exchange-results"><section class="card loading">Consultando OS encerradas e históricos dos clientes...</section></div>`;
  $("#exchange-filter").onsubmit = event => { event.preventDefault(); modemExchangeView($("#exchange-date").value, true); };
  try {
    const data = await api(`/api/superadmin/modem-exchanges?date=${encodeURIComponent(selectedDate)}${force ? "&refresh=1" : ""}`, { timeoutMs: 300000 });
    const results = $("#exchange-results");
    if (!results) return;
    const label = item => item.return_type === "troca" ? "Troca confirmada" : item.return_type === "retirada" ? "Retirada confirmada" : "Sem modem movimentado";
    results.innerHTML = `<section class="metrics exchange-metrics"><article class="card"><small>OS ENCERRADAS</small><b>${number(data.total)}</b></article><article class="card bad"><small>MODEM DE TROCA</small><b>${number(data.exchanges)}</b></article><article class="card good"><small>RETIRADAS</small><b>${number(data.removals)}</b></article><article class="card"><small>SEM MODIFICAÇÃO</small><b>${number(data.without_modem_event)}</b></article></section><section class="section-title"><div><h2>Resultado de ${esc(new Date(`${data.date}T12:00:00`).toLocaleDateString("pt-BR"))}</h2><p>Troca: desvinculou e vinculou outro GPON. Retirada: desvinculou e não vinculou outro GPON depois.</p></div><button id="refresh-exchanges" class="secondary">Atualizar análise</button></section><section class="exchange-list">${(data.items || []).map(item => `<article class="card exchange-card ${esc(item.return_type || "unchanged")}"><header><div><small>OS #${number(item.os_id)} · ${esc(item.technician)}</small><h2>${esc(item.client)}</h2></div><b>${label(item)}</b></header><p>${esc(item.reason)}</p>${item.return_type ? `<dl><div><dt>GPON desvinculado</dt><dd>${esc(item.old_gpon)}</dd></div>${item.return_type === "troca" ? `<div><dt>Novo GPON</dt><dd>${esc(item.new_gpon)}</dd></div>` : `<div><dt>Situação</dt><dd>Nenhum novo GPON vinculado</dd></div>`}<div><dt>Registro</dt><dd>${esc(formatOrderDate(item.evidence_at))}</dd></div></dl>` : ""}<a class="secondary button-link" href="#ordem/${esc(item.os_id)}">Abrir OS</a></article>`).join("") || '<section class="card empty"><h3>Nenhuma OS encontrada</h3><p>Não há OS encerrada com agendamento nesta data.</p></section>'}</section>`;
    $("#refresh-exchanges")?.addEventListener("click", () => modemExchangeView(selectedDate, true));
  } catch (error) {
    const results = $("#exchange-results");
    if (results) results.innerHTML = `<section class="card empty"><h3>Não foi possível concluir a análise</h3><p>${esc(error.message)}</p></section>`;
  }
}

async function loadMyRemovalCount() {
  const box = $("#my-removal-summary");
  if (!box) return;
  try {
    const data = await api("/api/removals/mine");
    if (!document.body.contains(box)) return;
    const manual = data.manual || { connectors: 0, modems: [], note: "" };
    const connectorTotal = Number(data.connector_total || 0) + Number(manual.connectors || 0);
    box.classList.toggle("has-removals", Number(data.total_modems) > 0 || connectorTotal > 0);
    box.querySelector("h2").textContent = data.message;
    box.querySelector("p").textContent = "Quantidade confirmada e ainda presente na lista de retiradas.";
    box.querySelector(":scope > b").textContent = number(Number(data.total_modems || 0) + connectorTotal);
    const automaticModems = (data.modems || []).map(modem => `
      <article><code>${esc(modem.gpon)}</code><b class="return-type ${esc(modem.return_type || "retirada")}">${esc(modem.return_type || "Retirada")}</b><span>Desvinculado em ${esc(formatOrderDate(modem.unlinked_at))}${modem.client ? ` · ${esc(modem.client)}` : ""}</span></article>
    `).join("");
    const manualModems = (manual.modems || []).map(value => { const modem = typeof value === "object" ? value : { gpon: value, type: "troca" }; return `<article class="manual"><code>${esc(modem.gpon)}</code><b class="return-type ${esc(modem.type)}">${esc(modem.type)}</b><span>Devolução manual</span></article>`; }).join("");
    const automaticConnectors = (data.connector_returns || []).map(item => `<article class="connectors"><code>${number(item.quantity)} ${esc(item.product)}</code><span>${esc(item.client || "Cliente")} · ${esc(formatOrderDate(item.used_at))}</span>${item.note ? `<span>${esc(item.note)}</span>` : ""}</article>`).join("");
    const modemItems = `${automaticModems}${manualModems}` || '<small>Nenhum modem pendente.</small>';
    const connectorItems = `${Number(manual.connectors) ? `<article class="connectors"><code>${number(manual.connectors)} CONECTOR(ES)</code><span>Quantidade manual para devolver</span></article>` : ""}${automaticConnectors}` || '<small>Nenhum conector pendente.</small>';
    const payment = data.payment || { count: 0, amount: 0, closes_at: "" };
    const closesAt = payment.closes_at ? new Date(payment.closes_at) : null;
    const closesLabel = closesAt && !Number.isNaN(closesAt.getTime()) ? `${closesAt.toLocaleDateString("pt-BR", { day: "2-digit", month: "2-digit" })} às ${closesAt.toLocaleTimeString("pt-BR", { hour: "2-digit", minute: "2-digit" }).replace(":", "h")}` : "—";
    box.querySelector(".my-removal-gpons").innerHTML = `<section class="tech-payment-summary"><div><small>APARELHOS ENTREGUES</small><b>${number(payment.count)}</b></div><div><small>VALOR A RECEBER</small><b>${money(payment.amount)}</b></div><div><small>CONTAGEM DURA ATÉ</small><b>${esc(closesLabel)}</b></div></section><section class="tech-return-group modem-group"><header><i>▣</i><div><small>MODENS PARA ENTREGAR</small><b>${number(data.total_modems || 0)}</b></div></header><div>${modemItems}</div></section><section class="tech-return-group connector-group"><header><i>⌁</i><div><small>CONECTORES PARA ENTREGAR</small><b>${number(connectorTotal)}</b></div></header><div>${connectorItems}</div></section>${manual.note ? `<p class="return-note">${esc(manual.note)}</p>` : ""}`;
  } catch (error) {
    if (error.name !== "AbortError" && document.body.contains(box)) {
      box.querySelector("h2").textContent = "Não foi possível abrir a contagem agora.";
      box.querySelector("p").textContent = "Tente atualizar a página mais tarde.";
    }
  }
}

function stockCards(stock) {
  const items = stock.items || [];
  return `<section class="inventory-list">${items.map(item => `
    <article class="card inventory-card">
      ${materialThumbnail(item.description, item.category)}
      <div class="inventory-card-main">
      <small>CÓD. ${esc(item.code || item.product_id || "—")}</small>
      <h3>${esc(item.description || "Material")}</h3>
      <p>${(item.references || []).length ? `${item.references.length} referência(s)` : "Controle por quantidade"}</p>
      </div>
      <div class="inventory-card-quantity"><small>SALDO</small><strong>${number(item.quantity)}</strong></div>
      ${(item.references || []).length ? `<details class="inventory-card-references"><summary>Ver referências</summary><div class="serials">${item.references.map(ref => `<code>${esc(ref.referencia)}</code>`).join("")}</div></details>` : ""}
    </article>`).join("") || '<div class="card empty">Nenhum material disponível.</div>'}</section>`;
}

function loadTesseract() {
  if (window.Tesseract) return Promise.resolve(window.Tesseract);
  if (tesseractLoader) return tesseractLoader;
  tesseractLoader = new Promise((resolve, reject) => {
    const script = document.createElement("script");
    script.src = "https://cdn.jsdelivr.net/npm/tesseract.js@5/dist/tesseract.min.js";
    script.onload = () => resolve(window.Tesseract);
    script.onerror = () => reject(new Error("Não foi possível carregar o leitor. Preencha o GPON manualmente."));
    document.head.appendChild(script);
  });
  return tesseractLoader;
}

function cleanGpon(value) {
  return String(value || "").toUpperCase().replace(/[^A-Z0-9]/g, "");
}

function purchaseGpons() {
  const values = String($("#purchase-gpon")?.value || "").split(/[\s,;]+/).map(cleanGpon).filter(Boolean);
  return [...new Set(values)];
}

function cleanMac(value) {
  return String(value || "").toUpperCase().replace(/[^0-9A-F]/g, "");
}

async function prepareLabelImage(file) {
  if (!window.createImageBitmap) return file;
  const bitmap = await createImageBitmap(file);
  const scale = Math.min(1, 2200 / Math.max(bitmap.width, bitmap.height));
  const width = Math.max(1, Math.round(bitmap.width * scale));
  const height = Math.max(1, Math.round(bitmap.height * scale));
  const source = document.createElement("canvas");
  source.width = width; source.height = height;
  const sourceContext = source.getContext("2d", { willReadFrequently: true });
  sourceContext.drawImage(bitmap, 0, 0, width, height);
  bitmap.close?.();

  const pixels = sourceContext.getImageData(0, 0, width, height);
  let top = 0, bottom = height - 1;
  const brightRows = [];
  for (let y = 0; y < height; y += 4) {
    let bright = 0;
    for (let x = 0; x < width; x += 4) {
      const offset = (y * width + x) * 4;
      const light = (pixels.data[offset] * 0.299) + (pixels.data[offset + 1] * 0.587) + (pixels.data[offset + 2] * 0.114);
      if (light > 145) bright += 1;
    }
    if (bright > (width / 4) * 0.12) brightRows.push(y);
  }
  if (brightRows.length > 3) {
    top = Math.max(0, brightRows[0] - 30);
    bottom = Math.min(height - 1, brightRows[brightRows.length - 1] + 30);
  }
  const croppedHeight = bottom - top + 1;
  const canvas = document.createElement("canvas");
  canvas.width = width; canvas.height = croppedHeight;
  const context = canvas.getContext("2d", { willReadFrequently: true });
  context.drawImage(source, 0, top, width, croppedHeight, 0, 0, width, croppedHeight);
  const image = context.getImageData(0, 0, width, croppedHeight);
  for (let index = 0; index < image.data.length; index += 4) {
    const gray = (image.data[index] * 0.299) + (image.data[index + 1] * 0.587) + (image.data[index + 2] * 0.114);
    const contrast = Math.max(0, Math.min(255, ((gray - 128) * 1.45) + 128));
    image.data[index] = contrast;
    image.data[index + 1] = contrast;
    image.data[index + 2] = contrast;
  }
  context.putImageData(image, 0, 0);
  return await new Promise(resolve => canvas.toBlob(blob => resolve(blob || file), "image/jpeg", 0.94));
}

function referencesFromBarcodes(values) {
  const rawValues = [...new Set((values || []).map(value => String(value || "").toUpperCase().trim()).filter(Boolean))];
  const entries = rawValues.map(raw => ({
    raw,
    value: cleanGpon(raw.replace(/^\s*(?:GPON|SN|S\/N|N\/S|MAC)\s*[:#=-]?\s*/, "")),
  })).filter(item => item.value.length >= 8 && item.value.length <= 24);
  let macEntry = entries.find(item => /^[0-9A-F]{12}$/.test(item.value));
  let mac = macEntry?.value || "";
  if (!mac) {
    for (const entry of entries) {
      const match = entry.raw.match(/(?:^|MAC\s*[:#=-]?\s*)([0-9A-F]{12})(?=[^0-9A-F]|$)/);
      if (match) { mac = match[1]; macEntry = entry; break; }
    }
  }
  const gpon = entries.find(item => item !== macEntry && /^[0-9A-F]{16}$/.test(item.value))?.value
    || entries.find(item => item !== macEntry && item.value.length <= 16 && /\d/.test(item.value) && /[G-Z]/.test(item.value))?.value
    || "";
  return { gpon, mac };
}

async function readLabelBarcodes(file) {
  if (!window.BarcodeDetector || !window.createImageBitmap) return { gpon: "", mac: "" };
  try {
    const supported = await BarcodeDetector.getSupportedFormats();
    const formats = ["code_128", "code_39", "codabar"].filter(format => supported.includes(format));
    if (!formats.length) return { gpon: "", mac: "" };
    const detector = new BarcodeDetector({ formats });
    const bitmap = await createImageBitmap(file);
    const detected = await detector.detect(bitmap);
    bitmap.close?.();
    return referencesFromBarcodes(detected.map(item => item.rawValue || ""));
  } catch {
    return { gpon: "", mac: "" };
  }
}

function extractModemReferences(text) {
  const upper = String(text || "").toUpperCase();
  const lines = upper.split(/\r?\n/).map(line => line.trim()).filter(Boolean);
  const valueAfterLabel = (label, parser) => {
    for (let index = 0; index < lines.length; index += 1) {
      const match = lines[index].match(label);
      if (!match) continue;
      const sameLine = lines[index].slice((match.index || 0) + match[0].length)
        .split(/\b(?:MAC|GPON|PON\s*(?:SN|S\/N|N\/S)|S\/N|N\/S|SN)\b/)[0];
      const parsed = parser(sameLine);
      if (parsed) return parsed;
      // Algumas etiquetas imprimem o nome do campo e o valor na linha seguinte.
      const nextLine = lines[index + 1] || "";
      const nextParsed = parser(nextLine);
      if (nextParsed && !/(?:GPON|S\/N|N\/S|\bSN\b|\bMAC\b)/.test(nextLine)) return nextParsed;
    }
    return "";
  };
  const parseMac = value => {
    const tail = String(value || "").replace(/^\s*(?:ADDRESS|ADDR)?\s*[:#=-]?\s*/, "");
    const separated = tail.match(/(?:[0-9A-F]{2}[:.\-\s]){5}[0-9A-F]{2}/);
    if (separated) return cleanMac(separated[0]);
    const compact = tail.match(/(?:^|[^0-9A-F])([0-9A-F]{12})(?![0-9A-F])/);
    return compact ? cleanMac(compact[1]) : "";
  };
  const parseGpon = value => {
    const tail = String(value || "")
      .replace(/^\s*(?:NO|NUMBER|NUMERO)?\s*[:#=-]?\s*/, "");
    const candidates = tail.match(/[A-Z0-9]{8,24}/g) || [];
    return cleanGpon(candidates.find(candidate => /\d/.test(candidate)) || "");
  };
  const mac = valueAfterLabel(/\b(?:WAN\s+)?MAC(?:\s+(?:ADDRESS|ADDR))?\b/, parseMac);
  const gpon = valueAfterLabel(
    /\b(?:GPON(?:\s*(?:SN|S\/N|N\/S))?|PON\s*(?:SN|S\/N|N\/S)|S\/N|N\/S|SN)\b/,
    parseGpon,
  );
  return { gpon, mac };
}

async function hashPhoto(file) {
  const digest = await crypto.subtle.digest("SHA-256", await file.arrayBuffer());
  return [...new Uint8Array(digest)].map(value => value.toString(16).padStart(2, "0")).join("");
}

async function modemPurchaseView(loadOptions = true) {
  setPage("Comprar modem");
  if (!loadOptions) {
    $("#content").innerHTML = `
      <section class="hero"><div><small>ENTRADA DE EQUIPAMENTO</small><h1>Comprar modem</h1><p>Nenhuma consulta foi realizada. Você pode digitar o GPON ou usar uma foto.</p></div><b>⊕</b></section>
      <section class="card page-intro"><b>⊕</b><h2>Preparar uma nova compra</h2><p>Ao continuar, o sistema consultará uma única vez os modelos, a empresa, o fornecedor e o estoque de destino.</p><button id="start-modem-purchase" class="primary">Iniciar compra</button></section>`;
    $("#start-modem-purchase").onclick = () => modemPurchaseView(true);
    return;
  }
  loading("Carregando modelos e estoque de destino...");
  try {
    modemPurchaseOptions = modemPurchaseOptions || await api("/api/superadmin/modem-purchase/options");
    const data = modemPurchaseOptions;
    modemPhotoHash = "";
    $("#content").innerHTML = `
      <section class="hero"><div><small>ENTRADA RÁPIDA</small><h1>Comprar modem pela foto</h1><p>A foto é lida no próprio celular e não é enviada para serviços externos.</p></div><b>⊕</b></section>
      <form id="modem-purchase-form" class="modem-purchase-layout">
        <section class="card modem-camera">
          <small>1 · FOTO DA ETIQUETA</small><h2>Fotografe o GPON/SN</h2>
          <label class="camera-button">📷 Tirar ou escolher foto (opcional)<input id="modem-photo" type="file" accept="image/jpeg,image/png,image/webp" capture="environment"></label>
          <img id="modem-photo-preview" class="hidden" alt="Pré-visualização da etiqueta">
          <button id="read-modem-label" class="secondary" type="button" disabled>Ler GPON/SN</button>
          <p id="ocr-progress" class="muted">Selecione uma foto para começar.</p>
        </section>
        <section class="card modem-purchase-fields">
          <small>2 · CONFERÊNCIA</small><h2>Revise antes de comprar</h2>
          <label>Modelo do modem *<select id="purchase-product" required><option value="">Selecione o modelo</option>${(data.products || []).map(item => {
            const name = String(item.descricao || "");
            const normalized = name.normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase();
            const label = /4\s*antenas|quatro\s+antenas/.test(normalized)
              ? `Modem 4 antenas · ${name}`
              : /modem\s+basico|zte\s+basica/.test(normalized)
                ? `Modem básico · ${name}`
                : name;
            return `<option value="${item.id}">${esc(label)}</option>`;
          }).join("")}</select></label>
          <label>GPONs ou SNs *<textarea id="purchase-gpon" rows="6" autocomplete="off" autocapitalize="characters" required placeholder="Um por linha&#10;ZTEGD42B013E&#10;HWTC387910A6"></textarea><small id="purchase-count">0 modens informados · máximo 50</small></label>
          <div class="purchase-destination"><span>Destino</span><strong>${esc(data.stock?.descricao || "Empresa")}</strong><small>Fornecedor: ${esc(data.supplier?.nome || "SG FIBRA")}</small></div>
          <p id="modem-purchase-error" class="error"></p>
          <button id="confirm-modem-purchase" class="primary">Confirmar compra no SGP</button>
        </section>
      </form>`;
    const fileInput = $("#modem-photo"), readButton = $("#read-modem-label"), submit = $("#confirm-modem-purchase");
    const validate = () => {
      const gpons = purchaseGpons();
      const ready = Boolean($("#purchase-product").value && gpons.length
        && gpons.length <= 50 && gpons.every(value => value.length >= 8));
      const counter = $("#purchase-count");
      if (counter) counter.textContent = `${gpons.length} ${gpons.length === 1 ? "modem informado" : "modens informados"} · máximo 50`;
      submit.dataset.ready = ready ? "1" : "0";
      return ready;
    };
    fileInput.onchange = async () => {
      const file = fileInput.files[0]; if (!file) return;
      $("#modem-photo-preview").src = URL.createObjectURL(file); $("#modem-photo-preview").classList.remove("hidden");
      $("#ocr-progress").textContent = "Foto pronta. Toque em “Ler GPON/SN”.";
      modemPhotoHash = await hashPhoto(file); readButton.disabled = false; validate();
    };
    readButton.onclick = async () => {
      const file = fileInput.files[0]; if (!file) return;
      readButton.disabled = true; $("#ocr-progress").textContent = "Lendo a etiqueta no aparelho...";
      try {
        const tesseract = await loadTesseract();
        $("#ocr-progress").textContent = "Lendo o código de barras do GPON/SN...";
        const barcodeRefs = await readLabelBarcodes(file);
        let refs = barcodeRefs;
        if (!refs.gpon) {
          $("#ocr-progress").textContent = "Preparando e destacando a etiqueta...";
          const labelImage = await prepareLabelImage(file);
          const result = await tesseract.recognize(labelImage, "eng", { logger: event => { if (event.status === "recognizing text") $("#ocr-progress").textContent = `Lendo etiqueta: ${Math.round((event.progress || 0) * 100)}%`; } });
          const ocrRefs = extractModemReferences(result.data?.text || "");
          refs = { gpon: barcodeRefs.gpon || ocrRefs.gpon, mac: barcodeRefs.mac || ocrRefs.mac };
        }
        if (refs.gpon) {
          const current = purchaseGpons();
          if (!current.includes(refs.gpon)) current.push(refs.gpon);
          $("#purchase-gpon").value = current.join("\n");
        }
        $("#ocr-progress").textContent = refs.gpon ? "Leitura concluída. Confira o GPON/SN." : "Não foi possível ler. Aponte a câmera para o GPON/SN ou digite manualmente.";
      } catch (error) { $("#ocr-progress").textContent = error.message; }
      finally { readButton.disabled = false; validate(); }
    };
    ["purchase-product", "purchase-gpon"].forEach(id => $(`#${id}`).addEventListener("input", validate));
    $("#modem-purchase-form").onsubmit = async event => {
      event.preventDefault();
      if (!$("#purchase-product").value) {
        $("#modem-purchase-error").textContent = "Selecione o modelo do modem.";
        $("#purchase-product").focus();
        return;
      }
      const gpons = purchaseGpons();
      if (!gpons.length || gpons.length > 50 || gpons.some(value => value.length < 8)) {
        $("#modem-purchase-error").textContent = "Confira os GPONs ou SNs. Informe um por linha, no máximo 50.";
        $("#purchase-gpon").focus();
        return;
      }
      const product = data.products.find(item => String(item.id) === $("#purchase-product").value);
      if (!confirm(`Comprar ${gpons.length} ${product?.descricao || "modem"}?\n\nGPONs:\n${gpons.join("\n")}\n\nDestino: Empresa`)) return;
      submit.disabled = true; submit.textContent = "Conferindo GPONs no SGP...";
      $("#modem-purchase-error").textContent = "";
      try {
        const payload = { product_id: Number(product.id), gpons, photo_hash: modemPhotoHash };
        let result;
        try {
          result = await api("/api/superadmin/modem-purchase", { method: "POST", body: JSON.stringify(payload) });
        } catch (firstError) {
          if (firstError.status !== 409 || !/confirme o retorno/i.test(firstError.message)) throw firstError;
          if (!confirm(`${firstError.message}\n\nOs aparelhos existentes serão devolvidos ao estoque Empresa. Deseja confirmar?`)) {
            throw new Error("Retorno cancelado. Nenhuma alteração foi feita.");
          }
          result = await api("/api/superadmin/modem-purchase", {
            method: "POST", body: JSON.stringify({ ...payload, confirm_return: true }),
          });
        }
        notify(result.notice || "Modem comprado");
        $("#content").innerHTML = `<section class="card purchase-success"><b>✓</b><h1>${esc(result.notice || "Operação concluída")}</h1><p>${esc(product.descricao)}</p>${result.returned_gpons?.length ? `<h2>Devolvidos</h2><div class="purchase-gpon-list">${result.returned_gpons.map(gpon => `<code>${esc(gpon)}</code>`).join("")}</div>` : ""}${result.purchased_gpons?.length ? `<h2>Comprados</h2><div class="purchase-gpon-list">${result.purchased_gpons.map(gpon => `<code>${esc(gpon)}</code>`).join("")}</div>` : ""}${result.already_company_gpons?.length ? `<h2>Já no saldo da Empresa</h2><div class="purchase-gpon-list">${result.already_company_gpons.map(gpon => `<code>${esc(gpon)}</code>`).join("")}</div>` : ""}${!result.returned_gpons && !result.purchased_gpons ? `<div class="purchase-gpon-list">${gpons.map(gpon => `<code>${esc(gpon)}</code>`).join("")}</div>` : ""}<small>Saldo Empresa</small><button id="purchase-another" class="primary">Fazer outra compra</button></section>`;
        $("#purchase-another").onclick = modemPurchaseView;
      } catch (error) { $("#modem-purchase-error").textContent = error.message; submit.disabled = false; submit.textContent = "Confirmar compra no SGP"; }
    };
  } catch (error) { errorView(error); }
}

async function personalStock(force = false) {
  if (state.user?.mode !== "technician") return homeView();
  setPage("Meus materiais");
  if (!force) loading("Consultando seus materiais...");
  try {
    const stock = await api(`/api/stock${force ? "?refresh=1" : ""}`);
    $("#content").innerHTML = `
      <section class="hero"><div><small>ESTOQUE INDIVIDUAL</small><h1>${esc(stock.location?.descricao || "Meu saldo")}</h1><p>Consulta feita somente ao abrir ou tocar em atualizar.</p></div><b>${number((stock.items || []).reduce((sum, item) => sum + Number(item.quantity || 0), 0))}</b></section>
      <div class="section-title"><div><h2>Materiais disponíveis</h2><span>${(stock.items || []).length} tipos</span></div><button id="refresh-stock" class="secondary">Atualizar agora</button></div>
      <section class="card"><a class="secondary button-link" href="#pendencias-estoque">Informar divergência / acompanhar resposta</a><p>Se o saldo não bater, informe o material, a quantidade e a data para análise do supervisor.</p></section>${stockCards(stock)}`;
    $("#refresh-stock").onclick = () => personalStock(true);
  } catch (error) { errorView(error); }
}

function replenishCards(stock) {
  const replenishment = stock.replenishment || [];
  const usage = stock.usage_today || [];
  return `
    <div class="section-title"><div><h2>Reposição necessária</h2><span>${replenishment.length} itens abaixo do padrão</span></div></div>
    <section class="replenish-grid">${replenishment.map(item => `
      <article class="card replenish"><small>REPOR</small><h3>${esc(item.name)}</h3><b>+${number(item.missing)}</b><span>Atual ${number(item.current)} · padrão ${number(item.target)}</span></article>
    `).join("") || '<div class="card success">✓ Saldo dentro dos padrões.</div>'}</section>
    <div class="section-title"><div><h2>Consumo de hoje</h2><span>${number(usage.reduce((sum, item) => sum + Number(item.quantity || 0), 0))} unidades</span></div></div>
    <section class="card usage">${usage.map(item => `
      <details><summary><strong>${esc(item.name)}</strong><b>${number(item.quantity)}</b></summary>
      ${(item.entries || []).map(entry => `<p>OS #${esc(entry.os_id)} · ${esc(entry.client || "Cliente")} · ${esc(entry.usage || "uso")} · ${number(entry.quantity)} un.</p>`).join("")}</details>
    `).join("") || '<div class="empty">Nenhum material utilizado hoje.</div>'}</section>`;
}

async function adminStock(locationId = "", force = false) {
  loading();
  try {
    if (!state.stockLocations || force) state.stockLocations = await api(`/api/admin/stock${force ? "?refresh=1" : ""}`);
    const locations = state.stockLocations.locations || [];
    const selected = String(locationId || "");
    const stock = selected ? await api(`/api/admin/stock?location=${encodeURIComponent(selected)}${force ? "&refresh=1" : ""}`) : { items: [] };
    $("#content").innerHTML = `
      <section class="hero"><div><small>SALDO DA EQUIPE</small><h1>${esc(stock.location?.descricao || "Selecione um estoque")}</h1><p>Nenhuma atualização automática está ativa.</p></div><b>${number((stock.items || []).reduce((sum, item) => sum + Number(item.quantity || 0), 0))}</b></section>
      <section class="card toolbar"><label>Técnico ou estoque<select id="stock-location"><option value="">Escolha para consultar...</option>${locations.map(item => `<option value="${item.id}" ${String(item.id) === selected ? "selected" : ""}>${esc(item.descricao)}</option>`).join("")}</select></label>${selected ? '<div class="toolbar-actions"><button id="replenish-stock" class="primary">+ Repor material</button><button id="refresh-stock" class="secondary">Atualizar agora</button></div>' : ""}</section>
      ${selected ? `${replenishCards(stock)}<div class="section-title"><div><h2>Saldo completo</h2><span>${(stock.items || []).length} tipos</span></div></div>${stockCards(stock)}` : '<section class="card stock-intro"><b>▦</b><h2>Nenhum saldo consultado</h2><p>Escolha um técnico ou estoque acima. A consulta ao saldo só será realizada depois da sua escolha.</p></section>'}`;
    $("#stock-location").onchange = event => adminStock(event.target.value);
    if (selected) {
      $("#refresh-stock").onclick = () => adminStock(selected, true);
      $("#replenish-stock").onclick = () => openTransfer(selected, stock.location?.descricao || "Destino");
    }
  } catch (error) { errorView(error); }
}

function highlighted(value, query) {
  const text = String(value || ""), needle = String(query || "").trim();
  if (!needle) return esc(text);
  const index = text.toLocaleLowerCase("pt-BR").indexOf(needle.toLocaleLowerCase("pt-BR"));
  if (index < 0) return esc(text);
  return `${esc(text.slice(0, index))}<mark>${esc(text.slice(index, index + needle.length))}</mark>${esc(text.slice(index + needle.length))}`;
}

function renderTransferItems(query = "") {
  const box = $("#transfer-items");
  const needle = query.trim().toLocaleLowerCase("pt-BR");
  const items = transferData?.items || [];
  const matches = needle ? items.reduce((count, item) => count
    + (String(item.description || "").toLocaleLowerCase("pt-BR").includes(needle) ? 1 : 0)
    + (item.references || []).filter(ref => String(ref.referencia || "").toLocaleLowerCase("pt-BR").includes(needle)).length, 0) : 0;
  box.innerHTML = `${needle ? `<p class="transfer-search-result">${matches ? `${matches} resultado(s) destacado(s)` : "Nenhum GPON ou material encontrado"}</p>` : ""}${items.map(item => {
    const serialized = (item.references || []).length > 0;
    const recommended = Math.min(Number(item.recommended_quantity) || 1, Number(item.quantity) || 1);
    return `<article class="transfer-item" data-product="${item.product_id}">
      <input type="checkbox" data-transfer-check>
      <div><strong>${highlighted(item.description, query)}</strong><small>Precisa repor ${number(item.recommended_quantity)} · ${number(item.quantity)} disponível na Empresa</small></div>
      <label>Quantidade<input data-transfer-quantity type="number" min="1" max="${Number(item.quantity) || 1}" value="${recommended}" ${serialized ? "readonly" : ""}></label>
      ${serialized ? `<div class="transfer-serials"><small>Selecione os seriais que serão enviados</small>${item.references.map(ref => `<label><input type="checkbox" data-transfer-ref value="${esc(ref.referencia)}"> <code>${highlighted(ref.referencia, query)}</code></label>`).join("")}</div>` : ""}
    </article>`;
  }).join("") || '<div class="empty">Nenhum dos materiais que precisam de reposição está disponível no estoque Empresa.</div>'}`;
  box.querySelectorAll("[data-transfer-ref]").forEach(input => input.onchange = () => {
    const row = input.closest(".transfer-item");
    const quantity = row.querySelectorAll("[data-transfer-ref]:checked").length;
    row.querySelector("[data-transfer-quantity]").value = Math.max(1, quantity);
    row.querySelector("[data-transfer-check]").checked = quantity > 0;
  });
  if (needle) box.querySelector("mark")?.scrollIntoView({ block: "center", behavior: "smooth" });
}

async function loadTransferOrigin(origin, destination) {
  $("#transfer-items").innerHTML = '<div class="loading inline"><span>Consultando o estoque de origem uma única vez...</span></div>';
  try {
    transferData = await api(`/api/admin/transfer?origin=${encodeURIComponent(origin)}&destination=${encodeURIComponent(destination)}`);
    if (transferData.endpoint_test_mode) {
      $("#confirm-transfer").textContent = "Testar formulário administrativo";
      $("#transfer-test-note").classList.remove("hidden");
    }
    renderTransferItems();
  } catch (error) {
    if (error.name !== "AbortError") $("#transfer-items").innerHTML = `<div class="empty">${esc(error.message)}</div>`;
  }
}

async function openTransfer(destination, destinationName) {
  const company = state.stockLocations?.company_stock;
  if (!company?.id) return notify("O estoque Empresa não foi localizado no SGP.");
  if (String(company.id) === String(destination)) return notify("Selecione o estoque de um técnico para fazer a reposição.");
  $("#transfer-dialog")?.remove();
  const dialog = document.createElement("dialog");
  dialog.id = "transfer-dialog";
  dialog.className = "transfer-dialog";
  dialog.innerHTML = `<form id="transfer-form" method="dialog">
    <button type="button" class="dialog-close" aria-label="Fechar">×</button>
    <small>TRANSFERÊNCIA DE MATERIAL</small><h2>Repor saldo</h2>
    <p>O material sairá de <b>${esc(company.descricao || "Empresa")}</b> e entrará em <b>${esc(destinationName)}</b>.</p>
    <p id="transfer-test-note" class="return-note hidden">Modo de teste: valida o formulário administrativo, mas não movimenta material.</p>
    <label>Pesquisar GPON ou material<input id="transfer-search" type="search" placeholder="Digite o GPON/serial para localizar"></label>
    <label>Observação<textarea id="transfer-observation" rows="2" maxlength="1000" placeholder="Ex.: Reposição de material"></textarea></label>
    <div id="transfer-items" class="transfer-items"></div>
    <p id="transfer-error" class="error"></p>
    <button id="confirm-transfer" class="primary" type="submit">Confirmar transferência</button>
  </form>`;
  document.body.appendChild(dialog);
  dialog.querySelector(".dialog-close").onclick = () => dialog.close();
  $("#transfer-search").oninput = event => renderTransferItems(event.target.value);
  $("#transfer-form").onsubmit = event => submitTransfer(event, destination, destinationName);
  dialog.showModal();
  await loadTransferOrigin(company.id, destination);
}

async function submitTransfer(event, destination, destinationName) {
  event.preventDefault();
  const rows = [...document.querySelectorAll(".transfer-item")].filter(row => row.querySelector("[data-transfer-check]").checked);
  const items = rows.map(row => ({
    product_id: Number(row.dataset.product),
    quantity: Number(row.querySelector("[data-transfer-quantity]").value),
    references: [...row.querySelectorAll("[data-transfer-ref]:checked")].map(input => input.value),
    annotation: "Reposição de material",
  }));
  if (!items.length) {
    $("#transfer-error").textContent = "Selecione pelo menos um material.";
    return;
  }
  const testMode = transferData?.endpoint_test_mode === true;
  if (!confirm(testMode
    ? `Testar o formulário administrativo com ${items.length} material(is) para ${destinationName}? Nenhum material será movimentado.`
    : `Confirmar ${items.length} material(is) para ${destinationName}? Esta ação movimentará o estoque real no SGP.`)) return;
  const button = $("#confirm-transfer");
  button.disabled = true;
  try {
    const result = await api("/api/admin/transfer", {
      method: "POST",
      body: JSON.stringify({
        origin: Number(state.stockLocations.company_stock.id),
        destination: Number(destination),
        observation: $("#transfer-observation").value,
        items,
      }),
    });
    $("#transfer-dialog").close();
    notify(result.notice || (result.test_only ? "Teste concluído" : "Transferência concluída"));
    state.stockLocations = null;
    await adminStock(String(destination), true);
  } catch (error) {
    if (error.name !== "AbortError") $("#transfer-error").textContent = error.message;
  } finally { button.disabled = false; }
}

async function stockView(loadStock = false) {
  setPage("Saldo de materiais");
  if (!loadStock) {
    const technician = state.user.mode === "technician";
    $("#content").innerHTML = `
      <section class="hero"><div><small>ESTOQUE</small><h1>${technician ? "Meu saldo de materiais" : "Saldo da equipe"}</h1><p>Nenhum estoque foi consultado ao abrir esta página.</p></div><b>▦</b></section>
      <section class="card page-intro"><b>▦</b><h2>${technician ? "Consultar meu saldo" : "Escolher um técnico ou estoque"}</h2><p>${technician ? "A consulta carregará somente os materiais disponíveis no seu usuário." : "Primeiro carregaremos apenas a lista de estoques. O saldo será consultado somente depois que você escolher um deles."}</p><button id="start-stock" class="primary">${technician ? "Consultar meu saldo" : "Escolher estoque"}</button></section>`;
    $("#start-stock").onclick = () => stockView(true);
    return;
  }
  if (["admin", "superadmin"].includes(state.user.mode)) return adminStock();
  return personalStock();
}

function typeRows(items) {
  return (items || []).map(item => `<div class="metric-row"><span>${esc(item.name)}</span><b>${number(item.count)}</b></div>`).join("") || '<p class="empty">Sem registros.</p>';
}

function repeatBlock(report) {
  const repeated = report.repeated || {};
  const count = Number(repeated.month_count ?? repeated.count ?? 0);
  if (!count) return '<section class="card repeat-success"><b>★</b><div><h3>Parabéns!</h3><p>Este mês não houve ordens repetidas.</p></div></section>';
  return `<section class="card repeat"><div><small>REPETIDAS</small><b>${count}</b></div><div><small>PONTOS PERDIDOS</small><b>-${number(repeated.month_lost_points ?? repeated.lost_points)}</b></div></section>`;
}

const scoreRules = [
  ["Instalação de KIT", 2], ["Mudança de endereço", 1.5],
  ["Remoção de KIT", 0.5], ["Corretiva", 1],
  ["Instalação de TV", 1], ["Mudança de plano", 0.5],
  ["Reativação de cliente cancelado", 2],
  ["Rede mesh (repetidor)", 1],
];

const rewardTiers = [
  ["90 a 120 pontos", "R$ 500,00"], ["121 a 150 pontos", "R$ 700,00"],
  ["151 a 180 pontos", "R$ 900,00"], ["181 a 210 pontos", "R$ 1.100,00"],
  ["Acima de 210 pontos", "R$ 1.800,00"],
];

function scoreReward(points) {
  const value = Number(points || 0);
  if (value > 210) return "R$ 1.800,00";
  if (value >= 181) return "R$ 1.100,00";
  if (value >= 151) return "R$ 900,00";
  if (value >= 121) return "R$ 700,00";
  if (value >= 90) return "R$ 500,00";
  return "Faixa ainda não atingida";
}

function scoreGuide() {
  return `<section class="score-guide">
    <article class="card score-guide-card"><header><small>PONTOS POR SERVIÇO</small><h2>Quanto vale cada OS</h2></header><div class="score-rule-grid">${scoreRules.map(([name, points]) => `<div><span>${esc(name)}</span><b>${number(points)} pt</b></div>`).join("")}</div></article>
    <article class="card score-guide-card reward-card"><header><small>FAIXAS DE PREMIAÇÃO</small><h2>Valor por pontuação</h2></header><div class="reward-tier-grid">${rewardTiers.map(([range, reward]) => `<div><span>${esc(range)}</span><b>${esc(reward)}</b></div>`).join("")}</div><p>As repetidas permanecem em análise e o total definitivo é contabilizado no fechamento do mês.</p></article>
  </section>`;
}

function currentMonthValue() {
  const now = new Date();
  return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}`;
}

async function teamScoreView(technician = "", force = false, month = currentMonthValue(), prefetched = null) {
  const general = technician === "__all__";
  if (!state.stockLocations || force) {
    loading("Carregando a lista de técnicos...");
    try { state.stockLocations = await api(`/api/admin/stock${force ? "?refresh=1" : ""}`); }
    catch (error) { return errorView(error); }
  }
  const locations = state.stockLocations?.locations || [];
  if (!technician) {
    scoreData = null;
    $("#content").innerHTML = `
      <section class="hero score-hero"><div><small>PONTUAÇÃO</small><h1>Pontuação da equipe</h1><p>Escolha um técnico ou consulte a pontuação de toda a equipe.</p></div><b>★</b></section>
      <section class="card toolbar"><label>Técnico<select id="score-technician"><option value="">Escolha para consultar...</option>${locations.map(item => `<option value="${esc(item.descricao)}">${esc(item.descricao)}</option>`).join("")}</select></label><label>Mês<input id="score-month" type="month" value="${esc(month)}" max="${currentMonthValue()}"></label><button id="general-score" class="primary">Consulta geral</button></section>
      <section class="card stock-intro"><b>★</b><h2>Nenhum cálculo realizado</h2><p>A consulta de ordens, pontos e repetidas só começará depois que você escolher um técnico e o mês.</p></section>`;
    $("#score-technician").onchange = event => teamScoreView(event.target.value, false, $("#score-month").value);
    $("#general-score").onclick = () => teamScoreView("__all__", false, $("#score-month").value);
    return;
  }
  loading(`Calculando a pontuação de ${general ? "todos os técnicos" : technician} em ${month.split("-").reverse().join("/")}...`);
  try {
    const query = new URLSearchParams(general ? { general: "1", month } : { technician, month });
    if (force) query.set("refresh", "1");
    const data = prefetched || await api(`/api/superadmin/scores?${query}`, { timeoutMs: 300000 });
    scoreData = { ...data, requested_technician: technician };
    const technicians = data.technicians || [];
    const total = technicians.reduce((sum, item) => sum + Number(item.points || 0), 0);
    const repeats = technicians.reduce((sum, item) => sum + Number(item.repeated?.count || 0), 0);
    $("#content").innerHTML = `
      <section class="hero score-hero"><div><small>${general ? "CONSULTA GERAL" : "PONTUAÇÃO INDIVIDUAL"}</small><h1>${general ? "Pontuação de todos os técnicos" : esc(technicians[0]?.name || technician)}</h1><p>${esc(data.start || "")} até ${esc(data.end || "")} · consulta sob demanda</p></div><b>${number(total)}</b></section>
      <section class="card toolbar"><label>Técnico<select id="score-technician"><option value="">Escolha para consultar...</option>${locations.map(item => `<option value="${esc(item.descricao)}" ${item.descricao === technician ? "selected" : ""}>${esc(item.descricao)}</option>`).join("")}</select></label><label>Mês<input id="score-month" type="month" value="${esc(data.month || month)}" max="${currentMonthValue()}"></label><div><strong>${number(technicians.reduce((sum, item) => sum + Number(item.closed_orders || 0), 0))} serviços</strong><small> · ${repeats} repetida(s)</small></div><button id="general-score" class="primary">Consulta geral</button><button id="refresh-score" class="secondary">Atualizar agora</button></section>
      <section class="team-scores">${technicians.map((item, index) => {
        const repeated = item.repeated || {};
        const details = repeated.details || [];
        return `<article class="card team-score-row">
          <span class="position">${index + 1}</span>
          <div><strong>${esc(item.name)}</strong><small>${number(item.closed_orders)} OS encerradas</small></div>
          <div><small>PONTOS</small><b>${number(item.points)}</b></div>
          <div><small>REPETIDAS</small><b>${details.length || number(repeated.count)}</b></div>
          <div class="score-row-actions"><button class="secondary open-score-report" data-technician-index="${index}">Relatório</button><button class="secondary open-repeat-audit" data-technician-index="${index}" ${details.length ? "" : "disabled"}>${details.length ? "Repetidas" : "Sem repetidas"}</button></div>
        </article>`;
      }).join("") || '<section class="card empty">Nenhuma pontuação encontrada.</section>'}</section>`;
    $("#score-technician").onchange = event => teamScoreView(event.target.value, false, $("#score-month").value);
    $("#score-month").onchange = event => teamScoreView(general ? "__all__" : $("#score-technician").value, false, event.target.value);
    $("#general-score").onclick = () => teamScoreView("__all__", false, $("#score-month").value);
    $("#refresh-score").onclick = () => teamScoreView(technician, true, month);
    document.querySelectorAll(".open-score-report").forEach(button => button.onclick = () => openScoreReport(Number(button.dataset.technicianIndex)));
    document.querySelectorAll(".open-repeat-audit").forEach(button => button.onclick = () => openRepeatAudit(Number(button.dataset.technicianIndex)));
  } catch (error) { if (force) notify(error.message); else errorView(error); }
}

function repeatDate(value) {
  if (!value) return "Não informado";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? String(value) : parsed.toLocaleString("pt-BR");
}

function openScoreReport(index) {
  const technician = scoreData?.technicians?.[index];
  if (!technician) return notify("Relatório não encontrado.");
  $("#score-report-dialog")?.remove();
  const types = technician.types || [], history = technician.history || [], repeated = technician.repeated || {};
  const lost = Number(repeated.lost_points || 0), refunded = Number(repeated.refunded_points || 0);
  const dialog = document.createElement("dialog");
  dialog.id = "score-report-dialog"; dialog.className = "score-report-dialog";
  dialog.innerHTML = `<form method="dialog"><button type="button" class="dialog-close" aria-label="Fechar">×</button><div class="score-report">
    <small>COMPROVANTE DE PONTUAÇÃO</small><h2>${esc(technician.name)}</h2><p class="muted">${esc(scoreData.start || "")} até ${esc(scoreData.end || "")}</p>
    <section class="score-proof-totals"><div><small>SERVIÇOS</small><b>${number(technician.closed_orders)}</b></div><div><small>PONTOS BRUTOS</small><b>${number(technician.gross_points)}</b></div><div><small>DESCONTOS</small><b>-${number(lost)}</b></div><div><small>TOTAL FINAL</small><b>${number(technician.points)}</b></div></section>
    <h3>Cálculo por tipo de serviço</h3><section class="score-type-proof">${types.map(item => `<article><strong>${esc(item.name)}</strong><span>${number(item.count)} × ${number(item.unit_points)} pt</span><b>${number(item.points)} pt</b></article>`).join("") || '<p class="empty">Nenhum serviço pontuado.</p>'}</section>
    <div class="score-equation"><span>${number(technician.gross_points)} pontos brutos</span><span>− ${number(lost)} em repetidas</span>${refunded ? `<span>(${number(refunded)} já devolvido)</span>` : ""}<b>= ${number(technician.points)} pontos</b></div>
    <h3>Ordens que comprovam a pontuação</h3><section class="score-order-proof">${history.map(order => `<article><div><strong>OS #${esc(order.id || "—")} · ${esc(order.client || "Cliente não informado")}</strong><small>${esc(repeatDate(order.closed_at))}${order.client_id ? ` · Cliente ID ${esc(order.client_id)}` : ""}</small></div><span>${esc(order.type || "Outros")}</span><b>${number(order.points)} pt</b></article>`).join("") || '<p class="empty">Nenhuma ordem encontrada.</p>'}</section>
    <p class="score-proof-note">O total considera somente ordens encerradas no período e desconta as repetidas não estornadas.</p>
  </div></form>`;
  document.body.appendChild(dialog); dialog.querySelector(".dialog-close").onclick = () => dialog.close();
  dialog.addEventListener("close", () => dialog.remove()); dialog.showModal();
}

function openRepeatAudit(index, readOnly = state.user?.mode === "technician") {
  const technician = scoreData?.technicians?.[index];
  const details = technician?.repeated?.details || [];
  if (!technician || !details.length) return notify("Este técnico não possui repetidas para auditar.");
  $("#repeat-audit-dialog")?.remove();
  const dialog = document.createElement("dialog");
  dialog.id = "repeat-audit-dialog";
  dialog.className = "repeat-audit-dialog";
  dialog.innerHTML = `<form method="dialog">
    <button type="button" class="dialog-close" aria-label="Fechar">×</button>
    <small>AUDITORIA DE PONTUAÇÃO</small>
    <h2>Repetidas de ${esc(technician.name)}</h2>
    <section class="repeat-explanation">
      <h3>Por que esses pontos foram descontados?</h3>
      <p>Uma ordem é considerada repetida quando o cliente recebe novos atendimentos técnicos no mesmo mês. O sistema acompanha também a terceira, quarta e demais visitas, identificando quem encerrou cada uma.</p>
      <p>Se o mesmo técnico voltou, os dois atendimentos ficam sem pontuar até você escolher qual OS merece o ponto. Se outro técnico realizou a repetida, ele pontua normalmente e somente a primeira OS pode ser devolvida.</p>
    </section>
    <div class="repeat-audit-list">${details.map((detail, detailIndex) => `<article class="repeat-audit ${detail.refunded ? "refunded" : ""}">
      <header><div><small>CLIENTE ${detail.client_id ? `· ID ${esc(detail.client_id)}` : ""}</small><h3>${esc(detail.client || "Cliente não informado")}</h3></div><b>-${number(detail.points_deducted ?? detail.points_lost)} pt</b></header>
      <div class="repeat-score-choices">
        ${detail.original_suspended ? `<label class="repeat-score-choice ${detail.restored_order_ids?.map(Number).includes(Number(detail.original_order_id)) ? "chosen" : ""}"><input type="checkbox" data-refund data-technician="${esc(technician.name)}" data-original="${esc(detail.original_order_id)}" data-repeat="${esc(detail.repeat_order_id)}" data-restored="${esc(detail.original_order_id)}" ${detail.refunded || readOnly ? "disabled" : ""}><span><small>${detail.refunded && detail.restored_order_ids?.map(Number).includes(Number(detail.original_order_id)) ? "PONTUAÇÃO DEVOLVIDA" : readOnly ? "NÃO SELECIONADA" : "SELECIONE PARA PONTUAR"}</small><b>OS #${esc(detail.original_order_id)}</b><em>${number(detail.original_points)} pt · primeiro atendimento</em></span></label>` : ""}
        ${detail.same_technician ? `<label class="repeat-score-choice ${detail.restored_order_ids?.map(Number).includes(Number(detail.repeat_order_id)) ? "chosen" : ""}"><input type="checkbox" data-refund data-technician="${esc(technician.name)}" data-original="${esc(detail.original_order_id)}" data-repeat="${esc(detail.repeat_order_id)}" data-restored="${esc(detail.repeat_order_id)}" ${detail.refunded || readOnly ? "disabled" : ""}><span><small>${detail.refunded && detail.restored_order_ids?.map(Number).includes(Number(detail.repeat_order_id)) ? "PONTUAÇÃO DEVOLVIDA" : readOnly ? "NÃO SELECIONADA" : "SELECIONE PARA PONTUAR"}</small><b>OS #${esc(detail.repeat_order_id)}</b><em>${number(detail.repeat_points)} pt · repetida pelo mesmo técnico</em></span></label>` : ""}
      </div>
      <div class="repeat-comparison">
        <section><small>PRIMEIRO ATENDIMENTO</small><h4>OS #${esc(detail.original_order_id)} · ${esc(detail.original_type || detail.original_reason || "Serviço")}</h4><p><b>Data:</b> ${esc(repeatDate(detail.original_date))}</p><p><b>Técnico:</b> ${esc(detail.original_technician || technician.name)}</p><p><b>Motivo:</b> ${esc(detail.original_reason || "Não informado")}</p><p><b>Serviço prestado:</b> ${esc(detail.original_service_performed || "Não informado")}</p></section>
        <section><small>ORDEM REPETIDA</small><h4>OS #${esc(detail.repeat_order_id)} · ${esc(detail.repeat_type || detail.repeat_reason || "Serviço")}</h4><p><b>Data:</b> ${esc(repeatDate(detail.repeat_date))}</p><p><b>Técnico:</b> ${esc(detail.repeat_technician || "Não informado")}</p><p><b>Motivo:</b> ${esc(detail.repeat_reason || "Não informado")}</p><p><b>Serviço prestado:</b> ${esc(detail.repeat_service_performed || "Não informado")}</p></section>
      </div>
      <footer>${readOnly ? (detail.same_technician ? "Verde significa que a OS foi aprovada para pontuar; vermelho significa que ela não pontua. O supervisor pode aprovar uma ou as duas." : `Outro técnico fez a repetida (${esc(detail.repeat_technician || "não informado")}); o caso será analisado no fechamento.`) : (detail.same_technician ? "Marque uma ou as duas OS. As selecionadas ficarão verdes e serão as que darão pontos ao técnico; as demais ficarão vermelhas." : `Outro técnico fez a repetida (${esc(detail.repeat_technician || "não informado")}): a repetida pontua normalmente; somente a primeira OS pode ser devolvida.`)}</footer>
    </article>`).join("")}</div>
    ${readOnly ? '<p class="repeat-readonly-note">Estas OS serão analisadas e contabilizadas ao final do mês. Esta consulta é somente para você entender as repetidas; apenas o supervisor pode devolver pontos.</p>' : ""}
    <div class="repeat-audit-actions"><button type="button" class="secondary close-repeat-audit">Fechar</button>${!readOnly && details.some(detail => !detail.refunded) ? '<button type="button" class="primary refund-selected">Devolver pontos selecionados</button>' : ""}</div>
  </form>`;
  document.body.appendChild(dialog);
  if (readOnly) dialog.querySelectorAll("[data-refund]").forEach(input => input.disabled = true);
  dialog.querySelectorAll("[data-refund]").forEach(input => input.addEventListener("change", () => input.closest(".repeat-score-choice")?.classList.toggle("selected", input.checked)));
  dialog.querySelector(".dialog-close").onclick = () => dialog.close();
  dialog.querySelector(".close-repeat-audit").onclick = () => dialog.close();
  dialog.querySelector(".refund-selected")?.addEventListener("click", event => refundSelected(event.currentTarget));
  dialog.showModal();
}

async function refundSelected(button) {
  const container = button.closest("form");
  const selected = [...container.querySelectorAll("[data-refund]:checked")];
  if (!selected.length) return notify("Selecione pelo menos uma repetida.");
  if (!confirm(`Fazer ${selected.length} OS selecionada(s) pontuar? As escolhidas ficarão verdes e as demais continuarão vermelhas.`)) return;
  button.disabled = true;
  const originalLabel = button.textContent;
  button.textContent = "Devolvendo pontos...";
  try {
    const grouped = new Map();
    selected.forEach(input => {
      const key = `${input.dataset.technician}|${input.dataset.original}|${input.dataset.repeat}`;
      const item = grouped.get(key) || { technician: input.dataset.technician, original_order_id: Number(input.dataset.original), repeat_order_id: Number(input.dataset.repeat), restored_order_ids: [] };
      item.restored_order_ids.push(Number(input.dataset.restored));
      grouped.set(key, item);
    });
    const result = await api("/api/superadmin/scores/refund", {
      method: "POST",
      body: JSON.stringify({
        month: scoreData?.month || currentMonthValue(),
        report_technician: scoreData?.requested_technician === "__all__" ? selected[0].dataset.technician : scoreData?.requested_technician || selected[0].dataset.technician,
        items: [...grouped.values()],
      }),
    });
    $("#repeat-audit-dialog")?.close();
    notify(result.notice || "Pontuação estornada");
    const refreshed = structuredClone(scoreData);
    grouped.forEach(group => {
      const technician = refreshed.technicians?.find(item => item.name === group.technician);
      const detail = technician?.repeated?.details?.find(item => Number(item.original_order_id) === group.original_order_id && Number(item.repeat_order_id) === group.repeat_order_id);
      if (!technician || !detail || detail.refunded) return;
      const restored = group.restored_order_ids.reduce((sum, orderId) => sum + Number(orderId === group.repeat_order_id ? detail.repeat_points : detail.original_points), 0);
      detail.refunded = true;
      detail.restored_order_ids = [...group.restored_order_ids];
      detail.points_deducted = Math.max(0, Number(detail.points_lost || 0) - restored);
      technician.repeated.lost_points = Math.max(0, Number(technician.repeated.lost_points || 0) - restored);
      technician.repeated.refunded_points = Number(technician.repeated.refunded_points || 0) + restored;
      technician.points = Number(technician.points || 0) + restored;
    });
    await teamScoreView(scoreData?.requested_technician || selected[0].dataset.technician, false, scoreData?.month || currentMonthValue(), refreshed);
  } catch (error) {
    if (error.name !== "AbortError") notify(error.message);
    button.disabled = false;
    button.textContent = originalLabel;
  }
}

async function scoreView(loadScore = false) {
  setPage("Pontuação");
  if (!loadScore) {
    const technician = state.user?.mode === "technician";
    $("#content").innerHTML = `
      <section class="hero score-hero"><div><small>PONTUAÇÃO</small><h1>${technician ? "Minha pontuação do mês" : "Auditoria de pontuação"}</h1><p>${technician ? "Veja seu total, quantos serviços pontuaram e se existe algo em análise." : "Consulte um técnico e analise os resultados."}</p></div><b>★</b></section>
      <section class="card score-reception"><h2>${technician ? "Consulta simples" : "Escolha quem deseja consultar"}</h2><p>${technician ? "O sistema soma as OS encerradas neste mês. Se houver atendimento repetido, ele aparecerá separado como “em análise”." : "Nenhuma ordem é consultada antes da sua escolha."}</p><div class="page-request"><span>${technician ? "Toque no botão para ver o resultado." : "A consulta começa somente quando você solicitar."}</span><button id="start-score" class="primary">${technician ? "Ver minha pontuação" : "Escolher um técnico"}</button></div></section>`;
    $("#start-score").onclick = () => scoreView(true);
    if (state.user?.mode === "superadmin") {
      $("#start-score").insertAdjacentHTML("afterend", '<button id="start-general-score" class="secondary">Consulta geral</button>');
      $("#start-general-score").onclick = () => teamScoreView("__all__");
    }
    return;
  }
  if (state.user?.mode === "technician") return technicianScoreView(false);
  return teamScoreView("", false);
}

async function technicianScoreView(force = false) {
  $("#content").innerHTML = `
    <section class="hero score-hero"><div><small>MINHA PONTUAÇÃO</small><h1>Calculando o mês atual</h1><p>Estamos conferindo suas OS encerradas.</p></div><b>★</b></section>
    <section class="card score-reception"><div class="score-loading"><i></i><span>Carregando sua pontuação…</span></div></section>`;
  try {
    const data = await api(`/api/technician/score${force ? "?refresh=1" : ""}`);
    scoreData = data;
    const item = (data.technicians || [])[0];
    if (!item) {
      $("#content").innerHTML = '<section class="card empty"><h2>Nenhuma pontuação neste mês</h2><p>Não encontramos OS encerradas no seu nome.</p></section>';
      return;
    }
    const repeated = item.repeated || {}, details = repeated.details || [];
    $("#content").innerHTML = `
      <section class="hero score-hero"><div><small>SEU TOTAL NO MÊS</small><h1>${number(item.points)} pontos</h1><p>${esc(data.start)} até ${esc(data.end)}</p></div><b>★</b></section>
      <section class="score-proof-totals"><div><small>OS ENCERRADAS</small><b>${number(item.closed_orders)}</b></div><div><small>PONTOS DAS OS</small><b>${number(item.gross_points)}</b></div><div><small>EM ANÁLISE</small><b>${number(repeated.count)}</b></div><div><small>TOTAL ATUAL</small><b>${number(item.points)}</b></div></section>
      <section class="card achieved-reward"><div><small>FAIXA ATUAL</small><h2>${esc(scoreReward(item.points))}</h2></div><p>Valor indicativo pela pontuação atual. O fechamento considera a análise final das repetidas.</p></section>
      <section class="card score-reception"><h2>Como ler</h2><p><b>Pontos das OS</b> é a soma antes das análises. <b>Em análise</b> mostra clientes atendidos novamente no mês. <b>Total atual</b> é o valor que está contando agora.</p></section>
      <section class="card toolbar"><div><strong>Quer conferir os detalhes?</strong><small>Esta parte é opcional.</small></div><div class="toolbar-actions"><button id="my-score-report" class="secondary">Ver OS que pontuaram</button>${details.length ? '<button id="my-repeats" class="secondary">Ver itens em análise</button>' : ""}<button id="refresh-my-score" class="secondary">Atualizar</button></div></section>
      ${details.length ? `<button id="repeat-summary" class="card repeat repeat-button"><div><small>REPETIDAS</small><b>${details.length}</b></div><div><small>PONTOS EM ANÁLISE</small><b>-${number(repeated.lost_points)}</b></div><span>Ver por que está contando →</span></button><p class="repeat-month-note">As OS repetidas serão analisadas e contabilizadas definitivamente ao final do mês.</p>` : '<section class="card repeat-success"><b>★</b><div><h3>Parabéns!</h3><p>Este mês não houve repetidas.</p></div></section>'}`;
    $("#my-score-report").onclick = () => openScoreReport(0);
    $("#my-repeats")?.addEventListener("click", () => openRepeatAudit(0, true));
    $("#repeat-summary")?.addEventListener("click", () => openRepeatAudit(0, true));
    $("#refresh-my-score").onclick = () => technicianScoreView(true);
  } catch (error) { errorView(error); }
}

function technicianOrdersLanding() {
  setPage("Ordens do dia");
  $("#content").innerHTML = `
    <section class="hero"><div><small>ROTA DE HOJE</small><h1>Ordens de serviço</h1><p>Nenhuma OS foi consultada ao abrir esta página.</p></div><b>☷</b></section>
    <section class="card page-intro"><b>☷</b><h2>Qual lista você quer consultar?</h2><p>Cada botão faz somente uma consulta do status escolhido e apenas para o dia atual.</p><nav class="intro-actions">${Object.entries(orderFilters).map(([key, value]) => `<a class="secondary" href="#ordens/${key}">${value.label}</a>`).join("")}</nav></section>`;
}

const orderFilters = {
  open: { status: 0, label: "Abertas" }, running: { status: 2, label: "Em execução" },
  pending: { status: 3, label: "Pendentes" }, closed: { status: 1, label: "Encerradas" },
};

function formatOrderDate(value) {
  if (!value) return "Sem horário";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? String(value) : parsed.toLocaleString("pt-BR", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
}

async function technicianOrdersView(filter = "open", force = false) {
  setPage("Ordens do dia");
  const selected = orderFilters[filter] || orderFilters.open;
  loading(`Consultando somente as OS ${selected.label.toLowerCase()} de hoje...`);
  try {
    const orders = await api(`/api/technician/orders?status=${selected.status}${force ? "&refresh=1" : ""}`);
    orders.sort((a, b) => String(a.os_data_agendamento || "").localeCompare(String(b.os_data_agendamento || "")));
    $("#content").innerHTML = `
      <section class="hero"><div><small>ROTA DE HOJE</small><h1>Ordens de serviço</h1><p>Somente serviços agendados ou encerrados hoje. Sem mapa e sem GPS.</p></div><b>${orders.length}</b></section>
      <nav class="order-tabs">${Object.entries(orderFilters).map(([key, value]) => `<a href="#ordens/${key}" class="${key === filter ? "active" : ""}">${value.label}</a>`).join("")}</nav>
      <div class="section-title"><div><h2>${orders.length} ${orders.length === 1 ? "ordem" : "ordens"}</h2><span>${selected.label}</span></div><button id="refresh-orders" class="secondary">Atualizar</button></div>
      <section class="order-list">${orders.map((order, index) => `<button class="card order-row" data-order="${esc(order.os_id)}"><span class="order-position">${index + 1}</span><div><small>OS #${esc(order.os_id)} · ${esc(order.os_motivo_descricao || "Atendimento")}</small><strong>${esc(order.cliente || "Cliente")}</strong></div><time>${esc(formatOrderDate(order.os_data_agendamento || order.os_data_finalizacao))}</time><b>›</b></button>`).join("") || '<section class="card empty">Nenhuma OS nesta situação hoje.</section>'}</section>`;
    $("#refresh-orders").onclick = () => technicianOrdersView(filter, true);
    document.querySelectorAll("[data-order]").forEach(button => button.onclick = () => { location.hash = `ordem/${button.dataset.order}`; });
  } catch (error) { errorView(error); }
}

function weeklyDeviceTotal(devices = {}) {
  return Object.values(devices).reduce((sum, value) => sum + Number(value || 0), 0);
}

function openWeeklyDayDetail(day, label) {
  $("#weekly-day-dialog")?.remove();
  const services = day.service_details || [], materials = day.material_details || [];
  const modems = materials.filter(item => item.gpon);
  const dialog = document.createElement("dialog");
  dialog.id = "weekly-day-dialog";
  dialog.className = "weekly-day-dialog";
  dialog.innerHTML = `<form method="dialog"><button type="button" class="dialog-close" aria-label="Fechar">×</button><small>AVALIAÇÃO DO DIA</small><h2>${esc(label)} · ${esc(new Date(`${day.date}T12:00:00`).toLocaleDateString("pt-BR"))}</h2><div class="weekly-detail-columns"><section><header><h3>Ordens encerradas</h3><b>${number(services.length)}</b></header>${services.map(item => `<a href="#ordem/${esc(item.os_id)}"><span><b>OS #${esc(item.os_id)}</b><small>${esc(item.reason || "Serviço")}</small></span><time>${esc(formatOrderDate(item.date))}</time></a>`).join("") || '<p class="empty">Nenhuma OS encerrada neste dia.</p>'}</section><section><header><h3>Modens lançados</h3><b>${number(modems.length)}</b></header>${modems.map(item => `<a href="${item.os_id ? `#ordem/${esc(item.os_id)}` : "#resumo-semanal"}"><span><b>${item.os_id ? `OS #${esc(item.os_id)}` : "Sem OS"} · GPON ${esc(item.gpon)}</b><small>${esc(item.product || "Modem")} · ${esc(item.client || "Cliente não informado")}</small></span><time>${esc(formatOrderDate(item.used_at))}</time></a>`).join("") || '<p class="empty">Nenhum modem lançado neste dia.</p>'}</section></div></form>`;
  document.body.appendChild(dialog);
  dialog.querySelector(".dialog-close").onclick = () => dialog.close();
  dialog.querySelectorAll("a[href^='#ordem/']").forEach(link => link.onclick = () => dialog.close());
  dialog.addEventListener("close", () => dialog.remove());
  dialog.showModal();
}

async function weeklySummaryAllView(force = false) {
  loading("Montando o resumo geral da equipe...");
  try {
    const data = await api(`/api/superadmin/weekly-summary-all${force ? "?refresh=1" : ""}`, { timeoutMs: 300000 });
    const technicians = data.technicians || [];
    $("#content").innerHTML = `<section class="hero"><div><small>CONSULTA GERAL</small><h1>Resumo semanal da equipe</h1><p>${esc(new Date(`${data.start}T12:00:00`).toLocaleDateString("pt-BR"))} até ${esc(new Date(`${data.end}T12:00:00`).toLocaleDateString("pt-BR"))} às 21h</p></div><b>${number(technicians.length)}</b></section><section class="section-title"><div><h2>Totais por técnico</h2><p>Clique em um card para abrir os dias, serviços e materiais.</p></div><div class="toolbar-actions"><button id="refresh-weekly-general" class="secondary">Atualizar dados</button><button id="back-weekly-selection" class="secondary">Voltar ao filtro</button></div></section><section class="weekly-team-grid">${technicians.map((item, index) => { const totals = item.totals || {}; return `<button class="card weekly-technician-card" data-weekly-technician="${index}" ${item.error ? "disabled" : ""}><header><h2>${esc(item.name || item.username)}</h2><span>${item.error ? "Não consultado" : "Ver semana →"}</span></header><div><p><small>SERVIÇOS</small><b>${number(totals.services)}</b></p><p><small>RETIRADAS</small><b>${number(totals.removals)}</b></p><p><small>APARELHOS</small><b>${number(totals.devices)}</b></p><p><small>CONECTORES</small><b>${number(totals.connectors)}</b></p><p><small>DROP</small><b>${number(totals.drop)} m</b></p></div>${item.error ? `<small>${esc(item.error)}</small>` : ""}</button>`; }).join("") || '<section class="card empty">Nenhum técnico disponível.</section>'}</section>`;
    $("#back-weekly-selection").onclick = () => weeklySummaryView();
    $("#refresh-weekly-general").onclick = () => weeklySummaryAllView(true);
    document.querySelectorAll("[data-weekly-technician]").forEach(card => card.onclick = () => weeklySummaryView(false, technicians[Number(card.dataset.weeklyTechnician)].username, true));
  } catch (error) { errorView(error); }
}

async function weeklySummaryView(force = false, selectedTechnician = "", returnToGeneral = false) {
  setPage("Resumo da semana");
  const supervisor = state.user?.mode === "superadmin";
  if (supervisor && !selectedTechnician) {
    loading("Carregando a equipe técnica...");
    try {
      const access = await api("/api/superadmin/users");
      const technicians = (access.users || []).filter(item => item.role === "technician").sort((a, b) => String(a.name || a.username).localeCompare(String(b.name || b.username), "pt-BR"));
      $("#content").innerHTML = `<section class="hero"><div><small>VISÃO DO SUPERVISOR</small><h1>Resumo semanal da equipe</h1><p>Escolha um técnico ou consulte toda a equipe.</p></div><b>▥</b></section><section class="card page-intro"><b>▥</b><h2>Selecionar técnico</h2><label>Técnico<select id="weekly-technician"><option value="">Escolha para consultar...</option>${technicians.map(item => `<option value="${esc(item.username)}">${esc(item.name || item.username)}</option>`).join("")}</select></label><button id="weekly-general-query" class="primary">Consulta geral</button></section>`;
      $("#weekly-technician").onchange = event => { if (event.target.value) weeklySummaryView(false, event.target.value); };
      $("#weekly-general-query").onclick = () => weeklySummaryAllView(false);
    } catch (error) { errorView(error); }
    return;
  }
  loading(`Montando ${supervisor ? "o" : "seu"} resumo de serviços e materiais...`);
  try {
    const query = new URLSearchParams();
    if (force) query.set("refresh", "1");
    if (selectedTechnician) query.set("technician", selectedTechnician);
    const data = await api(`/api/technician/weekly-summary${query.size ? `?${query}` : ""}`, { timeoutMs: 120000 });
    const labels = ["Sábado", "Domingo", "Segunda-feira", "Terça-feira", "Quarta-feira", "Quinta-feira", "Sexta-feira"];
    const days = data.days || [];
    const totals = days.reduce((result, day) => {
      result.services += Number(day.installations || 0) + Number(day.removals || 0) + Number(day.correctives || 0) + Number(day.other_services || 0);
      result.devices += weeklyDeviceTotal(day.devices);
      result.connectors += Number(day.connectors || 0);
      result.drop += Number(day.drop || 0);
      return result;
    }, { services: 0, devices: 0, connectors: 0, drop: 0 });
    $("#content").innerHTML = `
      <section class="hero"><div><small>SÁBADO A SEXTA-FEIRA</small><h1>${supervisor ? "Resumo semanal do técnico" : "Meu resumo semanal"}</h1><p>${esc(new Date(`${data.start}T12:00:00`).toLocaleDateString("pt-BR"))} até ${esc(new Date(`${data.end}T12:00:00`).toLocaleDateString("pt-BR"))} às 21h · salvo durante o ciclo</p></div><b>▥</b></section>
      ${supervisor ? `<section class="card"><button id="choose-another-weekly-technician" class="secondary">${returnToGeneral ? "Voltar ao resumo geral" : "Voltar ao filtro"}</button></section>` : ""}
      <section class="metrics weekly-totals"><article class="card"><small>SERVIÇOS</small><b>${number(totals.services)}</b></article><article class="card"><small>APARELHOS</small><b>${number(totals.devices)}</b></article><article class="card"><small>CONECTORES</small><b>${number(totals.connectors)}</b></article><article class="card"><small>DROP</small><b>${number(totals.drop)} m</b></article></section>
      <section class="section-title"><div><small>FECHAMENTO SEMANAL</small><h2>${esc(data.technician || "Técnico")}</h2><p>O bloco desta semana expira na sexta-feira às 21h.</p></div><button id="refresh-weekly-summary" class="secondary">Atualizar resumo</button></section>
      <section class="weekly-summary-grid">${days.map((day, index) => {
        const devices = day.devices || {};
        const serviceTotal = Number(day.installations || 0) + Number(day.removals || 0) + Number(day.correctives || 0) + Number(day.other_services || 0);
        return `<article class="card weekly-day-card" data-week-day="${index}" tabindex="0" role="button"><header><div><small>${esc(new Date(`${day.date}T12:00:00`).toLocaleDateString("pt-BR"))}</small><h2>${labels[index] || "Dia"}</h2></div><b>${number(serviceTotal)} OS</b></header><div class="weekly-day-sections"><section><h3>Serviços</h3><p><span>Instalações</span><b>${number(day.installations)}</b></p><p><span>Retiradas</span><b>${number(day.removals)}</b></p><p><span>Corretivas</span><b>${number(day.correctives)}</b></p>${Number(day.other_services || 0) ? `<p><span>Outros</span><b>${number(day.other_services)}</b></p>` : ""}</section><section><h3>Materiais</h3><p><span>Modem básico</span><b>${number(devices.basic)}</b></p><p><span>Modem giga</span><b>${number(devices.giga)}</b></p><p><span>Wi-Fi 6</span><b>${number(devices.wifi6)}</b></p><p><span>Rede Mesh</span><b>${number(devices.mesh)}</b></p><p><span>Conectores</span><b>${number(day.connectors)}</b></p><p><span>Drop</span><b>${number(day.drop)} m</b></p></section></div><footer>Ver detalhes das OS e modens →</footer></article>`;
      }).join("")}</section>`;
    $("#refresh-weekly-summary").onclick = () => weeklySummaryView(true, selectedTechnician);
    $("#choose-another-weekly-technician")?.addEventListener("click", () => returnToGeneral ? weeklySummaryAllView(false) : weeklySummaryView(false, ""));
    document.querySelectorAll("[data-week-day]").forEach(card => {
      const open = () => openWeeklyDayDetail(days[Number(card.dataset.weekDay)], labels[Number(card.dataset.weekDay)] || "Dia");
      card.onclick = open;
      card.onkeydown = event => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); open(); } };
    });
  } catch (error) { errorView(error); }
}

function localDateValue(value = new Date()) {
  return `${value.getFullYear()}-${String(value.getMonth() + 1).padStart(2, "0")}-${String(value.getDate()).padStart(2, "0")}`;
}

async function launchedModemView(locationId = "", selectedDate = localDateValue(), force = false) {
  setPage("Modem lançado");
  if (!state.stockLocations) {
    loading("Carregando os estoques dos técnicos...");
    try { state.stockLocations = await api("/api/admin/stock"); }
    catch (error) { return errorView(error); }
  }
  const locations = state.stockLocations?.locations || [];
  const renderShell = body => {
    $("#content").innerHTML = `<section class="hero"><div><small>AUDITORIA DE APARELHOS</small><h1>Modem lançado</h1><p>Confirme se o GPON baixado pelo técnico foi vinculado ao cliente correto.</p></div><b>✓</b></section><form id="launched-modem-filter" class="card launched-modem-filter"><label>Estoque do técnico<select id="launched-modem-location" required><option value="">Escolha o técnico...</option>${locations.map(item => `<option value="${esc(item.id)}" ${String(item.id) === String(locationId) ? "selected" : ""}>${esc(item.descricao)}</option>`).join("")}</select></label><label>Data da movimentação<input id="launched-modem-date" type="date" value="${esc(selectedDate)}" max="${localDateValue()}" required></label><button class="primary">Consultar técnico</button><button id="launched-modem-general" type="button" class="secondary">Consulta geral</button><a class="secondary button-link" href="#estoque">Voltar</a></form><div id="launched-modem-results">${body}</div>`;
    $("#launched-modem-filter").onsubmit = event => { event.preventDefault(); launchedModemView($("#launched-modem-location").value, $("#launched-modem-date").value, true); };
    $("#launched-modem-general").onclick = () => launchedModemView("all", $("#launched-modem-date").value, true);
  };
  if (!locationId) {
    renderShell('<section class="card empty"><h3>Nenhuma consulta realizada</h3><p>Escolha o estoque e a data. O mesmo dia será aplicado como data inicial e final no SGP.</p></section>');
    return;
  }
  renderShell('<section class="loading inline-loading"><span>Conferindo os lançamentos e o histórico dos clientes...</span></section>');
  try {
    const query = new URLSearchParams({ location: locationId, date: selectedDate });
    if (force) query.set("refresh", "1");
    const data = await api(`/api/superadmin/launched-modems?${query}`, { timeoutMs: 180000 });
    const counts = data.counts || {};
    const results = $("#launched-modem-results");
    results.innerHTML = `<section class="metrics launched-modem-metrics"><article class="card"><small>APARELHOS</small><b>${number(data.total)}</b></article><article class="card good"><small>CORRETOS</small><b>${number(counts.correct)}</b></article><article class="card bad"><small>DIVERGENTES</small><b>${number(counts.divergent)}</b></article><article class="card"><small>NÃO CONFIRMADOS</small><b>${number(Number(counts.not_confirmed || 0) + Number(counts.unlinked || 0))}</b></article></section><section class="section-title"><div><h2>${esc(data.location)}</h2><p>${esc(new Date(`${data.date}T12:00:00`).toLocaleDateString("pt-BR"))} · últimos seis caracteres do GPON</p></div><button id="refresh-launched-modems" class="secondary">Atualizar análise</button></section><section class="launched-modem-list">${(data.items || []).map(item => `<article class="card launched-modem-card ${esc(item.status)}"><header><div><small>${item.origin ? `${esc(item.origin)} · ` : ""}OS #${number(item.order_id)} · ${esc(item.product)}</small><h2>${esc(item.client || "Cliente não identificado")}</h2></div><b>${esc(item.label)}</b></header><dl><div><dt>GPON lançado</dt><dd>${esc(item.serial_suffix)}</dd></div><div><dt>Último vínculo</dt><dd>${esc(item.history_serial ? String(item.history_serial).slice(-6) : "Não localizado")}</dd></div><div><dt>Movimentação</dt><dd>${esc(item.movement)}</dd></div><div><dt>Data do lançamento</dt><dd>${esc(item.used_at)}</dd></div><div class="wide"><dt>Confirmação no histórico</dt><dd>${esc(item.history_at || "Sem registro localizado")}${item.history_user ? ` · ${esc(item.history_user)}` : ""}</dd></div></dl><a class="secondary button-link" href="#ordem/${esc(item.order_id)}">Abrir OS</a></article>`).join("") || '<section class="card empty"><h3>Nenhum aparelho encontrado</h3><p>O estoque não possui lançamentos de ONU nas categorias selecionadas para esta data.</p></section>'}</section>`;
    $("#refresh-launched-modems")?.addEventListener("click", () => launchedModemView(locationId, selectedDate, true));
  } catch (error) {
    const results = $("#launched-modem-results");
    if (results) results.innerHTML = `<section class="card empty"><h3>Não foi possível analisar</h3><p>${esc(error.message)}</p></section>`;
  }
}

function detailValue(order, keys, fallback = "Não informado") {
  for (const key of keys) if (order[key] !== undefined && order[key] !== null && String(order[key]).trim()) return order[key];
  return fallback;
}

async function technicianOrderDetail(orderId, force = false) {
  setPage("Detalhes da OS");
  loading("Abrindo esta OS...");
  try {
    const data = await api(`/api/technician/orders/${orderId}${force ? "?refresh=1" : ""}`), order = data.order || {}, materials = data.materials || [], photos = data.photos || [], draft = data.draft;
    const orderWarnings = Array.isArray(data.warnings) ? data.warnings : [];
    const supervisor = ["superadmin", "office"].includes(state.user?.mode), closed = Number(order.os_status) === 1, readOnly = closed || supervisor, onu = order.onu;
    const canEditOnuLabel = state.user?.mode === "technician" && !closed && onu;
    const online = order.servico_online === true || ["1", "true", "sim", "online"].includes(String(order.servico_online || "").toLowerCase());
    const signal = onu?.signal_context?.current ?? onu?.info_rx;
    const address = [order.endereco_logradouro, order.endereco_numero, order.endereco_bairro, order.endereco_cidade].filter(Boolean).join(", ");
    const contactText = detailValue(order, ["cliente_telefone", "os_telefone", "telefone_contato", "telefone", "telefone1", "celular", "cliente_celular", "cliente_contato", "contato"]);
    const contactDigits = String(contactText || "").replace(/\D/g, "");
    const callNumber = contactDigits.length === 10 || contactDigits.length === 11 ? `55${contactDigits}` :
      ((contactDigits.length === 12 || contactDigits.length === 13) && contactDigits.startsWith("55") ? contactDigits : "");
    $("#content").innerHTML = `
      <section class="order-mobile-shell">
        <header class="order-app-head"><button class="back-orders" aria-label="Voltar">‹</button><div><h1>Ordem de Serviço</h1><p>#${esc(order.os_id)} · ${esc(order.os_status_txt || "")}</p></div></header>
        <nav class="order-screen-tabs"><button data-order-pane="summary" class="active"><b>⌂</b><span>Resumo</span></button><button data-order-pane="status"><b>▣</b><span>Situação</span></button><button data-order-pane="tracking"><b>⌖</b><span>Acompanhamento</span></button><button data-order-pane="modem"><b>◉</b><span>Modem</span></button><button data-order-pane="disconnects"><b>⌁</b><span>Quedas</span></button><button data-order-pane="photos"><b>▧</b><span>Fotos</span></button><button data-order-pane="materials"><b>▤</b><span>Materiais</span></button></nav>
      </section>
      <main class="order-panes">
        ${orderWarnings.length ? `<section class="card order-load-warning" role="status"><strong>Algumas informações ficaram indisponíveis</strong><ul>${orderWarnings.map(warning => `<li>${esc(warning)}</li>`).join("")}</ul><button class="secondary" id="retry-order-details">Tentar carregar novamente</button></section>` : ""}
        <section data-pane="summary">
          <section class="order-glance"><article><small>CLIENTE</small><b>${esc(order.cliente || "Não informado")}</b></article><article><small>OS / SERVIÇO</small><b>#${esc(order.os_id)} · ${esc(order.os_motivo_descricao || "Atendimento")}</b></article><article class="${online ? "online" : "offline"}"><small>CONEXÃO</small><b>${online ? "● Online" : "● Offline"}</b></article><article><small>SINAL DA FIBRA</small><b>${signal != null && signal !== "" ? `${esc(signal)} dBm` : "Sem leitura"}</b></article></section>
          <section class="card service-request"><small>O QUE O TÉCNICO PRECISA FAZER</small><h2>${esc(order.os_motivo_descricao || "Atendimento")}</h2><p>${esc(detailValue(order, ["os_conteudo", "conteudo", "os_relato", "relato", "descricao"]))}</p></section>
          <article class="card"><h2>Cliente e endereço</h2><dl class="detail-dl"><div><dt>Agendamento</dt><dd>${esc(formatOrderDate(order.os_data_agendamento))}</dd></div><div><dt>Contato</dt><dd>${esc(contactText || "Não informado")}${state.user?.mode === "technician" && callNumber ? ` <a class="secondary button-link" href="tel:+${callNumber}">Ligar pelo celular</a>` : ""}</dd></div><div><dt>Endereço</dt><dd>${esc(address || "Não informado")}</dd></div><div><dt>Referência</dt><dd>${esc(detailValue(order, ["endereco_pontoreferencia", "ponto_referencia"]))}</dd></div><div><dt>Contrato</dt><dd>${esc(detailValue(order, ["contrato_id", "clientecontrato_id"]))}</dd></div><div><dt>Login</dt><dd>${esc(detailValue(order, ["servico_login"]))}</dd></div></dl></article>
          <section class="card technical-info"><header><div><small>DIAGNÓSTICO RADIUS</small><h2>Informações técnicas</h2></div><button id="refresh-technical-info" class="secondary">Atualizar</button></header><div id="technical-info-content" class="empty">Carregando informações técnicas...</div></section>
          <section class="card pon-analysis"><header><div><small>DIAGNÓSTICO ÓPTICO</small><h2>Sinal da PON</h2><p>Compare este cliente com as demais ONUs do mesmo slot e PON.</p></div><button id="analyze-order-pon" class="primary">Analisar PON</button></header><div id="order-pon-results"></div></section>
        </section>
        <section data-pane="status" class="hidden">
          <article class="card order-status-form"><small>SITUAÇÃO DA OS</small><h2>Atualizar atendimento</h2>${closed ? `<div class="closed-order-note">Esta OS está encerrada e não pode mais ser alterada.</div>` : `<label>Situação<select id="order-status-select"><option value="0" ${Number(order.os_status) === 0 ? "selected" : ""}>Aberta</option><option value="2" ${Number(order.os_status) === 2 ? "selected" : ""}>Em execução</option><option value="3" ${Number(order.os_status) === 3 ? "selected" : ""}>Pendente</option><option value="1">Encerrar serviço</option></select></label><label>Serviço prestado / motivo<textarea id="order-service-performed" rows="6" maxlength="4000" placeholder="Descreva o que foi realizado ou o motivo da pendência">${esc(draft ? draft.text_value : (order.os_servicoprestado || ""))}</textarea></label><p id="order-draft-status" class="muted">${draft ? "Rascunho recuperado e salvo no sistema." : "O texto é salvo automaticamente enquanto você digita."}</p><p class="muted">Para pendência e encerramento, a descrição é obrigatória.</p><button id="save-order-status" class="primary">Salvar situação</button>`}</article>
        </section>
        <section data-pane="tracking" class="hidden"><section class="card order-tracking-card"><small>DESLOCAMENTO E CONTATO</small><h2>Acompanhamento e comunicação com o cliente</h2><p id="order-tracking-state">${supervisor ? "Gere um link temporário e encaminhe ao cliente. O chat e o GPS ficam associados a esta OS." : "Carregando estado da visita…"}</p><div class="tracking-actions">${state.user?.mode === "superadmin" ? '<button id="generate-supervisor-tracking" class="primary">Gerar link para enviar ao cliente</button><button id="share-order-tracking" class="secondary" hidden>Compartilhar link</button>' : ""}<button id="start-order-tracking" class="secondary" hidden>Tentar carregar novamente</button><button id="copy-order-tracking" class="secondary" disabled>Copiar link de acompanhamento</button><button id="arrive-order-tracking" class="primary" hidden>Cheguei ao local</button></div><input id="order-tracking-share-url" class="tracking-share-url" type="text" readonly hidden aria-label="Link para compartilhar com o cliente"><p id="order-tracking-location" class="muted"></p><div class="tracking-chat"><h3>Chat com o cliente</h3><div id="order-tracking-messages" class="tracking-messages"></div><form id="order-tracking-chat-form"><input id="order-tracking-chat-input" maxlength="1000" placeholder="Escreva uma mensagem ao cliente" required><button class="primary">Enviar</button></form><div class="tracking-quick-replies">${["Estou a caminho.","Chegarei em breve.","Estou próximo do endereço.","Pode confirmar o número da residência?","Encontrei trânsito, mas já estou indo.","Cheguei ao local."].map(text=>`<button type="button" class="secondary" data-tracking-reply="${esc(text)}">${esc(text)}</button>`).join("")}</div></div></section></section>
        <section data-pane="modem" class="hidden"><article class="card onu-summary ebf-modem-card">${onu ? `<small>MODEM DO CLIENTE</small><h2>${esc(onu.type || onu.description || "ONU")}</h2><code>${esc(onu.phy_addr || onu.id || "—")}</code><p><b>Etiqueta atual:</b> <span id="current-onu-label">${esc(onu.ident || "Não informada")}</span></p><p><b>VLAN atual:</b> <span id="current-onu-vlan">${esc(onu.vlan || onu.info_vlan || "Não informada")}</span></p><p><b>Rede:</b> OLT ${esc(onu.olt_name || onu.olt || "—")} · Slot ${esc(onu.slot ?? "—")} · PON ${esc(onu.pon ?? "—")}</p>${canEditOnuLabel ? `<form id="onu-label-form" class="onu-label-form"><label>CTO e porta<input id="onu-label-input" maxlength="120" required value="${esc(onu.ident || "")}" placeholder="Ex.: CTO 7 PORTA 04"></label><label>VLAN<input id="onu-vlan-input" type="number" min="1" max="4094" required value="${esc(onu.vlan || onu.info_vlan || "")}" placeholder="Ex.: 304"></label><p class="muted">A etiqueta e a VLAN serão salvas juntas no SGP usando o acesso central.</p><button class="primary">Salvar</button></form>` : ""}${closed ? "" : '<button id="unlink-onu" class="secondary">Desvincular modem</button>'}` : `<small>MODEM DO CLIENTE</small><h2>Nenhum modem vinculado</h2><p>Selecione a OLT e a ONU detectada para vincular.</p>${closed ? "" : '<button id="link-onu" class="primary">Vincular modem ao cliente</button>'}`}</article></section>
        <section data-pane="disconnects" class="hidden"><section class="card disconnect-history"><small>EXTRATO DE TRÁFEGO</small><h2>Histórico de quedas</h2><p>Selecione o período para consultar somente as desconexões registradas no SGP.</p><form id="disconnect-filter"><label>Data inicial<input id="disconnect-start" type="date" required></label><label>Data final<input id="disconnect-end" type="date" required></label><button class="primary">Consultar quedas</button></form><div id="disconnect-results" class="disconnect-results"><div class="empty">A consulta será feita quando você escolher o período.</div></div></section></section>
        <section data-pane="photos" class="hidden"><section class="card photo-section"><div class="photo-section-head"><div><h2>Fotos (${photos.length})</h2><p class="muted">Depois do envio, a foto fica salva diretamente na OS do SGP.</p></div>${closed ? "" : '<button id="add-photo-order" class="primary">＋ Anexar foto</button>'}</div>${photos.length ? `<div class="order-photo-grid">${photos.map((photo, index) => { const photoId = photo.id || photo.imagem_id || photo.anexo_id; const description = photo.descricao || photo.description || `Foto ${index + 1}`; const src = `/api/technician/orders/${orderId}/photos/${encodeURIComponent(photoId)}`; return photoId ? `<button class="order-photo" type="button" data-photo-src="${src}" data-photo-title="${esc(description)}"><img src="${src}" alt="${esc(description)}" loading="lazy"><span>${esc(description)}</span><small>Salva no SGP · clique para ampliar</small></button>` : `<div class="order-photo unavailable"><span>${esc(description)}</span><small>Imagem indisponível</small></div>`; }).join("")}</div>` : '<div class="order-empty-state"><b>SEM FOTOS</b><span>Use o botão acima para fotografar ou escolher uma imagem.</span></div>'}</section></section>
        <section data-pane="materials" class="hidden"><section class="card"><h2>Material usado</h2><p class="muted">Escolha como o material será lançado. O saldo só será consultado depois do clique.</p>${closed ? "" : '<div class="material-mode-actions"><button id="add-loan-material" class="primary">Comodato</button><button id="add-kit-material" class="primary">Kit de instalação</button></div>'}<div class="material-history"><h3>Lançados nesta OS (${materials.length})</h3>${materials.map(item => `<p class="used-line"><span>${esc(item.produto || "Material")} ${item.referencia ? `· ${esc(item.referencia)}` : ""}</span><b>${number(item.quantidade)}</b></p>`).join("") || '<p>Nenhum material lançado.</p>'}</div></section></section>
      </main>`;
    $("#retry-order-details")?.addEventListener("click", () => technicianOrderDetail(orderId, true));
    if (supervisor) {
      $(".order-status-form").innerHTML = `<small>SITUAÇÃO DA OS</small><h2>Análise do atendimento</h2><div class="closed-order-note">Visualização do supervisor · ${esc(order.os_status_txt || "Situação não informada")}</div><p>${esc(order.os_servicoprestado || "Serviço prestado ainda não informado.")}</p>`;
      ["#unlink-onu", "#link-onu", "#add-photo-order", "#add-loan-material", "#add-kit-material"].forEach(selector => $(selector)?.remove());
    }
    if (state.user?.mode === "superadmin") {
      $("#generate-supervisor-tracking")?.addEventListener("click", event => createCustomerTrackingLink(orderId, event.currentTarget));
      $("#copy-order-tracking")?.addEventListener("click", copyCustomerTrackingLink);
      $("#share-order-tracking")?.addEventListener("click", shareCustomerTrackingLink);
      $("#order-tracking-chat-form")?.setAttribute("hidden", "");
      document.querySelectorAll("[data-tracking-reply]").forEach(button => button.setAttribute("hidden", ""));
    }
    $(".back-orders").onclick = () => history.back();
    document.querySelectorAll("[data-order-pane]").forEach(button => button.onclick = () => {
      document.querySelectorAll("[data-order-pane]").forEach(item => item.classList.toggle("active", item === button));
      document.querySelectorAll("[data-pane]").forEach(pane => pane.classList.toggle("hidden", pane.dataset.pane !== button.dataset.orderPane));
    });
    document.querySelectorAll(".order-photo[data-photo-src]").forEach(button => button.onclick = () => {
      const dialog = document.createElement("dialog");
      dialog.className = "order-photo-dialog";
      dialog.innerHTML = `<button class="dialog-close" type="button" aria-label="Fechar">×</button><img src="${button.dataset.photoSrc}" alt="${esc(button.dataset.photoTitle || "Foto da OS")}"><p>${esc(button.dataset.photoTitle || "Foto da OS")}</p>`;
      document.body.appendChild(dialog);
      dialog.querySelector(".dialog-close").onclick = () => dialog.close();
      dialog.onclick = event => { if (event.target === dialog) dialog.close(); };
      dialog.onclose = () => dialog.remove();
      dialog.showModal();
    });
    const today = new Date(), monthStart = new Date(today.getFullYear(), today.getMonth(), 1);
    const dateInput = value => `${value.getFullYear()}-${String(value.getMonth() + 1).padStart(2, "0")}-${String(value.getDate()).padStart(2, "0")}`;
    $("#disconnect-start").value = dateInput(monthStart); $("#disconnect-end").value = dateInput(today);
    $("#disconnect-filter").onsubmit = event => loadOrderDisconnects(event, orderId);
    $("#refresh-technical-info").onclick = () => loadOrderTechnicalInfo(orderId, true);
    $("#analyze-order-pon").onclick = () => loadOrderPonAnalysis(orderId, false);
    if (!supervisor) initializeOrderTracking(orderId);
    loadOrderTechnicalInfo(orderId, false);
    $("#save-order-status")?.addEventListener("click", () => saveTechnicianOrderStatus(orderId));
    const draftInput = $("#order-service-performed");
    draftInput?.addEventListener("input", () => scheduleOrderDraftSave(orderId));
    draftInput?.addEventListener("blur", () => saveOrderDraft(orderId));
    $("#add-loan-material")?.addEventListener("click", () => openTechnicianMaterial(orderId, "comodato"));
    $("#add-kit-material")?.addEventListener("click", () => openTechnicianMaterial(orderId, "kit"));
    $("#add-photo-order")?.addEventListener("click", () => openTechnicianPhoto(orderId));
    $("#link-onu")?.addEventListener("click", () => openTechnicianOnuLink(order));
    $("#unlink-onu")?.addEventListener("click", () => unlinkTechnicianOnu(orderId, onu));
    $("#onu-label-form")?.addEventListener("submit", event => saveTechnicianOnuLabel(event, orderId));
  } catch (error) { errorView(error); }
}

function ponSignal(value) {
  if (value == null || String(value).trim() === '') return '—';
  const signal = Number(String(value).trim().replace('−', '-').replace(',', '.').replace(/\s*dBm$/i, ''));
  if (!Number.isFinite(signal)) return '—';
  const color = signal >= -24 ? '#15803d' : signal <= -25 ? '#dc2626' : '#b77900';
  const label = signal >= -24 ? '−24 dBm ou melhor' : signal <= -25 ? '−25 dBm ou pior' : 'Entre −24 e −25 dBm';
  return `<span title="${label}" style="display:inline-block;color:${color};border-bottom:3px solid ${color};padding-bottom:2px;line-height:1.4;font-weight:800">${number(signal)} dBm</span>`;
}

function ponMetricCards(title, values = {}) {
  return `<section class="pon-signal-direction"><h3>${esc(title)}</h3><div class="attenuation-metrics"><article><small>MEDIANA</small><b>${ponSignal(values.median)}</b></article><article><small>PIOR SINAL</small><b>${ponSignal(values.worst)}</b></article><article><small>NO LIMITE</small><b>${number(values.limit_count || 0)}</b></article><article><small>RUINS</small><b>${number(values.bad_count || 0)}</b></article></div></section>`;
}

function ponSummaryBlock(title, subtitle, summary = {}) {
  const prevalent = { bad: "Sinal ruim predominante", limit: "Maioria no limite", good: "Maioria com sinal bom", no_signal: "Sem leituras" }[summary.prevalent] || "Sem leituras";
  const level = summary.prevalent === "bad" ? "bad" : summary.prevalent === "limit" ? "warn" : "good";
  return `<section class="pon-group-summary ${level}"><header><div><small>${esc(title)}</small><h3>${esc(prevalent)}</h3><p>${esc(subtitle)}</p></div><b>${number(summary.total || 0)} ONU(s)</b></header><div class="pon-count-grid"><article><small>SINAL BOM</small><b>${number(summary.good || 0)}</b></article><article><small>NO LIMITE</small><b>${number(summary.limit || 0)}</b></article><article><small>SINAL RUIM</small><b>${number(summary.bad || 0)}</b></article><article><small>SEM LEITURA</small><b>${number(summary.no_signal || 0)}</b></article></div>${summary.measured ? `<p><b>${number(summary.bad_percent || 0)}%</b> das ONUs com leitura estão com sinal ruim.</p>` : ""}</section>`;
}

function ponClientRows(items) {
  return (items || []).map(client => {
    const level = client.signal_level || (client.tx_dbm <= -25.25 || client.rx_dbm <= -25.25 ? "bad" : client.tx_dbm <= -25 || client.rx_dbm <= -25 ? "limit" : "good");
    const text = { bad: "Sinal ruim", limit: "No limite", good: "Sinal normal", no_signal: "Sem leitura" }[level] || "Sem leitura";
    const title = client.client || (client.contract_id ? `Contrato #${client.contract_id}` : "Cliente não identificado");
    return `<article class="pon-client-card ${esc(level)}${client.is_target ? " target" : ""}">
      <header><div><small>${client.is_target ? "CLIENTE DESTA OS" : client.contract_id ? `CONTRATO #${esc(client.contract_id)}` : "ONU DA PON"}</small><h3>${esc(title)}</h3></div><span>${esc(text)}</span></header>
      <dl>
        <div class="wide"><dt>Descrição FTTx</dt><dd>${esc(client.description || "Não informada")}</dd></div>
        <div><dt>Etiqueta / CTO</dt><dd>${esc(client.label || "Não informada")}</dd></div>
        <div><dt>VLAN</dt><dd>${esc(client.vlan || "Não informada")}</dd></div>
        <div><dt>Slot / PON</dt><dd>${esc(client.slot ?? "—")} / ${esc(client.pon ?? "—")}</dd></div>
        <div><dt>Sinal RX</dt><dd>${ponSignal(client.rx_dbm)}</dd></div>
        <div><dt>Sinal TX</dt><dd>${ponSignal(client.tx_dbm)}</dd></div>
      </dl>
      ${client.serial ? `<footer>ONU: <code>${esc(client.serial)}</code></footer>` : ""}
    </article>`;
  }).join("") || '<p class="empty">Nenhuma ONU encontrada neste grupo.</p>';
}

function renderOrderPonAnalysis(result) {
  const level = result.pon_attenuated ? "bad" : result.pon_attention || result.area_attenuated || result.target_attenuated || result.target_limit ? "warn" : "good";
  const target = result.target || {}, signals = result.signals || {};
  const sorted = [...(result.clients || [])].sort((a, b) => Math.min(a.tx_dbm ?? 99, a.rx_dbm ?? 99) - Math.min(b.tx_dbm ?? 99, b.rx_dbm ?? 99));
  const areaSorted = [...(result.area_clients || [])].sort((a, b) => Math.min(a.tx_dbm ?? 99, a.rx_dbm ?? 99) - Math.min(b.tx_dbm ?? 99, b.rx_dbm ?? 99));
  const searchText = result.street_search ? `Primeira palavra usada na busca: “${result.street_search}”` : "Nome da rua não identificado no relatório";
  return `<section class="attenuation-result ${level}"><header><div><small>RESULTADO DA ANÁLISE</small><h2>${esc(result.diagnosis)}</h2><p>Cliente: RX ${ponSignal(target.rx_dbm)} · TX ${ponSignal(target.tx_dbm)}</p></div><b>${number(result.clients?.length || 0)} ONU(s)</b></header>
    <div class="attenuation-network"><span>OLT <b>${esc(result.reference?.olt_name || result.reference?.olt_id || "—")}</b></span><span>Slot / PON <b>${esc(result.reference?.slot ?? "—")} / ${esc(result.reference?.pon ?? "—")}</b></span><span>Rua <b>${esc(result.area_name || "Não identificada")}</b></span></div>
    <p class="attenuation-rule">Regra da PON: 50% ou mais ruins = problema na PON; de 15% a 49,9% = analisar; abaixo de 15% = não é a PON. Padrão do sinal: acima de −25 normal; de −25,00 até −25,24 no limite; −25,25 ou menor ruim.</p></section>
    <section class="pon-report-groups">${ponSummaryBlock("PON COMPLETA", `OLT ${result.reference?.olt_name || result.reference?.olt_id || "—"} · Slot ${result.reference?.slot ?? "—"} · PON ${result.reference?.pon ?? "—"}`, result.pon_summary)}${ponSummaryBlock("MESMA RUA", `${result.area_name || "Rua não identificada"} · ${searchText}`, result.area_summary)}</section>
    <details class="card pon-client-details" open><summary>ONUs encontradas na mesma rua (${number(areaSorted.length)})</summary><section class="pon-client-grid">${ponClientRows(areaSorted)}</section></details>
    <details class="card pon-client-details"><summary>Relatório da PON completa (${number(sorted.length)})</summary><section class="pon-client-grid">${ponClientRows(sorted)}</section></details>
    <details class="card pon-client-details"><summary>Ver médias e medianas técnicas</summary>${ponMetricCards("RX · sinal recebido no modem", signals.rx)}${ponMetricCards("TX · retorno recebido na OLT", signals.tx)}</details>`;
}

async function loadOrderPonAnalysis(orderId, force = false) {
  const button = $("#analyze-order-pon"), target = $("#order-pon-results");
  button.disabled = true; button.textContent = "Analisando...";
  target.innerHTML = '<div class="empty">Consultando OLT, slot, PON e sinais no SGP...</div>';
  try {
    const result = await api(`/api/technician/orders/${orderId}/pon-analysis${force ? "?refresh=1" : ""}`, { timeoutMs: 120000, concurrent: true });
    target.innerHTML = renderOrderPonAnalysis(result);
    button.textContent = "Atualizar análise"; button.onclick = () => loadOrderPonAnalysis(orderId, true);
  } catch (error) { target.innerHTML = `<div class="empty error">${esc(error.message)}</div>`; button.textContent = "Tentar novamente"; }
  finally { button.disabled = false; }
}

function scheduleOrderDraftSave(orderId) {
  clearTimeout(orderDraftTimer);
  const status = $("#order-draft-status");
  if (status) status.textContent = "Salvando rascunho...";
  orderDraftTimer = setTimeout(() => saveOrderDraft(orderId), 700);
}

async function saveOrderDraft(orderId) {
  clearTimeout(orderDraftTimer);
  const input = $("#order-service-performed"), status = $("#order-draft-status");
  if (!input || !status) return;
  try {
    await api(`/api/technician/orders/${orderId}/draft`, { method: "POST", body: JSON.stringify({ text: input.value }) });
    if (document.body.contains(status)) status.textContent = "Rascunho salvo automaticamente.";
  } catch (error) {
    if (document.body.contains(status)) status.textContent = "Não foi possível salvar o rascunho. Tente novamente.";
  }
}

async function saveTechnicianOnuLabel(event, orderId) {
  event.preventDefault();
  const form = event.currentTarget, input = $("#onu-label-input"), vlanInput = $("#onu-vlan-input"), button = event.submitter;
  const label = String(input?.value || "").trim().replace(/\s+/g, " ");
  const vlan = Number(vlanInput?.value || 0);
  if (!label) return notify("Informe a CTO e a porta.");
  if (!Number.isInteger(vlan) || vlan < 1 || vlan > 4094) return notify("Informe uma VLAN entre 1 e 4094.");
  if (!confirm(`Salvar a etiqueta “${label}” e a VLAN ${vlan} neste modem?`)) return;
  button.disabled = true; button.textContent = "Salvando no SGP...";
  try {
    const data = await api(`/api/technician/orders/${orderId}/onu-label`, { method: "POST", body: JSON.stringify({ label, vlan }), timeoutMs: 90000 });
    const saved = data.result?.label || label;
    const savedVlan = data.result?.vlan || vlan;
    $("#current-onu-label").textContent = saved; input.value = saved;
    $("#current-onu-vlan").textContent = savedVlan; vlanInput.value = savedVlan;
    notify("Etiqueta e VLAN atualizadas no SGP");
  } catch (error) { notify(error.message || "Não foi possível alterar a etiqueta."); }
  finally { button.disabled = false; button.textContent = "Salvar"; }
}

async function loadOrderTechnicalInfo(orderId, force = false) {
  const target = $("#technical-info-content"), button = $("#refresh-technical-info");
  if (!target) return;
  if (force) { button.disabled = true; button.textContent = "Atualizando..."; }
  try {
    const data = await api(`/api/technician/orders/${orderId}/technical-info${force ? "?refresh=1" : ""}`, { timeoutMs: 90000 });
    const onu = data.onu || {}, online = data.online === true;
    const connection = data.found ? (online ? "Online" : "Offline") : "Sessão não localizada";
    target.className = "technical-info-grid";
    target.innerHTML = `
      <article class="${online ? "online" : "offline"}"><small>STATUS</small><b>● ${esc(connection)}</b></article>
      <article><small>LOGIN PPPoE</small><b>${esc(data.login || "Não informado")}</b></article>
      <article><small>IP RECEBIDO</small><b>${esc(data.ip || "Sem IP")}</b></article>
      <article><small>NAS / PORTA</small><b>${esc(data.nas_ip || "Não informado")} · ${esc(data.nas_port || "sem porta")}</b></article>
      <article><small>CALLING STATION / MAC</small><b>${esc(data.calling_station || "Não informado")}</b></article>
      <article><small>INÍCIO / DURAÇÃO</small><b>${esc(data.started_at ? formatOrderDate(data.started_at) : "Sem sessão")} · ${data.duration_seconds != null ? routeElapsed(data.duration_seconds) : "—"}</b></article>
      <article><small>PLANO</small><b>${esc(data.plan || "Não informado")}</b></article>
      <article><small>ONU / OLT</small><b>${esc(onu.serial || "Sem ONU")} · ${esc(onu.olt || "OLT não informada")}</b></article>
      <article><small>SLOT / PON</small><b>${esc(onu.slot ?? "—")} / ${esc(onu.pon ?? "—")}</b></article>
      <article><small>SINAL ÓPTICO</small><b>${onu.signal != null && onu.signal !== "" ? `${esc(onu.signal)} dBm` : "Sem leitura"}</b></article>`;
  } catch (error) {
    target.className = "empty error"; target.textContent = error.message;
  } finally {
    if (button) { button.disabled = false; button.textContent = "Atualizar"; }
  }
}

async function loadOrderDisconnects(event, orderId) {
  event.preventDefault();
  const button = event.submitter, target = $("#disconnect-results");
  button.disabled = true; button.textContent = "Consultando..."; target.innerHTML = '<div class="empty">Consultando o extrato de tráfego no SGP...</div>';
  try {
    const start = $("#disconnect-start").value, end = $("#disconnect-end").value;
    const data = await api(`/api/technician/orders/${orderId}/disconnects?start=${encodeURIComponent(start)}&end=${encodeURIComponent(end)}`, { timeoutMs: 90000 });
    target.innerHTML = `<div class="disconnect-summary"><b>${number(data.total)}</b><span>queda(s) registrada(s)</span><small>Contrato #${esc(data.contract_id)}</small></div>${(data.items || []).map(item => `<article><div><small>INÍCIO DA QUEDA</small><b>${esc(item.started_at)}</b></div><div><small>RECONECTOU</small><b>${esc(item.ended_at)}</b></div><strong>${esc(item.duration)}</strong></article>`).join("") || '<div class="empty">Nenhuma desconexão registrada neste período.</div>'}`;
  } catch (error) { target.innerHTML = `<div class="empty error">${esc(error.message)}</div>`; }
  finally { button.disabled = false; button.textContent = "Consultar quedas"; }
}

async function changeTechnicianOrderStatus(orderId, status, service) {
  try {
    const result=await api(`/api/technician/orders/${orderId}/status`, { method: "POST", body: JSON.stringify({ status, service_performed: service }) });
    notify(status === 1 ? "OS encerrada" : status === 3 ? "OS pendente" : "Serviço iniciado");
    if(status===2&&result.tracking_warning)notify(`OS iniciada, mas o acompanhamento não pôde ser preparado: ${result.tracking_warning}`);
    if (status === 2) document.querySelector('[data-order-pane="tracking"]')?.click();
    await technicianOrderDetail(orderId);
  } catch (error) { notify(error.message); }
}

let orderTrackingTimer = null, orderTrackingWatch = null, orderTrackingToken = null;
function stopOrderTrackingGps(){if(orderTrackingWatch!=null)navigator.geolocation?.clearWatch(orderTrackingWatch);orderTrackingWatch=null;}
async function initializeOrderTracking(orderId){
  if(orderTrackingTimer)clearInterval(orderTrackingTimer);stopOrderTrackingGps();orderTrackingToken=null;
  const stateNode=$("#order-tracking-state"), locationNode=$("#order-tracking-location");let autoAttempted=false;
  const refresh=async()=>{try{const data=await api(`/api/technician/orders/${orderId}/tracking`, {concurrent:true});orderTrackingToken=data.token||null;
    const copyButton=$("#copy-order-tracking");setTrackingShareUrl(data.tracking_url||"");
    stateNode.textContent=data.state==='ready'&&Number(data.os_status)!==2?'Link disponível. O chat e a localização serão liberados quando a OS estiver em execução.':({ready:'OS em execução. Solicitando permissão para iniciar o GPS.',traveling:'Deslocamento ativo · GPS e chat disponíveis.',arrived:'Técnico chegou ao endereço · GPS e chat encerrados.',finished:'Atendimento finalizado.',paused:'Acompanhamento pausado.'})[data.state]||'Aguardando deslocamento.';
    locationNode.textContent=data.last_location?.updated_at?`Última localização enviada ${new Date(data.last_location.updated_at*1000).toLocaleTimeString('pt-BR')}`:'Localização ainda não compartilhada.';
    $("#start-order-tracking").hidden=data.state!=='ready'||Number(data.os_status)!==2||!autoAttempted;$("#copy-order-tracking").disabled=!data.tracking_url;$("#arrive-order-tracking").hidden=data.state!=='traveling';
    const chatOpen=data.chat_open===true;$("#order-tracking-chat-form").hidden=!chatOpen;document.querySelectorAll('[data-tracking-reply]').forEach(button=>button.hidden=!chatOpen);
    renderOrderTrackingMessages(data.messages||[]);if(data.state!=='traveling')stopOrderTrackingGps();
    if(data.state==='ready'&&Number(data.os_status)===2&&!autoAttempted){autoAttempted=true;requestOrderTrackingGps(orderId,stateNode);}
  }catch(error){stateNode.textContent=error.message;}};
  await refresh();orderTrackingTimer=setInterval(refresh,5000);
  $("#start-order-tracking").onclick=()=>requestOrderTrackingGps(orderId,stateNode);
  $("#copy-order-tracking").onclick=copyCustomerTrackingLink;
  $("#arrive-order-tracking").onclick=async()=>{if(!confirm('Confirma que chegou ao endereço? Isso encerra GPS e chat desta visita.'))return;try{await api(`/api/technician/orders/${orderId}/tracking/arrived`,{method:'POST',concurrent:true,body:'{}'});stopOrderTrackingGps();await refresh();}catch(e){notify(e.message);}};
  $("#order-tracking-chat-form").onsubmit=async e=>{e.preventDefault();const input=$("#order-tracking-chat-input");try{await api(`/api/technician/orders/${orderId}/tracking/messages`,{method:'POST',concurrent:true,body:JSON.stringify({text:input.value})});input.value='';await refresh();}catch(err){notify(err.message);}};
  document.querySelectorAll('[data-tracking-reply]').forEach(b=>b.onclick=async()=>{try{await api(`/api/technician/orders/${orderId}/tracking/messages`,{method:'POST',concurrent:true,body:JSON.stringify({text:b.dataset.trackingReply})});await refresh();}catch(e){notify(e.message);}});
}
function requestOrderTrackingGps(orderId,stateNode){
  if(!navigator.geolocation){stateNode.textContent='Este aparelho não disponibiliza GPS pelo navegador. O acompanhamento não foi iniciado.';$("#start-order-tracking").hidden=false;return;}
  $("#start-order-tracking").hidden=true;
  stateNode.textContent='Aguardando autorização e posição inicial do GPS…';
  navigator.geolocation.getCurrentPosition(async pos=>{try{const started=await api(`/api/technician/orders/${orderId}/tracking/start`,{method:'POST',concurrent:true,body:JSON.stringify({latitude:pos.coords.latitude,longitude:pos.coords.longitude,accuracy:pos.coords.accuracy,token:orderTrackingToken})});setTrackingShareUrl(started.tracking_url||"");$("#start-order-tracking").hidden=true;$("#arrive-order-tracking").hidden=false;$("#order-tracking-chat-form").hidden=false;document.querySelectorAll('[data-tracking-reply]').forEach(button=>button.hidden=false);stateNode.textContent='Deslocamento ativo · GPS e chat disponíveis.';$("#order-tracking-location").textContent='Localização inicial enviada. Atualização automática ativa.';startOrderTrackingWatch(orderId);notify(started.tracking_url?'Acompanhamento iniciado. Link pronto para compartilhar.':'GPS iniciado. O supervisor precisa gerar o link depois de confirmar o CPF do titular.');}catch(e){stateNode.textContent=e.message;$("#start-order-tracking").hidden=false;}},err=>{stateNode.textContent=err.code===1?'A permissão de localização foi negada. Libere o GPS e tente novamente. O acompanhamento não está ativo.':'GPS indisponível. Tente novamente em local aberto. O acompanhamento não está ativo.';$("#start-order-tracking").hidden=false;},{enableHighAccuracy:true,timeout:15000,maximumAge:0});
}
function startOrderTrackingWatch(orderId){stopOrderTrackingGps();orderTrackingWatch=navigator.geolocation.watchPosition(pos=>api(`/api/technician/orders/${orderId}/tracking/location`,{method:'POST',concurrent:true,body:JSON.stringify({latitude:pos.coords.latitude,longitude:pos.coords.longitude,accuracy:pos.coords.accuracy})}).catch(()=>{}),()=>{}, {enableHighAccuracy:true,maximumAge:15000,timeout:25000});}
function renderOrderTrackingMessages(messages){const box=$("#order-tracking-messages");if(!box)return;box.innerHTML=messages.map(m=>`<p><b>${esc(m.side==='technician'?'Técnico':'Cliente')}</b> · ${new Date(m.created_at*1000).toLocaleTimeString('pt-BR')}<br>${esc(m.text)}</p>`).join('')||'<p class="muted">Nenhuma mensagem ainda.</p>';box.scrollTop=box.scrollHeight;}

function saveTechnicianOrderStatus(orderId) {
  const status = Number($("#order-status-select")?.value);
  const service = String($("#order-service-performed")?.value || "").trim();
  if (status === 0) return notify("Escolha Em execução, Pendente ou Encerrar serviço.");
  if ([1, 3].includes(status) && !service) return notify("Descreva o serviço prestado ou o motivo da pendência.");
  if (status === 1 && !confirm("Confirma o encerramento desta OS?")) return;
  changeTechnicianOrderStatus(orderId, status, service);
}

function promptOrderStatus(orderId, status) {
  const service = prompt(status === 1 ? "Descreva obrigatoriamente o serviço prestado:" : "Descreva o que foi feito e o motivo da pendência:");
  if (service === null) return;
  if (!service.trim()) return notify("A descrição é obrigatória.");
  if (status === 1 && !confirm("Confirma o encerramento desta OS?")) return;
  changeTechnicianOrderStatus(orderId, status, service.trim());
}

async function openTechnicianMaterial(orderId, mode = "kit") {
  const policy = state.user?.material_launch_policy;
  if (policy && !policy.allowed) {
    const next = policy.next_at ? new Date(policy.next_at).toLocaleString("pt-BR") : "06h00";
    return notify(`Lançamento de materiais bloqueado entre 20h00 e 06h00. Libera em ${next}.`);
  }
  notify("Consultando seu saldo uma única vez...");
  try {
    const stock = await api("/api/stock");
    const devicePattern = /modem|onu|ont|roteador|equipamento/i;
    const availableItems = (stock.items || []).filter(item => mode === "comodato"
      ? devicePattern.test(item.description || "")
      : !devicePattern.test(item.description || ""));
    const dialog = document.createElement("dialog"); dialog.className = "transfer-dialog"; dialog.id = "technician-material-dialog";
    dialog.innerHTML = `<form method="dialog"><button type="button" class="dialog-close">×</button><small>MATERIAL USADO · ${mode === "comodato" ? "COMODATO" : "KIT DE INSTALAÇÃO"}</small><h2>Selecione o que foi utilizado</h2><div class="material-launch-list">${availableItems.map(item => {
      const refs = item.references || [], classified = /modem|onu|conector/i.test(item.description || "");
      return `<article data-launch-row data-product="${item.product_id}" data-description="${esc(item.description)}"><label><input type="checkbox" data-launch-check> <strong>${esc(item.description)}</strong></label><small>Saldo: ${number(item.quantity)}</small>${refs.length ? `<select data-launch-ref><option value="">Selecione o serial</option>${refs.map(ref => `<option value="${ref.id}">${esc(ref.referencia)}</option>`).join("")}</select>` : `<input data-launch-qty type="number" min="0.01" max="${item.quantity}" step="0.01" placeholder="Quantidade">`}${classified ? '<select data-launch-usage><option value="">Instalação ou troca?</option><option value="instalacao">Instalação</option><option value="troca">Troca</option></select>' : ""}</article>`;
    }).join("") || '<p class="empty">Nenhum material desta categoria disponível no seu saldo.</p>'}</div><p class="error" id="material-launch-error"></p><button class="primary">Revisar e confirmar</button></form>`;
    document.body.appendChild(dialog); dialog.querySelector(".dialog-close").onclick = () => dialog.close();
    dialog.querySelector("form").onsubmit = async event => {
      event.preventDefault(); const items = [];
      for (const row of dialog.querySelectorAll("[data-launch-row]")) {
        if (!row.querySelector("[data-launch-check]").checked) continue;
        const ref = row.querySelector("[data-launch-ref]"), qty = row.querySelector("[data-launch-qty]"), usage = row.querySelector("[data-launch-usage]");
        if (ref && !ref.value) return $("#material-launch-error").textContent = `Selecione o serial de ${row.dataset.description}.`;
        if (qty && !(Number(qty.value) > 0)) return $("#material-launch-error").textContent = `Informe a quantidade de ${row.dataset.description}.`;
        if (usage && !usage.value) return $("#material-launch-error").textContent = `Informe instalação ou troca para ${row.dataset.description}.`;
        items.push({ product_id: Number(row.dataset.product), quantity: ref ? 1 : Number(qty.value), reference_id: ref ? Number(ref.value) : null, usage: usage?.value || "" });
      }
      if (!items.length) return $("#material-launch-error").textContent = "Selecione um material ou “Sem uso de material”.";
      if (!confirm(`Confirma o lançamento de ${items.length} item(ns) nesta OS?`)) return;
      try { await api(`/api/technician/orders/${orderId}/materials`, { method: "POST", body: JSON.stringify({ items, mode }) }); dialog.close(); notify("Material lançado"); await technicianOrderDetail(orderId); }
      catch (error) { $("#material-launch-error").textContent = error.message; }
    };
    dialog.addEventListener("close", () => dialog.remove()); dialog.showModal();
  } catch (error) { notify(error.message); }
}

function openTechnicianPhoto(orderId) {
  const dialog = document.createElement("dialog"); dialog.className = "transfer-dialog";
  dialog.innerHTML = `<form method="dialog"><button type="button" class="dialog-close">×</button><h2>Anexar foto</h2><label>Foto<input id="tech-photo-file" type="file" accept="image/jpeg,image/png,image/webp" capture="environment" required></label><label>Descrição<textarea id="tech-photo-description" required></textarea></label><p class="error"></p><button class="primary">Enviar foto</button></form>`;
  document.body.appendChild(dialog); dialog.querySelector(".dialog-close").onclick = () => dialog.close();
  dialog.querySelector("form").onsubmit = event => { event.preventDefault(); const file = $("#tech-photo-file").files[0], description = $("#tech-photo-description").value.trim(); if (!file || !description) return;
    const reader = new FileReader(); reader.onload = async () => { try { await api(`/api/technician/orders/${orderId}/photos`, { method: "POST", body: JSON.stringify({ filename: file.name, content_type: file.type, data: reader.result, description }) }); dialog.close(); notify("Foto anexada"); await technicianOrderDetail(orderId); } catch (error) { dialog.querySelector(".error").textContent = error.message; } }; reader.readAsDataURL(file);
  }; dialog.addEventListener("close", () => dialog.remove()); dialog.showModal();
}

async function openTechnicianOnuLink(order) {
  loading("Consultando as OLTs...");
  try {
    const olts = await api("/api/technician/olts");
    $("#content").innerHTML = `<button class="secondary" id="back-to-order">← Voltar à OS</button><section class="hero"><div><small>VINCULAR MODEM · ETAPA 1</small><h1>Escolha a OLT</h1><p>${esc(order.cliente || "Cliente")}</p></div><b>${olts.length}</b></section><section class="onu-choice-grid olt-card-grid">${olts.map(olt => `<button class="card onu-choice olt-card" data-olt="${olt.id}"><span>OLT #${olt.id}</span><h2>${esc(olt.name || "OLT")}</h2><p>${esc(olt.olttype || "Tipo não informado")}</p><b>Abrir ONUs →</b></button>`).join("") || '<section class="card empty">Nenhuma OLT disponível.</section>'}</section>`;
    $("#back-to-order").onclick = () => technicianOrderDetail(order.os_id);
    document.querySelectorAll("[data-olt]").forEach(button => button.onclick = () => chooseTechnicianOnu(order, button.dataset.olt));
  } catch (error) { errorView(error); }
}

async function chooseTechnicianOnu(order, oltId) {
  loading("Consultando somente as ONUs desvinculadas desta OLT...");
  try {
    const onus = await api(`/api/technician/olts/${oltId}/unauthorized`);
    $("#content").innerHTML = `<button class="secondary" id="back-to-olts">← Trocar OLT</button><section class="hero"><div><small>VINCULAR MODEM · ETAPA 2</small><h1>Escolha a ONU</h1><p>Confira o GPON/serial antes de continuar.</p></div><b>${onus.length}</b></section><section class="order-list">${onus.map((onu, index) => `<button class="card order-row" data-onu-index="${index}"><span class="order-position">◉</span><div><small>GPON / PHY ADDRESS</small><strong>${esc(onu.id || onu.phy_addr)}</strong><p>${esc(onu.type || "Modelo não informado")}</p></div><time>Slot ${esc(onu.slot)} · PON ${esc(onu.pon)}</time><b>›</b></button>`).join("") || '<section class="card empty">Nenhuma ONU desvinculada nesta OLT.</section>'}</section>`;
    $("#back-to-olts").onclick = () => openTechnicianOnuLink(order);
    document.querySelectorAll("[data-onu-index]").forEach(button => button.onclick = () => technicianOnuForm(order, oltId, onus[Number(button.dataset.onuIndex)]));
  } catch (error) { errorView(error); }
}

async function technicianOnuForm(order, oltId, onu) {
  loading("Carregando as opções da ONU...");
  try {
    const options = await api("/api/technician/onu/options");
    const mode = (options.modes || []).find(item => /pppoe/i.test(item.nome || item.name || ""));
    const template = (options.templates || []).find(item => /ONU HUAWEI FOXFIBRA BRIDG/i.test(item.description || item.name || ""))
      || (options.templates || []).find(item => /huawei.*bridg/i.test(item.description || item.name || ""));
    const typeLabel = item => String(item.name || item.code || item.descricao || item.description || "").trim();
    const type = (options.types || []).find(item => /^onu[\s_-]*1$/i.test(typeLabel(item)))
      || (options.types || []).find(item => typeLabel(item).toLowerCase().includes(String(onu.type || "").toLowerCase()));
    const firstName = String(order.cliente || "Cliente").trim().split(/\s+/)[0];
    const description = [firstName, order.endereco_logradouro, order.endereco_numero, order.cliente_cpf || order.cliente_cpfcnpj].filter(Boolean).join(" · ");
    $("#content").innerHTML = `<button class="secondary" id="back-to-onus">← Escolher outra ONU</button><form id="tech-onu-form" class="card onu-form"><small>VINCULAR MODEM · ETAPA 3</small><h1>Confira os dados obrigatórios</h1>
      <div class="onu-form-grid"><label>GPON / serial *<input name="id" value="${esc(onu.id || onu.phy_addr)}" readonly></label><label>Slot *<input name="slot" value="${esc(onu.slot)}" readonly></label><label>PON *<input name="pon" value="${esc(onu.pon)}" readonly></label>
      <label>Tipo da ONU *<select name="onutype" required><option value="">Selecione</option>${(options.types || []).map(item => `<option value="${item.id}" ${type && String(type.id) === String(item.id) ? "selected" : ""}>${esc(typeLabel(item))}</option>`).join("")}</select></label>
      <label>Modo de operação *<select name="mode" required>${(options.modes || []).map(item => `<option value="${item.id}" ${mode && String(mode.id) === String(item.id) ? "selected" : ""}>${esc(item.nome || item.name)}</option>`).join("")}</select></label>
      <label>Template *<select name="onutemplate" required><option value="">Selecione</option>${(options.templates || []).map(item => `<option value="${item.id}" ${template && String(template.id) === String(item.id) ? "selected" : ""}>${esc(item.description || item.name)}</option>`).join("")}</select></label>
      <label>Contrato *<input name="contrato" value="${esc(order.contrato_id || order.clientecontrato_id || "")}" required></label><label>Login do serviço *<input name="service" value="${esc(order.servico_login || "")}" required></label>
      <label class="wide">Descrição *<input name="description" value="${esc(description)}" required></label><label class="wide">Etiqueta *<input name="ident" value="${esc(order.onu_etiqueta || "")}" required placeholder="Informação da etiqueta"></label>
      <label>Login PPPoE<input name="pppoe_login" value="${esc(order.servico_login || "")}"></label><label>Senha PPPoE<input name="pppoe_password" value="foxfibra"></label></div>
      <p class="error" id="tech-onu-error"></p><button class="primary">Vincular modem</button></form>`;
    $("#back-to-onus").onclick = () => chooseTechnicianOnu(order, oltId);
    $("#tech-onu-form").onsubmit = async event => {
      event.preventDefault();
      const form = event.currentTarget, button = form.querySelector("button.primary");
      const body = { ...Object.fromEntries(new FormData(form).entries()), order_id: order.os_id };
      if (!confirm(`Vincular a ONU ${body.id} a ${order.cliente}?`)) return;
      button.disabled = true; button.textContent = "Vinculando…"; $("#tech-onu-error").textContent = "";
      loading(`Aguarde, vinculando a ONU ${body.id} ao cliente...`);
      try {
        const result = await api(`/api/technician/olts/${oltId}/authorize`, { method: "POST", body: JSON.stringify(body) });
        loading(result?.result?.already_linked ? "O modem já estava vinculado. Atualizando a OS..." : "Modem vinculado. Atualizando a OS...");
        await new Promise(resolve => window.setTimeout(resolve, 900));
        notify(result?.result?.already_linked ? "O modem já estava vinculado" : "Modem vinculado com sucesso");
        await technicianOrderDetail(order.os_id);
      } catch (error) {
        await technicianOnuForm(order, oltId, onu);
        if ($("#tech-onu-error")) $("#tech-onu-error").textContent = error.message;
        notify(error.message);
      }
    };
  } catch (error) { errorView(error); }
}

async function unlinkTechnicianOnu(orderId, onu) {
  const identifier = onu.id || onu.phy_addr;
  if (!identifier || !confirm(`Desvincular a ONU ${identifier} do SGP e da OLT?`)) return;
  try { await api(`/api/technician/onus/${encodeURIComponent(identifier)}/deauthorize`, { method: "POST", body: JSON.stringify({ order_id: orderId }) }); notify("Modem desvinculado"); await technicianOrderDetail(orderId); }
  catch (error) { notify(error.message); }
}

function contractCard(item) {
  const status = item.belongs === false ? "bad" : item.belongs === true ? "good" : "warn";
  const analysis = item.disconnect_analysis;
  const analysisHtml = analysis ? `<button type="button" class="contract-analysis-toggle ${esc(analysis.decision)}">${esc(analysis.title)} · ver provas</button><dialog class="contract-analysis-popover ${esc(analysis.decision)}"><header><div><small>ANÁLISE PARA DESCONEXÃO</small><h3>${esc(analysis.title)}</h3></div><button type="button" class="contract-analysis-close" aria-label="Fechar">×</button></header><p>${esc(analysis.summary)}</p><ul>${(analysis.evidence || []).map(proof => `<li><b>${proof.date ? esc(formatOrderDate(proof.date)) : "Evidência"}</b><span>${esc(proof.text)}</span></li>`).join("") || '<li><span>Nenhuma evidência detalhada encontrada.</span></li>'}</ul><small>Decisão baseada no status, rede atual, histórico de ONU e ordens de mudança de endereço disponíveis no SGP.</small></dialog>` : "";
  return `<article class="card contract ${status} ${analysis ? "has-analysis" : ""}">
    <header><strong>#${esc(item.contract_id)}</strong><span>${!item.found ? "Não localizado" : item.belongs === false ? "Divergente" : item.belongs === true ? "Na mesma CTO" : "Verificar"}</span></header>
    <h3>${esc(item.client || "Cliente não informado")}</h3>
    ${item.contract_status ? `<p class="${item.contract_restricted ? "restricted" : ""}">Status: <b>${esc(item.contract_status)}</b></p>` : ""}
    <dl><div><dt>OLT</dt><dd>${esc(item.olt_name || item.olt_id || "—")}</dd></div><div><dt>Slot / PON</dt><dd>${esc(item.slot ?? "—")} / ${esc(item.pon ?? "—")}</dd></div><div><dt>Etiqueta / CTO</dt><dd>${esc(item.label || "Não informada")}</dd></div><div><dt>Conexão</dt><dd>${item.found ? (item.online ? "Ativo/online" : "Sem conexão") : "—"}</dd></div><div class="wide"><dt>Endereço</dt><dd>${esc(item.address || "Não devolvido pelo SGP")}</dd></div></dl>
    <footer>${esc(item.reason || "")}</footer>${analysisHtml}
  </article>`;
}

function ctoResult(result) {
  const rows = result.contracts || [];
  const reference = result.reference;
  state.lastCtoReference = reference;
  const found = rows.filter(item => item.found);
  const outside = rows.filter(item => item.belongs === false);
  const unknown = rows.filter(item => item.belongs == null);
  const online = found.filter(item => item.online);
  const restricted = rows.filter(item => item.contract_restricted);
  const allActive = Boolean(reference && found.length === rows.length && !outside.length && !unknown.length && !restricted.length && online.length === found.length);
  const reportText = allActive ? `Todos os clientes da CTO (${reference.label || "sem etiqueta informada"}) estão ativos. Desconectei os sem contrato do slot (${reference.slot}) e PON (${reference.pon}) da OLT (${reference.olt_name || reference.olt_id}).` : "";
  return `
    <section class="verdict ${outside.length || restricted.length ? "bad" : unknown.length ? "warn" : "good"}"><div><small>RESULTADO DA ANÁLISE</small><h2>${outside.length ? "Há contratos divergentes" : unknown.length ? "Existem contratos não localizados" : "Todos estão no mesmo slot e PON"}</h2><p>${reference ? `OLT ${esc(reference.olt_name || reference.olt_id)} · Slot ${esc(reference.slot)} · PON ${esc(reference.pon)} · ${esc(reference.label || "etiqueta não informada")}` : "Nenhuma referência de rede localizada."}</p></div><b>${online.length}/${found.length}</b></section>
    ${reportText ? `<section class="card copy-box"><p id="cto-report">${esc(reportText)}</p><button id="copy-report" class="secondary">Copiar texto</button></section>` : ""}
    ${reference ? `<section class="card manual-check"><div><h3>Identificar cabos pela PON</h3><p>Após desconectar os cabos em dúvida, o sistema registra quem está offline. Conecte um cabo por vez para descobrir qual contrato sobe.</p></div><label>Cabos em teste<input id="pon-disconnected-count" type="number" min="1" max="64" value="1"></label><div class="pon-buttons"><button id="check-pon" class="secondary">Consultar uma vez</button><button id="start-pon-observation" class="primary">Bater PON por 8 min</button></div></section><div id="pon-result"></div>` : ""}
    <section class="metrics"><article class="card"><small>LOCALIZADOS</small><b>${found.length}</b></article><article class="card"><small>NO GRUPO</small><b>${rows.filter(item => item.belongs).length}</b></article><article class="card"><small>DIVERGENTES</small><b>${outside.length}</b></article></section>
    <section class="contract-grid">${rows.map(contractCard).join("")}</section>`;
}

function signalValue(value) {
  return value == null ? "—" : `${number(value)} dBm`;
}

function renderAttenuation(result) {
  const signals = result.signals || {};
  const tx = signals.tx || {}, rx = signals.rx || {};
  const level = result.attenuated ? "bad" : result.attention ? "warn" : "good";
  return `<section class="attenuation-result ${level}">
    <header><div><small>DIAGNÓSTICO DA CTO</small><h2>${esc(result.diagnosis)}</h2><p>${esc(result.contract?.client || "Cliente")} · Contrato #${esc(result.contract?.contract_id)}</p></div><b>${number(result.clients?.length || 0)} ONU(s)</b></header>
    <div class="attenuation-network"><span>OLT <b>${esc(result.reference?.olt_name || result.reference?.olt_id)}</b></span><span>Slot / PON <b>${esc(result.reference?.slot)} / ${esc(result.reference?.pon)}</b></span><span>CTO <b>${esc(result.reference?.label || result.contract?.label || "Não informada")}</b></span></div>
    <div class="attenuation-direction"><h3>TX · sinal no modem</h3><div class="attenuation-metrics"><article><small>MÉDIA</small><b>${signalValue(tx.average)}</b></article><article><small>MELHOR</small><b>${signalValue(tx.best)}</b></article><article><small>PIOR</small><b>${signalValue(tx.worst)}</b></article><article><small>ABAIXO DE -27</small><b>${number(tx.weak_count || 0)}</b></article></div></div>
    <div class="attenuation-direction"><h3>RX · sinal de retorno</h3><div class="attenuation-metrics"><article><small>MÉDIA</small><b>${signalValue(rx.average)}</b></article><article><small>MELHOR</small><b>${signalValue(rx.best)}</b></article><article><small>PIOR</small><b>${signalValue(rx.worst)}</b></article><article><small>ABAIXO DE -27</small><b>${number(rx.weak_count || 0)}</b></article></div></div>
    <p class="attenuation-rule">A comparação usa somente clientes com a mesma etiqueta de CTO no mesmo slot e PON. Se a maioria estiver ruim, o problema é da CTO; se somente o contrato consultado estiver ruim, o problema é individual.</p>
  </section>
  <section class="card signal-list"><h3>Clientes encontrados na mesma CTO</h3>${(result.clients || []).map(client => `<article class="${client.tx_dbm <= -27 || client.rx_dbm <= -27 ? "weak" : ""}"><div><strong>${client.is_target ? "● " : ""}${esc(client.client || "Cliente/ONU")}</strong><small>${client.contract_id ? `Contrato #${esc(client.contract_id)} · ` : ""}${esc(client.serial || client.id || "")}</small></div><b>TX ${signalValue(client.tx_dbm)}<br>RX ${signalValue(client.rx_dbm)}</b><span>${client.online ? "Online" : "Offline"}</span></article>`).join("") || '<p class="empty">Nenhuma leitura disponível.</p>'}</section>`;
}

async function analyzeAttenuation(event) {
  event.preventDefault();
  const contract = Number($("#attenuation-contract").value);
  if (!Number.isInteger(contract) || contract <= 0) {
    $("#attenuation-error").textContent = "Informe um ID de contrato válido.";
    return;
  }
  $("#attenuation-error").textContent = "";
  const box = $("#attenuation-results");
  box.innerHTML = '<section class="loading inline"><span>Identificando cliente, CTO e sinais da PON...</span></section>';
  try {
    const result = await api(`/api/cto/attenuation?contract=${contract}`);
    box.innerHTML = renderAttenuation(result);
  } catch (error) {
    if (error.name !== "AbortError") box.innerHTML = `<section class="card empty">${esc(error.message)}</section>`;
  }
}

async function verifyCto(event) {
  event.preventDefault();
  if (state.ctoBusy) return;
  stopPonObservation();
  const ids = [...new Set(($("#cto-contracts").value.match(/\d+/g) || []).map(Number).filter(Boolean))];
  if (!ids.length || ids.length > 40) {
    $("#cto-error").textContent = "Informe entre 1 e 40 contratos.";
    return;
  }
  $("#cto-error").textContent = "";
  state.ctoBusy = true;
  const submit = event.currentTarget.querySelector('button[type="submit"], button.primary');
  if (submit) { submit.disabled = true; submit.textContent = "Consultando..."; }
  const resultBox = $("#cto-results");
  resultBox.innerHTML = '<section class="loading inline"><span class="loading-indicator" aria-hidden="true"></span><span>Consultando contratos uma única vez...</span></section>';
  try {
    const result = await api(`/api/cto/verify?contracts=${encodeURIComponent(ids.join(","))}`);
    resultBox.innerHTML = ctoResult(result);
    if (result.consulted_at) resultBox.insertAdjacentHTML('afterbegin', `<p class="muted">Consultado em ${esc(new Date(result.consulted_at * 1000).toLocaleString('pt-BR'))} · resultados reutilizados por até 2 minutos.</p>`);
    $("#copy-report")?.addEventListener("click", async () => {
      await navigator.clipboard.writeText($("#cto-report").textContent);
      notify("Texto copiado");
    });
    $("#check-pon")?.addEventListener("click", manualPonCheck);
    $("#start-pon-observation")?.addEventListener("click", startPonObservation);
    let detailedResult = null;
    document.querySelectorAll(".contract-analysis-toggle").forEach(button => button.addEventListener("click", async event => {
      event.stopPropagation();
      const dialog = button.nextElementSibling;
      if (dialog?.showModal && !dialog.open) dialog.showModal();
      if (result.history_loaded || dialog.dataset.loaded || state.ctoBusy) return;
      state.ctoBusy = true;
      button.disabled = true;
      const status = document.createElement('p');
      status.textContent = 'Consultando histórico e ordens para conferir as provas...';
      dialog.append(status);
      try {
        detailedResult ||= await api(`/api/cto/verify?contracts=${encodeURIComponent(ids.join(','))}&deep=1`, { concurrent: true });
        const id = Number(button.closest('.contract').querySelector('header strong').textContent.replace('#', ''));
        const item = (detailedResult.contracts || []).find(row => Number(row.contract_id) === id);
        if (!item?.disconnect_analysis) { status.textContent = 'Não há análise detalhada disponível para este contrato.'; return; }
        const template = document.createElement('template');
        template.innerHTML = contractCard(item);
        dialog.innerHTML = template.content.querySelector('dialog').innerHTML;
        dialog.querySelector('.contract-analysis-close').onclick = () => dialog.close();
        dialog.dataset.loaded = '1';
      } catch (error) { status.textContent = error.message; }
      finally { state.ctoBusy = false; button.disabled = false; }
    }));
    document.querySelectorAll(".contract-analysis-close").forEach(button => button.addEventListener("click", event => {
      event.stopPropagation(); button.closest("dialog")?.close();
    }));
    document.querySelectorAll(".contract-analysis-popover").forEach(dialog => dialog.addEventListener("click", event => {
      if (event.target === dialog) dialog.close();
    }));
  } catch (error) {
    if (error.name !== "AbortError") resultBox.innerHTML = `<section class="card empty">${esc(error.message)}</section>`;
  } finally {
    state.ctoBusy = false;
    if (submit) { submit.disabled = false; submit.textContent = "Analisar contratos"; }
  }
}

function renderSelectedPonAnalysis(result) {
  const level = result.pon_attenuated ? 'bad' : result.pon_attention ? 'warn' : 'good';
  const sorted = [...(result.clients || [])].sort((a,b) => Math.min(a.tx_dbm ?? 99,a.rx_dbm ?? 99)-Math.min(b.tx_dbm ?? 99,b.rx_dbm ?? 99));
  return `<section class="attenuation-result ${level}"><header><div><small>RESULTADO DA PON SELECIONADA</small><h2>${esc(result.diagnosis)}</h2><p>OLT ${esc(result.reference?.olt_name || result.reference?.olt_id || '—')} · Slot ${esc(result.reference?.slot ?? '—')} · PON ${esc(result.reference?.pon ?? '—')}</p></div><b>${number(result.clients?.length || 0)} ONU(s)</b></header>
    <p class="attenuation-rule">Regra: 50% ou mais ruins = problema na PON; de 15% a 49,9% = analisar; abaixo de 15% = dentro do esperado. Acima de −25 normal; de −25,00 até −25,24 no limite; −25,25 ou menor ruim.</p></section>
    ${ponSummaryBlock('PON COMPLETA','Resumo de todas as ONUs consultadas',result.pon_summary)}
    <details class="card pon-client-details"><summary>Ver ONUs e sinais (${number(sorted.length)})</summary><section class="pon-client-grid">${ponClientRows(sorted)}</section></details>
    <details class="card pon-client-details"><summary>Ver médias e medianas</summary>${ponMetricCards('RX · sinal recebido no modem',result.signals?.rx || {})}${ponMetricCards('TX · retorno recebido na OLT',result.signals?.tx || {})}</details>`;
}

async function coverageAnalyzePon(force=false) {
  const olt=$('#coverage-filter-olt')?.value, slot=$('#coverage-filter-slot')?.value, pon=$('#coverage-filter-pon')?.value;
  if (olt==='' || slot==='' || pon==='') return notify('Escolha a OLT, o Slot e a PON que deseja analisar.');
  const panel=$('#coverage-analysis-panel'), target=$('#coverage-analysis-result'), button=$('#coverage-analyze-pon');
  panel?.classList.remove('hidden'); button.disabled=true; button.textContent='Analisando...';
  target.innerHTML='<div class="empty">Consultando os sinais ópticos no SGP...</div>';
  try {
    const query=new URLSearchParams({olt_id:olt,slot,pon}); if(force) query.set('refresh','1');
    const result=await api(`/api/office/pon-analysis?${query}`,{concurrent:true,timeoutMs:120000});
    target.innerHTML=renderSelectedPonAnalysis(result);
    button.textContent='Atualizar análise'; button.onclick=()=>coverageAnalyzePon(true);
  } catch(error) { target.innerHTML=`<div class="empty error">${esc(error.message)}</div>`; button.textContent='Tentar novamente'; }
  finally { button.disabled=false; }
}

let ponHealthTimer = null;
let ponHealthFilter = 'all';
let ponHealthData = null;
let ponHealthOlts = [];

function ponHealthStop() { window.clearTimeout(ponHealthTimer); ponHealthTimer = null; }

function ponHealthClass(item) {
  return item.status === 'problem' ? 'bad' : item.status === 'review' ? 'warn' : item.status === 'unavailable' ? 'muted' : 'good';
}

function ponHealthTotals(rows) {
  return rows.reduce((total, item) => ({
    pons: total.pons + 1, good: total.good + Number(item.good || 0), limit: total.limit + Number(item.limit || 0),
    bad: total.bad + Number(item.bad || 0), unavailable: total.unavailable + (item.status === 'unavailable' ? 1 : 0),
  }), {pons:0,good:0,limit:0,bad:0,unavailable:0});
}

function ponHealthCard(item) {
  const status = item.status === 'problem' ? 'SINAL RUIM' : item.status === 'review' ? 'ANALISAR' : item.status === 'unavailable' ? 'SEM LEITURA' : 'DENTRO DO ESPERADO';
  return `<article class="card pon-health-card ${ponHealthClass(item)}"><header><div><small>${esc(status)}</small><h3>${esc(item.olt || 'OLT')} · Slot ${esc(item.slot)} · PON ${esc(item.pon)}</h3></div><b>${number(item.total || 0)} ONU(s)</b></header><p>${esc(item.diagnosis || 'Resultado não informado')}</p><div class="pon-health-counts"><span><b>${number(item.bad || 0)}</b> ruins</span><span><b>${number(item.limit || 0)}</b> limite</span><span><b>${number(item.good || 0)}</b> boas</span><span><b>${number(item.no_signal || 0)}</b> sem leitura</span></div>${item.measured ? `<small>${number(item.bad_percent || 0)}% das ONUs medidas estão ruins.</small>` : ''}${item.error ? `<small class="error">${esc(item.error)}</small>` : ''}</article>`;
}

function ponHealthMatchesFilter(item) {
  if (ponHealthOlts.length && !ponHealthOlts.includes(String(item.olt_id))) return false;
  if (ponHealthFilter === 'bad') return Number(item.bad) > 0;
  if (ponHealthFilter === 'attention') return Number(item.bad) > 0 || Number(item.limit) > 0;
  if (ponHealthFilter === 'good') return item.status !== 'unavailable' && Number(item.good) > 0 && !Number(item.bad) && !Number(item.limit) && !Number(item.no_signal);
  if (ponHealthFilter === 'unavailable') return item.status === 'unavailable';
  return true;
}

function renderPonHealth(data) {
  setPage('Saúde das PONs');
  ponHealthData = data;
  const allRows = [...(data.results || [])].sort((a,b) => {
    const weight = {problem:0,review:1,unavailable:2,not_pon:3};
    return (weight[a.status] ?? 4) - (weight[b.status] ?? 4) || Number(b.bad || 0) - Number(a.bad || 0);
  });
  const rows = allRows.filter(ponHealthMatchesFilter);
  const totals = ponHealthTotals(rows), build = data.build || {}, running = build.state === 'running';
  const updated = data.generated_at ? new Date(Number(data.generated_at) * 1000).toLocaleString('pt-BR') : 'Ainda não consultado';
  $('#content').innerHTML = `<section class="hero"><div><small>MONITORAMENTO ÓPTICO</small><h1>Saúde das PONs</h1><p>Consulta geral dos sinais ópticos por OLT, Slot e PON. Resultados são salvos para consulta rápida.</p></div><b>◉</b></section>
    <section class="card toolbar pon-health-toolbar"><div><strong>Consulta geral</strong><p id="pon-health-status">${running ? `${esc(build.stage || 'Consultando sinais')} · ${number(build.done || 0)}/${number(build.total || 0)}` : `${number(rows.length)} de ${number(allRows.length)} PON(s) exibida(s) · ${esc(updated)}`}</p></div><label class="pon-health-filter">Exibir<select id="pon-health-filter"><option value="all">Todas</option><option value="bad">Somente ruins</option><option value="attention">Ruins e no limite</option><option value="good">Somente boas</option><option value="unavailable">Sem leitura</option></select></label>${state.user.mode==='superadmin'?'<button id="pon-health-scan" class="primary">↻ Atualizar consulta</button>':''}<a class="secondary button-link" href="#mapa-pon">Voltar ao Mapa PON</a></section>
    <section class="metrics"><article class="card"><small>PONS CONSULTADAS</small><b>${number(totals.pons)}</b><p>Resultado salvo</p></article><article class="card bad"><small>ONUS COM SINAL RUIM</small><b>${number(totals.bad)}</b><p>−25,25 dBm ou menor</p></article><article class="card warn"><small>NO LIMITE</small><b>${number(totals.limit)}</b><p>De −25,00 até −25,24</p></article><article class="card good"><small>ONUS BOAS</small><b>${number(totals.good)}</b><p>Acima de −25 dBm</p></article></section>
    <section class="section-title"><div><small>RESULTADO POR PON</small><h2>${rows.length ? 'PONs que exigem atenção aparecem primeiro' : 'Nenhuma consulta geral feita ainda'}</h2><p>${rows.length ? `${number(totals.unavailable)} PON(s) sem leitura disponível nesta consulta.` : 'Clique em “Consultar todas as PONs” para iniciar a leitura sequencial no SGP.'}</p></div></section>
    <section class="pon-health-grid">${rows.map(ponHealthCard).join('') || '<article class="card empty">A consulta será feita uma PON por vez para evitar sobrecarga no SGP.</article>'}</section>`;
  const scan = $('#pon-health-scan');
  document.querySelectorAll('.pon-health-card').forEach((card, index) => {
    card.tabIndex = 0; card.setAttribute('role', 'button');
    card.setAttribute('aria-label', `Ver clientes e sinais: ${rows[index].olt}, Slot ${rows[index].slot}, PON ${rows[index].pon}`);
    card.onclick = () => openPonDetails(rows[index]);
    card.onkeydown = event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); openPonDetails(rows[index]); } };
  });
  const toolbar = document.querySelector('.pon-health-toolbar');
  if (toolbar) {
    const options = data.olts || [];
    toolbar.insertAdjacentHTML('afterend', `<section class="card"><label>Pesquisar OLT<input id="pon-olt-search" placeholder="Digite o nome da OLT"></label><small>Marque as OLTs que deseja consultar. Sem seleção, consulta todas.</small><div id="pon-olt-options">${options.map(olt => `<label data-olt-name="${esc(String(olt.name).toLowerCase())}" style="display:inline-flex;align-items:center;gap:8px;margin:8px"><input type="checkbox" style="width:auto" value="${esc(olt.id)}" ${ponHealthOlts.includes(String(olt.id))?'checked':''}>${esc(olt.name)}</label>`).join('')}</div></section>`);
    $('#pon-olt-search').oninput = event => document.querySelectorAll('[data-olt-name]').forEach(label => label.style.display = label.dataset.oltName.includes(event.target.value.toLowerCase()) ? 'inline-flex' : 'none');
    document.querySelectorAll('#pon-olt-options input').forEach(input => input.onchange = () => {
      ponHealthOlts = [...document.querySelectorAll('#pon-olt-options input:checked')].map(node => node.value);
      renderPonHealth(ponHealthData);
    });
  }
  if (scan) scan.disabled = running;
  if (scan) scan.onclick = async () => {
    scan.disabled = true; scan.textContent = 'Consulta iniciada...';
    try { await api('/api/superadmin/pon-health/scan', {method:'POST', body:JSON.stringify({olt_ids:ponHealthOlts}), concurrent:true, timeoutMs:20000}); await ponHealthView(true); }
    catch (error) { notify(error.message); scan.disabled = false; scan.textContent = '↻ Atualizar consulta'; }
  };
  const filter = $('#pon-health-filter');
  if (filter) {
    filter.value = ponHealthFilter;
    filter.onchange = () => { ponHealthFilter = filter.value; if (ponHealthData) renderPonHealth(ponHealthData); };
  }
  ponHealthStop();
  if (running) ponHealthTimer = window.setTimeout(() => ponHealthView(true), 5000);
}

async function ponHealthView(silent = false) {
  ponHealthStop();
  if (!silent) { setPage('Saúde das PONs'); loading('Carregando resultados salvos das PONs...'); }
  try {
    const data = await api('/api/office/pon-health', {concurrent:true, timeoutMs:20000});
    if (location.hash !== '#saude-pons') return;
    renderPonHealth(data);
  } catch (error) {
    if (location.hash !== '#saude-pons') return;
    if (!silent) errorView(error);
    else { notify(error.message); ponHealthTimer = window.setTimeout(() => ponHealthView(true), 10000); }
  }
}

async function manualPonCheck() {
  if (ponObservation) return notify("Cancele ou aguarde a observação de 8 minutos.");
  const reference = state.lastCtoReference;
  if (!reference) return;
  const box = $("#pon-result");
  box.innerHTML = '<section class="loading inline"><span>Fazendo uma consulta manual...</span></section>';
  try {
    const query = new URLSearchParams({ olt_id: reference.olt_id, slot: reference.slot, pon: reference.pon, offline_only: 1 });
    const result = await api(`/api/cto/pon-status?${query}`);
    box.innerHTML = `<section class="card pon-summary"><h3>Clientes offline nesta PON</h3><p><b>${number(result.offline ?? result.onus?.length)}</b> ONU(s) offline agora, em um total de <b>${number(result.total_on_pon ?? result.total)}</b> nesta PON.</p><small>Nenhuma nova consulta será feita automaticamente.</small></section>`;
  } catch (error) {
    if (error.name !== "AbortError") box.innerHTML = `<section class="card empty">${esc(error.message)}</section>`;
  }
}

function ponIdentity(onu) {
  return String(onu.serial || onu.id || onu.contrato || onu.contract_id || "");
}

function stopPonObservation(message = "") {
  if (!ponObservation) return;
  window.clearTimeout(ponObservation.pollTimer);
  window.clearTimeout(ponObservation.clockTimer);
  ponObservation = null;
  if (message && $("#pon-result")) $("#pon-result").innerHTML = `<section class="card empty">${esc(message)}</section>`;
}

function schedulePonClock() {
  if (!ponObservation) return;
  const elapsed = Math.floor((Date.now() - ponObservation.started) / 1000);
  const remaining = Math.max(0, 480 - elapsed);
  const clock = $("#pon-observation-clock");
  if (clock) clock.textContent = `${String(Math.floor(remaining / 60)).padStart(2, "0")}:${String(remaining % 60).padStart(2, "0")}`;
  if (remaining > 0) ponObservation.clockTimer = window.setTimeout(schedulePonClock, 1000);
}

function renderPonObservation(current, finished = false) {
  if (!ponObservation) return;
  window.clearTimeout(ponObservation.clockTimer);
  const identified = [...ponObservation.identified.values()];
  const offlineNow = Number(current?.offline ?? current?.onus?.length ?? 0);
  $("#pon-result").innerHTML = `<section class="pon-observation ${finished ? "finished" : ""}">
    <header><div><i></i><span>${identified.length ? "CLIENTE IDENTIFICADO" : finished ? "OBSERVAÇÃO CONCLUÍDA" : "AGUARDANDO UMA ONU SUBIR"}</span></div><b id="pon-observation-clock">${finished ? "00:00" : "08:00"}</b></header>
    <div class="pon-observation-metrics"><div><strong>${ponObservation.baseline.size}</strong><small>offline no início</small></div><div><strong>${offlineNow}</strong><small>offline agora</small></div><div><strong>${identified.length}</strong><small>ONUs que subiram</small></div></div>
    <p>${identified.length ? "A ONU abaixo saiu da lista de offline depois que o cabo foi conectado." : finished ? "Nenhuma ONU que estava offline subiu durante os 8 minutos." : `Conecte somente um cabo. Consulta ${ponObservation.checks}/8 concluída; a próxima ocorrerá em um minuto.`}</p>
    ${identified.map(onu => `<article class="identified-onu"><div><strong>${esc(onu.client || "Cliente/ONU identificada")}</strong><span>${esc(onu.serial || onu.id)}</span></div><b>${onu.contract_id ? `Contrato #${esc(onu.contract_id)}` : "Sem contrato vinculado"}</b></article>`).join("")}
    ${identified.length > 1 ? '<div class="pon-warning">Mais de uma ONU subiu. Confirme se somente um cabo foi conectado.</div>' : ""}
    ${finished ? '<button id="next-cable-test" class="primary">Conectar e testar o próximo cabo</button>' : '<button id="cancel-pon-observation" class="secondary">Cancelar observação</button>'}
  </section>`;
  $("#cancel-pon-observation")?.addEventListener("click", () => stopPonObservation("Observação da PON cancelada."));
  $("#next-cable-test")?.addEventListener("click", startPonObservation);
  if (!finished) schedulePonClock();
}

async function startPonObservation() {
  const reference = state.lastCtoReference;
  const disconnectedCount = Number($("#pon-disconnected-count")?.value);
  if (!reference || !Number.isInteger(disconnectedCount) || disconnectedCount < 1) return notify("Informe quantos cabos foram retirados.");
  if (!confirm(`Os cabos já foram desconectados? O sistema registrará as ONUs offline da OLT ${reference.olt_name || reference.olt_id}, slot ${reference.slot}, PON ${reference.pon}. Depois conecte somente um cabo por vez.`)) return;
  stopPonObservation();
  $("#pon-result").innerHTML = '<section class="loading inline"><span>Registrando somente as ONUs offline desta PON...</span></section>';
  try {
    const query = new URLSearchParams({ olt_id: reference.olt_id, slot: reference.slot, pon: reference.pon, offline_only: 1 });
    const baseline = await api(`/api/cto/pon-status?${query}`);
    const offline = (baseline.onus || []).filter(onu => !onu.online);
    if (!offline.length) {
      $("#pon-result").innerHTML = '<section class="card empty">Nenhuma ONU offline foi encontrada nesta PON. Não há cliente desconectado para identificar.</section>';
      return;
    }
    ponObservation = {
      reference, query, disconnectedCount, started: Date.now(), checks: 0,
      baseline: new Map(offline.map(onu => [ponIdentity(onu), onu])),
      identified: new Map(), pollTimer: null, clockTimer: null, checking: false, failures: 0,
    };
    renderPonObservation(baseline);
    ponObservation.pollTimer = window.setTimeout(checkPonObservation, 60000);
  } catch (error) {
    if (error.name !== "AbortError") $("#pon-result").innerHTML = `<section class="card empty">${esc(error.message)}</section>`;
  }
}

async function checkPonObservation() {
  if (!ponObservation || ponObservation.checking) return;
  ponObservation.checking = true;
  try {
    const current = await api(`/api/cto/pon-status?${ponObservation.query}`);
    ponObservation.failures = 0;
    if (Number(current.total_on_pon) === 0 && ponObservation.baseline.size) {
      throw new Error("O SGP devolveu a PON vazia. A identificação foi ignorada para evitar falso positivo.");
    }
    const currentOffline = new Map((current.onus || []).map(onu => [ponIdentity(onu), onu]));
    for (const [id, onu] of ponObservation.baseline) {
      if (!currentOffline.has(id) && !ponObservation.identified.has(id)) {
        ponObservation.identified.set(id, { ...onu, identified_at: current.checked_at });
      }
    }
    ponObservation.checks += 1;
    if (ponObservation.identified.size || ponObservation.checks >= 8) {
      renderPonObservation(current, true);
      window.clearTimeout(ponObservation.clockTimer);
      return;
    }
    renderPonObservation(current);
    ponObservation.checking = false;
    ponObservation.pollTimer = window.setTimeout(checkPonObservation, 60000);
  } catch (error) {
    if (!ponObservation) return;
    ponObservation.checking = false;
    ponObservation.failures = Number(ponObservation.failures || 0) + 1;
    if (ponObservation.failures >= 2) {
      const message = `O teste foi interrompido após duas falhas do SGP: ${error.message}`;
      stopPonObservation(message);
      return;
    }
    const note = $("#pon-result .pon-observation>p");
    if (note) note.textContent = `A consulta falhou: ${error.message}. Será feita somente mais uma tentativa em um minuto.`;
    ponObservation.pollTimer = window.setTimeout(checkPonObservation, 60000);
  }
}

function ctoConsultationView() {
  stopPonObservation();
  setPage("Consultar CTO");
  $("#content").innerHTML = `
    <section class="hero"><div><small>PORTAS ATIVAS DA CTO</small><h1>Verificar clientes da caixa</h1><p>Confira os contratos ativos, slot, PON e identifique cada porta da CTO.</p></div><b>◫</b></section>
    <section class="cto-tool-title"><small>CONFERÊNCIA DE CONTRATOS</small><h2>Verificar contratos e portas ativas</h2><p>Cole os contratos encontrados fisicamente na caixa.</p></section>
    <form id="cto-form" class="card cto-form"><label>IDs dos contratos<textarea id="cto-contracts" rows="9" placeholder="8045&#10;7076&#10;10011" required></textarea><small>Até 40 contratos, separados por linha, espaço ou vírgula.</small></label><button class="primary">Analisar contratos</button><p id="cto-error" class="error"></p></form>
    <div id="cto-results"></div>`;
  $("#cto-form").onsubmit = verifyCto;
}

function ctoView() {
  stopPonObservation();
  setPage("Consultar CTO");
  $("#content").innerHTML = `<section class="hero"><div><small>CONSULTAS DE REDE</small><h1>Portas da CTO</h1><p>Escolha uma ação. Nenhuma consulta ao SGP começa nesta tela.</p></div><b>◫</b></section><section class="folder-grid"><a class="card folder-card" href="#cto/consulta"><b>1</b><h2>Nova consulta</h2><p>Analisar contratos, portas, PON e atenuação.</p></a><a class="card folder-card" href="#cto/historico"><b>2</b><h2>Últimas consultas</h2><p>Veja quem consultou e o resultado salvo por 14 dias.</p></a></section>`;
}

async function ctoHistoryView() {
  setPage("Histórico de CTO"); loading("Abrindo consultas salvas...");
  try {
    const data = await api("/api/superadmin/cto/history");
    const items = data.items || [];
    $("#content").innerHTML = `<section class="hero"><div><small>ÚLTIMOS ${number(data.retention_days || 14)} DIAS</small><h1>Histórico de consultas</h1><p>Registro das análises solicitadas pelos supervisores.</p></div><b>${number(items.length)}</b></section><section class="card"><a class="secondary button-link" href="#cto">Voltar às opções</a></section><section class="card removal-table-scroll">${items.length ? `<table class="removal-table"><thead><tr><th>Data</th><th>Usuário</th><th>Consulta</th><th>Contratos</th><th>OLT / Slot / PON</th><th>Resultado</th></tr></thead><tbody>${items.map(item => { const s=item.summary||{}; return `<tr><td>${esc(formatOrderDate(new Date(Number(item.queried_at)*1000).toISOString()))}</td><td><strong>${esc(item.username)}</strong></td><td>${esc(item.query_type)}</td><td>${esc((item.contracts||[]).join(", "))}</td><td>${esc(s.olt||"—")} · ${esc(s.slot??"—")} / ${esc(s.pon??"—")}</td><td>${s.divergent != null ? `${number(s.found)}/${number(s.total)} localizados · ${number(s.divergent)} divergente(s)` : `${number(s.total||0)} cliente(s) · ${s.attenuated ? "atenuada" : "sem atenuação geral"}`}</td></tr>`; }).join("")}</tbody></table>` : '<div class="empty">Nenhuma consulta salva nos últimos 14 dias.</div>'}</section>`;
  } catch (error) { errorView(error); }
}

function removalsView() {
  stopPonObservation(); setPage("Devoluções");
  $("#content").innerHTML = `<section class="hero"><div><small>CONTROLE DE DEVOLUÇÕES</small><h1>Escolha uma pasta</h1><p>Nenhuma consulta ao SGP começa até você solicitar.</p></div><b>↩</b></section><section class="folder-grid"><a class="card folder-card" href="#retiradas/consultas"><b>1</b><h2>Consultas</h2><p>Consulta geral, lista salva e planilha de entregues.</p></a><a class="card folder-card" href="#retiradas/manual"><b>2</b><h2>Lançamento manual</h2><p>Adicionar ou editar materiais para devolução.</p></a><a class="card folder-card" href="#retiradas/pendencias"><b>3</b><h2>Carregar lista de devolução</h2><p>Visão das pendências agrupadas por técnico.</p></a></section>`;
}

function removalConsultationView() {
  stopPonObservation();
  setPage("Devoluções");
  const today = new Date(), minimum = new Date(); minimum.setDate(today.getDate() - 13);
  const dateValue = value => `${value.getFullYear()}-${String(value.getMonth()+1).padStart(2,"0")}-${String(value.getDate()).padStart(2,"0")}`;
  $("#content").innerHTML = `
    <section class="hero"><div><small>CONSULTA GERAL POR DATA</small><h1>Materiais e devoluções</h1><p>Uma única consulta atualiza modens de troca/retirada, conectores e instalações sem material.</p></div><b>↩</b></section>
    <section class="card page-intro"><b>✓</b><h2>Escolha o dia dos serviços</h2><p>Você pode recuperar finais de semana e dias não consultados. Repetir uma data atualiza o resultado sem duplicar registros.</p><nav class="general-scan-actions"><label>Data da consulta<input id="general-material-date" type="date" value="${dateValue(today)}" min="${dateValue(minimum)}" max="${dateValue(today)}"></label><button id="general-material-scan" class="primary">Fazer consulta geral</button></nav><div id="general-material-result"></div></section>
    <section class="card page-intro"><b>1</b><h2>Modens trocados e retirados</h2><nav class="intro-actions"><button id="saved-removals" class="secondary">Ver lista salva</button><button id="removal-payments" class="secondary">Ver entregues</button></nav></section>
    <section class="card page-intro"><b>2</b><h2>Conectores</h2><nav class="intro-actions"><button id="load-connector-returns" class="secondary">Ver lista salva</button></nav><div id="connector-return-results"></div></section>
    <section class="card"><a class="secondary button-link" href="#retiradas">Voltar às pastas</a></section>`;
  $("#saved-removals").onclick = event => loadRemovals(false, event.currentTarget);
  $("#removal-payments").onclick = loadRemovalPayments;
  $("#load-connector-returns").onclick = () => loadConnectorReturns(false);
  $("#general-material-scan").onclick = runGeneralMaterialScan;
}

function removalManualView() {
  setPage("Lançamento manual");
  $("#content").innerHTML = `<section class="hero"><div><small>DEVOLUÇÃO EDITÁVEL</small><h1>Lançamento manual</h1><p>Adicione conectores e modens ou carregue os ajustes já salvos.</p></div><b>✎</b></section><section class="card"><a class="secondary button-link" href="#retiradas">Voltar às pastas</a></section><section class="section-title"><div><h2>Adicionar uma devolução</h2></div><button id="load-home-obligations" class="secondary">Ver ajustes salvos</button></section><div id="return-obligations">${returnObligationFormHtml()}</div>`;
  $("#return-obligation-form").onsubmit = saveReturnObligation; $("#load-home-obligations").onclick = loadReturnObligations; loadReturnTechnicianOptions();
}

function removalPendingView() {
  setPage("Pendências por técnico");
  $("#content").innerHTML = `<section class="hero"><div><small>LISTA SALVA</small><h1>Materiais para devolução</h1><p>Carregue somente os dados já armazenados no banco.</p></div><b>☷</b></section><section class="card"><a class="secondary button-link" href="#retiradas">Voltar às pastas</a></section>${returnSummarySection()}`;
  $("#load-return-summary").onclick = loadReturnSummary;
}

async function runGeneralMaterialScan(event) {
  const button = event.currentTarget, target = $("#general-material-result");
  const selectedDate = $("#general-material-date")?.value;
  if (!selectedDate) return notify("Escolha a data que deseja consultar.");
  button.disabled = true; button.textContent = "Consultando…";
  target.innerHTML = '<div class="loading inline"><span>Consultando as OS uma vez e atualizando as três listas...</span></div>';
  try {
    const data = await api("/api/superadmin/material-audit/scan", { method: "POST", body: JSON.stringify({ date: selectedDate }) });
    target.innerHTML = `<p class="scan-date-result">Resultado de <b>${esc(new Date(`${data.selected_date}T12:00:00`).toLocaleDateString("pt-BR"))}</b></p><section class="metrics"><article class="card"><small>OS ENCERRADAS</small><b>${number(data.closed_orders)}</b></article><article class="card"><small>MODENS ENCONTRADOS</small><b>${number(data.removals_found)}</b><span>${number(data.removals_added)} novo(s)</span></article><article class="card"><small>CONECTORES ENCONTRADOS</small><b>${number(data.connectors_found)}</b><span>${number(data.connectors_added)} novo(s)</span></article><article class="card"><small>INSTALAÇÕES SEM MATERIAL</small><b>${number(data.installations_without_materials)}</b></article></section>`;
    notify(data.notice);
  } catch (error) { if (error.name !== "AbortError") target.innerHTML = `<p class="error">${esc(error.message)}</p>`; }
  finally { button.disabled = false; button.textContent = "Fazer consulta geral"; }
}

async function installationsWithoutMaterialsView() {
  setPage("Instalações sem material");
  loading("Abrindo a lista salva no banco...");
  try {
    const data = await api("/api/superadmin/installations-without-materials");
    const items = data.items || [];
    $("#content").innerHTML = `<section class="hero"><div><small>LISTA PRÉ-CARREGADA</small><h1>Instalações sem baixa de material</h1><p>Depois de lançar o material, selecione a OS para removê-la desta lista.</p></div><b>${number(items.length)}</b></section><section class="card"><a class="primary button-link" href="#retiradas/consultas">Abrir consulta geral</a></section><section class="section-title"><h2>Lista salva</h2><button id="delete-installation-alerts" class="danger" disabled>Excluir selecionadas</button></section><section class="card removal-table-scroll">${items.length ? `<table class="removal-table"><thead><tr><th></th><th>OS</th><th>Cliente</th><th>Contrato</th><th>Técnico</th><th>Encerrada em</th><th>Serviço</th></tr></thead><tbody>${items.map(item => `<tr><td><input class="installation-alert-check" type="checkbox" value="${Number(item.os_id)}"></td><td><strong>#${number(item.os_id)}</strong></td><td>${esc(item.client || "Não informado")}</td><td>${esc(item.contract_id || "—")}</td><td>${esc(item.technician || "Não identificado")}</td><td>${esc(formatOrderDate(item.closed_at))}</td><td>${esc(item.reason || "Instalação")}</td></tr>`).join("")}</tbody></table>` : '<div class="empty">Nenhuma instalação sem material está salva.</div>'}</section>`;
    const checks=[...document.querySelectorAll(".installation-alert-check")], button=$("#delete-installation-alerts");
    checks.forEach(check => check.onchange=()=>button.disabled=!checks.some(item=>item.checked));
    button.onclick=async()=>{ const order_ids=checks.filter(item=>item.checked).map(item=>Number(item.value)); if(!order_ids.length||!confirm(`Excluir ${order_ids.length} OS da lista?`)) return; button.disabled=true; try { const result=await api("/api/superadmin/installations-without-materials",{method:"DELETE",body:JSON.stringify({order_ids})}); notify(result.notice); installationsWithoutMaterialsView(); } catch(error){ button.disabled=false; notify(error.message); } };
  } catch (error) { errorView(error); }
}

function returnSummarySection() {
  return `<section class="section-title return-summary-title"><div><small>VISÃO GERAL</small><h2>Pendências por técnico</h2><p>Conectores, aparelhos e GPONs ainda aguardando devolução.</p></div><button id="load-return-summary" class="primary">Carregar visão geral</button></section><div id="return-team-summary"></div>`;
}

function returnSummaryKey(name) {
  return String(name || "Não identificado").normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase().replace(/[^a-z0-9]/g, "");
}

async function loadReturnSummary() {
  const target = $("#return-team-summary"), button = $("#load-return-summary");
  if (!target) return;
  if (button) button.disabled = true;
  target.innerHTML = '<section class="loading inline"><span>Abrindo somente os dados salvos...</span></section>';
  try {
    const summaryData = await api("/api/superadmin/returns/summary");
    if (!document.body.contains(target)) return;
    const grouped = new Map();
    const bucket = name => {
      const key = returnSummaryKey(name);
      if (!grouped.has(key)) grouped.set(key, { name: String(name || "Não identificado").replaceAll("_", " "), connectors: 0, gpons: new Set() });
      return grouped.get(key);
    };
    (summaryData.removals || []).forEach(item => { if (item.serial) bucket(item.technician).gpons.add(String(item.serial).toUpperCase()); });
    (summaryData.connectors || []).forEach(item => { bucket(item.technician).connectors += Number(item.quantity || 0); });
    (summaryData.manual || []).forEach(item => {
      const row = bucket(item.technician); row.connectors += Number(item.connectors || 0);
      (item.modems || []).forEach(value => { const modem = typeof value === "object" ? value : { gpon: value }; row.gpons.add(String(modem.gpon).toUpperCase()); });
    });
    const rows = [...grouped.values()].filter(item => item.connectors > 0 || item.gpons.size).sort((a, b) => a.name.localeCompare(b.name, "pt-BR"));
    target.innerHTML = `<section class="return-team-overview">${rows.map(item => `<article class="card"><header><div><small>TÉCNICO</small><h3>${esc(item.name)}</h3></div><b>${number(item.connectors)} conectores</b></header><p><strong>${number(item.gpons.size)} aparelho(s)</strong></p><div>${[...item.gpons].map(gpon => `<code>${esc(gpon)}</code>`).join("") || '<small>Nenhum GPON pendente.</small>'}</div></article>`).join("") || '<div class="card empty">Nenhum material pendente para devolução.</div>'}</section>`;
    if (button) { button.disabled = false; button.textContent = "Atualizar visão geral"; }
  } catch (error) {
    if (error.name !== "AbortError" && document.body.contains(target)) target.innerHTML = `<section class="card empty">${esc(error.message)}</section>`;
    if (button) button.disabled = false;
  }
}

function renderRemovals(data) {
  const items = data.items || [];
  $("#content").innerHTML = `
    <section class="hero"><div><small>ÚLTIMOS ${number(data.retention_days || 14)} DIAS</small><h1>Retiradas confirmadas</h1><p>OS encerrada + prova de ONU desvinculada no histórico do cliente.</p></div><b>${number(data.total || 0)}</b></section>
    <section class="metrics removal-metrics"><article class="card"><small>HOJE</small><b>${number(data.today || 0)}</b><span>retirada(s)</span></article><article class="card"><small>TOTAL SALVO</small><b>${number(data.total || 0)}</b><span>últimos 14 dias</span></article><article class="card"><small>NOVAS AGORA</small><b>${number(data.added || 0)}</b><span>sem duplicar OS</span></article></section>
    <section class="section-title"><div><h2>Por técnico</h2><p>A planilha é atualizada automaticamente. Use o reenvio somente para recuperar alguma falha temporária.</p></div><div class="toolbar-actions"><button id="back-to-removal-home" class="secondary">Voltar</button><button id="sync-removals-sheet" class="secondary">Reenviar dados à planilha</button><button id="open-removal-payments" class="secondary">Planilha de pagamentos</button><button id="new-removal-scan" class="primary">Nova contagem</button></div></section>
    <section class="removal-technicians">${(data.technicians || []).map(item => `<article class="card"><span>${esc(item.name)}</span><strong>${number(item.count)}</strong></article>`).join("") || '<div class="card empty">Nenhuma retirada confirmada nos últimos 14 dias.</div>'}</section>
    <section class="section-title removal-list-title"><div><h2>Lista de retiradas</h2><span>${number(items.length)} registro(s)</span></div><div class="removal-actions"><label><input id="select-all-removals" type="checkbox"> Selecionar todas</label><button id="delete-removals" class="danger" disabled>Marcar como entregues</button></div></section>
    <section class="card removal-table-scroll">${items.length ? `<table class="removal-table"><thead><tr><th></th><th>Técnico</th><th>Modem / GPON</th><th>Tipo</th><th>Cliente</th><th>Desvinculado em</th><th>OS</th><th>Situação</th></tr></thead><tbody>${items.map(item => `<tr><td><input class="removal-check" type="checkbox" value="${Number(item.os_id)}" aria-label="Selecionar OS ${Number(item.os_id)}"></td><td><strong>${esc(item.technician || "Não identificado")}</strong></td><td><code>${esc(item.serial || "Não identificado")}</code></td><td><b class="return-type ${esc(item.return_type || "retirada")}">${esc(item.return_type || "Retirada")}</b></td><td>${esc(item.client || "Cliente não informado")}</td><td>${esc(formatOrderDate(item.evidence_at))}</td><td>#${number(item.os_id)}</td><td><span class="removal-ok">devolução pendente</span></td></tr>`).join("")}</tbody></table>` : '<div class="empty">Nenhuma retirada confirmada salva.</div>'}</section>
    <section class="card return-editor-intro"><div><small>CONTROLE MANUAL</small><h2>Conectores e modens de troca</h2><p>Cadastre materiais que cada técnico precisa devolver. Nenhuma consulta ao SGP é feita.</p></div><button id="manage-return-obligations" class="secondary">Gerenciar devoluções</button></section><div id="return-obligations"></div>
    ${returnSummarySection()}`;
  $("#back-to-removal-home").onclick = removalsView;
  $("#new-removal-scan").onclick = event => loadRemovals(true, event.currentTarget);
  $("#open-removal-payments").onclick = loadRemovalPayments;
  $("#sync-removals-sheet").onclick = syncRemovalsSheet;
  const checks = [...document.querySelectorAll(".removal-check")];
  const deleteButton = $("#delete-removals");
  const updateRemovalSelection = () => { if (deleteButton) deleteButton.disabled = !checks.some(input => input.checked); };
  checks.forEach(input => input.onchange = updateRemovalSelection);
  if ($("#select-all-removals")) $("#select-all-removals").onchange = event => { checks.forEach(input => { input.checked = event.target.checked; }); updateRemovalSelection(); };
  if (deleteButton) deleteButton.onclick = dismissSelectedRemovals;
  $("#manage-return-obligations").onclick = loadReturnObligations;
  $("#load-return-summary").onclick = loadReturnSummary;
}

async function syncRemovalsSheet(event) {
  const button=event.currentTarget; button.disabled=true; button.textContent="Sincronizando…";
  try { const result=await api("/api/superadmin/removals/sync-sheet",{method:"POST"}); notify(result.notice); button.textContent="Planilha sincronizada"; }
  catch(error){ notify(error.message); button.disabled=false; button.textContent="Reenviar dados à planilha"; }
}

async function loadRemovalPayments() {
  loading("Abrindo planilhas semanais salvas...");
  try {
    const data = await api("/api/superadmin/removals/payments");
    renderRemovalPayments(data);
  } catch (error) {
    if (error.name !== "AbortError") { removalsView(); notify(error.message); }
  }
}

function localDatetimeValue(date = new Date()) {
  const pad = value => String(value).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

async function renderRemovalPayments(data) {
  const periods = data.periods || [];
  $("#content").innerHTML = `<section class="hero"><div><small>PAGAMENTO DE RETIRADAS</small><h1>Planilhas semanais</h1><p>Cada retirada entregue vale ${money(data.unit_value || 30)}. O fechamento ocorre às sextas-feiras, às 18h00.</p></div><b>R$</b></section><section class="section-title"><div><h2>Últimos ${number(data.retention_days || 14)} dias</h2><p>Depois desse prazo, o lote é excluído automaticamente.</p></div><button id="back-to-removals" class="secondary">Voltar às retiradas</button></section><form id="manual-payment-form" class="card manual-payment-form"><div><small>AJUSTE MANUAL</small><h2>Adicionar retiradas à contagem</h2><p>A data e hora definem automaticamente a semana do pagamento.</p></div><label>Técnico<select id="payment-technician" required><option value="">Carregando técnicos...</option></select></label><label>Quantidade<input id="payment-quantity" type="number" min="1" max="100" value="1" required></label><label>Data e hora da entrega<input id="payment-delivered-at" type="datetime-local" value="${localDatetimeValue()}" required></label><label>GPON ou observação opcional<input id="payment-serial" maxlength="80" placeholder="Ex.: ajuste da semana"></label><button class="primary">Adicionar à planilha</button><p id="payment-form-error" class="error"></p></form>${periods.map(renderRemovalPaymentPeriod).join("") || '<section class="card empty">Nenhuma retirada entregue aguardando pagamento.</section>'}`;
  $("#back-to-removals").onclick = removalsView;
  $("#manual-payment-form").onsubmit = addManualRemovalPayment;
  document.querySelectorAll(".delete-payment-items").forEach(button => button.onclick = deletePaymentItems);
  try {
    const users = await api("/api/superadmin/users");
    const names = [...new Set((users.users || []).filter(user => user.role !== "disabled" && user.role !== "superadmin").map(user => user.name || user.username).filter(Boolean))].sort((a, b) => a.localeCompare(b, "pt-BR"));
    const select = $("#payment-technician");
    if (select) select.innerHTML = `<option value="">Selecione</option>${names.map(name => `<option value="${esc(name)}">${esc(name)}</option>`).join("")}`;
  } catch (error) { if (error.name !== "AbortError") notify("Não foi possível carregar os técnicos."); }
}

async function addManualRemovalPayment(event) {
  event.preventDefault(); const button = event.submitter; button.disabled = true; $("#payment-form-error").textContent = "";
  try {
    const result = await api("/api/superadmin/removals/payments", { method: "POST", body: JSON.stringify({ technician: $("#payment-technician").value, quantity: Number($("#payment-quantity").value), delivered_at: $("#payment-delivered-at").value, serial: $("#payment-serial").value }) });
    await renderRemovalPayments(result); notify(result.notice);
  } catch (error) { if (error.name !== "AbortError") { $("#payment-form-error").textContent = error.message; button.disabled = false; } }
}

async function deletePaymentItems(event) {
  const period = event.currentTarget.dataset.period;
  const orderIds = [...document.querySelectorAll(`.payment-check[data-period="${period}"]:checked`)].map(input => Number(input.value));
  if (!orderIds.length) return notify("Selecione as retiradas que deseja excluir da contagem.");
  if (!confirm(`Excluir ${orderIds.length} retirada(s) desta planilha?`)) return;
  event.currentTarget.disabled = true;
  try {
    const result = await api("/api/superadmin/removals/payments", { method: "DELETE", body: JSON.stringify({ order_ids: orderIds }) });
    await renderRemovalPayments(result); notify(result.notice);
  } catch (error) { if (error.name !== "AbortError") { notify(error.message); event.currentTarget.disabled = false; } }
}

function renderRemovalPaymentPeriod(period) {
  const payday = new Date(period.payday);
  const periodKey = encodeURIComponent(period.payday);
  const rows = (period.items || []).map(item => `<tr><td><input class="payment-check" data-period="${periodKey}" type="checkbox" value="${Number(item.os_id)}"></td><td><strong>${esc(item.technician || "Não identificado")}</strong></td><td><code>${esc(item.serial || "Não identificado")}</code></td><td><b class="return-type ${esc(item.return_type || "retirada")}">${esc(item.return_type || "Retirada")}</b></td><td>${esc(item.client || "Cliente não informado")}</td><td>${esc(formatOrderDate(item.delivered_at))}</td><td>${Number(item.os_id) > 0 ? `#${number(item.os_id)}` : "Manual"}</td><td><strong>${money(item.value ?? 30)}</strong></td></tr>`).join("");
  return `<section class="card removal-payment-sheet"><header><div><small>PERÍODO: ${esc(formatOrderDate(period.starts_at))} ATÉ ${esc(formatOrderDate(period.ends_at))}</small><h2>Pagamento: sexta-feira, ${payday.toLocaleDateString("pt-BR")} às 18h00</h2></div><div><b>${number(period.count)}</b><strong>${money(period.amount)}</strong></div></header><div class="payment-technicians">${(period.technicians || []).map(item => `<article><span>${esc(item.name)}</span><b>${number(item.count)} aparelho(s)</b><strong>${money(item.amount)}</strong></article>`).join("")}</div><div class="payment-sheet-actions"><span>Somente retiradas geram R$ 30,00; trocas ficam registradas com valor R$ 0,00.</span><button class="danger delete-payment-items" data-period="${periodKey}">Excluir selecionadas</button></div><div class="removal-table-scroll"><table class="removal-table"><thead><tr><th></th><th>Técnico</th><th>Modem / GPON</th><th>Tipo</th><th>Cliente</th><th>Entregue em</th><th>OS</th><th>Valor</th></tr></thead><tbody>${rows}</tbody></table></div></section>`;
}

async function loadReturnObligations() {
  const target = $("#return-obligations");
  const button = $("#manage-return-obligations");
  if (button) button.disabled = true;
  target.innerHTML = '<section class="loading-inline">Abrindo pendências salvas...</section>';
  try {
    const data = await api("/api/superadmin/return-obligations");
    renderReturnObligations(data.items || []);
  } catch (error) {
    if (error.name !== "AbortError") { target.innerHTML = `<p class="error">${esc(error.message)}</p>`; if (button) button.disabled = false; }
  }
}

function renderReturnObligations(items) {
  const target = $("#return-obligations");
  target.innerHTML = `
    ${returnObligationFormHtml()}
    <section class="return-obligation-list">${items.map((item, index) => `<article class="card"><header><div><small>TÉCNICO</small><h3>${esc(item.technician)}</h3></div><b>${number(item.connectors)} conector(es)</b></header><div class="return-gpons">${(item.modems || []).map(value => { const modem = typeof value === "object" ? value : { gpon: value, type: "troca" }; return `<code>${esc(modem.gpon)} · ${esc(modem.type)}</code>`; }).join("") || '<small>Nenhum modem.</small>'}</div>${item.note ? `<p>${esc(item.note)}</p>` : ""}<footer><button class="secondary edit-return" data-index="${index}">Editar</button><button class="danger delete-return" data-index="${index}">Entregue · excluir</button></footer></article>`).join("") || '<div class="card empty">Nenhuma devolução manual cadastrada.</div>'}</section>`;
  $("#return-obligation-form").onsubmit = saveReturnObligation;
  loadReturnTechnicianOptions();
  document.querySelectorAll(".edit-return").forEach(button => button.onclick = () => {
    const item = items[Number(button.dataset.index)];
    loadReturnTechnicianOptions(item.technician); $("#return-connectors").value = item.connectors;
    const modems = (item.modems || []).map(value => typeof value === "object" ? value : { gpon: value, type: "troca" });
    $("#return-modems-exchange").value = modems.filter(modem => modem.type !== "retirada").map(modem => modem.gpon).join("\n");
    $("#return-modems-removal").value = modems.filter(modem => modem.type === "retirada").map(modem => modem.gpon).join("\n"); $("#return-note").value = item.note || "";
    $("#return-technician").scrollIntoView({ behavior: "smooth", block: "center" });
  });
  document.querySelectorAll(".delete-return").forEach(button => button.onclick = () => deleteReturnObligation(items[Number(button.dataset.index)], button));
}

async function saveReturnObligation(event) {
  event.preventDefault(); const button = event.submitter; button.disabled = true; $("#return-error").textContent = "";
  const parseModems = (selector, type) => $(selector).value.split(/[\s,;]+/).map(cleanGpon).filter(Boolean).map(gpon => ({ gpon, type }));
  const modems = [...parseModems("#return-modems-exchange", "troca"), ...parseModems("#return-modems-removal", "retirada")];
  try {
    const data = await api("/api/superadmin/return-obligations", { method: "POST", body: JSON.stringify({ technician: $("#return-technician").value, connectors: Number($("#return-connectors").value || 0), modems, note: $("#return-note").value }) });
    renderReturnObligations(data.items || []); notify(data.notice);
  } catch (error) { if (error.name !== "AbortError") { $("#return-error").textContent = error.message; button.disabled = false; } }
}

async function deleteReturnObligation(item, button) {
  if (!window.confirm(`Confirmar que ${item.technician} entregou estes materiais?`)) return;
  button.disabled = true;
  try {
    const data = await api(`/api/superadmin/return-obligations?technician=${encodeURIComponent(item.technician)}`, { method: "DELETE" });
    renderReturnObligations(data.items || []); notify(data.notice);
  } catch (error) { if (error.name !== "AbortError") { notify(error.message); button.disabled = false; } }
}

async function loadConnectorReturns(scan) {
  const target = $("#connector-return-results");
  if (!target) return;
  target.className = "card loading-inline";
  target.textContent = scan ? "Consultando lançamentos de conectores feitos hoje..." : "Abrindo conectores já salvos...";
  try {
    const data = await api(scan ? "/api/superadmin/connector-returns/scan" : "/api/superadmin/connector-returns", scan ? { method: "POST" } : {});
    renderConnectorReturns(data);
    if (data.notice) notify(data.notice);
  } catch (error) {
    if (error.name !== "AbortError") { target.className = "card empty"; target.textContent = error.message; }
  }
}

function renderConnectorReturns(data) {
  const target = $("#connector-return-results"), items = data.items || [];
  target.className = "connector-return-panel";
  const rows = items.map((item, index) => `<tr><td><input aria-label="Selecionar" type="checkbox" class="connector-return-check" value="${esc(item.source_id)}"></td><td><strong>${esc(item.technician)}</strong></td><td><strong>${esc(item.client || "Cliente não informado")}</strong><small>OS #${number(item.os_id)} · ${esc(item.reason || "Reparo")}</small></td><td>${esc(item.product)}</td><td>${esc(formatOrderDate(item.used_at))}</td><td><input aria-label="Quantidade" class="connector-return-qty" data-index="${index}" type="number" min="0" max="1000" step="1" value="${Number(item.quantity)}"></td><td><input aria-label="Observação" class="connector-return-note" data-index="${index}" value="${esc(item.note || "")}" placeholder="Ex.: acoplador, não cobrar"></td><td><button class="secondary save-connector-return" data-index="${index}">Salvar</button></td></tr>`).join("");
  target.innerHTML = `<section class="section-title"><div><h2>Conectores salvos</h2><p>As alterações feitas na planilha também atualizam o app.</p></div><button id="sync-connectors-sheet" class="secondary">Atualizar conectores na planilha</button></section><section class="metrics"><article class="card"><small>TOTAL PARA DEVOLVER</small><b>${number(data.total)}</b><span>conectores</span></article>${(data.technicians || []).map(item => `<article class="card"><small>${esc(item.name)}</small><b>${number(item.quantity)}</b><span>conectores</span></article>`).join("")}</section>${items.length ? `<div class="connector-table-wrap"><table class="connector-table"><thead><tr><th></th><th>Técnico</th><th>Cliente / serviço</th><th>Material</th><th>Lançado em</th><th>Qtd.</th><th>Observação</th><th>Ação</th></tr></thead><tbody>${rows}</tbody></table></div><div class="connector-table-footer"><small>${number(items.length)} lançamento(s) salvo(s). Consultas repetidas não duplicam registros.</small><button id="dismiss-connector-returns" class="danger">Entregues · excluir selecionados</button></div>` : '<div class="card empty">Nenhum conector de reparo salvo.</div>'}`;
  $("#sync-connectors-sheet").onclick = async event => {
    const button = event.currentTarget;
    button.disabled = true;
    button.textContent = "Atualizando planilha...";
    try {
      const result = await api("/api/superadmin/connector-returns/sync-sheet", { method: "POST" });
      notify(result.notice);
      button.textContent = "Planilha atualizada";
    } catch (error) {
      if (error.name !== "AbortError") notify(error.message);
      button.disabled = false;
      button.textContent = "Atualizar conectores na planilha";
    }
  };
  document.querySelectorAll(".save-connector-return").forEach(button => button.onclick = async () => {
    const index = Number(button.dataset.index), item = items[index]; button.disabled = true;
    try {
      const result = await api("/api/superadmin/connector-returns/update", { method: "POST", body: JSON.stringify({ source_id: item.source_id, quantity: Number(document.querySelector(`.connector-return-qty[data-index="${index}"]`).value), note: document.querySelector(`.connector-return-note[data-index="${index}"]`).value }) });
      renderConnectorReturns(result); notify(result.notice);
    } catch (error) { if (error.name !== "AbortError") { notify(error.message); button.disabled = false; } }
  });
  if ($("#dismiss-connector-returns")) $("#dismiss-connector-returns").onclick = async event => {
    const sourceIds = [...document.querySelectorAll(".connector-return-check:checked")].map(input => input.value);
    if (!sourceIds.length) return notify("Selecione os conectores entregues.");
    if (!confirm(`Excluir ${sourceIds.length} pendência(s) de conectores?`)) return;
    event.currentTarget.disabled = true;
    try {
      const result = await api("/api/superadmin/connector-returns/dismiss", { method: "POST", body: JSON.stringify({ source_ids: sourceIds }) });
      renderConnectorReturns(result); notify(result.notice);
    } catch (error) { if (error.name !== "AbortError") { notify(error.message); event.currentTarget.disabled = false; } }
  };
}

async function dismissSelectedRemovals(event) {
  const orderIds = [...document.querySelectorAll(".removal-check:checked")].map(input => Number(input.value));
  if (!orderIds.length || !window.confirm(`Confirmar a entrega de ${orderIds.length} retirada(s)? Elas irão para a planilha semanal de pagamento.`)) return;
  event.currentTarget.disabled = true;
  try {
    const data = await api("/api/superadmin/removals/dismiss", { method: "POST", body: JSON.stringify({ order_ids: orderIds }) });
    renderRemovals(data);
    notify(data.notice || "Retiradas removidas da lista.");
  } catch (error) {
    if (error.name !== "AbortError") { notify(error.message); event.currentTarget.disabled = false; }
  }
}

async function loadRemovals(scan, button) {
  if (button) button.disabled = true;
  loading(scan ? "Consultando OS encerradas e históricos no SGP..." : "Abrindo contagem salva...");
  try {
    const data = await api(scan ? "/api/superadmin/removals/scan" : "/api/superadmin/removals", scan ? { method: "POST" } : {});
    renderRemovals(data);
    if (data.notice) notify(data.notice);
  } catch (error) {
    if (error.name !== "AbortError") { removalsView(); notify(error.message); }
  }
}

function accessRoleOptions(selected = "technician") {
  return [["technician", "Técnico"], ["office", "Escritório"], ["superadmin", "Supervisor"], ["admin", "Administrador legado"], ["disabled", "Bloqueado"]]
    .map(([value, label]) => `<option value="${value}" ${value === selected ? "selected" : ""}>${label}</option>`).join("");
}

function accessView(load = false) {
  stopPonObservation();
  setPage("Acessos");
  $("#content").innerHTML = `
    <section class="hero"><div><small>CONTROLE DA PLATAFORMA</small><h1>Acessos autorizados</h1><p>Cadastre o usuário do SGP e escolha quais ferramentas ele poderá utilizar.</p></div><b>♙</b></section>
    <section class="card page-intro"><b>♙</b><h2>Nenhuma consulta realizada</h2><p>A lista de usuários só será carregada quando você solicitar. A senha continua sendo a senha do próprio SGP e não é cadastrada aqui.</p><button id="load-access-users" class="primary">Gerenciar acessos</button></section>`;
  $("#load-access-users").onclick = loadAccessUsers;
  if (load) loadAccessUsers();
}

async function loadAccessUsers() {
  const keepList = Boolean(document.querySelector('.access-list'));
  if (!keepList) loading("Consultando acessos autorizados...");
  try {
    const data = await api("/api/superadmin/users");
    const users = data.users || [];
    $("#content").innerHTML = `
      <section class="hero"><div><small>CONTROLE DA PLATAFORMA</small><h1>Acessos autorizados</h1><p>O usuário precisa existir no SGP e usará a própria senha para entrar.</p></div><b>${number(users.length)}</b></section>
      ${data.persistent ? "" : `<section class="card error"><b>Perfis sem armazenamento permanente</b><p>${esc(data.storage_notice || "Configure um banco PostgreSQL persistente no Render.")}</p></section>`}
      <form id="new-access" class="card access-create"><div><small>NOVO ACESSO</small><h2>Autorizar usuário</h2></div><label>Usuário do SGP<input id="access-username" required placeholder="Ex.: KaiqueD"></label><label>Nome de exibição<input id="access-name" placeholder="Ex.: Kaique Dias"></label><label>Perfil<select id="access-role">${accessRoleOptions()}</select></label><label class="access-route-tag"><input id="access-route-monitor" type="checkbox"> <span><b>Acompanhar rota</b><small>Aparece na tabela e no mapa</small></span></label><label class="access-route-tag"><input id="access-popup" type="checkbox"> <span><b>Popup</b><small>Libera o modo flutuante</small></span></label><button class="primary">Salvar acesso</button><p id="access-error" class="error"></p></form>
      <section class="section-title"><h2>Usuários cadastrados</h2><span>${number(users.length)} registro(s)</span></section>
      <section class="access-list">${users.map((user, index) => `<article class="card access-user ${user.role === "disabled" ? "disabled" : ""}"><div><small>${user.role === "superadmin" ? "SUPERVISOR" : user.role === "disabled" ? "BLOQUEADO" : "USUÁRIO"}</small><h3>${esc(user.name || user.username)}</h3><code>${esc(user.username)}</code></div><label>Perfil<select data-access-role="${index}">${accessRoleOptions(user.role)}</select></label><div class="access-extra-roles"><label class="access-route-tag ${user.route_monitor ? "active" : ""}"><input type="checkbox" data-access-route="${index}" ${user.route_monitor ? "checked" : ""}> <span><b>◉ Acompanhar rota</b><small>${user.route_monitor ? "Cargo ativo" : "Sem o cargo"}</small></span></label><label class="access-route-tag ${user.popup_enabled ? "active" : ""}"><input type="checkbox" data-access-popup="${index}" ${user.popup_enabled ? "checked" : ""}> <span><b>▣ Popup</b><small>${user.popup_enabled ? "Cargo ativo" : "Sem o cargo"}</small></span></label></div><div class="access-user-actions"><button class="secondary save-access-role" data-index="${index}">Salvar</button>${String(user.username).toLowerCase() === "gustavo" ? "" : `<button class="danger delete-access-user" data-index="${index}">Excluir perfil</button>`}</div></article>`).join("") || '<div class="card empty">Nenhum usuário cadastrado.</div>'}</section>`;
    $("#new-access").onsubmit = async event => {
      event.preventDefault();
      await savePlatformAccess({ username: $("#access-username").value, name: $("#access-name").value, role: $("#access-role").value, route_monitor: $("#access-route-monitor").checked, popup_enabled: $("#access-popup").checked }, event.submitter, $("#access-error"));
    };
    document.querySelectorAll(".save-access-role").forEach(button => button.onclick = async () => {
      const user = users[Number(button.dataset.index)];
      await savePlatformAccess({ username: user.username, name: user.name, role: document.querySelector(`[data-access-role="${button.dataset.index}"]`).value, route_monitor: document.querySelector(`[data-access-route="${button.dataset.index}"]`).checked, popup_enabled: document.querySelector(`[data-access-popup="${button.dataset.index}"]`).checked }, button);
    });
    document.querySelectorAll(".delete-access-user").forEach(button => button.onclick = () => deletePlatformAccess(users[Number(button.dataset.index)], button));
  } catch (error) {
    if (error.name !== "AbortError") {
      if (!keepList) accessView(false);
      notify(error.message);
    }
  }
}

async function deletePlatformAccess(user, button) {
  const name = user.name || user.username;
  if (!confirm(`Excluir o perfil de “${name}” do app?\n\nO usuário continuará existindo no SGP, mas perderá o acesso ao app e sairá do Acompanhar rota.`)) return;
  button.disabled = true; button.textContent = "Excluindo...";
  try {
    const result = await api(`/api/superadmin/users/${encodeURIComponent(user.username)}`, { method: "DELETE" });
    notify(result.notice || "Perfil excluído do app.");
    await loadAccessUsers();
  } catch (error) {
    if (error.name !== "AbortError") notify(error.message);
    button.disabled = false; button.textContent = "Excluir perfil";
  }
}

async function savePlatformAccess(payload, button, errorBox = null) {
  button.disabled = true;
  if (errorBox) errorBox.textContent = "";
  try {
    const result = await api("/api/superadmin/users/role", { method: "POST", body: JSON.stringify(payload) });
    notify(result.notice || "Acesso atualizado.");
    await loadAccessUsers();
  } catch (error) {
    if (error.name !== "AbortError") errorBox ? errorBox.textContent = error.message : notify(error.message);
    button.disabled = false;
  }
}

function onuLinkView() {
  setPage("Vincular modem");
  loading("Carregando as OLTs...");
  api("/api/superadmin/onu-link/olts").then(renderOnuLinkOlts).catch(error => errorView(error));
}

function modemSearchView() {
  stopPonObservation();
  setPage("Procurar modem");
  $("#content").innerHTML = `
    <section class="hero"><div><small>BUSCA NOS APARELHOS VINCULADOS</small><h1>Procurar modem</h1><p>Digite exatamente os seis últimos caracteres do GPON/PHY Address. Modens offline também são encontrados.</p></div><b>⌕</b></section>
    <form id="modem-search-form" class="card modem-search-form">
      <label>Final do modem<input id="modem-search-suffix" minlength="6" maxlength="6" pattern="[A-Za-z0-9]{6}" autocomplete="off" required placeholder="Ex.: ABCD54"></label>
      <button class="primary">Procurar modem vinculado</button><p id="modem-search-error" class="error"></p>
    </form><section id="modem-search-results"></section>`;
  $("#modem-search-form").onsubmit = async event => {
    event.preventDefault();
    const button = event.submitter;
    const suffix = $("#modem-search-suffix").value.replace(/[^A-Za-z0-9]/g, "").toUpperCase();
    if (suffix.length !== 6) return $("#modem-search-error").textContent = "Informe exatamente seis caracteres.";
    button.disabled = true; button.textContent = "Analisando aparelhos vinculados..."; $("#modem-search-error").textContent = "A busca pode demorar alguns segundos porque o SGP só aceita o GPON completo em sua API.";
    try {
      const data = await api(`/api/office/modem-search?suffix=${encodeURIComponent(suffix)}`, { timeoutMs: 180000 });
      const items = data.items || [];
      $("#modem-search-error").textContent = "";
      const scan = data.mode === "linked-registry" ? `${number(data.onus_checked || 0)} aparelhos vinculados conferidos` : data.mode === "all-olts" ? `${number(data.onus_checked || 0)} aparelhos conferidos em ${number(data.olts_scanned || 0)} OLTs` : "localizado pelo filtro rápido do SGP";
      $("#modem-search-results").innerHTML = items.length ? `<section class="section-title"><div><h2>${number(items.length)} resultado(s)</h2><p>Final pesquisado: ${esc(data.suffix)} · ${esc(scan)}</p></div></section><section class="modem-search-results">${items.map(item => `
        <article class="card modem-search-result"><header><div><small>CLIENTE</small><h2>${esc(item.client)}</h2></div><span class="${item.online ? "online" : "offline"}">${item.online ? "Online" : "Situação não confirmada"}</span></header>
        <dl><div><dt>ID do cliente</dt><dd>${esc(item.client_id || "—")}</dd></div><div><dt>CPF/CNPJ</dt><dd>${esc(item.cpf || "Não informado")}</dd></div><div><dt>Contrato</dt><dd>${esc(item.contract_id || "—")}</dd></div><div><dt>GPON / PHY Address</dt><dd><code>${esc(item.serial)}</code></dd></div><div><dt>OLT</dt><dd>${esc(item.olt)} · Slot ${esc(item.slot ?? "—")} · PON ${esc(item.pon ?? "—")}</dd></div><div><dt>ID da ONU</dt><dd>${esc(item.onu_id || "—")}</dd></div></dl>
      </article>`).join("")}</section>` : `<section class="card empty">Nenhum modem com esse final foi localizado após ${esc(scan)}.</section>`;
    } catch (error) { if (error.name !== "AbortError") $("#modem-search-error").textContent = error.message; }
    finally { button.disabled = false; button.textContent = "Procurar modem vinculado"; }
  };
}

function withdrawalCandidateRow(item, technicians) {
  const suggested = item.suggestion?.technician || "";
  return `<article class="card withdrawal-candidate" data-withdrawal-contract="${esc(item.contract_id)}">
    <div class="withdrawal-candidate-summary"><small>SUSPENSA · ${number(item.days_overdue)} DIAS EM ATRASO</small><h2>${esc(item.client)}</h2><p>${esc(item.address || "Endereço não informado")}</p><span>Contrato #${esc(item.contract_id)} · GPON ${esc(item.serial || "não informado")}</span>${suggested ? `<span>Mais próximo: ${esc(suggested)}${item.suggestion.distance_km != null ? ` · ${number(item.suggestion.distance_km)} km` : ""}</span>` : ""}</div>
    <label>Técnico responsável<select class="withdrawal-technician"><option value="">Selecione</option>${(technicians || []).map(tech => `<option value="${esc(tech.id)}" ${_sameUiPerson(tech.name, suggested) ? "selected" : ""}>${esc(tech.name)}</option>`).join("")}</select></label>
    <button class="primary withdrawal-open">Abrir retirada</button><p class="error"></p>
  </article>`;
}

function _sameUiPerson(left, right) {
  const normalize = value => String(value || "").normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase();
  const a = normalize(left), b = normalize(right);
  return Boolean(a && b && (a.includes(b) || b.includes(a) || a.split(" ")[0] === b.split(" ")[0]));
}

function bindWithdrawalCandidates(technicians) {
  document.querySelectorAll(".withdrawal-candidate").forEach(row => {
    row.querySelector(".withdrawal-open").onclick = async event => {
      const button = event.currentTarget;
      const contractId = Number(row.dataset.withdrawalContract);
      const select = row.querySelector(".withdrawal-technician");
      const technicianId = Number(select.value);
      const technician = select.options[select.selectedIndex]?.text || "";
      if (!technicianId) return row.querySelector(".error").textContent = "Selecione o técnico responsável.";
      if (!confirm(`Abrir ocorrência e OS de retirada para o contrato #${contractId} e enviar para ${technician}?`)) return;
      button.disabled = true; button.textContent = "Validando e abrindo..."; row.querySelector(".error").textContent = "";
      try {
        const result = await api("/api/office/withdrawals", { method: "POST", body: JSON.stringify({ contract_id: contractId, technician_id: technicianId }), timeoutMs: 120000 });
        row.innerHTML = `<div class="withdrawal-created"><b>✓ Retirada aberta</b><span>${esc(result.notice)}</span><a class="primary button-link" href="#ordem/${esc(result.order_id)}">Abrir OS #${esc(result.order_id)}</a></div>`;
      } catch (error) { row.querySelector(".error").textContent = error.message; button.disabled = false; button.textContent = "Abrir retirada"; }
    };
  });
}

async function withdrawalDispatchLoad(force = false) {
  const button = $("#load-withdrawal-candidates");
  const target = $("#withdrawal-candidates");
  button.disabled = true; button.textContent = "Validando clientes no SGP...";
  target.innerHTML = '<section class="card empty">A consulta cruza suspensão, atraso, modem vinculado e OS abertas. Isso pode levar cerca de um minuto.</section>';
  try {
    const data = await api(`/api/office/withdrawal-candidates${force ? "?refresh=1" : ""}`, { timeoutMs: 180000 });
    target.innerHTML = `<section class="metrics withdrawal-metrics"><article class="card"><small>ELEGÍVEIS</small><b>${number(data.total)}</b></article><article class="card"><small>SUSPENSOS CONFERIDOS</small><b>${number(data.suspended_checked)}</b></article><article class="card"><small>EXCLUÍDOS COM OS</small><b>${number(data.excluded_open_os)}</b></article></section><section class="section-title"><div><h2>Clientes validados</h2><p>O contrato será validado novamente antes da criação.</p></div><button id="refresh-withdrawal-candidates" class="secondary">Atualizar lista</button></section><section class="withdrawal-candidate-list">${(data.items || []).map(item => withdrawalCandidateRow(item, data.technicians)).join("") || '<div class="card empty">Nenhum cliente atende a todos os critérios agora.</div>'}</section>`;
    bindWithdrawalCandidates(data.technicians || []);
    $("#refresh-withdrawal-candidates").onclick = () => withdrawalDispatchLoad(true);
  } catch (error) { target.innerHTML = `<section class="card empty error">${esc(error.message)}</section>`; }
  finally { button.disabled = false; button.textContent = "Carregar clientes elegíveis"; }
}

function withdrawalDispatchView() {
  setPage("Jogar retirada");
  $("#content").innerHTML = `<section class="hero"><div><small>RETIRADA DE APARELHO</small><h1>Jogar retirada</h1><p>Somente suspensos financeiros com mais de 40 dias, modem vinculado e nenhuma OS aberta.</p></div><b>↪</b></section><section class="card page-intro"><b>✓</b><h2>Consulta validada antes da abertura</h2><p>A ocorrência será aberta pelo app no usuário atual. A OS será externa, motivo Remoção de KIT, prioridade normal e Serviço Prestado ficará vazio para o técnico preencher.</p><button id="load-withdrawal-candidates" class="primary">Carregar clientes elegíveis</button></section><div id="withdrawal-candidates"></div>`;
  $("#load-withdrawal-candidates").onclick = () => withdrawalDispatchLoad(false);
}

function renderOnuLinkOlts(olts) {
  $("#content").innerHTML = `<section class="hero"><div><small>VINCULAR MODEM · ETAPA 1</small><h1>Escolha a OLT</h1><p>Depois escolha o modem que apareceu na rede.</p></div><b>${olts.length}</b></section>
    <section class="onu-choice-grid olt-card-grid">${olts.map(olt => `<button class="card onu-choice olt-card" data-super-olt="${olt.id}"><span>OLT #${olt.id}</span><h2>${esc(olt.name || "OLT")}</h2><p>${esc(olt.olttype || "Tipo não informado")}</p><b>Procurar modem →</b></button>`).join("") || '<section class="card empty">Nenhuma OLT disponível.</section>'}</section>`;
  document.querySelectorAll("[data-super-olt]").forEach(button => button.onclick = () => renderOnuLinkChoices(olts, button.dataset.superOlt));
}

async function renderOnuLinkChoices(olts, oltId) {
  loading("Procurando modens disponíveis na OLT...");
  try {
    const onus = await api(`/api/superadmin/onu-link/olts/${oltId}/unauthorized`);
    $("#content").innerHTML = `<button class="secondary" id="onu-link-back-olt">← Trocar OLT</button><section class="hero"><div><small>VINCULAR MODEM · ETAPA 2</small><h1>Escolha o modem</h1><p>Confira o GPON/serial e a posição encontrada.</p></div><b>${onus.length}</b></section><section class="order-list">${onus.map((onu, index) => `<button class="card order-row" data-super-onu="${index}"><span class="order-position">◉</span><div><small>GPON / SERIAL</small><strong>${esc(onu.id || onu.phy_addr)}</strong><p>${esc(onu.type || "Modelo não informado")}</p></div><time>Slot ${esc(onu.slot)} · PON ${esc(onu.pon)}</time><b>›</b></button>`).join("") || '<section class="card empty">Nenhum modem disponível nesta OLT.</section>'}</section>`;
    $("#onu-link-back-olt").onclick = () => renderOnuLinkOlts(olts);
    document.querySelectorAll("[data-super-onu]").forEach(button => button.onclick = () => renderOnuLinkClientForm(olts, oltId, onus[Number(button.dataset.superOnu)]));
  } catch (error) { errorView(error); }
}

function renderOnuLinkClientForm(olts, oltId, onu) {
  $("#content").innerHTML = `<button class="secondary" id="onu-link-back-onu">← Escolher outro modem</button><section class="hero"><div><small>VINCULAR MODEM · ETAPA 3</small><h1>Informe o cliente</h1><p>Modem ${esc(onu.id || onu.phy_addr)} · Slot ${esc(onu.slot)} · PON ${esc(onu.pon)}</p></div><b>◉</b></section><form id="onu-link-client-form" class="card page-intro"><label>ID do cliente ou contrato<input id="onu-link-client-id" type="number" min="1" required autofocus placeholder="ID do cadastro ou número do contrato"></label><button class="primary">Preencher dados</button><p id="onu-link-error" class="error"></p></form>`;
  $("#onu-link-back-onu").onclick = () => renderOnuLinkChoices(olts, oltId);
  $("#onu-link-client-form").onsubmit = async event => {
    event.preventDefault(); const clientId = Number($("#onu-link-client-id").value);
    if (!Number.isInteger(clientId) || clientId <= 0) return;
    loading("Preenchendo os dados do cliente...");
    try { const client = await api(`/api/superadmin/onu-link/client?contract_id=${clientId}`); await renderOnuLinkConfirm(client, olts, oltId, onu); }
    catch (error) { renderOnuLinkClientForm(olts, oltId, onu); $("#onu-link-client-id").value = clientId; if (error.name !== "AbortError") $("#onu-link-error").textContent = error.message; }
  };
}

async function renderOnuLinkConfirm(client, olts, oltId, onu) {
  try {
    const options = await api("/api/superadmin/onu-link/options");
    const label = item => String(item.name || item.code || item.nome || item.descricao || item.description || "").trim();
    const mode = (options.modes || []).find(item => /pppoe/i.test(label(item))) || (options.modes || [])[0];
    const template = (options.templates || []).find(item => /huawei.*bridg/i.test(label(item))) || (options.templates || [])[0];
    const type = (options.types || []).find(item => /^onu[\s_-]*1$/i.test(label(item))) || (options.types || []).find(item => label(item).toLowerCase().includes(String(onu.type || "").toLowerCase())) || (options.types || [])[0];
    if (!mode || !template || !type) throw new Error("O SGP não devolveu tipo, modo ou template para esta ONU.");
    $("#content").innerHTML = `<button class="secondary" id="onu-link-back-onu">← Escolher outro modem</button><form id="super-onu-link-form" class="card onu-form"><small>ÚLTIMA ETAPA</small><h1>Informe apenas a etiqueta</h1><div class="onu-form-grid"><label>Cliente<input value="${esc(client.client)}" readonly></label><label>Contrato<input value="${esc(client.contract_id)}" readonly></label><label>GPON / serial<input value="${esc(onu.id || onu.phy_addr)}" readonly></label><label>Rede<input value="Slot ${esc(onu.slot)} · PON ${esc(onu.pon)}" readonly></label><label class="wide">Etiqueta · CTO e porta *<input name="ident" required autofocus placeholder="Ex.: CTO 12 PT 3"></label></div><p class="error" id="super-onu-link-error"></p><button class="primary">Vincular modem pela API</button></form>`;
    $("#onu-link-back-onu").onclick = () => renderOnuLinkClientForm(olts, oltId, onu);
    $("#super-onu-link-form").onsubmit = async event => {
      event.preventDefault(); const button = event.currentTarget.querySelector("button.primary");
      const ident = event.currentTarget.elements.ident.value.trim();
      const payload = { client_id: client.client_id, id: onu.id || onu.phy_addr, slot: onu.slot, pon: onu.pon, onutype: type.id, mode: mode.id, onutemplate: template.id, contrato: client.contract_id, service: client.service, description: client.description, ident, pppoe_login: client.service, pppoe_password: "foxfibra" };
      if (!confirm(`Vincular o modem ${payload.id} ao cliente ${client.client} com a etiqueta ${ident}?`)) return;
      button.disabled = true; button.textContent = "Vinculando…";
      try {
        await api(`/api/superadmin/onu-link/olts/${oltId}/authorize`, { method: "POST", body: JSON.stringify(payload) });
        $("#content").innerHTML = `<section class="card purchase-success"><b>✓</b><h1>Modem vinculado</h1><p>${esc(client.client)} · contrato #${esc(client.contract_id)}</p><code>${esc(payload.id)}</code><small>${esc(ident)}</small><button id="onu-link-another" class="primary">Vincular outro modem</button></section>`;
        $("#onu-link-another").onclick = onuLinkView; notify("Modem vinculado com sucesso");
      } catch (error) { button.disabled = false; button.textContent = "Vincular modem pela API"; $("#super-onu-link-error").textContent = error.message; }
    };
  } catch (error) { errorView(error); }
}

let routeMonitorClock = null;
let routeMonitorRefreshTimer = null;
let routeMonitorRefreshing = false;
let routeMonitorMap = null;
let routeMonitorMarkers = new Map();
let openServiceMap = null;
let openServiceLayers = null;
let openServiceRefreshTimer = null;
let openServiceRefreshing = false;
let openServiceLastData = null;
let openServiceLastRouteData = null;
let itineraryTechnician = null;
let itineraryData = null;
let itineraryRequest = 0;
let itineraryLayer = null;
let itineraryError = '';
// Road geometry is optional.  The map must remain useful if the public router
// is slow or temporarily unavailable, so the straight segments stay as a
// lightweight fallback and routes are cached per exact itinerary.
const itineraryRoadCache = new Map();

function openServiceMapStop() {
  itineraryTechnician = null; itineraryData = null; itineraryLayer = null; itineraryError = ''; itineraryRequest++;
  document.body.classList.remove('map-is-expanded');
  if (openServiceRefreshTimer) window.clearInterval(openServiceRefreshTimer);
  openServiceRefreshTimer = null;
  openServiceRefreshing = false;
  if (openServiceMap) openServiceMap.remove();
  openServiceMap = null;
  openServiceLayers = null;
}

function technicianMapIcon(point) {
  const running = point.state === "Em execução";
  return L.divIcon({
    className: "combined-tech-marker-wrap",
    html: `<span class="combined-tech-marker ${running ? "running" : "closed"}">♟</span>`,
    iconSize: [30, 36], iconAnchor: [15, 32], tooltipAnchor: [0, -30], popupAnchor: [0, -30],
  });
}

function serviceMapIcon() {
  return L.divIcon({
    className: "combined-service-marker-wrap",
    html: '<span class="combined-service-marker">⌖</span>',
    iconSize: [30, 36], iconAnchor: [15, 32], tooltipAnchor: [0, -30], popupAnchor: [0, -30],
  });
}

async function focusTechnicianItinerary(technician, refresh = false) {
  const requestId = ++itineraryRequest;
  const changed = itineraryTechnician !== technician;
  itineraryTechnician = technician;
  if (changed) itineraryData = null;
  itineraryError = '';
  renderCombinedServiceMap(openServiceLastData || {}, openServiceLastRouteData || {}, !changed);
  try {
    const data = await api(`/api/office/itinerary?technician=${encodeURIComponent(technician)}${refresh ? '&refresh=1' : ''}`, { concurrent: true, timeoutMs: 120000 });
    if (requestId !== itineraryRequest || !openServiceMap) return;
    itineraryData = data;
  } catch (error) {
    if (requestId !== itineraryRequest || !openServiceMap) return;
    itineraryError = error.message;
  }
  renderCombinedServiceMap(openServiceLastData || {}, openServiceLastRouteData || {}, !changed);
}

function sameTechnician(left, right) {
  const normalize = value => String(value || '').normalize('NFD').replace(/[\u0300-\u036f]/g, '').trim().toLowerCase();
  return normalize(left) === normalize(right);
}

function clearTechnicianItinerary() {
  itineraryRequest++; itineraryTechnician = null; itineraryData = null; itineraryError = '';
  renderCombinedServiceMap(openServiceLastData || {}, openServiceLastRouteData || {}, false);
}

function drawTechnicianItinerary(preserveView) {
  $('.open-service-map-card')?.classList.toggle('has-itinerary', Boolean(itineraryTechnician));
  if (itineraryLayer) { itineraryLayer.remove(); itineraryLayer = null; }
  let panel = $('#map-itinerary');
  if (!itineraryTechnician) { panel?.remove(); openServiceMap?.invalidateSize(); return; }
  if (!panel) {
    panel = document.createElement('section'); panel.id = 'map-itinerary';
    $('.open-service-map-card').appendChild(panel);
  }
  const data = itineraryData;
  const completed = data?.completed || [], current = data?.current || [], upcoming = data?.upcoming || [];
  const allStops = [...completed, ...current, ...upcoming];
  const orderNumber = item => allStops.indexOf(item) + 1;
  const timeLabel = value => value ? String(value).slice(11,16) : 'Sem horário';
  const rows = (items, kind) => items.map(item => `<li><span class="itinerary-number ${kind}">${orderNumber(item)}</span><div><a href="#ordem/${Number(item.id)}">OS #${esc(item.id)} · ${esc(item.client)}</a><small>${esc(item.reason)} · ${esc(kind === 'done' ? 'Encerrada '+timeLabel(item.finished_at) : kind === 'current' ? 'Em execução' : 'Agendada '+timeLabel(item.scheduled_at))}${item.status === 3 ? ' · Pendente' : ''}${!item.position_available ? ' · Sem coordenadas' : ''}</small></div></li>`).join('');
  panel.innerHTML = `<header><strong>Itinerário · ${esc(itineraryTechnician)}</strong><button type="button" class="secondary" id="itinerary-clear">Voltar ao mapa geral</button></header><p class="itinerary-legend">Verde: serviços encerrados · Laranja: em execução e próximos. A sequência segue o horário do dia e as setas indicam o sentido.</p>${itineraryError ? `<p class="error">${esc(itineraryError)} <button type="button" id="itinerary-retry">Tentar novamente</button></p>` : ''}${!data ? '<p>Carregando itinerário do dia...</p>' : `<p id="itinerary-route-state">Calculando o caminho pelas ruas...</p><div class="itinerary-groups"><div><h3>Encerrados (${completed.length})</h3><ol>${rows(completed,'done') || '<li>Nenhum realizado.</li>'}</ol></div><div><h3>Em execução (${current.length})</h3><ol>${rows(current,'current') || '<li>Nenhum em execução.</li>'}</ol></div><div><h3>Próximos (${upcoming.length})</h3><ol>${rows(upcoming,'next') || '<li>Nenhum agendado.</li>'}</ol></div></div>${data.missing_coordinates ? `<p>${data.missing_coordinates} serviço(s) sem coordenadas: exibidos na lista, sem inventar trechos no mapa.</p>` : ''}`}`;
  $('#itinerary-clear').onclick = clearTechnicianItinerary;
  if ($('#itinerary-retry')) $('#itinerary-retry').onclick = () => focusTechnicianItinerary(itineraryTechnician, true);
  itineraryLayer = L.layerGroup().addTo(openServiceMap);
  const valid = item => item?.position_available && item.latitude != null && item.longitude != null && Number.isFinite(Number(item.latitude)) && Number.isFinite(Number(item.longitude));
  const bounds = [];
  const technician = (openServiceLastRouteData?.map_points || []).find(p => p.technician === itineraryTechnician);
  if (valid(technician)) bounds.push([technician.latitude, technician.longitude]);
  const addPoints = (items, color) => items.forEach(item => {
    if (!valid(item)) return;
    const point = [item.latitude, item.longitude]; bounds.push(point);
    L.marker(point, {zIndexOffset: 900, icon:L.divIcon({className:'itinerary-stop',html:`<span style="background:${color}">${orderNumber(item)}</span>`,iconSize:[34,34],iconAnchor:[17,17]})})
      .addTo(itineraryLayer).bindPopup(`<b>${esc(item.client)}</b><br>OS #${esc(item.id)} · ${esc(item.reason)}<br>${esc(item.status_text)}<br><a href="#ordem/${Number(item.id)}">Abrir OS</a>`);
  });
  addPoints(completed,'#25805a'); addPoints(current,'#e66b13'); addPoints(upcoming,'#e66b13');
  // No straight-line fallback: the itinerary is drawn only from road geometry.
  drawItineraryRoads(allStops, itineraryLayer, itineraryRequest);
  openServiceMap.invalidateSize();
  if (!preserveView && bounds.length) openServiceMap.fitBounds(L.latLngBounds(bounds).pad(.22),{maxZoom:15,animate:true});
  window.setTimeout(()=>openServiceMap?.invalidateSize(),80);
}

function drawItineraryRoads(allStops, layer, requestId) {
  const valid = item => item?.position_available && Number.isFinite(Number(item.latitude)) && Number.isFinite(Number(item.longitude));
  const renderRoad = async () => {
    const stops = allStops.filter(valid);
    const stateNode = $('#itinerary-route-state');
    if (stops.length < 2) {
      if (stateNode) stateNode.textContent = 'Não há duas OS com coordenadas para montar a rota.';
      return;
    }
    const coordinates = stops.map(item => `${Number(item.longitude)},${Number(item.latitude)}`).join(';');
    const key = `sequence|${coordinates}`;
    let route = itineraryRoadCache.get(key);
    if (!route) {
      try {
        route = await api(`/api/office/itinerary-route?points=${encodeURIComponent(stops.map(item => `${Number(item.latitude)},${Number(item.longitude)}`).join(';'))}`, { concurrent: true, timeoutMs: 30000 });
        if (!route?.legs?.length) throw new Error('empty route');
        itineraryRoadCache.set(key, route);
      } catch (error) {
        if (stateNode && requestId === itineraryRequest) stateNode.textContent = `Não foi possível traçar pelas ruas: ${error.message}`;
        return;
      }
    }
    if (requestId !== itineraryRequest || layer !== itineraryLayer || !openServiceMap) return;
    (route.legs || []).forEach((leg, index) => {
      const from = stops[index], to = stops[index + 1];
      if (!from || !to) return;
      const geometry = leg.geometry?.coordinates || [];
      if (geometry.length < 2) return;
      const color = Number(to.status) === 1 ? '#25805a' : '#e66b13';
      const roadPoints = geometry.map(([lng, lat]) => [lat, lng]);
      L.polyline(roadPoints, { color, weight: 5, opacity: .96, lineJoin: 'round', lineCap: 'round' }).addTo(layer);
      const middleIndex = Math.max(1, Math.floor(geometry.length / 2));
      const before = geometry[middleIndex - 1], middle = geometry[middleIndex];
      const angle = Math.atan2(-(middle[1] - before[1]), middle[0] - before[0]) * 180 / Math.PI;
      L.marker([middle[1], middle[0]], { interactive: false, zIndexOffset: 950, icon: L.divIcon({ className: 'itinerary-direction', html: `<span style="--direction:${angle}deg">➤</span><small>${(Number(leg.distance || 0) / 1000).toFixed(1)} km</small>`, iconSize: [62, 28], iconAnchor: [31, 14] }) }).addTo(layer);
    });
    if (stateNode) stateNode.textContent = `Rota pelas ruas · ${route.legs.length} trecho(s) em sequência`;
  };
  renderRoad();
}

function serviceMapDistance(service, technician) {
  const values = [service.latitude, service.longitude, technician.latitude, technician.longitude];
  if (!service.position_available || !technician.position_available || values.some(value => value == null || value === '' || !Number.isFinite(Number(value)))) return null;
  const [a, b, c, d] = values.map(value => Number(value) * Math.PI / 180);
  const h = Math.sin((c-a)/2)**2 + Math.cos(a)*Math.cos(c)*Math.sin((d-b)/2)**2;
  return 6371 * 2 * Math.asin(Math.sqrt(Math.min(1, h)));
}

function setTrackingShareUrl(url){const value=String(url||'').trim();const copy=$('#copy-order-tracking'),field=$('#order-tracking-share-url'),share=$('#share-order-tracking');if(copy){copy.dataset.url=value;copy.disabled=!value;}if(field){field.value=value;field.hidden=!value;}if(share)share.hidden=!value;}
async function copyCustomerTrackingLink(){const url=$('#copy-order-tracking')?.dataset.url;if(!url)return;try{await navigator.clipboard.writeText(url);notify('Link de acompanhamento copiado.');}catch{const field=$('#order-tracking-share-url');if(field){field.value=url;field.hidden=false;field.focus();field.select();}notify('Selecione e copie o link exibido.');}}
function showCustomerTrackingLink(url) {
  if (!$('#order-tracking-share-url')) {
    document.querySelector('#customer-tracking-link-dialog')?.remove();
    const dialog = document.createElement('dialog');
    dialog.id = 'customer-tracking-link-dialog';
    dialog.innerHTML = '<h2>Link do cliente</h2><p>O cliente confirma o CPF do titular para acompanhar esta visita.</p><input id="order-tracking-share-url" readonly aria-label="Link do cliente"><div class="tracking-actions"><button id="copy-order-tracking" class="primary">Copiar link</button><button id="share-order-tracking" class="secondary">Compartilhar link</button><button class="secondary" data-close-link>Fechar</button></div>';
    document.body.appendChild(dialog);
    dialog.querySelector('[data-close-link]').onclick = () => dialog.close();
    dialog.onclose = () => dialog.remove();
    dialog.querySelector('#copy-order-tracking').onclick = copyCustomerTrackingLink;
    dialog.querySelector('#share-order-tracking').onclick = shareCustomerTrackingLink;
    dialog.showModal();
  }
  setTrackingShareUrl(url);
  const status = $('#order-tracking-state');
  if (status) status.textContent = 'Link criado. Use “Copiar link” ou “Compartilhar link” para encaminhar ao cliente.';
}
async function createCustomerTrackingLink(orderId, button) {
  if (!confirm('Gerar um link temporário para este cliente acompanhar somente o técnico desta visita?')) return;
  button.disabled = true;
  try {
    let result;
    try {
      result = await api(`/api/office/orders/${Number(orderId)}/tracking-link`, {method:'POST', body:'{}', concurrent:true, timeoutMs:90000});
    } catch (error) {
      if (error.status !== 422 || !error.message.toLowerCase().includes('sgp não forneceu um cpf válido')) throw error;
      const cpf = window.prompt('O SGP não retornou o CPF do titular desta OS. Confira o cadastro e digite o CPF do titular para proteger o link:');
      if (cpf === null) return;
      result = await api(`/api/office/orders/${Number(orderId)}/tracking-link`, {method:'POST', body:JSON.stringify({cpf}), concurrent:true, timeoutMs:90000});
    }
    showCustomerTrackingLink(result.url);
    notify('Link criado e pronto para compartilhar.');
  } catch (error) {
    notify(error.message);
    const status = $('#order-tracking-state');
    if (status) status.textContent = error.message;
  } finally { button.disabled = false; }
}
async function shareCustomerTrackingLink(){const url=$('#copy-order-tracking')?.dataset.url;if(!url)return;try{if(navigator.share){await navigator.share({title:'Acompanhe a visita técnica',text:'Acompanhe a chegada do técnico da SG Fibra:',url});return;}await navigator.clipboard.writeText(`Acompanhe a chegada do técnico da SG Fibra:\n${url}`);notify('Mensagem com o link copiada.');}catch(error){if(error.name==='AbortError')return;const field=$('#order-tracking-share-url');if(field){field.value=url;field.hidden=false;field.focus();field.select();}notify('Selecione e copie o link exibido.');}}

function serviceMapFilters(items, points) {
  const tech = $('#map-filter-tech')?.value || '';
  const type = $('#map-filter-type')?.value || '';
  const search = ($('#map-filter-search')?.value || '').normalize('NFD').replace(/[\u0300-\u036f]/g, '').toLowerCase();
  const near = points.find(point => point.technician === $('#map-filter-near')?.value);
  const selected = items.filter(item => (!tech || (tech === '__none' ? !item.technician || item.technician === 'Sem técnico' : item.technician === tech)) && (!type || item.reason === type) && (!search || `${item.client} ${item.order_id} ${item.address}`.normalize('NFD').replace(/[\u0300-\u036f]/g, '').toLowerCase().includes(search)));
  if (near) selected.sort((a,b) => (serviceMapDistance(a,near) ?? Infinity) - (serviceMapDistance(b,near) ?? Infinity));
  return { items: selected, near };
}

function installServiceMapControls() {
  const card = $('.open-service-map-card');
  if (!card || $('#map-logistics-controls')) return;
  card.querySelector('header').insertAdjacentHTML('beforeend', '<button id="map-fullscreen" class="secondary" type="button">⛶ Ver mapa em tela cheia</button>');
  card.querySelector('#open-service-map').insertAdjacentHTML('beforebegin', `<section id="map-logistics-controls" class="map-logistics-controls"><label>Técnico<select id="map-filter-tech"><option value="">Todos os técnicos</option><option value="__none">Sem técnico</option></select></label><label>Tipo de serviço<select id="map-filter-type"><option value="">Todos os tipos</option></select></label><label>Cliente, OS ou endereço<input id="map-filter-search" type="search" placeholder="Pesquisar serviços"></label><label>Mais próximos de<select id="map-filter-near"><option value="">Sem ordenar por proximidade</option></select></label><p>Proximidade em linha reta, calculada pela posição do último serviço conhecido do técnico. Não representa GPS ao vivo nem tempo de viagem.</p><p id="map-filter-count" aria-live="polite"></p></section>`);
  $('#map-fullscreen').onclick = () => {
    const expanded = card.classList.toggle('map-expanded');
    document.body.classList.toggle('map-is-expanded', expanded);
    $('#map-fullscreen').textContent = expanded ? '✕ Sair da tela cheia' : '⛶ Ver mapa em tela cheia';
    window.setTimeout(() => { openServiceMap?.invalidateSize(); if (itineraryTechnician && openServiceMap) drawTechnicianItinerary(false); }, 80);
  };
  const rerender = () => renderCombinedServiceMap(openServiceLastData || {}, openServiceLastRouteData || {}, false);
  ['map-filter-tech', 'map-filter-type', 'map-filter-near'].forEach(id => $('#'+id).onchange = rerender);
  $('#map-filter-search').oninput = rerender;
  card.addEventListener('keydown', event => { if (event.key === 'Escape' && card.classList.contains('map-expanded')) $('#map-fullscreen').click(); });
}

function refreshServiceMapOptions(items, points) {
  const options = (id, values, initial) => {
    const select = $('#'+id);
    if (!select) return;
    const value = select.value;
    const html = initial + [...new Set(values.filter(Boolean))].sort((a,b) => a.localeCompare(b,'pt-BR')).map(name => `<option value="${esc(name)}">${esc(name)}</option>`).join('');
    if (select.innerHTML !== html) { select.innerHTML = html; select.value = value; }
  };
  options('map-filter-tech', items.map(item => item.technician).filter(name => name !== 'Sem técnico'), '<option value="">Todos os técnicos</option><option value="__none">Sem técnico</option>');
  options('map-filter-type', items.map(item => item.reason), '<option value="">Todos os tipos</option>');
  options('map-filter-near', points.filter(point => point.position_available).map(point => point.technician), '<option value="">Sem ordenar por proximidade</option>');
}

async function assignServiceDialog(orderId) {
  if (document.querySelector('.assignment-dialog')) return;
  const dialog = document.createElement('dialog');
  dialog.className = 'route-event-dialog assignment-dialog';
  dialog.innerHTML = `<section><button type="button" class="dialog-close" aria-label="Fechar">×</button><h2>Direcionar OS #${esc(orderId)}</h2><div class="assignment-body"><p>Consultando os técnicos disponíveis no SGP...</p></div></section>`;
  document.body.appendChild(dialog);
  dialog.querySelector('.dialog-close').onclick = () => dialog.close();
  dialog.addEventListener('close', () => dialog.remove());
  dialog.showModal();
  let saving = false;
  dialog.addEventListener('cancel', event => { if (saving) event.preventDefault(); });
  try {
    const options = await api(`/api/office/orders/${Number(orderId)}/assignment`, { concurrent: true, timeoutMs: 100000 });
    if (!dialog.isConnected) return;
    dialog.querySelector('.assignment-body').innerHTML = `<form><label>Técnico responsável<select required>${options.technicians.map(t => `<option value="${esc(t.id)}" ${String(t.id) === String(options.technician_id) ? 'selected' : ''}>${esc(t.name)}</option>`).join('')}</select></label><p>Agendamento: ${esc(options.scheduled_at || 'não informado')}. Apenas o responsável será alterado; os demais dados da OS serão mantidos.</p><p class="error" role="alert"></p><button type="submit" class="primary">Confirmar técnico no SGP</button></form>`;
    if (!options.technician_id) dialog.querySelector('select').selectedIndex = -1;
    dialog.querySelector('form').onsubmit = async event => {
      event.preventDefault();
      if (saving) return;
      saving = true;
      const submit = dialog.querySelector('[type=submit]');
      submit.disabled = true;
      dialog.querySelector('.dialog-close').disabled = true;
      dialog.querySelector('select').disabled = true;
      submit.textContent = 'Salvando e conferindo no SGP...';
      try {
        const result = await api(`/api/office/orders/${Number(orderId)}/assignment`, { method: 'POST', concurrent: true, timeoutMs: 180000,
          body: JSON.stringify({ technician_id: Number(dialog.querySelector('select').value), revision: options.revision }) });
        dialog.close();
        notify(`OS #${orderId} direcionada para ${result.technician}.`);
      } catch (error) {
        dialog.querySelector('.error').textContent = `${error.message} Feche e consulte novamente antes de tentar outra vez.`;
        // An uncertain write must not offer an immediate duplicate submission.
        submit.textContent = 'Consulte novamente para continuar';
        return;
      } finally { saving = false; dialog.querySelector('.dialog-close').disabled = false; }
      try {
        if (location.hash.startsWith('#mapa-servicos')) {
          const data = await loadCombinedServiceMap(true);
          if (location.hash.startsWith('#mapa-servicos')) renderCombinedServiceMap(data.serviceData, data.routeData, true);
        } else if (location.hash.startsWith('#acompanhar-rota')) {
          const data = await api('/api/superadmin/route-monitor?refresh=1', { concurrent: true, timeoutMs: 90000 });
          if (location.hash.startsWith('#acompanhar-rota')) routeMonitorRender(data);
        }
      } catch (_) { notify('Atribuição salva. A atualização da lista será tentada na próxima consulta.'); }
    };
  } catch (error) {
    if (dialog.isConnected) dialog.querySelector('.assignment-body').textContent = error.message;
  }
}

function bindServiceAssignments(root) {
  root.querySelectorAll('[data-assign-order]').forEach(button => {
    button.onclick = () => assignServiceDialog(Number(button.dataset.assignOrder));
  });
}

function renderServiceLogisticsList(items, points, near) {
  const list = $('.open-service-list');
  if (!list) return;
  list.innerHTML = items.map(item => {
    const nearest = points.map(point => ({ point, distance: serviceMapDistance(item,point) })).filter(entry => entry.distance != null).sort((a,b) => a.distance-b.distance).slice(0,3);
    return `<article class="card"><div class="open-service-row"><span class="${item.position_available ? 'located' : 'missing'}">⌖</span><div><small>${item.order_id ? `OS #${esc(item.order_id)}` : `Ocorrência #${esc(item.occurrence_id)}`} · ${esc(item.scheduled_at || 'Sem horário')}</small><h3>${esc(item.client)}</h3><p>${esc(item.address)} · ${esc(item.reason)}</p></div><b>${esc(item.technician || 'Sem técnico')}</b></div>${near ? `<p>Distância de ${esc(near.technician)}: ${serviceMapDistance(item,near) == null ? 'sem coordenadas' : number(serviceMapDistance(item,near))+' km'}</p>` : ''}<p class="muted">Mais próximos (último serviço): ${nearest.map(entry => `${esc(entry.point.technician)} · ${number(entry.distance)} km`).join(' / ') || 'posição indisponível'}</p>${item.order_id ? `<a class="secondary button-link" href="#ordem/${Number(item.order_id)}">Abrir OS</a> <button class="primary" onclick="createCustomerTrackingLink(${Number(item.order_id)},this)">Gerar link do cliente</button>` : ''}</article>`;
  }).join('') || '<section class="card empty">Nenhum serviço corresponde aos filtros.</section>';
  list.querySelectorAll('a[href^="#ordem/"]').forEach(link => {
    const button = document.createElement('button');
    button.className = 'primary';
    button.type = 'button';
    button.textContent = 'Direcionar técnico';
    button.dataset.assignOrder = link.getAttribute('href').split('/')[1];
    link.after(button);
  });
  bindServiceAssignments(list);
  if ($('#map-filter-count')) $('#map-filter-count').textContent = `${items.length} serviço(s) no filtro`;
}

function renderCombinedServiceMap(serviceData, routeData, preserveView = false) {
  const technicians = routeData.map_points || [];
  installServiceMapControls();
  refreshServiceMapOptions(serviceData.items || [], technicians);
  const { items, near } = serviceMapFilters(serviceData.items || [], technicians);
  renderServiceLogisticsList(items, technicians, near);
  const serviceLocated = items.filter(item => item.position_available && Number.isFinite(Number(item.latitude)) && Number.isFinite(Number(item.longitude)));
  const technicianLocated = technicians.filter(item => item.position_available && Number.isFinite(Number(item.latitude)) && Number.isFinite(Number(item.longitude)));
  const container = $("#open-service-map");
  if (!container || !window.L) return;
  if (!openServiceMap) {
    openServiceMap = L.map(container).setView([-23.55, -46.63], 11);
    // The volunteer OSM service blocked the shared production IP and CARTO now
    // requires an API key. Use the public Esri World Street Map tiles instead.
    L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}", {
      maxZoom: 19,
      attribution: 'Tiles &copy; <a href="https://www.esri.com/">Esri</a> — Esri, HERE, Garmin, FAO, NOAA, USGS, OpenStreetMap contributors',
    }).addTo(openServiceMap);
    openServiceLayers = { services: L.layerGroup().addTo(openServiceMap), technicians: L.layerGroup().addTo(openServiceMap) };
  }
  openServiceLayers.services.clearLayers();
  openServiceLayers.technicians.clearLayers();
  serviceLocated.forEach(item => L.marker([item.latitude, item.longitude], { icon: serviceMapIcon() })
    .addTo(openServiceLayers.services).setOpacity(itineraryTechnician && !sameTechnician(item.technician, itineraryTechnician) ? .3 : 1)
    .bindTooltip(`${item.order_id ? `OS #${esc(item.order_id)}` : `Ocorrência #${esc(item.occurrence_id)}`} · ${esc(item.client)}`)
    .bindPopup(`<b>${item.order_id ? `OS #${esc(item.order_id)}` : `Ocorrência #${esc(item.occurrence_id)}`}</b><br>${esc(item.client)}<br>${esc(item.address)}<br>${esc(item.technician)}${item.order_id ? `<br><button type="button" class="primary" data-assign-order="${Number(item.order_id)}">Direcionar técnico</button>` : ''}`)
    .on('popupopen', event => bindServiceAssignments(event.popup.getElement())));
  technicianLocated.forEach(point => L.marker([point.latitude, point.longitude], { icon: technicianMapIcon(point) })
    .addTo(openServiceLayers.technicians).setOpacity(itineraryTechnician && !sameTechnician(point.technician, itineraryTechnician) ? .5 : 1)
    .bindTooltip(esc(point.technician), { permanent: true, direction: "top", opacity: itineraryTechnician && !sameTechnician(point.technician, itineraryTechnician) ? .5 : .95 })
    .on('click', () => focusTechnicianItinerary(point.technician)));
  const serviceToggle = $("#map-toggle-services");
  const technicianToggle = $("#map-toggle-technicians");
  const applyVisibility = () => {
    [[serviceToggle, openServiceLayers.services], [technicianToggle, openServiceLayers.technicians]].forEach(([button, layer]) => {
      const visible = button.getAttribute("aria-pressed") === "true";
      if (visible && !openServiceMap.hasLayer(layer)) layer.addTo(openServiceMap);
      if (!visible && openServiceMap.hasLayer(layer)) openServiceMap.removeLayer(layer);
      button.classList.toggle("inactive", !visible);
    });
  };
  [serviceToggle, technicianToggle].forEach(button => button.onclick = () => {
    button.setAttribute("aria-pressed", button.getAttribute("aria-pressed") === "true" ? "false" : "true");
    applyVisibility();
  });
  applyVisibility();
  const allMarkers = [...openServiceLayers.services.getLayers(), ...openServiceLayers.technicians.getLayers()];
  if (!itineraryTechnician && !preserveView && allMarkers.length) openServiceMap.fitBounds(L.featureGroup(allMarkers).getBounds().pad(.15), { maxZoom: 15 });
  drawTechnicianItinerary(preserveView);
  $("#map-service-count").textContent = number(serviceLocated.length);
  $("#map-tech-count").textContent = number(technicianLocated.length);
  $("#map-updated").textContent = `Atualizado em ${new Date().toLocaleTimeString("pt-BR", { hour: "2-digit", minute: "2-digit" })}`;
  window.setTimeout(() => openServiceMap?.invalidateSize(), 50);
}

async function loadCombinedServiceMap(refresh = false) {
  const suffix = refresh ? "?refresh=1" : "";
  const [serviceResult, routeResult] = await Promise.allSettled([
    api(`/api/office/open-service-map${suffix}`, { timeoutMs: 90000, concurrent: true }),
    api(`/api/superadmin/route-monitor${suffix}`, { timeoutMs: 90000, concurrent: true }),
  ]);
  if (serviceResult.status === "fulfilled") openServiceLastData = serviceResult.value;
  if (routeResult.status === "fulfilled") openServiceLastRouteData = routeResult.value;
  if (!openServiceLastData && !openServiceLastRouteData) {
    throw new Error(serviceResult.reason?.message || routeResult.reason?.message || "Não foi possível carregar o mapa agora.");
  }
  return {
    serviceData: openServiceLastData || { date: new Date().toISOString().slice(0, 10), items: [], total: 0, located: 0 },
    routeData: openServiceLastRouteData || { map_points: [] },
    serviceError: serviceResult.status === "rejected" ? serviceResult.reason?.message : "",
    routeError: routeResult.status === "rejected" ? routeResult.reason?.message : "",
  };
}

async function openServiceMapView() {
  openServiceMapStop();
  setPage("Mapa de serviços");
  loading("Carregando serviços e posições da equipe...");
  try {
    const { serviceData: data, routeData, serviceError, routeError } = await loadCombinedServiceMap(true);
    const items = data.items || [];
    $("#content").innerHTML = `
      <section class="hero"><div><small>OCORRÊNCIAS E OS ABERTAS</small><h1>Mapa de serviços de hoje</h1><p>Serviços abertos com agendamento para ${esc(new Date(`${data.date}T12:00:00`).toLocaleDateString("pt-BR"))}.</p></div><b>${number(data.total)}</b></section>
      <section class="metrics open-service-metrics"><article class="card"><small>OS ABERTAS</small><b>${number(data.total)}</b></article><article class="card"><small>NO MAPA</small><b>${number(data.located)}</b></article><article class="card"><small>SEM COORDENADAS</small><b>${number(data.total - data.located)}</b></article></section>
      <section class="card open-service-map-card"><header><div><small>VISÃO OPERACIONAL</small><h2>Técnicos e serviços abertos</h2><p id="map-updated">Mapa atualizado agora</p></div><div class="map-layer-filters"><button id="map-toggle-technicians" type="button" aria-pressed="true"><i class="technicians"></i>Técnicos (<span id="map-tech-count">0</span>)</button><button id="map-toggle-services" type="button" aria-pressed="true"><i class="services"></i>Ocorrências (<span id="map-service-count">0</span>)</button></div></header><div id="open-service-map"></div></section>
      <p id="map-partial-warning" class="map-partial-warning">${esc([serviceError && `Ocorrências: ${serviceError}`, routeError && `Técnicos: ${routeError}`].filter(Boolean).join(" · "))}</p>
      <p class="map-no-service">${(routeData.map_points || []).filter(item => item.no_service).map(item => `Técnico “${esc(item.technician)}” não realizou nenhum serviço hoje.`).join(" · ")}</p>
      <section class="section-title"><div><h2>Serviços agendados</h2><p>Clique na OS para abrir os detalhes no SG Fibra Técnico.</p></div></section>
      <section class="open-service-list">${items.map(item => `${item.order_id ? `<a class="card open-service-row" href="#ordem/${esc(item.order_id)}">` : '<article class="card open-service-row">'}<span class="${item.position_available ? "located" : "missing"}">⌖</span><div><small>${item.order_id ? `OS #${esc(item.order_id)}` : `Ocorrência #${esc(item.occurrence_id)}`} · ${esc(item.scheduled_at || "Sem horário")}</small><h3>${esc(item.client)}</h3><p>${esc(item.address)} · ${esc(item.reason)}</p></div><b>${esc(item.technician)}</b>${item.order_id ? "</a>" : "</article>"}`).join("") || '<div class="card empty">Nenhuma ocorrência aberta agendada para hoje.</div>'}</section>`;
    renderCombinedServiceMap(data, routeData);
    openServiceRefreshTimer = window.setInterval(async () => {
      if (openServiceRefreshing || document.querySelector('.assignment-dialog') || !location.hash.startsWith("#mapa-servicos")) return;
      const hour = Number(new Intl.DateTimeFormat("pt-BR", { hour: "2-digit", hour12: false, timeZone: "America/Sao_Paulo" }).format(new Date()));
      if (hour < 8 || hour >= 19) return;
      openServiceRefreshing = true;
      try {
        const fresh = await loadCombinedServiceMap(true);
        if (location.hash.startsWith("#mapa-servicos")) {
          renderCombinedServiceMap(fresh.serviceData, fresh.routeData, true);
          const warning = $("#map-partial-warning");
          if (warning) warning.textContent = [fresh.serviceError && `Ocorrências: ${fresh.serviceError}`, fresh.routeError && `Técnicos: ${fresh.routeError}`].filter(Boolean).join(" · ");
          if (itineraryTechnician) await focusTechnicianItinerary(itineraryTechnician, true);
        }
      } catch (error) { console.warn("Atualização silenciosa do mapa falhou:", error.message); }
      finally { openServiceRefreshing = false; }
    }, 60000);
  } catch (error) { errorView(error); }
}

let coverageMap = null;
let coverageLayers = null;
let coverageMapData = null;
let coverageBuildTimer = null;
let coverageDrawingPoints = [];
let coverageDrawingLayer = null;

function coverageMapStop() {
  window.clearTimeout(coverageBuildTimer);
  document.body.classList.remove('pon-map-page');
  if (coverageMap) coverageMap.remove();
  coverageMap = null; coverageLayers = null; coverageMapData = null;
}

function coverageFilteredClients() {
  const olt = $('#coverage-filter-olt')?.value || '';
  const slot = $('#coverage-filter-slot')?.value || '';
  const pon = $('#coverage-filter-pon')?.value || '';
  return (coverageMapData?.clients || []).filter(item =>
    (!olt || String(item.olt_id) === olt) && (!slot || String(item.slot) === slot) &&
    (!pon || String(item.pon) === pon));
}

function ponColor(group) {
  let hash = 0;
  for (const char of `${group.olt_id}-${group.slot}-${group.pon}`) hash = ((hash * 31) + char.charCodeAt(0)) >>> 0;
  return `hsl(${(hash * 137.507764) % 360} 65% 38%)`;
}

function ponRouteKey(group) { return `${group.olt_id}:${group.slot}:${group.pon}`; }

function coverageFocusPon(group) {
  const oltNode = $('#coverage-filter-olt'), slotNode = $('#coverage-filter-slot'), ponNode = $('#coverage-filter-pon');
  if (!oltNode || !slotNode || !ponNode) return;
  oltNode.value = String(group.olt_id); coverageRefreshFilterOptions();
  slotNode.value = String(group.slot); coverageRefreshFilterOptions();
  ponNode.value = String(group.pon);
  coverageRenderMap(false);
}

function coverageSelectedClients() {
  const olt=$('#coverage-filter-olt')?.value || '', slot=$('#coverage-filter-slot')?.value || '', pon=$('#coverage-filter-pon')?.value || '';
  if (!slot || !pon) return [];
  return (coverageMapData?.clients || []).filter(item => (!olt || String(item.olt_id)===olt) && String(item.slot)===slot && String(item.pon)===pon);
}

function coverageHasSelectedPon() {
  return Boolean($('#coverage-filter-olt')?.value && $('#coverage-filter-slot')?.value && $('#coverage-filter-pon')?.value);
}

function coverageRenderClientList() {
  const panel=$('#coverage-client-panel'), list=$('#coverage-client-list');
  if (!panel || !list) return;
  const clients=coverageSelectedClients();
  panel.classList.toggle('hidden', !coverageHasSelectedPon());
  list.innerHTML=clients.map(item=>`<article><b>${esc(item.client || 'Cliente não informado')}</b><span>${esc(item.address || 'Endereço não informado')}</span><small>Contrato ${esc(item.contract_id || '—')}</small></article>`).join('') || '<p>Nenhum cliente localizado nessa PON.</p>';
  $('#coverage-client-count').textContent=number(clients.length);
}

function coverageSearchAddress() {
  const value=String($('#coverage-address-search')?.value || '').trim().toLocaleLowerCase('pt-BR');
  if (value.length < 3) return notify('Digite ao menos três caracteres da rua ou do CEP.');
  const digits=value.replace(/\D/g,'');
  const matches=(coverageMapData?.clients || []).filter(item=>{
    const address=String(item.address || '').toLocaleLowerCase('pt-BR');
    return address.includes(value) || (digits.length>=5 && address.replace(/\D/g,'').includes(digits));
  });
  if (!matches.length) return notify('Rua ou CEP não encontrado nos endereços salvos.');
  const points=matches.map(item=>[Number(item.located_latitude ?? item.latitude),Number(item.located_longitude ?? item.longitude)]).filter(point=>point.every(Number.isFinite));
  if (points.length) coverageMap.fitBounds(points,{padding:[50,50],maxZoom:18});
  const layerInput=document.querySelector('[data-coverage-layer="clients"]');
  if(layerInput&&!layerInput.checked){layerInput.checked=true;coverageLayers.clients.addTo(coverageMap);}
  notify(`${number(matches.length)} endereço(s) encontrado(s).`);
}

function coverageResetDrawing() {
  coverageDrawingPoints=[];
  if (coverageDrawingLayer && coverageMap) coverageMap.removeLayer(coverageDrawingLayer);
  coverageDrawingLayer=null;
  const count=$('#coverage-draw-count'); if(count) count.textContent='0 pontos';
}

function coverageUpdateDrawing() {
  if (coverageDrawingLayer) coverageMap.removeLayer(coverageDrawingLayer);
  coverageDrawingLayer=L.polygon(coverageDrawingPoints,{color:'#e84b2c',weight:3,dashArray:'7 5',fillOpacity:.18}).addTo(coverageMap);
  $('#coverage-draw-count').textContent=`${coverageDrawingPoints.length} ponto(s)`;
}

async function coverageSaveDrawing(event) {
  event.preventDefault();
  if(coverageDrawingPoints.length<3) return notify('Marque pelo menos três pontos no mapa.');
  const oltNode=$('#coverage-draw-olt'), button=event.submitter; button.disabled=true;
  try {
    const ring=coverageDrawingPoints.map(point=>[point[1],point[0]]); ring.push([...ring[0]]);
    const saved=await api('/api/superadmin/coverage-pon-areas',{method:'POST',body:JSON.stringify({
      olt_id:oltNode.value,olt_name:oltNode.options[oltNode.selectedIndex]?.text || '',slot:$('#coverage-draw-slot').value,
      pon:$('#coverage-draw-pon').value,note:$('#coverage-draw-note').value,geometry:{type:'Polygon',coordinates:[ring]},
    })});
    coverageMapData.manual_areas=[...(coverageMapData.manual_areas||[]),saved];
    coverageResetDrawing(); $('#coverage-draw-form').classList.add('hidden'); coverageRenderMap(true); notify('Área da PON salva no banco de dados.');
  } catch(error){notify(error.message);} finally{button.disabled=false;}
}

async function coverageDeleteManualArea(id) {
  if(!window.confirm('Excluir esta área desenhada?')) return;
  try { await api(`/api/superadmin/coverage-pon-areas/${Number(id)}`,{method:'DELETE'});
    coverageMapData.manual_areas=(coverageMapData.manual_areas||[]).filter(item=>Number(item.id)!==Number(id)); coverageRenderMap(true); notify('Área excluída.');
  } catch(error){notify(error.message);}
}

function coverageRefreshFilterOptions() {
  const clients = coverageMapData?.groups || [];
  const olt = $('#coverage-filter-olt')?.value || '';
  const slotNode = $('#coverage-filter-slot'), ponNode = $('#coverage-filter-pon');
  const currentSlot = slotNode?.value || '', currentPon = ponNode?.value || '';
  const byOlt = clients.filter(item => !olt || String(item.olt_id) === olt);
  const optionList = (values, label) => `<option value="">${label}</option>` + [...new Set(values.filter(value => value !== null && value !== undefined && value !== ''))].sort((a,b)=>Number(a)-Number(b)).map(value => `<option value="${esc(value)}">${esc(value)}</option>`).join('');
  if (slotNode) { slotNode.innerHTML = optionList(byOlt.map(item => item.slot), 'Todos os slots'); slotNode.value = currentSlot; }
  const bySlot = byOlt.filter(item => !slotNode?.value || String(item.slot) === slotNode.value);
  if (ponNode) { ponNode.innerHTML = optionList(bySlot.map(item => item.pon), 'Todas as PONs'); ponNode.value = currentPon; }
}

function coverageSelectInitialPon() {
  const groups = coverageMapData?.groups || [];
  if (!groups.length) return;
  // Abertura operacional: Itaquera, Slot 2, PON 15. Caso esse cadastro não
  // exista numa base futura, escolhemos uma PON válida em vez de abrir todas.
  const preferred = groups.find(item =>
    Number(item.slot) === 2 && Number(item.pon) === 15 && /itaquera/i.test(String(item.olt || ''))
  ) || groups.find(item => Number(item.slot) === 2 && Number(item.pon) === 15) || groups[0];
  const oltNode = $('#coverage-filter-olt'), slotNode = $('#coverage-filter-slot'), ponNode = $('#coverage-filter-pon');
  if (!oltNode || !slotNode || !ponNode) return;
  oltNode.value = String(preferred.olt_id);
  coverageRefreshFilterOptions();
  slotNode.value = String(preferred.slot);
  coverageRefreshFilterOptions();
  ponNode.value = String(preferred.pon);
}

function coverageHasSavedArea(group) {
  if (group.area_geometry) return true;
  return (coverageMapData?.manual_areas || []).some(area =>
    String(area.olt_id || '') === String(group.olt_id || '') &&
    String(area.slot ?? '') === String(group.slot ?? '') &&
    String(area.pon ?? '') === String(group.pon ?? '')
  );
}

function coverageBuildingPopup(building) {
  const actions = state.user.mode === 'superadmin' ? `<div class="coverage-popup-actions"><button type="button" class="secondary" data-building-edit="${Number(building.id)}">Editar</button><button type="button" class="danger" data-building-delete="${Number(building.id)}">Excluir</button></div>` : '';
  return `<b>▥ ${esc(building.name)}</b><br>${esc(building.note || 'Sem observação')}<br><small>${Number(building.latitude).toFixed(6)}, ${Number(building.longitude).toFixed(6)}</small>${actions}`;
}

function coverageBindBuildingActions(root) {
  root?.querySelectorAll('[data-building-edit]').forEach(button => button.onclick = () => {
    const building = (coverageMapData.buildings || []).find(item => Number(item.id) === Number(button.dataset.buildingEdit));
    if (!building) return;
    $('#coverage-building-id').value = building.id; $('#coverage-building-name').value = building.name;
    $('#coverage-building-note').value = building.note || ''; $('#coverage-building-lat').value = building.latitude;
    $('#coverage-building-lng').value = building.longitude; $('#coverage-building-form').classList.remove('hidden');
  });
  root?.querySelectorAll('[data-building-delete]').forEach(button => button.onclick = async () => {
    const building = (coverageMapData.buildings || []).find(item => Number(item.id) === Number(button.dataset.buildingDelete));
    if (!building || !window.confirm(`Excluir o prédio “${building.name}”?`)) return;
    try {
      await api(`/api/superadmin/coverage-buildings/${Number(building.id)}`, { method: 'DELETE' });
      coverageMapData.buildings = coverageMapData.buildings.filter(item => Number(item.id) !== Number(building.id));
      coverageRenderMap(true); notify('Prédio excluído.');
    } catch (error) { notify(error.message); }
  });
}

function dataIsLegacy() { return coverageMapData?.geometry_version !== 'street-blocks-v1'; }

function coverageRenderMap(preserveView = false) {
  const container = $('#coverage-map');
  if (!container || !window.L || !coverageMapData) return;
  if (!coverageMap) {
    // Ponto inicial operacional: Parque do Carmo, Itaquera. A PON escolhida
    // continua assumindo o foco assim que o supervisor concluir os filtros.
    coverageMap = L.map(container, { preferCanvas: true }).setView([-23.5738, -46.4706], 14);
    L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}', {
      maxZoom: 19, attribution: 'Tiles &copy; Esri — HERE, Garmin, FAO, NOAA, USGS, OpenStreetMap contributors',
    }).addTo(coverageMap);
    coverageLayers = {
      coverage: L.layerGroup().addTo(coverageMap), clients: L.layerGroup().addTo(coverageMap),
      roads: L.layerGroup().addTo(coverageMap),
      offline: L.layerGroup().addTo(coverageMap), buildings: L.layerGroup().addTo(coverageMap),
      outages: L.layerGroup().addTo(coverageMap),
    };
    coverageMap.on('click', event => {
      if (state.user.mode==='superadmin' && !$('#coverage-draw-form')?.classList.contains('hidden')) {
        coverageDrawingPoints.push([event.latlng.lat,event.latlng.lng]); coverageUpdateDrawing(); return;
      }
      if (!$('#coverage-building-form') || state.user.mode !== 'superadmin' || $('#coverage-building-form').classList.contains('hidden')) return;
      $('#coverage-building-lat').value = event.latlng.lat.toFixed(7);
      $('#coverage-building-lng').value = event.latlng.lng.toFixed(7);
    });
  }
  Object.values(coverageLayers).forEach(layer => layer.clearLayers());
  // A visualização geral de todas as PONs torna o mapa ilegível e também
  // deixa o navegador trabalhar sem necessidade. A área só é desenhada
  // depois da seleção completa da PON que se quer consultar.
  const hasSelectedPon = coverageHasSelectedPon();
  const clients = hasSelectedPon ? coverageFilteredClients() : [];
  const olt = $('#coverage-filter-olt')?.value || '', slot = $('#coverage-filter-slot')?.value || '', pon = $('#coverage-filter-pon')?.value || '';
  const groups = hasSelectedPon ? (coverageMapData.groups || []).filter(item => String(item.olt_id) === olt && String(item.slot) === slot && String(item.pon) === pon) : [];
  const popup = item => `<b>${esc(item.client)}</b><br>Endereço: ${esc(item.address || 'não informado')}<br>${esc(item.olt || 'OLT não informada')} · Slot ${esc(item.slot ?? '—')} · PON ${esc(item.pon ?? '—')}`;
  groups.forEach(group => {
    if (!group.area_geometry || dataIsLegacy()) return;
    const color = ponColor(group);
    L.geoJSON(group.area_geometry, {style:{color, weight:1.5, fillColor:color, fillOpacity:.32}})
      .addTo(coverageLayers.coverage)
      .bindPopup(`<b>${esc(group.olt)} · Slot ${esc(group.slot)} · PON ${esc(group.pon)}</b><br>${number(group.total)} clientes`);
    if (group.label_point) L.marker(group.label_point, {interactive:false, icon:L.divIcon({
      className:'pon-area-label', html:`<span>Slot ${esc(group.slot)} · PON ${esc(group.pon)}</span>`,
      iconSize:[110,28], iconAnchor:[55,14],
    })}).addTo(coverageLayers.coverage);
  });
  (coverageMapData.manual_areas || []).forEach(area=>{
    if(!hasSelectedPon || !area.geometry || String(area.olt_id)!==olt || String(area.slot)!==slot || String(area.pon)!==pon) return;
    const color=ponColor({olt_id:area.olt_id,slot:area.slot,pon:area.pon});
    const actions=state.user.mode==='superadmin'?`<br><button type="button" class="danger" data-manual-area-delete="${Number(area.id)}">Excluir área</button>`:'';
    L.geoJSON(area.geometry,{style:{color,weight:3,fillColor:color,fillOpacity:.28}}).addTo(coverageLayers.coverage)
      .bindPopup(`<b>${esc(area.olt_name || 'OLT')} · Slot ${esc(area.slot)} · PON ${esc(area.pon)}</b><br>${esc(area.note || 'Área desenhada manualmente')}${actions}`)
      .on('popupopen',event=>event.popup.getElement()?.querySelectorAll('[data-manual-area-delete]').forEach(button=>button.onclick=()=>coverageDeleteManualArea(button.dataset.manualAreaDelete)));
  });
  clients.forEach(item => {
    const point = [Number(item.located_latitude ?? item.latitude), Number(item.located_longitude ?? item.longitude)];
    L.circleMarker(point, { radius: 3, color: '#fff', weight: 1, fillColor: '#34495e', fillOpacity: .80 }).addTo(coverageLayers.clients).bindPopup(popup(item));
  });
  (coverageMapData.buildings || []).forEach(building => {
    L.marker([building.latitude, building.longitude], { icon: L.divIcon({ className:'coverage-building-icon', html:'<span>▥</span>', iconSize:[34,34], iconAnchor:[17,17] }) })
      .addTo(coverageLayers.buildings).bindPopup(coverageBuildingPopup(building)).on('popupopen', event => coverageBindBuildingActions(event.popup.getElement()));
  });
  document.querySelectorAll('[data-coverage-layer]').forEach(input => {
    const layer = coverageLayers[input.dataset.coverageLayer];
    if (!layer) return;
    if (input.checked && !coverageMap.hasLayer(layer)) layer.addTo(coverageMap);
    if (!input.checked && coverageMap.hasLayer(layer)) coverageMap.removeLayer(layer);
  });
  const bounds = clients.map(item => [item.located_latitude ?? item.latitude,item.located_longitude ?? item.longitude]).concat((coverageMapData.buildings || []).map(item => [item.latitude,item.longitude]));
  if (!preserveView && bounds.length) coverageMap.fitBounds(bounds, { padding:[28,28], maxZoom:16 });
  $('#coverage-visible-count').textContent = number(clients.length);
  coverageRenderClientList();
  const panel = $('#coverage-outage-list');
  const panelTitle = $('#coverage-panel-title');
  const panelText = $('#coverage-panel-text');
  if (panelTitle) panelTitle.textContent = hasSelectedPon ? 'Área da PON' : 'Escolha uma PON';
  if (panelText) panelText.textContent = hasSelectedPon ? 'Clique na área para aproximar ou confira os clientes ao lado.' : 'Selecione OLT, Slot e PON acima. O mapa não mistura todas as áreas.';
  if (panel) panel.innerHTML = hasSelectedPon
    ? groups.map(item => `<button type="button" data-pon-key="${esc(ponRouteKey(item))}"><b>${esc(item.olt)} · Slot ${esc(item.slot)} · PON ${esc(item.pon)}</b><span>${number(item.total)} cliente(s) · ${coverageHasSavedArea(item) ? 'área salva' : 'área pendente'}</span></button>`).join('') || '<p>Nenhum endereço encontrado nessa PON.</p>'
    : '<p class="coverage-select-hint">Nenhuma área é exibida até você selecionar uma PON.</p>';
  panel?.querySelectorAll('button').forEach(button => button.onclick = () => {
    const group = (coverageMapData.groups || []).find(item => ponRouteKey(item) === button.dataset.ponKey);
    if (group) coverageFocusPon(group);
  });
  window.setTimeout(()=>coverageMap?.invalidateSize(),60);
}

function coverageInstallControls() {
  const resetAnalysis=()=>{ $('#coverage-analysis-panel')?.classList.add('hidden'); const button=$('#coverage-analyze-pon'); if(button){button.textContent='Analisar PON';button.onclick=()=>coverageAnalyzePon(false);} };
  $('#coverage-filter-olt').onchange = () => { coverageRefreshFilterOptions(); coverageRenderMap(false); resetAnalysis(); };
  $('#coverage-filter-slot').onchange = () => { coverageRefreshFilterOptions(); coverageRenderMap(false); resetAnalysis(); };
  $('#coverage-filter-pon').onchange = () => { coverageRenderMap(false); resetAnalysis(); };
  $('#coverage-analyze-pon').onclick=()=>coverageAnalyzePon(false);
  $('#coverage-analysis-close').onclick=()=>$('#coverage-analysis-panel').classList.add('hidden');
  $('#coverage-address-search-button').onclick=coverageSearchAddress;
  $('#coverage-address-search').onkeydown=event=>{if(event.key==='Enter'){event.preventDefault();coverageSearchAddress();}};
  $('#coverage-client-close').onclick=()=>$('#coverage-client-panel').classList.add('hidden');
  const expandButton = $('#coverage-expand');
  if (expandButton) expandButton.onclick = () => {
    const workspace = $('#coverage-workspace');
    const expanded = workspace.classList.toggle('coverage-map-expanded');
    expandButton.textContent = expanded ? '↙ Recolher mapa' : '⛶ Expandir mapa';
    expandButton.setAttribute('aria-pressed', String(expanded));
    window.setTimeout(() => coverageMap?.invalidateSize(), 120);
  };
  if(state.user.mode==='superadmin') {
    $('#coverage-auto-selected').onclick=async event=>{
      const olt=$('#coverage-filter-olt')?.value || '', slot=$('#coverage-filter-slot')?.value || '', pon=$('#coverage-filter-pon')?.value || '';
      if (!olt || !slot || !pon) return notify('Selecione uma OLT, Slot e PON para testar uma área.');
      const button=event.currentTarget; button.disabled=true; button.textContent='Gerando esta PON...';
      try {
        const saved=await api('/api/superadmin/coverage-pon-areas/generate-selected',{method:'POST',body:JSON.stringify({olt_id:olt,slot,pon}),concurrent:true,timeoutMs:60000});
        coverageMapData.manual_areas=[...(coverageMapData.manual_areas||[]).filter(item=>Number(item.id)!==Number(saved.id)),saved]; coverageRenderMap(false);
        notify(saved.already_saved ? 'Área salva carregada para conferência.' : `Área criada com ${number(saved.clients_used)} cliente(s) próximos. Confira o contorno antes de gerar as demais.`);
      } catch(error){notify(error.message);} finally{button.disabled=false;button.textContent='Gerar esta PON';}
    };
    $('#coverage-draw-start').onclick=()=>{coverageResetDrawing();$('#coverage-draw-form').classList.remove('hidden');};
    $('#coverage-draw-cancel').onclick=()=>{coverageResetDrawing();$('#coverage-draw-form').classList.add('hidden');};
    $('#coverage-draw-undo').onclick=()=>{coverageDrawingPoints.pop();coverageDrawingPoints.length?coverageUpdateDrawing():coverageResetDrawing();};
    $('#coverage-draw-form').onsubmit=coverageSaveDrawing;
    $('#coverage-auto-areas').onclick=async event=>{
      if(!window.confirm('Gerar uma área para cada PON ainda não desenhada? Áreas manuais existentes serão preservadas.')) return;
      const button=event.currentTarget; button.disabled=true; button.textContent='Gerando áreas...';
      try {
        const result=await api('/api/superadmin/coverage-pon-areas/generate',{method:'POST',concurrent:true,timeoutMs:300000});
        coverageMapData.manual_areas=[...(coverageMapData.manual_areas||[]),...(result.saved||[])]; coverageRenderMap(false);
        notify(`${number(result.created)} área(s) criada(s); ${number(result.preserved)} manual(is) preservada(s).`);
      } catch(error){notify(error.message);} finally{button.disabled=false;button.textContent='Gerar áreas das PONs';}
    };
  }
  $('#coverage-offline-only')?.addEventListener('change', () => coverageRenderMap(false));
  document.querySelectorAll('[data-coverage-layer]').forEach(input => input.onchange = () => coverageRenderMap(true));
  const refreshButton = $('#coverage-refresh');
  if (refreshButton) refreshButton.onclick = async () => {
    const button = $('#coverage-refresh'); button.disabled = true; button.textContent = 'Atualizando todas as OLTs...';
    try {
      const fresh = await api('/api/office/service-coverage-map?refresh=1', { concurrent:true, timeoutMs:300000 });
      await serviceCoverageView(false, fresh); notify('Geração iniciada. Você pode continuar usando o app.');
    } catch (error) { notify(error.message); button.disabled = false; button.textContent = '↻ Atualizar mapa'; }
  };
  const addBuilding = $('#coverage-add-building');
  const buildingForm = $('#coverage-building-form');
  const buildingCancel = $('#coverage-building-cancel');
  // Mapa PON does not currently expose the building action.  Older coverage
  // screens may still render it, so only bind handlers when their elements
  // actually exist.  This prevents a supervisor-only null onclick failure.
  if (state.user.mode === 'superadmin' && addBuilding && buildingForm && buildingCancel) {
    addBuilding.onclick = () => { buildingForm.classList.toggle('hidden'); $('#coverage-building-id').value=''; };
    buildingCancel.onclick = () => buildingForm.classList.add('hidden');
    buildingForm.onsubmit = async event => {
      event.preventDefault(); const button = event.submitter; button.disabled = true;
      try {
        const saved = await api('/api/superadmin/coverage-buildings', { method:'POST', body:JSON.stringify({
          id: $('#coverage-building-id').value || null, name:$('#coverage-building-name').value, note:$('#coverage-building-note').value,
          latitude:$('#coverage-building-lat').value, longitude:$('#coverage-building-lng').value,
        }) });
        coverageMapData.buildings = [...(coverageMapData.buildings || []).filter(item => Number(item.id)!==Number(saved.id)),saved];
        buildingForm.reset(); buildingForm.classList.add('hidden'); coverageRenderMap(true); notify('Prédio salvo.');
      } catch (error) { $('#coverage-building-error').textContent = error.message; }
      finally { button.disabled = false; }
    };
  }
  const panel = $('#coverage-pon-panel');
  const handle = $('#coverage-panel-handle');
  const workspace = $('#coverage-workspace');
  if (panel && handle && workspace) {
    let dragging = null;
    handle.onpointerdown = event => {
      dragging = { x:event.clientX, y:event.clientY, left:panel.offsetLeft, top:panel.offsetTop };
      handle.setPointerCapture?.(event.pointerId); event.preventDefault();
    };
    handle.onpointermove = event => {
      if (!dragging) return;
      const maxLeft = Math.max(0, workspace.clientWidth - panel.offsetWidth);
      const maxTop = Math.max(0, workspace.clientHeight - panel.offsetHeight);
      panel.style.left = `${Math.max(0, Math.min(maxLeft, dragging.left + event.clientX - dragging.x))}px`;
      panel.style.top = `${Math.max(0, Math.min(maxTop, dragging.top + event.clientY - dragging.y))}px`;
    };
    handle.onpointerup = () => { dragging = null; };
  }
}

function coverageWatchBuild(build) {
  const node = $('#coverage-build-status');
  if (!node) return;
  node.textContent = build?.state === 'running'
    ? `${build.stage || 'Gerando mapa'}${build.total ? ` · ${number(build.done)}/${number(build.total)}` : ''}`
    : build?.state === 'error' ? `Consulta interrompida: ${build.message}`
    : dataIsLegacy() ? 'Clique em Consultar novamente para gerar as áreas por rua.' : '';
  const refresh = $('#coverage-refresh');
  if (refresh) refresh.disabled = build?.state === 'running';
  window.clearTimeout(coverageBuildTimer);
  if (build?.state !== 'running') return;
  coverageBuildTimer = window.setTimeout(async () => {
    if (!$('#coverage-workspace')) return;
    try {
      const next = await api('/api/office/pon-map-progress', {concurrent:true, timeoutMs:15000});
      if (!$('#coverage-workspace')) return;
      if (next?.state === 'complete') await serviceCoverageView();
      else coverageWatchBuild(next);
    } catch (error) { if ($('#coverage-workspace')) coverageWatchBuild(build); }
  }, 5000);
}

async function serviceCoverageView(force = false, supplied = null) {
  coverageMapStop(); setPage('Mapa PON');
  if (!supplied) loading('Abrindo o mapa salvo...');
  try {
    const data = supplied || await api(`/api/office/service-coverage-map${force ? '?refresh=1' : ''}`, { concurrent:true, timeoutMs:300000 });
    coverageMapData = data;
    const oltOptions = (data.olts || []).map(item => `<option value="${esc(item.id)}">${esc(item.name)}</option>`).join('');
    const updated = data.generated_at ? new Date(data.generated_at).toLocaleString('pt-BR') : 'não informado';
    document.body.classList.add('pon-map-page');
    $('#content').innerHTML = `<section id="coverage-workspace" class="coverage-layout">
      <article class="card coverage-map-card"><div id="coverage-map"></div>
      <div class="coverage-map-tools">
        <label>OLT<select id="coverage-filter-olt"><option value="">Todas as OLTs</option>${oltOptions}</select></label>
        <label>Slot<select id="coverage-filter-slot"><option value="">Todos os slots</option></select></label>
        <label>PON<select id="coverage-filter-pon"><option value="">Todas as PONs</option></select></label>
        <label class="coverage-address-search">Rua ou CEP<input id="coverage-address-search" placeholder="Ex.: Rua Flores ou 08210-120"></label><button id="coverage-address-search-button" class="secondary">Localizar</button>
        <button id="coverage-analyze-pon" class="primary">Analisar PON</button><a class="secondary button-link" href="#saude-pons">◉ Saúde das PONs</a><button id="coverage-expand" class="secondary" type="button" aria-pressed="false">⛶ Expandir mapa</button>
        ${state.user.mode==='superadmin'?'<button id="coverage-draw-start" class="secondary">✎ Desenhar área</button><button id="coverage-auto-selected" class="secondary">Gerar esta PON</button><button id="coverage-auto-areas" class="secondary">Gerar áreas das PONs</button>':''}
        ${state.user.mode==='superadmin'?'<button id="coverage-refresh" class="primary">↻ Consultar novamente</button>':''}
        <div class="coverage-layer-controls"><label><input type="checkbox" data-coverage-layer="coverage" checked> Áreas por rua</label><label><input type="checkbox" data-coverage-layer="clients"> Endereços</label></div>
        <div id="coverage-build-status" role="status"></div>
        <small>Salvo: ${esc(updated)} · <span id="coverage-visible-count">0</span> clientes · ${number((data.groups||[]).filter(g=>g.area_geometry || (data.manual_areas||[]).some(area=>String(area.olt_id||'')===String(g.olt_id||'') && String(area.slot??'')===String(g.slot??'') && String(area.pon??'')===String(g.pon??''))).length)} PONs com áreas salvas · ${number(data.total - (data.address_summary?.street_matched || 0))} endereços pendentes</small>
      </div>
      <div id="coverage-analysis-panel" class="coverage-analysis-panel hidden"><header><div><small>DIAGNÓSTICO ÓPTICO</small><h2>Análise da PON</h2></div><button id="coverage-analysis-close" type="button" aria-label="Fechar análise">×</button></header><div id="coverage-analysis-result"></div></div>
      <div id="coverage-client-panel" class="coverage-client-panel hidden"><header><div><small>CLIENTES DA PON</small><h2><span id="coverage-client-count">0</span> clientes</h2></div><button id="coverage-client-close" type="button" aria-label="Fechar lista">×</button></header><div id="coverage-client-list"></div></div>
      ${state.user.mode==='superadmin'?`<form id="coverage-draw-form" class="coverage-draw-form hidden"><header><div><small>EDITOR DO SUPERVISOR</small><h2>Desenhar área da PON</h2></div><button id="coverage-draw-cancel" type="button" aria-label="Fechar">×</button></header><p>Clique no mapa acompanhando as ruas. Marque pelo menos três pontos e depois salve.</p><label>OLT<select id="coverage-draw-olt"><option value="">OLT escrita manualmente</option>${oltOptions}</select></label><label>Slot<input id="coverage-draw-slot" required placeholder="Ex.: 2"></label><label>PON<input id="coverage-draw-pon" required placeholder="Ex.: 15"></label><label>Observação<input id="coverage-draw-note" placeholder="Área atendida / referência"></label><div><b id="coverage-draw-count">0 pontos</b><button id="coverage-draw-undo" type="button" class="secondary">Desfazer ponto</button><button type="submit" class="primary">Salvar área</button></div></form>`:''}
      <p class="coverage-disclaimer">Cobertura estimada pelos trechos de rua dos clientes. Posição por CEP, rua e número quando disponível; demais posições do SGP conferidas pela rua. © OpenStreetMap contributors.</p>
      </article>
      <div id="coverage-pon-panel" class="card coverage-outage-panel"><button id="coverage-panel-handle" type="button">⠿ Mover painel</button><small>PON NO MAPA</small><h2 id="coverage-panel-title">Escolha uma PON</h2><p id="coverage-panel-text">Selecione OLT, Slot e PON acima. O mapa não mistura todas as áreas.</p><div id="coverage-outage-list"></div></div>
      </section>`;
    coverageRefreshFilterOptions(); coverageSelectInitialPon(); coverageInstallControls(); coverageRenderMap(false);
    coverageWatchBuild(data.build);
  } catch (error) { errorView(error); }
}

function routeElapsed(seconds) {
  if (seconds === null || seconds === undefined) return "Início não informado";
  const total = Math.max(0, Math.floor(Number(seconds) || 0));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  return `${hours ? `${hours}h ` : ""}${String(minutes).padStart(2, "0")}min`;
}

function routeMonitorStopClock() {
  if (routeMonitorClock) window.clearInterval(routeMonitorClock);
  if (routeMonitorRefreshTimer) window.clearInterval(routeMonitorRefreshTimer);
  routeMonitorClock = null;
  routeMonitorRefreshTimer = null;
  routeMonitorRefreshing = false;
}

function routeMonitorStartClock() {
  routeMonitorStopClock();
  const tick = () => document.querySelectorAll("[data-route-start]").forEach(element => {
    const started = new Date(element.dataset.routeStart);
    if (Number.isNaN(started.getTime())) return;
    const seconds = Math.max(0, Math.floor((Date.now() - started.getTime()) / 1000));
    element.textContent = routeElapsed(seconds);
    const row = element.closest("tr");
    if (row) {
      row.classList.toggle("attention", seconds >= 3600 && seconds < 7200);
      row.classList.toggle("urgent", seconds >= 7200);
    }
  });
  tick();
  routeMonitorClock = window.setInterval(tick, 1000);
}

function routeServiceList(services) {
  if (!services.length) return '<span class="route-muted">Nenhuma OS hoje</span>';
  return `<details class="route-services"><summary>${services.length} OS no dia</summary>${services.map(service => `
    <article><b>#${esc(service.id)} · ${esc(service.reason)}</b><span>${esc(service.client)}</span><small>${esc(service.status_text)}${service.elapsed_seconds !== null ? ` · ${routeElapsed(service.elapsed_seconds)}` : ""}</small></article>
  `).join("")}</details>`;
}

function routeTimelineItem(service, day, nowIso, overlap = false) {
  const markerOnly = !service.started_at && service.status === 1 && service.finished_at;
  if (!service.started_at && !markerOnly) return "";
  const dayStart = new Date(`${day}T08:00:00`);
  const start = new Date(markerOnly ? service.finished_at : service.started_at);
  const rawEnd = service.finished_at ? new Date(service.finished_at) : service.status === 2 ? new Date(nowIso) : start;
  if ([dayStart, start, rawEnd].some(value => Number.isNaN(value.getTime()))) return "";
  const minutes = value => Math.max(0, Math.min(840, (value - dayStart) / 60000));
  const left = minutes(start) / 8.4;
  const width = Math.max(markerOnly ? .9 : .65, (minutes(rawEnd) - minutes(start)) / 8.4);
  const duration = markerOnly ? "encerrada sem início registrado" : routeElapsed(service.elapsed_seconds);
  const time = value => value && !Number.isNaN(new Date(value).getTime()) ? new Date(value).toLocaleTimeString("pt-BR", { hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "não registrado";
  const startText = markerOnly ? "não registrado" : time(service.started_at);
  const endText = service.finished_at ? time(service.finished_at) : service.status === 2 ? "ainda em execução" : "não registrado";
  const title = `OS #${service.id} · ${service.client}\nInício: ${startText}\nTérmino: ${endText}\nDuração: ${duration}\nSituação: ${service.status_text}${overlap ? "\nAtenção: iniciou antes da OS anterior terminar" : ""}`;
  const state = service.status === 2 ? "running" : service.status === 1 ? "closed" : "other";
  return `<button class="route-event ${state} ${esc(service.duration_level)}${markerOnly ? " marker-only" : ""}${overlap ? " overlap" : ""}" data-route-order="${esc(service.id)}" data-route-detail="${esc(title)}" style="left:${left}%;width:${width}%" aria-label="${esc(title)}"><b>#${esc(service.id)}</b><span>${esc(service.reason)}</span></button>`;
}

function routeTimelineItems(services, day, nowIso) {
  const timed = services.map(service => {
    const markerOnly = !service.started_at && service.status === 1 && service.finished_at;
    const start = new Date(markerOnly ? service.finished_at : service.started_at);
    const end = new Date(service.finished_at || (service.status === 2 ? nowIso : service.started_at));
    return { service, markerOnly, start, end };
  }).filter(item => !Number.isNaN(item.start.getTime()));
  timed.sort((a, b) => a.start - b.start || Number(a.service.id) - Number(b.service.id));
  let furthestEnd = null;
  return timed.map(item => {
    const overlap = !item.markerOnly && furthestEnd !== null && item.start < furthestEnd;
    if (!item.markerOnly && !Number.isNaN(item.end.getTime()) && (furthestEnd === null || item.end > furthestEnd)) furthestEnd = item.end;
    return routeTimelineItem(item.service, day, nowIso, overlap);
  }).join("");
}

function routeAlertReport(alerts) {
  if (!alerts.length) return '<section class="card route-alert-empty"><b>✓</b><div><h3>Nenhum atendimento acima de 1 hora</h3><p>Não há ocorrências para avaliar neste dia.</p></div></section>';
  return `<section class="route-alert-list">${alerts.map(alert => `<article class="card route-alert ${esc(alert.duration_level)} ${alert.review ? "reviewed" : "pending-review"}"><span>${alert.review ? "AVALIADA" : alert.duration_level === "urgent" ? "URGENTE · +2H" : "ATENÇÃO · +1H"}</span><div><h3>${esc(alert.technician)} · OS #${esc(alert.id)}</h3><p>${esc(alert.client)} · ${esc(alert.reason)}</p>${alert.review ? `<small>${esc(alert.review.comment)} · ${esc(alert.review.reviewed_by)}</small>` : ""}</div><b>${routeElapsed(alert.elapsed_seconds)}</b><button class="${alert.review ? "secondary" : "primary"}" data-route-review="${esc(alert.id)}" data-route-technician="${esc(alert.technician)}" data-route-client="${esc(alert.client)}">${alert.review ? "Ver análise" : "Analisar OS"}</button></article>`).join("")}</section>`;
}

function routeReviewDialog(button) {
  const orderId = Number(button.dataset.routeReview);
  const technician = button.dataset.routeTechnician;
  const alert = [...document.querySelectorAll(".route-alert")].find(item => item.contains(button));
  const existingComment = alert?.querySelector("small")?.textContent || "";
  const dialog = document.createElement("dialog");
  dialog.className = "route-review-dialog";
  dialog.innerHTML = `<form><button type="button" class="dialog-close" aria-label="Fechar">×</button><small>ANÁLISE DE DEMORA</small><h2>OS #${esc(orderId)} · ${esc(technician)}</h2><p>${esc(button.dataset.routeClient || "")}</p><label>O que foi feito e por que houve demora<textarea id="route-review-comment" rows="6" required placeholder="Descreva a análise realizada..."></textarea></label><p class="error"></p><button class="primary" type="submit">Concluir análise</button></form>`;
  document.body.appendChild(dialog);
  dialog.querySelector(".dialog-close").onclick = () => dialog.close();
  if (existingComment) dialog.querySelector("textarea").value = existingComment.split(" · ")[0];
  dialog.querySelector("form").onsubmit = async event => {
    event.preventDefault();
    const submit = event.submitter;
    const comment = dialog.querySelector("textarea").value.trim();
    if (!comment) { dialog.querySelector(".error").textContent = "O comentário é obrigatório."; return; }
    submit.disabled = true;
    try {
      await api("/api/superadmin/route-monitor/reviews", { method: "POST", body: JSON.stringify({ order_id: orderId, technician, comment }) });
      dialog.close();
      const data = await api("/api/superadmin/route-monitor", { timeoutMs: 70000 });
      if (location.hash.startsWith("#acompanhar-rota")) routeMonitorRender(data);
      notify("OS avaliada. Você já pode analisar a próxima.");
    } catch (error) { dialog.querySelector(".error").textContent = error.message; submit.disabled = false; }
  };
  dialog.addEventListener("close", () => dialog.remove());
  dialog.showModal();
}

function bindRouteReviews(root = document) {
  root.querySelectorAll("[data-route-review]").forEach(button => button.onclick = () => routeReviewDialog(button));
}

function routeMapRender(points) {
  const container = $("#route-map");
  if (!container) return;
  if (!window.L) {
    container.innerHTML = '<div class="route-map-unavailable">O mapa não pôde ser carregado. As posições serão tentadas novamente na próxima atualização.</div>';
    return;
  }
  if (!routeMonitorMap) {
    routeMonitorMap = L.map(container, { zoomControl: true }).setView([-23.55, -46.63], 11);
    L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}", {
      maxZoom: 19,
      attribution: 'Tiles &copy; <a href="https://www.esri.com/">Esri</a> — Esri, HERE, Garmin, FAO, NOAA, USGS, OpenStreetMap contributors',
    }).addTo(routeMonitorMap);
  }
  const active = new Set();
  const located = (points || []).filter(point => point.position_available !== false && Number.isFinite(Number(point.latitude)) && Number.isFinite(Number(point.longitude)));
  located.forEach(point => {
    const key = String(point.technician);
    active.add(key);
    const color = point.state === "Em execução" ? "#2563c9" : "#25805a";
    const icon = L.divIcon({ className: "route-map-marker-wrap", html: `<span class="route-map-marker" style="--marker:${color}"></span>`, iconSize: [20, 20], iconAnchor: [10, 10] });
    const popup = `<b>${esc(point.technician)}</b><br>${esc(point.state)} · OS #${esc(point.order_id)}<br>${esc(point.client)}`;
    let marker = routeMonitorMarkers.get(key);
    if (!marker) {
      marker = L.marker([point.latitude, point.longitude], { icon }).addTo(routeMonitorMap).bindTooltip(esc(point.technician), { permanent: true, direction: "top", offset: [0, -8] }).bindPopup(popup);
      routeMonitorMarkers.set(key, marker);
    } else { marker.setLatLng([point.latitude, point.longitude]).setIcon(icon).setPopupContent(popup); }
  });
  for (const [key, marker] of routeMonitorMarkers) if (!active.has(key)) { routeMonitorMap.removeLayer(marker); routeMonitorMarkers.delete(key); }
  const markers = [...routeMonitorMarkers.values()];
  if (markers.length) routeMonitorMap.fitBounds(L.featureGroup(markers).getBounds().pad(.18), { maxZoom: 14 });
  const missing = (points || []).filter(point => !located.includes(point));
  const noService = missing.filter(point => point.no_service);
  const withoutCoordinates = missing.filter(point => !point.no_service);
  const status = $("#route-map-status");
  if (status) status.innerHTML = `<b>${number((points || []).length)} técnicos acompanhados · ${number(located.length)} no mapa.</b>${withoutCoordinates.length ? `<span>Sem coordenadas na OS: ${withoutCoordinates.map(item => esc(item.technician)).join(", ")}.</span>` : ""}${noService.map(item => `<span class="route-map-no-service">Técnico “${esc(item.technician)}” não realizou nenhum serviço hoje.</span>`).join("")}`;
  window.setTimeout(() => routeMonitorMap?.invalidateSize(), 50);
}

function routeMapDestroy() {
  if (routeMonitorMap) routeMonitorMap.remove();
  routeMonitorMap = null;
  routeMonitorMarkers = new Map();
}

function routeTrackMarkup(item, data) {
  return `${Array.from({length: 14}, () => "<i></i>").join("")}${routeTimelineItems(item.services || [], data.date, data.generated_at)}`;
}

function bindRouteOrderLinks(root = document) {
  const hideTooltip = () => document.querySelector(".route-event-tooltip")?.remove();
  const showTooltip = button => {
    hideTooltip();
    const tooltip = document.createElement("div");
    tooltip.className = "route-event-tooltip";
    tooltip.textContent = button.dataset.routeDetail || "";
    document.body.appendChild(tooltip);
    const rect = button.getBoundingClientRect();
    const tip = tooltip.getBoundingClientRect();
    const left = Math.max(8, Math.min(window.innerWidth - tip.width - 8, rect.left + rect.width / 2 - tip.width / 2));
    const top = rect.top > tip.height + 14 ? rect.top - tip.height - 9 : rect.bottom + 9;
    tooltip.style.left = `${left}px`;
    tooltip.style.top = `${Math.max(8, top)}px`;
  };
  root.querySelectorAll("[data-route-order]").forEach(button => {
    button.onmouseenter = () => showTooltip(button);
    button.onmouseleave = hideTooltip;
    button.onfocus = () => showTooltip(button);
    button.onblur = hideTooltip;
    button.onclick = () => {
      hideTooltip();
      const dialog = document.createElement("dialog");
      dialog.className = "route-event-dialog";
      dialog.innerHTML = `<section><button type="button" class="dialog-close" aria-label="Fechar">×</button><small>DETALHES DO ATENDIMENTO</small><h2>OS #${esc(button.dataset.routeOrder)}</h2><p>${esc(button.dataset.routeDetail || "").replace(/\n/g, "<br>")}</p><button type="button" class="primary route-event-open">Abrir OS</button></section>`;
      document.body.appendChild(dialog);
      dialog.querySelector(".dialog-close").onclick = () => dialog.close();
      dialog.querySelector(".route-event-open").onclick = () => { dialog.close(); location.hash = `ordem/${button.dataset.routeOrder}`; };
      const assign = document.createElement('button');
      assign.type = 'button'; assign.className = 'secondary'; assign.textContent = 'Direcionar técnico';
      assign.onclick = () => { dialog.close(); assignServiceDialog(Number(button.dataset.routeOrder)); };
      dialog.querySelector('section').appendChild(assign);
      const certify = document.createElement('button');
      certify.type = 'button'; certify.className = 'secondary'; certify.textContent = 'Criar anotação / Certificar';
      certify.onclick = () => { dialog.close(); certificationDialog(Number(button.dataset.routeOrder)); };
      dialog.querySelector('section').appendChild(certify);
      dialog.addEventListener("close", () => dialog.remove());
      dialog.showModal();
    };
  });
}

function routeHourHeader() {
  return Array.from({length: 14}, (_, index) => `<span><b>${String(index + 8).padStart(2, "0")}:00</b><small>${String(index + 8).padStart(2, "0")}:30</small></span>`).join("");
}

function bindRouteTimelineScroll() {
  const header = $("#route-sticky-hours"), body = $("#route-timeline-scroll");
  if (!header || !body || header.dataset.bound) return;
  header.dataset.bound = "1";
  let syncing = false;
  const mirror = (source, target) => {
    if (syncing) return;
    syncing = true; target.scrollLeft = source.scrollLeft; syncing = false;
  };
  header.addEventListener("scroll", () => mirror(header, body));
  body.addEventListener("scroll", () => mirror(body, header));
}

function routeMonitorRender(data) {
  const rows = data.technicians || [];
  const alertItems = data.alerts || [];
  const alerts = alertItems.filter(item => !item.review).length;
  const running = rows.reduce((sum, item) => sum + Number(item.counts?.running || 0), 0);
  const closed = rows.reduce((sum, item) => sum + Number(item.counts?.closed || 0), 0);
  const updated = new Date(data.generated_at).toLocaleTimeString("pt-BR", { hour: "2-digit", minute: "2-digit" });
  const existing = $("#route-monitor-live");
  if (!existing) {
    $("#content").innerHTML = `
      <div id="route-monitor-live">
      <section class="hero"><div><small>MONITORAMENTO DE SERVIÇOS</small><h1>Acompanhar rota</h1><p>Atualização automática e silenciosa a cada minuto.</p></div><b>◷</b></section>
      <section class="route-metrics"><article class="card"><small>OS DO DIA</small><b id="route-total">${number(data.total_orders)}</b></article><article class="card"><small>EM EXECUÇÃO</small><b id="route-running">${number(running)}</b></article><article class="card"><small>ENCERRADAS</small><b id="route-closed">${number(closed)}</b></article><article id="route-alert-card" class="card ${alerts ? "has-alert" : ""}"><small>PRECISAM DE ANÁLISE</small><b id="route-alert-count">${number(alerts)}</b></article></section>
      <section class="section-title route-title"><div><h2>Equipe técnica</h2><span id="route-updated">Atualizado em ${updated}</span></div><div class="route-title-actions"><button id="refresh-route-monitor" class="secondary">Atualizar agora</button><a class="secondary button-link" href="#mapa-servicos">Mapa e serviços sem técnico</a></div></section>
      <div id="route-sticky-hours" class="route-timeline-sticky"><div class="route-timeline-header"><strong>Responsável</strong><div class="route-hours">${routeHourHeader()}</div></div></div>
      <div id="route-timeline-scroll" class="route-timeline-wrap"><div class="route-timeline">${rows.map((item, index) => `<section class="route-lane" data-route-tech="${index}"><div class="route-person"><strong>${esc(item.technician)}</strong><small>${item.counts?.total || 0} OS no dia</small></div><div class="route-track">${routeTrackMarkup(item, data)}</div></section>`).join("")}</div></div>
      <p class="route-note"><b>Regras:</b> acima de 1 hora fica em atenção; acima de 2 horas fica urgente. Marcador curto indica OS encerrada sem início registrado. Vermelho transparente indica que uma nova OS começou antes da anterior terminar.</p>
      <section class="section-title route-alert-title"><div><small>RELATÓRIO PARA AVALIAÇÃO</small><h2>Notificações do dia</h2><span>Atendimentos que ultrapassaram os limites de tempo.</span></div></section>
      <div id="route-alert-report">${routeAlertReport(alertItems)}</div></div>`;
    bindRouteOrderLinks(document);
    bindRouteReviews(document);
    bindRouteTimelineScroll();
    $("#refresh-route-monitor").onclick = () => routeMonitorView(true);
    return;
  }
  $("#route-total").textContent = number(data.total_orders);
  $("#route-running").textContent = number(running);
  $("#route-closed").textContent = number(closed);
  $("#route-alert-count").textContent = number(alerts);
  $("#route-alert-card").classList.toggle("has-alert", Boolean(alerts));
  $("#route-updated").textContent = `Atualizado em ${updated}`;
  rows.forEach((item, index) => {
    const lane = document.querySelector(`[data-route-tech="${index}"]`);
    if (!lane) return;
    const count = `${item.counts?.total || 0} OS no dia`;
    const countNode = lane.querySelector(".route-person small");
    if (countNode.textContent !== count) countNode.textContent = count;
    const track = lane.querySelector(".route-track");
    const markup = routeTrackMarkup(item, data);
    if (track.innerHTML !== markup) { track.innerHTML = markup; bindRouteOrderLinks(track); }
  });
  const report = $("#route-alert-report");
  const reportMarkup = routeAlertReport(alertItems);
  if (report.innerHTML !== reportMarkup) { report.innerHTML = reportMarkup; bindRouteReviews(report); }
}

function routeMonitorStartRefresh() {
  if (routeMonitorRefreshTimer) window.clearInterval(routeMonitorRefreshTimer);
  routeMonitorRefreshTimer = window.setInterval(async () => {
    if (routeMonitorRefreshing || document.querySelector('.assignment-dialog') || !location.hash.startsWith("#acompanhar-rota")) return;
    routeMonitorRefreshing = true;
    try {
      const data = await api("/api/superadmin/route-monitor?refresh=1", { timeoutMs: 70000 });
      if (location.hash.startsWith("#acompanhar-rota")) { routeMonitorRender(data); routeMonitorStartClock(); routeMonitorStartRefresh(); }
    } catch (error) {
      if (error.name !== "AbortError") console.warn("Atualização silenciosa da rota falhou:", error.message);
    } finally { routeMonitorRefreshing = false; }
  }, 60000);
}

async function ruptureTriageView() {
  setPage("Puxar rompimento"); loading("Carregando OLTs para a triagem...");
  try {
    const olts=await api("/api/technician/olts",{concurrent:true,timeoutMs:60000});
    $("#content").innerHTML=`<section class="hero"><div><small>TRIAGEM DE REDE</small><h1>Puxar rompimento</h1><p>Consulte uma PON e veja os clientes online e offline retornados pelo SGP.</p></div><b>◉</b></section><form id="rupture-form" class="card toolbar"><label>OLT<select name="olt_id" required><option value="">Selecione</option>${(olts||[]).map(o=>`<option value="${esc(o.id)}">${esc(o.name||`OLT ${o.id}`)}</option>`).join('')}</select></label><label>Slot<input name="slot" type="number" min="0" required></label><label>PON<input name="pon" type="number" min="0" required></label><button class="primary">Consultar clientes</button></form><section id="rupture-result"></section>`;
    $("#rupture-form").onsubmit=async event=>{event.preventDefault();const form=event.currentTarget,button=form.querySelector('button'),target=$("#rupture-result"),values=new FormData(form);button.disabled=true;button.textContent='Consultando...';target.innerHTML='<section class="card">Consultando todos os clientes da PON no SGP...</section>';try{const query=new URLSearchParams({olt_id:values.get('olt_id'),slot:values.get('slot'),pon:values.get('pon')}),data=await api(`/api/cto/pon-status?${query}`,{concurrent:true,timeoutMs:180000}),rows=data.onus||[],online=rows.filter(x=>x.online),offline=rows.filter(x=>!x.online),cards=list=>list.map(x=>`<article class="card"><small>${x.online?'ONLINE':'OFFLINE'} · Contrato ${esc(x.contract_id||'não informado')}</small><h3>${esc(x.client||x.label||x.serial||'Cliente não identificado')}</h3><p>${esc(x.address||'Endereço não informado')}</p><p>ONU: ${esc(x.serial||x.label||'não informada')} ${x.signal?`· Sinal ${esc(x.signal)}`:''}</p></article>`).join('')||'<article class="card empty">Nenhum cliente nesta situação.</article>';target.innerHTML=`<section class="stock-overview-metrics"><article><small>TOTAL</small><b>${number(rows.length)}</b></article><article><small>ONLINE</small><b>${number(online.length)}</b></article><article class="${offline.length?'alert':'ok'}"><small>OFFLINE</small><b>${number(offline.length)}</b></article><article><small>% OFFLINE</small><b>${rows.length?number(offline.length*100/rows.length):0}%</b></article></section><div class="cert-grid"><section><h2>Offline</h2>${cards(offline)}</section><section><h2>Online</h2>${cards(online)}</section></div>`;}catch(error){target.innerHTML=`<section class="card error">${esc(error.message)}</section>`;}finally{button.disabled=false;button.textContent='Consultar clientes';}};
  } catch(error) { errorView(error); }
}

async function routeMonitorView(force = false) {
  routeMonitorStopClock();
  setPage("Acompanhar rota");
  loading("Carregando as ordens de serviço de hoje...");
  try {
    const data = await api(`/api/superadmin/route-monitor${force ? "?refresh=1" : ""}`, { timeoutMs: 70000 });
    routeMonitorRender(data);
    routeMonitorStartClock();
    routeMonitorStartRefresh();
  } catch (error) { errorView(error); }
}

const routes = { "historico-logistica": () => logisticsAuditView(), "puxar-rompimento": ruptureTriageView, "pendencias-estoque": logisticsCasesView, "monitor-materiais": materialMonitorView, home: homeView, rota: routeFolderView, estoque: inventoryFolderView, "saldo-estoque": inventoryBalanceView, "conferencia-saldo": stockReconciliationView, "conferir-gpon": inventoryGponView, "comprar-modem": modemPurchaseView, relatorios: reportsView, "materiais-usados": materialUsageView, "analise-os": modemExchangeView, "acompanhar-rota": routeMonitorView, "acompanhamento-instalacoes": supervisorVisitTrackingView, "mapa-servicos": openServiceMapView, "mapa-atendimento": serviceCoverageView, "mapa-pon": serviceCoverageView, "saude-pons": ponHealthView, "vincular-modem": onuLinkView, "procurar-modem": modemSearchView, "modem-lancado": launchedModemView, "resumo-semanal": weeklySummaryView, pontuacao: scoreView, cto: ctoView, acessos: accessView };

routes.tutoriais = tutorialsView;
async function route() {
  if (!state.user) return;
  const raw = location.hash.slice(1) || "home";
  const [name, id] = raw.split("/");
  if (name === 'pontuacao' && state.user.mode !== 'superadmin') { location.hash = 'home'; return; }
  if (name === 'voip' && !['superadmin','office','admin'].includes(state.user.mode)) { location.hash = 'home'; return; }
  if (["office", "admin"].includes(state.user.mode) && !["home", "rota", "puxar-rompimento", "acompanhar-rota", "mapa-servicos", "mapa-atendimento", "mapa-pon", "saude-pons", "vincular-modem", "procurar-modem", "cto", "ordem", "tutoriais", "voip"].includes(name)) {
    location.hash = "home";
    return;
  }
  if (name !== "acompanhar-rota") { routeMonitorStopClock(); routeMapDestroy(); }
  if (name !== "acompanhamento-instalacoes") supervisorVisitTrackingStop();
  if (name !== "monitor-materiais") materialMonitorStop();
  if (name !== "estoque") stockDashboardStop();
  if (name !== "mapa-servicos") openServiceMapStop();
  if (name !== "mapa-atendimento" && name !== "mapa-pon") coverageMapStop();
  if (name !== "saude-pons") ponHealthStop();
  if (name !== "cto") stopPonObservation();
  if (name === "retiradas" || name === "jogar-retirada") { location.hash = "home"; return; }
  if (name === "alertas-estoque") return stockAlertsView(id);
  if (name === "meus-materiais") return personalStock();
  if (name === "cto" && id === "consulta") return ctoConsultationView();
  if (name === "cto" && id === "historico") return ctoHistoryView();
  if (name === "ordens") return id ? technicianOrdersView(id) : technicianOrdersLanding();
  if (name === "ordem" && id) return technicianOrderDetail(id);
  if (name === 'voip') return voipView();
  (routes[name] || homeView)();
}

function showApp() {
  $("#login").classList.add("hidden");
  $("#application").classList.remove("hidden");
  $("#user-name").textContent = state.user.nome || state.user.usuario;
  $("#user-mode").textContent = ({ technician: "Técnico", office: "Escritório", admin: "Administrador", superadmin: "Supervisor" })[state.user.mode] || "Usuário";
  $("#avatar").textContent = (state.user.nome || state.user.usuario || "U").split(/\s+/).slice(0, 2).map(word => word[0]).join("").toUpperCase();
  document.querySelectorAll("[data-access]").forEach(link => link.classList.toggle("hidden", !link.dataset.access.split(/\s+/).includes(state.user.mode)));
  syncPopupAccess();
  localStorage.removeItem('ebf-live-location');
  if(state.user.mode==='technician')syncTechnicianShift();
  history.replaceState(null, "", "#home");
  homeView();
}

function syncPopupAccess() {
  const enabled = state.user?.popup_enabled === true;
  try { window.EbfNative?.postMessage(JSON.stringify({ action: "setPopupEnabled", enabled })); } catch (_) {}
  document.querySelector("#popup-mode")?.remove();
  if (!enabled) return;
  const button = document.createElement("button");
  button.id = "popup-mode";
  button.className = "popup-mode-button";
  button.type = "button";
  button.title = "Abrir modo flutuante";
  button.innerHTML = "▣ <span>Popup</span>";
  button.onclick = () => {
    if (window.EbfNative) {
      window.EbfNative.postMessage(JSON.stringify({ action: "openPopup" }));
    } else {
      const popup = window.open(location.href, "ebfTecnicoPopup", "popup=yes,width=520,height=720,resizable=yes,scrollbars=yes");
      if (!popup) notify("O navegador bloqueou a janela. Libere popups para este site.");
    }
  };
  document.body.appendChild(button);
}

$("#login-form").addEventListener("submit", async event => {
  event.preventDefault();
  const button = event.submitter;
  button.disabled = true;
  $("#login-error").textContent = "";
  try {
    state.user = await api("/api/login", {
      method: "POST",
      body: JSON.stringify({
        username: $("#username").value,
        password: $("#password").value,
        remember: $("#remember").checked,
      }),
    });
    showApp();
  } catch (error) {
    if (error.name !== "AbortError") $("#login-error").textContent = error.message;
  } finally { button.disabled = false; }
});

$("#logout").onclick = async () => {
  stopLiveLocation();
  clearInterval(shiftScheduleTimer);
  try { await api("/api/logout", { method: "POST" }); } finally { location.reload(); }
};
function setMenuOpen(open) {
  document.body.classList.toggle("menu-open", open);
  $("#menu").setAttribute("aria-expanded", String(open));
}
$("#menu").onclick = () => setMenuOpen(!document.body.classList.contains("menu-open"));
$("#menu-scrim").onclick = () => { setMenuOpen(false); $("#menu").focus(); };
document.addEventListener("keydown", event => {
  if (event.key === "Escape" && document.body.classList.contains("menu-open")) {
    setMenuOpen(false);
    $("#menu").focus();
  }
});
window.addEventListener("hashchange", route);

(async function initialize() {
  if ("serviceWorker" in navigator) {
    try { await navigator.serviceWorker.register("/service-worker.js?v=20260810-2"); }
    catch (error) { console.warn("Service worker indisponível", error); }
  }
  try {
    state.user = await api("/api/me");
    showApp();
  } catch {
    $("#login").classList.remove("hidden");
  }
})();
