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

// Global SSE cleanup manager
window.pfSSEConnections = new Map();

// Cleanup all SSE connections
function pfCleanupAllSSE() {
  window.pfSSEConnections.forEach((conn, label) => {
    try {
      if (conn && typeof conn.close === 'function') {
        conn.close();
      }
    } catch (e) {
      console.warn(`Error closing SSE connection ${label}:`, e);
    }
  });
  window.pfSSEConnections.clear();
}

// Cleanup SSE connections on page unload
window.addEventListener('beforeunload', pfCleanupAllSSE);

// Intercept navigation to clean up SSE connections
(function() {
  const originalPushState = window.history.pushState;
  const originalReplaceState = window.history.replaceState;

  window.history.pushState = function() {
    pfCleanupAllSSE();
    return originalPushState.apply(this, arguments);
  };

  window.history.replaceState = function() {
    pfCleanupAllSSE();
    return originalReplaceState.apply(this, arguments);
  };

  // Handle back/forward navigation
  window.addEventListener('popstate', pfCleanupAllSSE);

  // Intercept link clicks for internal navigation
  document.addEventListener('click', function(e) {
    const link = e.target.closest('a');
    if (link && link.href && link.href.startsWith(window.location.origin)) {
      pfCleanupAllSSE();
    }
  });
})();

// Generic SSE with exponential backoff + jitter and timeout
function pfSSEWithBackoff(url, onMessage, label = 'sse') {
  if (typeof EventSource === 'undefined') return { close: () => {} };

  // Close existing connection with same label
  if (window.pfSSEConnections.has(label)) {
    const existingConn = window.pfSSEConnections.get(label);
    if (existingConn && typeof existingConn.close === 'function') {
      existingConn.close();
    }
    window.pfSSEConnections.delete(label);
  }

  let attempt = 0;
  let es = null;
  let closed = false;
  let timeoutId = null;

  function cleanup() {
    closed = true;
    if (timeoutId) {
      clearTimeout(timeoutId);
      timeoutId = null;
    }
    if (es) {
      try {
        es.close();
      } catch (e) {
        console.warn(`Error closing EventSource for ${label}:`, e);
      }
      es = null;
    }
  }

  function open() {
    if (closed) return;

    // Set connection timeout
    timeoutId = setTimeout(() => {
      console.warn(`SSE connection timeout for ${label}:`, url);
      cleanup();
      // Don't retry if we hit timeout - might be server issue
      return;
    }, 15000); // 15 second timeout

    try {
      es = new EventSource(url);

      es.onopen = () => {
        // Connection successful, clear timeout
        if (timeoutId) {
          clearTimeout(timeoutId);
          timeoutId = null;
        }
        attempt = 0;
      };

      es.onmessage = (ev) => {
        attempt = 0; // reset on success
        try { onMessage(ev); } catch (e) {
          console.warn(`Error in SSE message handler for ${label}:`, e);
        }
      };

      es.onerror = () => {
        if (timeoutId) {
          clearTimeout(timeoutId);
          timeoutId = null;
        }

        try { es && es.close(); } catch {}
        es = null;

        if (closed) return;

        attempt += 1;
        // Stop retrying after too many attempts
        if (attempt > 5) {
          console.warn(`SSE connection failed after ${attempt} attempts for ${label}:`, url);
          return;
        }

        const base = Math.min(30000, 1000 * Math.pow(2, Math.min(6, attempt - 1)));
        const jitter = Math.floor(Math.random() * 400);
        const delay = base + jitter;

        console.log(`Retrying SSE connection for ${label} in ${delay}ms (attempt ${attempt})`);
        setTimeout(open, delay);
      };
    } catch (e) {
      console.error(`Failed to create EventSource for ${label}:`, e);
      cleanup();
    }
  }

  open();

  const conn = {
    close: cleanup,
    url: url,
    label: label
  };

  window.pfSSEConnections.set(label, conn);
  return conn;
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
      } catch (e) {
        console.warn('Error parsing trade data:', e);
      }
    }, 'trades');
  } catch (e) {
    console.warn('Error initializing trades SSE:', e);
  }
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
  // Only initialize for logged-in users
  if (!document.querySelector('.pf-npub')) return;
  try {
    pfSSEWithBackoff('/sse/alerts', (ev) => {
      try {
        const data = JSON.parse(ev.data);
        const { symbol, condition, threshold, price } = data;
        const msg = `${symbol} ${String(condition || '').replaceAll('_',' ')}: price ${Number(price||0).toFixed(6)} threshold ${Number(threshold||0).toFixed(6)}`;
        pfToast(msg, 'info', 5000);
      } catch (e) {
        console.warn('Error parsing alert data:', e);
      }
    }, 'alerts');
  } catch (e) {
    console.warn('Error initializing alerts SSE:', e);
  }
}

