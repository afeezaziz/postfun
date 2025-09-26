// NIP-07 login/logout helpers for templates
function pfGetCookie(name) {
  const value = `; ${document.cookie}`;
  const parts = value.split(`; ${name}=`);
  if (parts.length === 2) return parts.pop().split(';').shift();
  return null;
}

// Progress bars: set widths from data-progress attributes
function pfInitProgressBars() {
  document.querySelectorAll('.pf-progress-bar[data-progress]')
    .forEach(el => {
      const pct = Math.max(0, Math.min(100, Number(el.getAttribute('data-progress') || '0')));
      el.style.width = pct + '%';
    });
}

// Generic SSE with exponential backoff + jitter
function pfSSEWithBackoff(url, onMessage, label = 'sse') {
  if (typeof EventSource === 'undefined') return { close: () => {} };
  let attempt = 0;
  let es = null;
  let closed = false;
  function open() {
    if (closed) return;
    es = new EventSource(url);
    es.onmessage = (ev) => {
      attempt = 0; // reset on success
      try { onMessage(ev); } catch (e) {}
    };
    es.onerror = () => {
      try { es && es.close(); } catch {}
      if (closed) return;
      attempt += 1;
      const base = Math.min(30000, 1000 * Math.pow(2, Math.min(6, attempt - 1)));
      const jitter = Math.floor(Math.random() * 400);
      const delay = base + jitter;
      setTimeout(open, delay);
    };
  }
  open();
  return { close: () => { closed = true; try { es && es.close(); } catch {} } };
}

// Home: SSE for live trades into ticker
function pfInitTradesSSE() {
  const track = document.getElementById('pf-ticker-track');
  if (!track || typeof EventSource === 'undefined') return;
  try {
    pfSSEWithBackoff('/sse/trades', (ev) => {
      try {
        const data = JSON.parse(ev.data);
        const { symbol, side, price } = data;
        const span = document.createElement('span');
        span.className = 'pf-tick';
        span.style.display = 'inline-block';
        span.style.marginRight = '24px';
        span.innerHTML = `<span class="badge">${symbol}</span> <span class="pf-green" style="text-transform:uppercase;">${side}</span> <span>${Number(price||0).toFixed(6)}</span>`;
        track.appendChild(span);
        const clone = document.getElementById('pf-ticker-track-clone');
        if (clone) clone.appendChild(span.cloneNode(true));
      } catch (e) {}
    }, 'trades');
  } catch (e) {}
}

// Watchlist AJAX (uses /api/watchlist)
function pfInitWatchlist() {
  const onClick = async (btn) => {
    const symbol = btn.getAttribute('data-symbol');
    if (!symbol) return;
    const active = btn.getAttribute('data-active') === '1';
    const method = active ? 'DELETE' : 'POST';
    try {
      const res = await fetch('/api/watchlist', {
        method,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ symbol }),
        credentials: 'same-origin',
      });
      if (res.status === 401) {
        if (typeof window.pfNostrLogin === 'function') window.pfNostrLogin();
        else pfToast('Please login to use Watchlist', 'info');
        return;
      }
      if (!res.ok) {
        const j = await res.json().catch(() => ({}));
        throw new Error(j.error || `HTTP ${res.status}`);
      }
      if (method === 'POST') {
        btn.setAttribute('data-active', '1');
        btn.textContent = '★ Watchlisted';
        pfToast(`Added ${symbol} to watchlist`, 'success');
      } else {
        btn.setAttribute('data-active', '0');
        btn.textContent = '☆ Watchlist';
        pfToast(`Removed ${symbol} from watchlist`, 'info');
      }
    } catch (e) {
      pfToast(`Watchlist failed: ${e.message || e}`, 'error');
    }
  };
  document.addEventListener('click', (e) => {
    const btn = e.target.closest('.pf-watchlist-btn');
    if (!btn) return;
    e.preventDefault();
    onClick(btn);
  });
}

// Home: live ticker marquee
function pfInitTicker() {
  const track = document.getElementById('pf-ticker-track');
  if (!track) return;
  try {
    // Duplicate content to make loop seamless
    const clone = track.cloneNode(true);
    clone.id = 'pf-ticker-track-clone';
    track.parentNode.appendChild(clone);
    let x = 0;
    const speed = 60; // px per second
    let lastTs = performance.now();
    function step(ts) {
      const dt = (ts - lastTs) / 1000;
      lastTs = ts;
      x -= speed * dt;
      const w = track.getBoundingClientRect().width;
      // Loop when original fully out of view
      if (-x >= w) {
        x += w;
      }
      track.style.transform = `translateX(${x}px)`;
      clone.style.transform = `translateX(${x + w}px)`;
      requestAnimationFrame(step);
    }
    requestAnimationFrame(step);
  } catch (e) {
    // ignore
  }
}

