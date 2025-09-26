// NIP-07 login/logout helpers for templates
function pfGetCookie(name) {
  const value = `; ${document.cookie}`;
  const parts = value.split(`; ${name}=`);
  if (parts.length === 2) return parts.pop().split(';').shift();
  return null;
}

async function pfNostrLogin() {
  try {
    if (!window.nostr || !window.nostr.getPublicKey || !window.nostr.signEvent) {
      alert('NIP-07 provider not found. Install a Nostr extension.');
      return;
    }
    const csrf = pfGetCookie('csrf_token');
    const chRes = await fetch('/auth/challenge', { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrf || '' } });
    const ch = await chRes.json();
    const pubkey = await window.nostr.getPublicKey();
    const content = JSON.stringify({
      challenge_id: ch.challenge_id,
      challenge: ch.challenge,
      domain: 'postfun',
      exp: Math.floor(Date.now() / 1000) + 10 * 60
    });
    const evt = { kind: 1, content, tags: [], created_at: Math.floor(Date.now() / 1000), pubkey };
    const signed = await window.nostr.signEvent(evt);
    const vRes = await fetch('/auth/verify', { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrf || '' }, body: JSON.stringify({ event: signed }) });
    const out = await vRes.json();
    if (out.token) {
      // Reload to let server read the HttpOnly cookie and render user
      window.location.reload();
    } else {
      alert('Login failed: ' + JSON.stringify(out));
    }
  } catch (e) {
    alert('Login error: ' + e);
  }
}

async function pfLogout() {
  try {
    const jwt = localStorage.getItem('postfun_jwt');
    const csrf = pfGetCookie('csrf_token');
    const headers = { 'X-CSRFToken': csrf || '' };
    if (jwt) headers['Authorization'] = 'Bearer ' + jwt;
    await fetch('/auth/logout', { method: 'POST', headers });
    localStorage.removeItem('postfun_jwt');
    window.location.reload();
  } catch (e) {
    window.location.reload();
  }
}

// Phase 3 UI helpers
function pfInitFlashes() {
  const flashes = document.querySelectorAll('.pf-flash');
  if (!flashes || flashes.length === 0) return;
  setTimeout(() => {
    flashes.forEach(el => {
      el.style.transition = 'opacity 0.5s ease';
      el.style.opacity = '0';
      setTimeout(() => el.remove(), 600);
    });
  }, 3000);
  // Also show toasts immediately for visibility
  flashes.forEach(el => {
    let type = 'info';
    if (el.classList.contains('pf-flash-success')) type = 'success';
    else if (el.classList.contains('pf-flash-error')) type = 'error';
    pfToast(el.textContent, type, 3000);
  });
}

function pfInitTabs() {
  document.querySelectorAll('[data-tabs]').forEach(tabs => {
    const tabButtons = tabs.querySelectorAll('.pf-tab');
    const tabContents = tabs.querySelectorAll('.pf-tab-content');
    tabButtons.forEach(btn => {
      btn.addEventListener('click', () => {
        const target = btn.getAttribute('data-tab-target');
        tabButtons.forEach(b => b.classList.remove('pf-tab-active'));
        tabContents.forEach(c => c.classList.remove('pf-tab-content-active'));
        btn.classList.add('pf-tab-active');
        const active = tabs.querySelector(`[data-tab-content="${target}"]`);
        if (active) active.classList.add('pf-tab-content-active');
      });
    });
    // Keyboard navigation: Left/Right arrows to move focus + activate
    tabs.addEventListener('keydown', (e) => {
      if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;
      const buttons = Array.from(tabButtons);
      const activeIndex = buttons.findIndex(b => b.classList.contains('pf-tab-active'));
      let next = activeIndex;
      if (e.key === 'ArrowLeft') next = (activeIndex - 1 + buttons.length) % buttons.length;
      if (e.key === 'ArrowRight') next = (activeIndex + 1) % buttons.length;
      const btn = buttons[next];
      if (btn) {
        btn.focus();
        btn.click();
        e.preventDefault();
      }
    });
  });
}

function pfBuildChartPath(series, width, height) {
  if (!series || series.length === 0) return '';
  const prices = series.map(p => Number(p.price) || 0);
  const min = Math.min(...prices);
  const max = Math.max(...prices);
  const spread = (max - min) || 1;
  const n = series.length;
  const padX = 6; // px
  const padY = 6; // px
  const innerW = Math.max(1, width - padX * 2);
  const innerH = Math.max(1, height - padY * 2);
  const points = series.map((p, i) => {
    const x = padX + (i / (n - 1)) * innerW;
    const y = padY + (1 - ((Number(p.price) - min) / spread)) * innerH;
    return [x, y];
  });
  let d = '';
  points.forEach(([x, y], i) => {
    d += (i === 0 ? 'M' : 'L') + x + ' ' + y + ' ';
  });
  return d.trim();
}