// SSE: follower notifications (creator launches and stage changes)
function pfInitFollowSSE() {
  if (typeof EventSource === 'undefined') return;
  // Only initialize for logged-in users (header renders npub span when authed)
  if (!document.querySelector('.pf-npub')) return;
  try {
    pfSSEWithBackoff('/sse/follow', (ev) => {
      try {
        const data = JSON.parse(ev.data);
        if (!data || !data.type) return;
        if (data.type === 'launch') {
          const sym = data.symbol || '?';
          const cr = data.creator || 'Creator';
          pfToast(`${cr} launched ${sym} 🚀`, 'success', 6000);
        } else if (data.type === 'stage') {
          const sym = data.symbol || '?';
          const st = Number(data.stage || 0);
          pfToast(`${sym} progressed to stage ${st}`, 'info', 5000);
        }
      } catch (e) {
        console.warn('Error parsing follow data:', e);
      }
    }, 'follow');
  } catch (e) {
    console.warn('Error initializing follow SSE:', e);
  }
}

// Dashboard login with wallet selection
async function pfDashboardLogin() {
  console.log('pfDashboardLogin called');
  try {
    const res = await fetch('/api/auth/check', { credentials: 'same-origin' });
    console.log('Auth check response:', res.status);
    if (res.ok) {
      const data = await res.json();
      console.log('Auth check data:', data);
      if (data.authenticated) {
        console.log('User authenticated, redirecting to dashboard');
        window.location.href = '/dashboard';
        return;
      }
    }
    console.log('User not authenticated, showing wallet selection');
    showWalletSelectionModal();
  } catch (e) {
    console.log('Auth check error:', e);
    showWalletSelectionModal();
  }
}

