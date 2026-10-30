const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('static/essential.js', 'utf8');
const elements = new Map();
const element = key => { if (!elements.has(key)) elements.set(key, { innerHTML: '', textContent: '', classList: { toggle() {} } }); return elements.get(key); };
let click, opened;
const context = {
  state: { user: { mode: 'superadmin' } }, sessionStorage: { getItem() { return null; } },
  Intl, Date, Number, String, Set, encodeURIComponent,
  esc: value => String(value ?? ''), number: value => new Intl.NumberFormat('pt-BR').format(value || 0),
  $: element, materialMonitorStop() {}, setPage() {}, loading() {}, errorView(error) { throw error; },
  materialAnalysisDetailView(...args) { opened = args; },
  api: async () => ({ date: '2026-09-28', closed_orders: 1, ai_available: true, items: [{ os_id: 84012, status: 'correct', client: 'Teste' }] }),
  document: { querySelectorAll() { const id = element('#content').innerHTML.match(/data-order-id="([^"]+)"/)[1]; const button = { dataset: { orderId: id } }; click = button; return [button]; } }
};
vm.createContext(context);
vm.runInContext(source.slice(source.indexOf('async function materialMonitorView('), source.indexOf('async function materialAnalysisDetailView(')), context);
(async () => {
  await context.materialMonitorView(false, '2026-09-28');
  click.onclick();
  assert.equal(opened[0], 84012, 'Card must preserve the integer OS ID, without locale separators');
  assert.equal(opened[1], '2026-09-28');
  assert.equal(opened[3].os_id, 84012);
  console.log('Material monitor: card opens the original OS and retains its audit.');
})().catch(error => { console.error(error); process.exitCode = 1; });