// Deterministic mini-series from symbol + basePrice
function pfGenerateSeries(symbol, basePrice = 1, points = 20) {
  const s = (symbol || '').split('').reduce((acc, c) => acc + c.charCodeAt(0), 0) || 1;
  let p = Number(basePrice) || 1;
  const now = Date.now();
  const out = [];
  for (let i = 0; i < points; i++) {
    const delta = (((s + i * 3) % 7) - 3) * 0.001;
    p = Math.max(0.0001, p * (1 + delta));
    out.push({ t: new Date(now - (points - i) * 60000).toISOString(), price: Number(p.toFixed(6)) });
  }
  return out;
}

function pfInitSparklines() {
  const nodes = document.querySelectorAll('.pf-spark');
  nodes.forEach(node => {
    const sym = node.dataset.symbol || '';
    const price = Number(node.dataset.price || 0);
    const series = pfGenerateSeries(sym, price, 20);
    // ensure svg child exists
    let svg = node.querySelector('svg');
    if (!svg) {
      svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
      svg.setAttribute('class', 'pf-spark-svg');
      svg.setAttribute('width', '100%');
      svg.setAttribute('height', '40');
      svg.setAttribute('preserveAspectRatio', 'none');
      const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
      path.setAttribute('class', 'pf-spark-path');
      path.setAttribute('fill', 'none');
      path.setAttribute('stroke', 'currentColor');
      path.setAttribute('stroke-width', '1.5');
      svg.appendChild(path);
      node.appendChild(svg);
    }
    const path = svg.querySelector('path');
    const box = svg.getBoundingClientRect();
    const width = Math.max(120, Math.floor(box.width) || 120);
    const height = 40;
    const d = pfBuildChartPath(series, width, height);
    path.setAttribute('d', d);
  });
}

function pfInitChart() {
  const chart = document.getElementById('pf-chart');
  if (!chart) return;
  try {
    const series = JSON.parse(chart.dataset.series || '[]');
    const svg = chart.querySelector('.pf-chart-svg');
    const path = chart.querySelector('.pf-chart-path');
    if (!svg || !path) return;
    const box = svg.getBoundingClientRect();
    const width = Math.max(200, Math.floor(box.width));
    const height = Math.max(120, Math.floor(box.height));
    const d = pfBuildChartPath(series, width, height);
    path.setAttribute('d', d);

    // Set min/max labels if present
    const prices = series.map(p => Number(p.price) || 0);
    if (prices.length > 0) {
      const min = Math.min(...prices);
      const max = Math.max(...prices);
      const minEl = document.getElementById('pf-chart-min');
      const maxEl = document.getElementById('pf-chart-max');
      if (minEl) minEl.textContent = min.toFixed(6);
      if (maxEl) maxEl.textContent = max.toFixed(6);
    }
  } catch (e) {
    // ignore
  }
}

function pfInitTrade() {
  const form = document.getElementById('pf-trade-form');
  if (!form) return;
  const priceEl = document.getElementById('pf-trade-price');
  const totalEl = document.getElementById('pf-trade-total');
  const amountInput = form.querySelector('input[name="amount"]');
  function refresh() {
    const price = Number((priceEl && priceEl.textContent) || form.dataset.price || '0') || 0;
    const amt = Number(amountInput.value || '0') || 0;
    const total = price * amt;
    if (totalEl) totalEl.textContent = total.toFixed(6);
  }
  amountInput && amountInput.addEventListener('input', refresh);
  refresh();
}

window.addEventListener('DOMContentLoaded', () => {
  pfInitFlashes();
  pfInitTabs();
  pfInitChart();
  pfInitTrade();
  pfInitSparklines();
});

// Toasts
function pfEnsureToastContainer() {
  let container = document.querySelector('.pf-toast-container');
  if (!container) {
    container = document.createElement('div');
    container.className = 'pf-toast-container';
    document.body.appendChild(container);
  }
  return container;
}

function pfToast(message, type = 'info', timeout = 3000) {
  const container = pfEnsureToastContainer();
  const el = document.createElement('div');
  el.className = `pf-toast pf-toast-${type}`;
  el.textContent = message;
  container.appendChild(el);
  setTimeout(() => {
    el.style.transition = 'opacity 0.3s ease';
    el.style.opacity = '0';
    setTimeout(() => el.remove(), 350);
  }, timeout);
}