// Show wallet selection modal
function showWalletSelectionModal() {
  console.log('showWalletSelectionModal called');

  // Remove existing modal if any
  const existingModal = document.getElementById('pf-wallet-modal');
  if (existingModal) {
    existingModal.remove();
  }

  // Create modal
  const modal = document.createElement('div');
  modal.id = 'pf-wallet-modal';
  modal.className = 'pf-modal-overlay';
  modal.innerHTML = `
    <div class="pf-modal">
      <div class="pf-modal-header">
        <h3>Select Wallet</h3>
        <button class="pf-modal-close" onclick="closeWalletModal()">&times;</button>
      </div>
      <div class="pf-modal-body">
        <div class="pf-wallet-options">
          <button class="pf-wallet-btn" onclick="pfConnectNostrWallet()">
            <div class="pf-wallet-icon">🔑</div>
            <div class="pf-wallet-info">
              <div class="pf-wallet-name">Nostr Wallet</div>
              <div class="pf-wallet-desc">Connect using NIP-07 extension</div>
            </div>
          </button>
          <button class="pf-wallet-btn" onclick="pfConnectOKXWallet()">
            <div class="pf-wallet-icon">🦊</div>
            <div class="pf-wallet-info">
              <div class="pf-wallet-name">OKX Wallet</div>
              <div class="pf-wallet-desc">Connect using OKX browser extension</div>
            </div>
          </button>
        </div>
      </div>
    </div>
  `;

  // Add modal styles if not already present
  if (!document.getElementById('pf-wallet-modal-styles')) {
    const styles = document.createElement('style');
    styles.id = 'pf-wallet-modal-styles';
    styles.textContent = `
      .pf-modal-overlay {
        position: fixed;
        top: 0;
        left: 0;
        width: 100%;
        height: 100%;
        background: rgba(0, 0, 0, 0.8);
        display: flex;
        align-items: center;
        justify-content: center;
        z-index: 9999;
      }
      .pf-modal {
        background: #1a1a1a;
        border: 2px solid #00ff00;
        border-radius: 8px;
        max-width: 400px;
        width: 90%;
        max-height: 90vh;
        overflow-y: auto;
      }
      .pf-modal-header {
        display: flex;
        justify-content: space-between;
        align-items: center;
        padding: 16px;
        border-bottom: 1px solid #00ff00;
      }
      .pf-modal-header h3 {
        color: #00ff00;
        margin: 0;
        font-size: 18px;
      }
      .pf-modal-close {
        background: none;
        border: none;
        color: #00ff00;
        font-size: 24px;
        cursor: pointer;
        padding: 0;
        width: 30px;
        height: 30px;
        display: flex;
        align-items: center;
        justify-content: center;
      }
      .pf-modal-close:hover {
        color: #ff0000;
      }
      .pf-modal-body {
        padding: 16px;
      }
      .pf-wallet-options {
        display: flex;
        flex-direction: column;
        gap: 12px;
      }
      .pf-wallet-btn {
        background: #2a2a2a;
        border: 1px solid #00ff00;
        border-radius: 6px;
        padding: 16px;
        display: flex;
        align-items: center;
        gap: 12px;
        cursor: pointer;
        transition: all 0.2s;
        width: 100%;
        text-align: left;
      }
      .pf-wallet-btn:hover {
        background: #333333;
        border-color: #00cc00;
      }
      .pf-wallet-icon {
        font-size: 24px;
        width: 40px;
        height: 40px;
        display: flex;
        align-items: center;
        justify-content: center;
        background: #1a1a1a;
        border-radius: 50%;
      }
      .pf-wallet-info {
        flex: 1;
      }
      .pf-wallet-name {
        color: #00ff00;
        font-weight: bold;
        font-size: 16px;
        margin-bottom: 4px;
      }
      .pf-wallet-desc {
        color: #888888;
        font-size: 14px;
      }
    `;
    document.head.appendChild(styles);
  }

  document.body.appendChild(modal);

  // Close modal when clicking outside
  modal.addEventListener('click', (e) => {
    if (e.target === modal) {
      closeWalletModal();
    }
  });
}

// Close wallet modal
function closeWalletModal() {
  const modal = document.getElementById('pf-wallet-modal');
  if (modal) {
    modal.remove();
  }
}

// Connect with Nostr wallet
async function pfConnectNostrWallet() {
  console.log('pfConnectNostrWallet called');
  closeWalletModal();

  try {
    if (!window.nostr || !window.nostr.getPublicKey || !window.nostr.signEvent) {
      alert('NIP-07 provider not found. Please install a Nostr extension like nos2x or Alby.');
      return;
    }

    const csrf = pfGetCookie('csrf_token');
    const chRes = await fetch('/auth/challenge', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrf || '' }
    });
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
    const vRes = await fetch('/auth/verify', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrf || '' },
      body: JSON.stringify({ event: signed })
    });
    const out = await vRes.json();
    if (out.token) {
      localStorage.setItem('postfun_jwt', out.token);
      window.location.reload();
    } else {
      alert('Login failed: ' + JSON.stringify(out));
    }
  } catch (e) {
    alert('Nostr login error: ' + e);
  }
}

