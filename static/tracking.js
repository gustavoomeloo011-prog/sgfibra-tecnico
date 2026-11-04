'use strict';
const el = id => document.getElementById(id);
const apiPath = '/api/public/tracking/' + encodeURIComponent(location.pathname.split('/').filter(Boolean).pop());
function deviceIdentity() {
  const key = 'sgfibra-tracking-device-v1';
  let saved;
  try { saved = localStorage.getItem(key); } catch {}
  if (/^[A-Za-z0-9_-]{32,80}$/.test(saved || '')) return saved;
  const id = Array.from(crypto.getRandomValues(new Uint8Array(24)), b => b.toString(16).padStart(2, '0')).join('');
  try { localStorage.setItem(key, id); } catch {}
  return id;
}
const device = deviceIdentity();
async function request(suffix = '', payload) {
  const options = {cache:'no-store', headers:{'X-Tracking-Device':device}};
  if (payload !== undefined) {
    options.method = 'POST';
    options.headers['Content-Type'] = 'application/json';
    options.body = JSON.stringify(payload);
  }
  const response = await fetch(apiPath + suffix, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(data.error || 'Não foi possível consultar a visita. Tente novamente.');
    error.status = response.status;
    throw error;
  }
  return data;
}
let map, technicianMarker, destinationMarker, library, lastLog, pollTimer, busy = false;
function loadMapLibrary() {
  if (window.L) return Promise.resolve(window.L);
  if (!library) library = new Promise((resolve, reject) => {
    const css = document.createElement('link');
    css.rel = 'stylesheet'; css.href = 'https://unpkg.com/leaflet@1.9.4/dist/leaflet.css';
    document.head.append(css);
    const script = document.createElement('script');
    script.src = 'https://unpkg.com/leaflet@1.9.4/dist/leaflet.js';
    script.onload = () => resolve(window.L);
    script.onerror = () => reject(new Error('Mapa indisponível.'));
    document.head.append(script);
  });
  return library;
}
let mapRevision = 0;
function clearMap() {
  mapRevision++;
  el('map-card').hidden = true;
  if (map) map.remove();
  map = technicianMarker = destinationMarker = null;
}
async function showMap(position, destination) {
  if (!position || !destination) { clearMap(); return; }
  const revision = ++mapRevision;
  el('map-card').hidden = false;
  try {
    const L = await loadMapLibrary();
    if (revision !== mapRevision) return;
    const tech = [position.latitude, position.longitude], home = [destination.latitude, destination.longitude];
    if (!map) {
      map = L.map('map'); L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',{attribution:'© OpenStreetMap'}).addTo(map);
      const marker = (point, cls, label) => L.marker(point,{icon:L.divIcon({className:'',html:'<div class="'+cls+'"></div>',iconSize:[22,22]})}).addTo(map).bindTooltip(label);
      technicianMarker = marker(tech, 'tech', 'Técnico'); destinationMarker = marker(home, 'home', 'Endereço da visita');
      map.fitBounds(L.latLngBounds([tech,home]).pad(.25),{maxZoom:16});
    } else { technicianMarker.setLatLng(tech); destinationMarker.setLatLng(home); }
    el('map-note').textContent = 'Posição atualizada às ' + new Date(position.updated_at*1000).toLocaleTimeString('pt-BR') + '.';
  } catch { if (revision === mapRevision) el('map-note').textContent = 'Não foi possível carregar o mapa. Você pode continuar pelo chat.'; }
}
function messages(rows) {
  const signature=JSON.stringify(rows);
  if (lastLog === signature) return;
  lastLog=signature;
  const log=el('chat-log'); log.replaceChildren();
  for (const row of rows) {
    const entry=document.createElement('div'), label=document.createElement('small'), body=document.createElement('span');
    entry.className='message '+(row.side==='client'?'client':'');
    label.textContent=(row.side==='client'?'Você':'Técnico')+' · '+new Date(row.created_at*1000).toLocaleTimeString('pt-BR',{hour:'2-digit',minute:'2-digit'});
    body.textContent=row.text; entry.append(label,body); log.append(entry);
  }
  log.scrollTop=log.scrollHeight;
}
function notice(text) { el('page-notice').textContent=text; el('page-notice').hidden=!text; }
function render(data) {
  notice(''); el('identity-gate').hidden=!data.identity_required; el('tracking-content').hidden=!!data.identity_required;
  if (data.identity_required) { clearMap(); return; }
  const arrived=data.status==='arrived', open=data.chat_open===true, stage=arrived?2:open?1:0;
  el('order-number').textContent='OS '+data.order_id;
  Array.from(el('steps').children).forEach((step,i)=>{
    step.className=i===stage?'current':i<stage?'done':'';
    if(i===stage) step.setAttribute('aria-current','step'); else step.removeAttribute('aria-current');
  });
  el('status').textContent=arrived?'O técnico chegou':open?'Sua visita está em execução':'Sua visita está agendada';
  el('updated').textContent=arrived?'O deslocamento terminou.':open?(data.location?'O técnico está a caminho.':'Aguardando uma posição atual do técnico.'):'Quando o técnico iniciar o serviço, você poderá conversar com ele e acompanhar o deslocamento.';
  el('chat-form').hidden=!open; el('chat-input').disabled=!open; el('send').disabled=!open;
  el('chat-note').textContent=open?'As mensagens são enviadas diretamente ao técnico desta visita.':arrived?'Conversa encerrada após a chegada do técnico.':'As mensagens serão liberadas quando a OS estiver em execução.';
  messages(data.messages||[]);
  showMap(data.location,data.destination);
}
async function refresh() {
  if (busy) return;
  busy=true;
  try { render(await request()); }
  catch(error) {
    clearMap(); el('tracking-content').hidden=true; el('identity-gate').hidden=true; notice(error.message);
    if (error.status===410 || error.status===404) clearInterval(pollTimer);
  } finally {busy=false;}
}
el('identity-form').addEventListener('submit', async event=>{
  event.preventDefault(); el('identity-submit').disabled=true; el('identity-error').textContent='';
  try {await request('/verify',{cpf:el('identity-cpf').value}); el('identity-cpf').value=''; await refresh();}
  catch(error){el('identity-error').textContent=error.message;}
  finally{el('identity-submit').disabled=false;}
});
el('chat-form').addEventListener('submit',async event=>{
  event.preventDefault(); const input=el('chat-input');
  if(input.disabled || !input.value.trim()) return;
  el('send').disabled=true; el('chat-error').textContent='';
  try { await request('/messages',{text:input.value.trim()}); input.value=''; }
  catch(error){el('chat-error').textContent=error.message;}
  finally{await refresh();}
});
pollTimer=setInterval(refresh,5000); refresh();