// Home: tokenize input quickflow
function pfInitTokenize() {
  const input = document.getElementById('pf-tokenize-input');
  if (!input) return;
  const go = () => {
    const v = (input.value || '').trim();
    const url = v ? `/launchpad?q=${encodeURIComponent(v)}` : '/launchpad';
    window.location.href = url;
  };
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      go();
    }
  });
  const btn = document.querySelector('.pf-hero .retro-button[href*="/launchpad"]');
  if (btn) {
    btn.addEventListener('click', (e) => {
      const v = (input.value || '').trim();
      if (v) {
        e.preventDefault();
        window.location.href = `/launchpad?q=${encodeURIComponent(v)}`;
      }
    });
  }
}

// Util: debounce
function pfDebounce(fn, ms) {
  let t = null;
  return (...args) => {
    if (t) clearTimeout(t);
    t = setTimeout(() => fn.apply(null, args), ms);
  };
}

// Home: OG preview for pasted URL
function pfInitOgPreview() {
  const input = document.getElementById('pf-tokenize-input');
  const box = document.getElementById('pf-og-preview');
  if (!input || !box) return;
  const titleEl = document.getElementById('pf-og-title');
  const descEl = document.getElementById('pf-og-desc');
  const imgEl = document.getElementById('pf-og-img');
  const isUrl = (s) => /^https?:\/\//i.test(s);
  const update = pfDebounce(async () => {
    const v = (input.value || '').trim();
    if (!isUrl(v)) {
      box.style.display = 'none';
      return;
    }
    try {
      const res = await fetch(`/api/og/preview?url=${encodeURIComponent(v)}`, { credentials: 'same-origin' });
      const j = await res.json();
      if (!res.ok || j.error) throw new Error(j.error || 'preview_failed');
      titleEl.textContent = j.title || '';
      descEl.textContent = j.description || '';
      if (j.image) {
        imgEl.src = j.image;
        imgEl.style.display = '';
      } else {
        imgEl.style.display = 'none';
      }
      box.style.display = '';
    } catch (e) {
      box.style.display = 'none';
    }
  }, 350);
  input.addEventListener('input', update);
  input.addEventListener('paste', () => setTimeout(update, 10));
}

// Confetti utility
function pfConfetti(durationMs = 2000, count = 120) {
  const colors = ['#00ff00', '#00cc00', '#66ff66', '#33cc33'];
  const container = document.body;
  const pieces = [];
  for (let i = 0; i < count; i++) {
    const el = document.createElement('div');
    el.style.position = 'fixed';
    el.style.top = '-10px';
    el.style.left = (Math.random() * 100) + 'vw';
    el.style.width = '6px';
    el.style.height = '10px';
    el.style.background = colors[i % colors.length];
    el.style.opacity = '0.9';
    el.style.transform = `rotate(${Math.random()*360}deg)`;
    el.style.zIndex = '9999';
    el.style.pointerEvents = 'none';
    el.style.transition = 'transform 2.5s ease-out, top 2.5s ease-out, opacity 0.5s ease';
    container.appendChild(el);
    pieces.push(el);
    // start
    requestAnimationFrame(() => {
      const dx = (Math.random() * 2 - 1) * 200; // -200..200 px sideways
      const dy = window.innerHeight + 50;
      el.style.top = dy + 'px';
      el.style.transform = `translate(${dx}px, 0) rotate(${Math.random()*720}deg)`;
    });
  }
  setTimeout(() => {
    pieces.forEach(el => {
      el.style.opacity = '0';
      setTimeout(() => el.remove(), 600);
    });
  }, durationMs);
}

function pfInitLaunchConfetti() {
  const params = new URLSearchParams(window.location.search);
  if (params.get('launched') === '1') {
    pfConfetti();
    pfToast('Token launched! 🎉', 'success');
    params.delete('launched');
    const url = `${window.location.pathname}${params.toString()?('?'+params.toString()):''}`;
    history.replaceState(null, '', url);
  }
}