// Connect with OKX wallet
async function pfConnectOKXWallet() {
  console.log('pfConnectOKXWallet called');
  closeWalletModal();

  try {
    // Check if OKX wallet is available
    if (!window.okxwallet || !window.okxwallet.nostr) {
      alert('OKX wallet not found. Please install OKX Wallet browser extension and enable Nostr support.');
      return;
    }

    const okxNostr = window.okxwallet.nostr;
    console.log('OKX - Available methods:', Object.getOwnPropertyNames(okxNostr));

    // Test if the required methods are available
    if (!okxNostr.getPublicKey || !okxNostr.signEvent) {
      alert('OKX wallet Nostr interface not available. Please ensure Nostr is enabled in OKX wallet settings.');
      return;
    }

    // Check if OKX wallet has the right interface
    console.log('OKX - Nostr interface type:', typeof okxNostr);
    console.log('OKX - Available Nostr methods:', Object.getOwnPropertyNames(Object.getPrototypeOf(okxNostr)));

    const csrf = pfGetCookie('csrf_token');
    const chRes = await fetch('/auth/challenge', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrf || '' }
    });
    const ch = await chRes.json();
    console.log('OKX - Challenge response:', ch);

    const pubkey = await okxNostr.getPublicKey();
    console.log('OKX - Public key:', pubkey);

    const content = JSON.stringify({
      challenge_id: ch.challenge_id,
      challenge: ch.challenge,
      domain: 'postfun',
      exp: Math.floor(Date.now() / 1000) + 10 * 60
    });

    // Try multiple signing approaches
    const signingAttempts = [
      {
        name: 'Standard NIP-01 event',
        event: { kind: 1, content, tags: [], created_at: Math.floor(Date.now() / 1000), pubkey }
      },
      {
        name: 'Event without explicit pubkey',
        event: { kind: 1, content, tags: [], created_at: Math.floor(Date.now() / 1000) }
      },
      {
        name: 'Minimal event',
        event: { kind: 1, created_at: Math.floor(Date.now() / 1000), content, tags: [] }
      },
      {
        name: 'OKX specific approach',
        event: { kind: 1, created_at: Math.floor(Date.now() / 1000), content, tags: [], pubkey }
      }
    ];

    for (const attempt of signingAttempts) {
      try {
        console.log(`OKX - Attempting ${attempt.name}:`, attempt.event);

        // Add timeout for signing
        let signed;
        try {
          signed = await Promise.race([
            okxNostr.signEvent(attempt.event),
            new Promise((_, reject) =>
              setTimeout(() => reject(new Error('Signing timeout')), 10000)
            )
          ]);
          console.log(`OKX - Signed event (${attempt.name}):`, signed);
        } catch (signError) {
          console.error(`OKX - Signing failed for ${attempt.name}:`, signError);
          throw signError;
        }

        // Validate the signed event structure
        if (!signed || typeof signed !== 'object') {
          console.error(`OKX - Invalid response for ${attempt.name}:`, signed);
          continue;
        }

        if (!signed.id || !signed.sig || !signed.pubkey) {
          console.error(`OKX - Missing required fields for ${attempt.name}:`, signed);
          continue;
        }

        // Try to verify with the server
        const vRes = await fetch('/auth/verify', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrf || '' },
          body: JSON.stringify({ event: signed })
        });
        const out = await vRes.json();
        console.log(`OKX - Verify response (${attempt.name}):`, out);

        if (out.token) {
          console.log(`OKX - Success with ${attempt.name}!`);
          localStorage.setItem('postfun_jwt', out.token);
          window.location.reload();
          return;
        } else if (out.error !== 'invalid_signature_or_payload') {
          // If it's a different error, no point in trying other approaches
          alert(`OKX login failed: ${JSON.stringify(out)}`);
          return;
        }
      } catch (attemptError) {
        console.error(`OKX - Error with ${attempt.name}:`, attemptError);
        // Continue to next attempt
      }
    }

    // If all attempts failed, provide a helpful error message
    const errorMessage = `OKX wallet authentication failed.

This could be due to:
1. OKX wallet's Nostr implementation compatibility issues
2. Nostr not being properly enabled in OKX wallet settings
3. Network connectivity issues

Troubleshooting steps:
1. Make sure Nostr is enabled in OKX wallet settings
2. Try refreshing the page and reconnecting
3. Consider using a standard Nostr extension like Alby or nos2x instead

Console logs may have more details about the specific error.`;

    alert(errorMessage);

  } catch (e) {
    console.error('OKX wallet error:', e);
    alert('OKX wallet error: ' + e);
  }
}

