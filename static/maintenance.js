(() => {
  let busy = false, administratorLogin = false;
  async function refresh() {
    if (busy) return;
    busy = true;
    try {
      const response = await fetch('/api/maintenance', {cache:'no-store'});
      if (!response.ok) return;
      const status = await response.json();
      let button = document.querySelector('#maintenance-control');
      if (status.can_manage && !button) {
        button = document.createElement('button'); button.id = 'maintenance-control';
        button.className = 'secondary';
        document.querySelector('#application nav')?.appendChild(button);
      }
      if (button) {
        button.hidden = !status.can_manage;
        button.textContent = status.enabled ? 'Reabrir aplicativo' : 'Ativar manutenção';
        button.onclick = async () => {
          if (!confirm(status.enabled ? 'Reabrir o aplicativo para todos?' : 'Ativar manutenção? Os demais usuários verão a tela de manutenção.')) return;
          button.disabled = true;
          try {
            const result = await fetch('/api/maintenance', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:!status.enabled})});
            if (!result.ok) throw new Error((await result.json()).error || 'Não foi possível salvar.');
            await refresh();
          } catch(error) { alert(error.message); }
          finally { button.disabled = false; }
        };
      }
      let overlay = document.querySelector('#maintenance-screen');
      if (status.enabled && !status.can_manage && !administratorLogin) {
        if (!overlay) {
          overlay = document.createElement('section'); overlay.id = 'maintenance-screen';
          overlay.style.cssText = 'position:fixed;inset:0;z-index:99999;background:#f3f6fa;display:grid;place-content:center;text-align:center;padding:24px;color:#14283e';
          overlay.innerHTML = '<h1>Site em manutenção</h1><p>O aplicativo está temporariamente indisponível.<br>Tente novamente mais tarde.</p><button type="button">Acesso do administrador</button>';
          overlay.querySelector('button').onclick = () => { administratorLogin = true; overlay.remove(); document.querySelector('#application').classList.add('hidden'); document.querySelector('#login').classList.remove('hidden'); };
          document.body.appendChild(overlay);
        }
      } else overlay?.remove();
      if (status.can_manage) administratorLogin = false;
    } finally { busy = false; }
  }
  document.querySelector('#login-form')?.addEventListener('submit', () => { administratorLogin = false; setTimeout(() => refresh().catch(()=>{}), 2000); });
  refresh().catch(()=>{});
  setInterval(() => refresh().catch(()=>{}), 15000);
})();