// SSE: in-app toasts for user alert events
function pfInitAlertsSSE() {
  if (typeof EventSource === 'undefined') return;
  try {
    pfSSEWithBackoff('/sse/alerts', (ev) => {
      try {
        const data = JSON.parse(ev.data);
        const { symbol, condition, threshold, price } = data;
        const msg = `${symbol} ${String(condition || '').replaceAll('_',' ')}: price ${Number(price||0).toFixed(6)} threshold ${Number(threshold||0).toFixed(6)}`;
        pfToast(msg, 'info', 5000);
      } catch (e) {
        // ignore parse errors
      }
    }, 'alerts');
  } catch (e) {
    // ignore
  }
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
  const svg = chart.querySelector('.pf-chart-svg');
  const path = chart.querySelector('.pf-chart-path');
  if (!svg || !path) return;

  function redraw(series) {
    try {
      const box = svg.getBoundingClientRect();
      const width = Math.max(200, Math.floor(box.width));
      const height = Math.max(120, Math.floor(box.height));
      const d = pfBuildChartPath(series, width, height);
      path.setAttribute('d', d);
      const prices = series.map(p => Number(p.price) || 0);
      if (prices.length > 0) {
        const min = Math.min(...prices);
        const max = Math.max(...prices);
        const minEl = document.getElementById('pf-chart-min');
        const maxEl = document.getElementById('pf-chart-max');
        if (minEl) minEl.textContent = min.toFixed(6);
        if (maxEl) maxEl.textContent = max.toFixed(6);
      }
    } catch (e) {}
  }

  // Start with server-provided series
  let series = [];
  try { series = JSON.parse(chart.dataset.series || '[]'); } catch {};
  window.pfChartSeries = Array.isArray(series) ? series.slice() : [];
  redraw(window.pfChartSeries);

  // Fetch richer history from API (24h by default)
  const sseEl = document.querySelector('[data-sse-symbol]');
  const symbol = sseEl ? sseEl.getAttribute('data-sse-symbol') : null;
  if (symbol) {
    fetch(`/api/tokens/${encodeURIComponent(symbol)}/series?window=24h&limit=300`, { credentials: 'same-origin' })
      .then(res => res.json())
      .then(j => {
        if (j && Array.isArray(j.items) && j.items.length) {
          window.pfChartSeries = j.items;
          redraw(window.pfChartSeries);
        }
      })
      .catch(() => {});
  }

  // Expose redraw for SSE appends
  window.pfChartRedraw = () => redraw(window.pfChartSeries || []);
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
  pfInitProgressBars();
  pfInitPricesSSE();
  pfInitAlertsSSE();
  pfInitTicker();
  pfInitTradesSSE();
  pfInitTokenize();
  pfInitOgPreview();
  pfInitLaunchConfetti();
  pfInitWatchlist();
  pfInitShareButtons();
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

// SSE: live price updates for a specific token
function pfInitPricesSSE() {
  const el = document.querySelector('[data-sse-symbol]');
  if (!el || typeof EventSource === 'undefined') return;
  const symbol = el.getAttribute('data-sse-symbol');
  if (!symbol) return;
  try {
    pfSSEWithBackoff(`/sse/prices?symbol=${encodeURIComponent(symbol)}`,(ev) => {
      try {
        const data = JSON.parse(ev.data);
        const price = Number(data.price || 0) || 0;
        document.querySelectorAll('[data-price-target]').forEach(node => {
          node.textContent = price.toFixed(6);
        });
        // Update trade total if present
        const totalEl = document.getElementById('pf-trade-total');
        const amountInput = document.querySelector('#pf-trade-form input[name="amount"]');
        if (totalEl && amountInput) {
          const amt = Number(amountInput.value || '0') || 0;
          totalEl.textContent = (price * amt).toFixed(6);
        }

        // Append to chart series and redraw (cap to 300 points)
        if (window.pfChartSeries && Array.isArray(window.pfChartSeries)) {
          const nowIso = new Date().toISOString();
          window.pfChartSeries.push({ t: nowIso, price });
          if (window.pfChartSeries.length > 300) {
            window.pfChartSeries = window.pfChartSeries.slice(window.pfChartSeries.length - 300);
          }
          if (typeof window.pfChartRedraw === 'function') window.pfChartRedraw();
        }
      } catch (e) {
        // ignore parse errors
      }
    }, 'prices');
  } catch (e) {
    // ignore
  }
}

// Social share helpers
function pfCopyLink(url) {
  try {
    navigator.clipboard.writeText(url).then(() => pfToast('Link copied', 'success'));
  } catch (e) {
    // Fallback
    const ta = document.createElement('textarea');
    ta.value = url; document.body.appendChild(ta); ta.select();
    try { document.execCommand('copy'); pfToast('Link copied', 'success'); } catch {}
    ta.remove();
  }
}

function pfShareUrl(url, text = 'Check this out on Postfun') {
  if (navigator.share) {
    navigator.share({ url, text }).catch(() => pfCopyLink(url));
  } else {
    pfCopyLink(url);
  }
}

function pfInitShareButtons() {
  document.addEventListener('click', (e) => {
    const shareBtn = e.target.closest('[data-share-url]');
    if (shareBtn) {
      e.preventDefault();
      const url = shareBtn.getAttribute('data-share-url');
      const text = shareBtn.getAttribute('data-share-text') || '';
      pfShareUrl(url, text);
      return;
    }
    const copyBtn = e.target.closest('[data-copy-url]');
    if (copyBtn) {
      e.preventDefault();
      const url = copyBtn.getAttribute('data-copy-url');
      pfCopyLink(url);
    }
  });
}