// Legacy function for backward compatibility
async function pfNostrLogin() {
  console.log('pfNostrLogin called (legacy)');
  await pfConnectNostrWallet();
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

  // Candle layer container
  let gCandles = svg.querySelector('.pf-candles');
  if (!gCandles) {
    gCandles = document.createElementNS('http://www.w3.org/2000/svg', 'g');
    gCandles.setAttribute('class', 'pf-candles');
    svg.appendChild(gCandles);
  }

  function updateMinMax(min, max) {
    try {
      const minEl = document.getElementById('pf-chart-min');
      const maxEl = document.getElementById('pf-chart-max');
      if (typeof min === 'number' && isFinite(min) && minEl) minEl.textContent = min.toFixed(6);
      if (typeof max === 'number' && isFinite(max) && maxEl) maxEl.textContent = max.toFixed(6);
    } catch (e) {}
  }

  function redrawLine(series) {
    try {
      const box = svg.getBoundingClientRect();
      const width = Math.max(200, Math.floor(box.width));
      const height = Math.max(120, Math.floor(box.height));
      const d = pfBuildChartPath(series, width, height);
      path.setAttribute('d', d);
      path.style.display = '';
      gCandles.style.display = 'none';
      const prices = series.map(p => Number(p.price) || 0);
      if (prices.length > 0) {
        updateMinMax(Math.min(...prices), Math.max(...prices));
      }
    } catch (e) {}
  }

  function redrawCandles(candles) {
    try {
      const box = svg.getBoundingClientRect();
      const width = Math.max(200, Math.floor(box.width));
      const height = Math.max(120, Math.floor(box.height));
      const padX = 6, padY = 6;
      const innerW = Math.max(1, width - padX * 2);
      const innerH = Math.max(1, height - padY * 2);
      const highs = candles.map(c => Number(c.h) || 0);
      const lows = candles.map(c => Number(c.l) || 0);
      const min = lows.length ? Math.min(...lows) : 0;
      const max = highs.length ? Math.max(...highs) : 1;
      const spread = (max - min) || 1;
      const n = Math.max(1, candles.length);
      const candleSpace = innerW / n;
      const bodyW = Math.max(2, Math.floor(candleSpace * 0.6));
      // Clear previous
      while (gCandles.firstChild) gCandles.removeChild(gCandles.firstChild);
      // Draw
      candles.forEach((c, i) => {
        const o = Number(c.o), h = Number(c.h), l = Number(c.l), cl = Number(c.c);
        const xCenter = padX + i * candleSpace + candleSpace / 2;
        const y = (val) => padY + (1 - ((val - min) / spread)) * innerH;
        const yO = y(o), yH = y(h), yL = y(l), yC = y(cl);
        const up = cl >= o;
        const stroke = up ? '#00cc66' : '#cc3333';
        const fill = up ? '#00cc66' : 'transparent';
        const bodyTop = up ? yC : yO;
        const bodyBottom = up ? yO : yC;
        // Wick
        const wick = document.createElementNS('http://www.w3.org/2000/svg', 'line');
        wick.setAttribute('x1', String(xCenter));
        wick.setAttribute('x2', String(xCenter));
        wick.setAttribute('y1', String(yH));
        wick.setAttribute('y2', String(yL));
        wick.setAttribute('stroke', stroke);
        wick.setAttribute('stroke-width', '1');
        gCandles.appendChild(wick);
        // Body
        const rect = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
        rect.setAttribute('x', String(Math.round(xCenter - bodyW / 2)));
        rect.setAttribute('y', String(Math.round(Math.min(bodyTop, bodyBottom))));
        rect.setAttribute('width', String(bodyW));
        rect.setAttribute('height', String(Math.max(1, Math.abs(bodyBottom - bodyTop))));
        rect.setAttribute('fill', fill);
        rect.setAttribute('stroke', stroke);
        rect.setAttribute('stroke-width', '1');
        gCandles.appendChild(rect);
      });
      // Toggle layers
      gCandles.style.display = '';
      path.style.display = 'none';
      updateMinMax(min, max);
    } catch (e) {}
  }

  // Fetch OHLC data from API
  function fetchOHLC(symbol, interval = '1m', windowArg = '24h') {
    const url = `/api/ohlc?symbol=${encodeURIComponent(symbol)}&interval=${encodeURIComponent(interval)}&window=${encodeURIComponent(windowArg)}`;
    return fetch(url, { credentials: 'same-origin' })
      .then(res => res.json())
      .then(j => (j && Array.isArray(j.items)) ? j.items : [])
      .catch(() => []);
  }

  // Start with server-provided series (line mode default)
  let series = [];
  try { series = JSON.parse(chart.dataset.series || '[]'); } catch {};
  window.pfChartSeries = Array.isArray(series) ? series.slice() : [];
  window.pfChartCandles = [];
  window.pfChartMode = 'line';

  // Fetch richer history from API (24h by default)
  const sseEl = document.querySelector('[data-sse-symbol]');
  const symbol = sseEl ? sseEl.getAttribute('data-sse-symbol') : null;
  if (symbol) {
    fetch(`/api/tokens/${encodeURIComponent(symbol)}/series?window=24h&limit=300`, { credentials: 'same-origin' })
      .then(res => res.json())
      .then(j => {
        if (j && Array.isArray(j.items) && j.items.length) {
          window.pfChartSeries = j.items;
          if (window.pfChartMode === 'line') redrawLine(window.pfChartSeries);
        }
      })
      .catch(() => {});
  }

  // Controls: mode, interval, refresh
  const intervalSelect = chart.querySelector('[data-chart-interval]');
  const modeButtons = chart.querySelectorAll('[data-chart-mode]');
  const refreshBtn = chart.querySelector('[data-chart-refresh]');
  const getInterval = () => (intervalSelect ? (intervalSelect.value || '1m') : '1m');

  function setMode(nextMode) {
    window.pfChartMode = nextMode === 'candle' ? 'candle' : 'line';
    // toggle button active state
    modeButtons.forEach(btn => {
      const m = btn.getAttribute('data-chart-mode');
      if (m === window.pfChartMode) btn.classList.add('pf-active'); else btn.classList.remove('pf-active');
    });
    if (window.pfChartMode === 'line') {
      redrawLine(window.pfChartSeries || []);
    } else if (symbol) {
      // draw existing or fetch
      const candles = window.pfChartCandles || [];
      if (candles.length) {
        redrawCandles(candles);
      } else {
        fetchOHLC(symbol, getInterval()).then(items => { window.pfChartCandles = items; redrawCandles(items); });
      }
    }
  }

  modeButtons.forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.preventDefault();
      const m = btn.getAttribute('data-chart-mode');
      setMode(m);
    });
  });

  if (intervalSelect) {
    intervalSelect.addEventListener('change', () => {
      if (window.pfChartMode === 'candle' && symbol) {
        fetchOHLC(symbol, getInterval()).then(items => { window.pfChartCandles = items; redrawCandles(items); });
      }
    });
  }

  if (refreshBtn) {
    refreshBtn.addEventListener('click', (e) => {
      e.preventDefault();
      if (window.pfChartMode === 'candle' && symbol) {
        fetchOHLC(symbol, getInterval()).then(items => { window.pfChartCandles = items; redrawCandles(items); });
      } else {
        redrawLine(window.pfChartSeries || []);
      }
    });
  }

  // Initial draw
  redrawLine(window.pfChartSeries);

  // Expose redraw for SSE appends; respects current mode
  window.pfChartRedraw = () => {
    if (window.pfChartMode === 'candle') {
      redrawCandles(window.pfChartCandles || []);
    } else {
      redrawLine(window.pfChartSeries || []);
    }
  };
}

