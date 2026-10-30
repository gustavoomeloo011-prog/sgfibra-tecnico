// CTO identifiers are extracted only when explicit; arbitrary descriptions
// must never merge unrelated customers into a supposed physical box.
function ponCtoGroup(onu) {
  const extract = value => {
    const text = String(value || '').normalize('NFD').replace(/[\u0300-\u036f]/g, '').toUpperCase();
    return [...text.matchAll(/\bCTO\s*[:#._-]?\s*([A-Z]*\d+[A-Z0-9]*(?:[-./][A-Z0-9]+)*)\b/g)].map(match => match[1]);
  };
  const ids = [...new Set([...extract(onu.label), ...extract(onu.description)])];
  if (ids.length === 1) return `CTO ${ids[0]}`;
  return ids.length > 1 ? 'CTO divergente — conferir etiqueta e descrição' : 'Sem informação de CTO';
}

function ponHighlightedSignal(value) {
  return ponSignal(value);
}

function ponDetailsMarkup(result) {
  const groups = new Map();
  for (const onu of result.clients || []) {
    const key = ponCtoGroup(onu);
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(onu);
  }
  const priority = {bad:0, limit:1, no_signal:2, good:3};
  return `<p>${number(result.clients?.length || 0)} clientes · Leitura: ${esc(result.checked_at || 'não informada')}</p><p>Agrupamento pelos códigos de CTO na etiqueta ou descrição da ONU. Informações divergentes ficam separadas para conferência.</p>` + [...groups].sort(([a],[b]) => {
    const rank = value => value.startsWith('CTO divergente') ? 1 : value.startsWith('Sem ') ? 2 : 0;
    return rank(a)-rank(b) || a.localeCompare(b, 'pt-BR', {numeric:true});
  }).map(([name, clients]) => `<section class="card"><h2>${esc(name)} · ${number(clients.length)} clientes</h2><p>${number(clients.filter(c=>c.signal_level==='bad').length)} com sinal ruim · ${number(clients.filter(c=>c.signal_level==='no_signal').length)} sem leitura</p><div class="pon-details-clients">${clients.sort((a,b)=>(priority[a.signal_level]??4)-(priority[b.signal_level]??4)).map(onu => `<article class="card"><strong>${esc(onu.client || 'Cliente não informado')}</strong><p>Contrato ${esc(onu.contract_id ?? '—')} · ONU ${esc(onu.serial || onu.id || '—')}</p><p>${esc(onu.address || '')}</p><p><b>RX (modem): ${ponHighlightedSignal(onu.rx_dbm)}</b> · <b>Retorno na OLT: ${ponHighlightedSignal(onu.tx_dbm)}</b></p><p>${esc({bad:'🔴 Sinal ruim',limit:'🟠 No limite',good:'🟢 Sinal bom',no_signal:'⚪ Sem leitura'}[onu.signal_level] || 'Sem classificação')}</p><small>Etiqueta: ${esc(onu.label || '—')}<br>Descrição: ${esc(onu.description || '—')}</small></article>`).join('')}</div></section>`).join('') || '<p>Nenhum cliente retornado.</p>';
}

async function openPonDetails(item) {
  document.querySelector('#pon-details-dialog')?.remove();
  const dialog = document.createElement('dialog');
  dialog.id = 'pon-details-dialog';
  dialog.className = 'pon-details-dialog';
  dialog.innerHTML = `<header><div><small>CLIENTES E SINAIS POR CTO</small><h2>${esc(item.olt)} · Slot ${esc(item.slot)} · PON ${esc(item.pon)}</h2></div><button type="button" class="secondary" data-close>Fechar ×</button></header><div class="toolbar"><button type="button" class="primary" data-refresh>Atualizar sinais</button><span role="status" data-status></span></div><div data-results>Carregando sinais dos clientes...</div>`;
  document.body.appendChild(dialog);
  dialog.querySelector('[data-close]').onclick = () => dialog.close();
  dialog.addEventListener('close', () => dialog.remove(), {once:true});
  dialog.showModal();
  const load = async force => {
    const button = dialog.querySelector('[data-refresh]'), status = dialog.querySelector('[data-status]');
    button.disabled = true; status.textContent = 'Consultando sinais...';
    try {
      const query = new URLSearchParams({olt_id:item.olt_id,slot:item.slot,pon:item.pon});
      if (force) query.set('refresh','1');
      const result = await api(`/api/office/pon-analysis?${query}`, {concurrent:true,timeoutMs:120000});
      if (!dialog.isConnected) return;
      dialog.querySelector('[data-results]').innerHTML = ponDetailsMarkup(result);
      status.textContent = 'Consulta concluída';
    } catch(error) { status.textContent = error.message; }
    finally { button.disabled = false; }
  };
  dialog.querySelector('[data-refresh]').onclick = () => load(true);
  await load(false);
}