function pfInitTrade() {
  const form = document.getElementById('pf-trade-form');
  if (!form) return;
  const priceEl = document.getElementById('pf-trade-price');
  const totalEl = document.getElementById('pf-trade-total');
  const impactEl = document.getElementById('pf-trade-impact');
  const feeEl = document.getElementById('pf-trade-fee');
  const feeBpsEl = document.getElementById('pf-trade-fee-bps');
  const stageEl = document.getElementById('pf-trade-stage');
  const warnEl = document.getElementById('pf-trade-warn');
  const amountInput = form.querySelector('input[name="amount"]');
  const sideInputs = form.querySelectorAll('input[name="side"]');
  const slippageInput = document.getElementById('pf-slippage-bps');
  const minOutHidden = document.getElementById('pf-min-amount-out');
  const maxSlipHidden = document.getElementById('pf-max-slippage-bps');
  const symbol = form.getAttribute('data-symbol');

  function setWarn(msg) {
    if (!warnEl) return;
    warnEl.textContent = msg || '';
  }

  async function fetchQuote() {
    try {
      const amt = Number(amountInput.value || '0') || 0;
      const side = Array.from(sideInputs).find(i => i.checked)?.value || 'buy';
      if (!symbol || !amt || amt <= 0) {
        // reset
        if (totalEl) totalEl.textContent = '0.000000';
        if (impactEl) impactEl.textContent = '—';
        if (feeEl) feeEl.textContent = '—';
        setWarn('');
        return;
      }
      const body = { symbol, action: side, amount_in: String(amt) };
      const res = await fetch('/api/amm/quote', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'same-origin',
        body: JSON.stringify(body)
      });
      const j = await res.json();
      if (!res.ok || j.error) {
        setWarn(String(j.error || `Quote failed (HTTP ${res.status})`));
        return;
      }
      const execPrice = Number(j.execution_price || 0) || 0;
      const out = Number(j.amount_out || 0) || 0;
      const impactBps = Number(j.price_impact_bps || 0) || 0;
      const fee = Number(j.fee_amount || 0) || 0;
      if (priceEl) priceEl.textContent = execPrice.toFixed(6);
      if (totalEl) totalEl.textContent = out.toFixed(6);
      if (impactEl) impactEl.textContent = (impactBps / 100).toFixed(2) + '%';
      if (feeEl) feeEl.textContent = fee.toFixed(6);
      if (feeBpsEl) feeBpsEl.textContent = String(j.fee_bps || feeBpsEl.textContent || '0');
      if (stageEl) stageEl.textContent = String(j.stage || stageEl.textContent || '1');

      // Compute min amount out from slippage
      const slipBps = Math.max(0, Math.min(5000, Number((slippageInput && slippageInput.value) || (maxSlipHidden && maxSlipHidden.value) || '0') || 0));
      const minOut = out * (1 - slipBps / 10000);
      if (minOutHidden) minOutHidden.value = String(minOut.toFixed(12));
      if (maxSlipHidden) maxSlipHidden.value = String(slipBps);

      // Warnings
      if (impactBps > slipBps * 2) {
        setWarn(`High price impact: ${(impactBps/100).toFixed(2)}%`);
      } else if (impactBps > slipBps) {
        setWarn(`Price impact ${(impactBps/100).toFixed(2)}% exceeds slippage ${ (slipBps/100).toFixed(2)}%`);
      } else {
        setWarn('');
      }
    } catch (e) {
      setWarn('Quote failed');
    }
  }

  const refresh = pfDebounce(fetchQuote, 200);
  amountInput && amountInput.addEventListener('input', refresh);
  slippageInput && slippageInput.addEventListener('input', refresh);
  sideInputs.forEach(i => i.addEventListener('change', refresh));
  refresh();

  // Keep constraints in sync on submit
  form.addEventListener('submit', (e) => {
    try {
      const amt = Number(amountInput.value || '0') || 0;
      if (!amt || amt <= 0) {
        e.preventDefault();
        setWarn('Enter a valid amount');
        return;
      }
      const out = Number((totalEl && totalEl.textContent) || '0') || 0;
      const slipBps = Math.max(0, Math.min(5000, Number((slippageInput && slippageInput.value) || (maxSlipHidden && maxSlipHidden.value) || '0') || 0));
      const minOut = out * (1 - slipBps / 10000);
      if (minOutHidden) minOutHidden.value = String(minOut.toFixed(12));
      if (maxSlipHidden) maxSlipHidden.value = String(slipBps);
    } catch (err) {
      // allow submit; server will validate
    }
  });
}

window.addEventListener('DOMContentLoaded', () => {
  // Initialize non-SSE components first
  pfInitFlashes();
  pfInitTabs();
  pfInitChart();
  pfInitTrade();
  pfInitSparklines();
  pfInitProgressBars();
  pfInitTokenize();
  pfInitOgPreview();
  pfInitLaunchConfetti();
  pfInitWatchlist();
  pfInitShareButtons();

  // Initialize SSE components with a small delay to avoid overwhelming the server
  setTimeout(() => {
    try {
      // Only initialize SSE components that are relevant to the current page
      if (document.getElementById('pf-ticker-track')) {
        pfInitTicker();
        pfInitTradesSSE();
      }

      if (document.querySelector('[data-sse-symbol]')) {
        pfInitPricesSSE();
      }

      // User-specific SSE (only for authenticated users)
      if (document.querySelector('.pf-npub')) {
        pfInitAlertsSSE();
        pfInitFollowSSE();
      }
    } catch (e) {
      console.warn('Error initializing SSE components:', e);
    }
  }, 500);
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
        console.warn('Error parsing price data:', e);
      }
    }, 'prices');
  } catch (e) {
    console.warn('Error initializing prices SSE:', e);
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
