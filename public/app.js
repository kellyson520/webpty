const shell = document.getElementById('shell');
// Page carousel uses transform: translateX on this track, not shell.scrollLeft.
// See the .shell `overflow: clip` rationale in styles.css.
const track = document.createElement('div');
track.className = 'track';
shell.appendChild(track);const tabsEl = document.getElementById('tabs');
const openDrawerBtn = document.getElementById('open-drawer');
const openMenuBtn = document.getElementById('open-menu');
const tplSession = document.getElementById('tpl-session');
const tplChat = document.getElementById('tpl-chat');
const tplAdd = document.getElementById('tpl-add');
const menuBackdrop = document.getElementById('menu-backdrop');
const menuPop = document.getElementById('menu-pop');
const drawer = document.getElementById('drawer');
const drawerFolders = document.getElementById('drawer-folders');
const drawerBackdrop = drawer.querySelector('.drawer-backdrop');
const folderSortBtn = document.getElementById('folder-sort-btn');
const folderSortLabel = document.getElementById('folder-sort-label');
const folderSortPop = document.getElementById('folder-sort-pop');
const addFolderBtn = document.getElementById('add-folder-btn');
const newProjectBtn = document.getElementById('new-project-btn');
const newProjectName = document.getElementById('new-project-name');
const tokenGate = document.getElementById('token-gate');
const tokenGateInput = document.getElementById('token-gate-input');
const tokenGateBtn = document.getElementById('token-gate-btn');
const tokenGateErr = document.getElementById('token-gate-err');
const notifyBackdrop = document.getElementById('notify-backdrop');
const notifyRules = document.getElementById('notify-rules');
const notifyMessages = document.getElementById('notify-messages');
const costBackdrop = document.getElementById('cost-backdrop');
const backupBackdrop = document.getElementById('backup-backdrop');
const migrateBackdrop = document.getElementById('migrate-backdrop');
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const folderPickerBackdrop = document.getElementById('folder-picker-backdrop');
const folderPickerList = document.getElementById('folder-picker-list');
const folderPickerPath = document.getElementById('folder-picker-path');
const folderPickerUp = document.getElementById('folder-picker-up');
const folderPickerClose = document.getElementById('folder-picker-close');
const folderPickerCancel = document.getElementById('folder-picker-cancel');
const folderPickerSelect = document.getElementById('folder-picker-select');

const SORT_LABEL = { active: '最近活跃', name: '按名称', date: '按日期' };
let folderSort = localStorage.getItem('webpty.folderSort') || 'active';
function setFolderSort(mode) {
  folderSort = mode;
  localStorage.setItem('webpty.folderSort', mode);
  folderSortLabel.textContent = SORT_LABEL[mode] || mode;
  for (const b of folderSortPop.querySelectorAll('.sort-opt')) {
    b.classList.toggle('active', b.dataset.sort === mode);
  }
  populateFolders();
}
folderSortBtn.onclick = (ev) => {
  ev.stopPropagation();
  folderSortPop.hidden = !folderSortPop.hidden;
};
for (const b of folderSortPop.querySelectorAll('.sort-opt')) {
  b.onclick = (ev) => {
    ev.stopPropagation();
    setFolderSort(b.dataset.sort);
    folderSortPop.hidden = true;
  };
}
document.addEventListener('click', (ev) => {
  if (folderSortPop.hidden) return;
  if (!folderSortPop.contains(ev.target) && ev.target !== folderSortBtn) {
    folderSortPop.hidden = true;
  }
});
folderSortLabel.textContent = SORT_LABEL[folderSort] || folderSort;
for (const b of folderSortPop.querySelectorAll('.sort-opt')) {
  if (b.dataset.sort === folderSort) b.classList.add('active');
}

let config = null;
let projects = [];
let sessions = [];
let activeIndex = 0;
let pollTimer = null;
let pollFailures = 0;

// id -> { page, term, fit, socket, host, composeInput, composeSubmit }
const live = new Map();
let addPage = null;

// Shared tab-bar controls
openDrawerBtn.onclick = (ev) => { ev.stopPropagation(); openDrawer(); };
openMenuBtn.onclick = (ev) => {
  ev.stopPropagation();
  const s = sessions[activeIndex];
  if (s) openMenu(s.id);
};

const api = async (url, opts = {}) => {
  const headers = { 'content-type': 'application/json', ...(opts.headers || {}) };
  const token = localStorage.getItem('webpty.token');
  if (token) headers['authorization'] = `Bearer ${token}`;
  // Audit L2: backend hangs must not leave buttons pending forever.
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), 15000);
  let res;
  try {
    res = await fetch(url, { ...opts, headers, signal: ctl.signal });
  } catch (e) {
    clearTimeout(timer);
    throw new Error(e.name === 'AbortError' ? '请求超时，请重试' : e.message);
  }
  clearTimeout(timer);
  if (res.status === 403) {
    const body = await res.json().catch(() => ({}));
    if (body.reason === 'bad-token') showTokenGate();
    throw new Error(body.error || 'forbidden');
  }
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.error || res.statusText);
  }
  return res.json();
};

function showTokenGate() {
  tokenGate.hidden = false;
  tokenGateErr.textContent = '';
  // Audit L: entering the gate with a stale token only guarantees 403s —
  // clear it so the next attempt starts clean.
  localStorage.removeItem('webpty.token');
  document.cookie = 'webpty_token=; path=/; max-age=0';
  requestAnimationFrame(() => tokenGateInput.focus());
}

function clearToken() {
  localStorage.removeItem('webpty.token');
  document.cookie = 'webpty_token=; path=/; max-age=0';
  location.reload();
}

async function unlockToken() {
  const token = tokenGateInput.value.trim();
  if (!token) return;
  tokenGateBtn.disabled = true;
  tokenGateErr.textContent = '验证中…';
  try {
    localStorage.setItem('webpty.token', token);
    // Mirror to a cookie so the WebSocket handshake can auth without putting
    // the token in the URL (Issue 3.3: no token in logs/history/Referer).
    document.cookie = `webpty_token=${encodeURIComponent(token)}; path=/; samesite=strict; max-age=2592000`;
    const c = await api('/api/config');
    config = c;
    tokenGate.hidden = true;
    tokenGateBtn.disabled = false;
    await bootstrap();
  } catch (e) {
    localStorage.removeItem('webpty.token');
    document.cookie = 'webpty_token=; path=/; max-age=0';
    tokenGateBtn.disabled = false;
    tokenGateErr.textContent = e.message.includes('forbidden') ? '令牌错误，请重试' : e.message;
  }
}

tokenGateBtn.onclick = unlockToken;
document.getElementById('token-gate-clear').onclick = (ev) => {
  ev.preventDefault();
  clearToken();
};
tokenGateInput.addEventListener('keydown', (e) => {
  if (e.key === 'Enter') unlockToken();
});

function pageCount() {
  return sessions.length;
}

// The page carousel is programmatic-only. We translate the track instead of
// scrolling the shell so there's no scroll container the browser can nudge.
let navTimer = null;
function scrollToIndex(i, smooth = true) {
  i = Math.max(0, Math.min(pageCount() - 1, i));
  activeIndex = i;
  // Explicit navigation → the new active tab must be visible, even if the
  // user had manually scrolled the strip elsewhere.
  tabStripUserScrolled = false;
  renderTabs();
  if (smooth) {
    track.classList.remove('no-anim');
  } else {
    track.classList.add('no-anim');
  }
  track.style.transform = `translateX(${-i * 100}%)`;
  // Move focus to the new active tab synchronously. If we leave it for the
  // deferred onActivate, the previous tab's now-offscreen helper textarea
  // still holds focus during the slide — and the next keystroke makes the
  // browser scrollIntoView it, shifting the layout left/right. We don't
  // touch agent (chat) pages here since they have a real composer that the
  // user has to tap to focus anyway.
  const nextSession = sessions[i];
  if (nextSession && !isAgent(nextSession)) {
    const nextEntry = live.get(nextSession.id);
    if (nextEntry?.term) {
      try { nextEntry.term.focus(); } catch {}
    } else if (document.activeElement && document.activeElement !== document.body) {
      try { document.activeElement.blur(); } catch {}
    }
  }
  clearTimeout(navTimer);
  navTimer = setTimeout(() => onActivate(activeIndex), smooth ? 380 : 60);
}

const TUI_TOOLS = new Set(['claude', 'codex', 'agy', 'reasonix', 'opencode']);
const isMobileViewport = () => window.matchMedia('(max-width: 600px)').matches;

// Which render engine a tool/session uses: 'agent' (HTML chat) or 'pty' (xterm).
function toolEngine(tool) { return config?.tools?.[tool]?.engine || 'pty'; }
function engineOf(session) { return session?.engine || toolEngine(session?.tool) || 'pty'; }
function isAgent(session) { return engineOf(session) === 'agent'; }

// Merge a server-pushed session state into the local list and repaint tabs.
// Lets the busy↔idle / exit indicator update instantly without a full refetch.
function applySessionState(updated) {
  if (!updated) return;
  const i = sessions.findIndex((s) => s.id === updated.id);
  if (i < 0) { schedulePoll(0); return; }
  sessions[i] = updated;
  renderTabs();
  // Audit C1: reflect turnActive on the chat page's stop button.
  const e = live.get(updated.id);
  if (e && typeof e.updateStopBtn === 'function') e.updateStopBtn();
}

// Chunked replay (audit V5/8.1): 4000 items inserted synchronously is a
// multi-hundred-ms main-thread block on reconnect; 100/frame lets the
// browser breathe. Shared by single-frame and chunked snapshots.
function replaySnapshot(entry, items) {
  let i = 0;
  const step = () => {
    const end = Math.min(i + 100, items.length);
    for (; i < end; i++) renderChatItem(entry, items[i]);
    if (i < items.length) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
  const last = items[items.length - 1];
  const done = !last || last.t === 'result' || last.t === 'exit' || last.t === 'error';
  setChatPending(entry, !done);
}

// On-demand addon loading (audit 5.1): the canvas/unicode11/web-links
// addons are no longer blocking <script> tags; they load lazily and the
// loader retries the install once the module arrives.
const _addonPromises = {};
function ensureAddons(term) {
  const load = (globalKey, url, install) => {
    if (window[globalKey]) { install(); return; }
    if (!_addonPromises[url]) {
      _addonPromises[url] = new Promise((resolve) => {
        const s = document.createElement('script');
        s.src = url;
        s.defer = true;
        s.onload = () => resolve();
        s.onerror = () => resolve(); // degraded mode: feature stays off
        document.head.appendChild(s);
      });
    }
    _addonPromises[url].then(() => {
      if (window[globalKey]) install();
    });
  };
  load('Unicode11Addon', '/vendor/xterm-unicode11/lib/addon-unicode11.js', () => {
    try { term.loadAddon(new Unicode11Addon.Unicode11Addon()); term.unicode.activeVersion = '11'; } catch {}
  });
  load('WebLinksAddon', '/vendor/xterm-web-links/lib/addon-web-links.js', () => {
    try { term.loadAddon(new WebLinksAddon.WebLinksAddon((ev, uri) => window.open(uri, '_blank', 'noopener'))); } catch {}
  });
  load('CanvasAddon', '/vendor/xterm-canvas/lib/addon-canvas.js', () => {
    try {
      term.loadAddon(new CanvasAddon.CanvasAddon());
    } catch (e) {
      // Audit C1: canvas renderer failure (script load error or xterm
      // private-API drift) falls back to DOM silently — surface it so TUI
      // performance issues are diagnosable later.
      console.warn('[webpty] canvas renderer unavailable, using DOM:', e);
    }
  });
}

function makeTerminal(session, host) {
  const isTUI = TUI_TOOLS.has(session.tool);
  const term = new Terminal({
    cursorBlink: !isTUI,
    cursorStyle: 'bar',
    convertEol: true,
    scrollback: isMobileViewport() ? 10000 : 30000,
    allowProposedApi: true,
    fontFamily: '"D2Coding", "Cascadia Mono", Menlo, Consolas, monospace',
    fontSize: isMobileViewport() ? 16 : 15,
    theme: {
      background: '#0f0f0f',
      foreground: '#ededed',
      cursor: isTUI ? 'rgba(0,0,0,0)' : '#3fbf7f',
      cursorAccent: isTUI ? 'rgba(0,0,0,0)' : '#0f0f0f'
    }
  });
  const fit = new FitAddon.FitAddon();
  term.loadAddon(fit);
  // Any selection change invalidates the "selected via triple-click" flag.
  // expandSelectionToLogicalLine re-sets the flag *after* calling selectLines,
  // so its own synchronous event clears here first, then the flag is restored.
  try { term.onSelectionChange?.(() => tripleClickTerms.delete(term)); } catch {}
  ensureAddons(term);
  // Ctrl+C / Cmd+C copies the selection (when there is one) instead of sending
  // SIGINT. With no selection, let it fall through to the PTY as usual.
  // F5 / Ctrl+R: hand back to the browser (page refresh) instead of swallowing.
  term.attachCustomKeyEventHandler((e) => {
    if (e.type === 'keydown' && (e.ctrlKey || e.metaKey) && (e.key === 'c' || e.key === 'C')) {
      const sel = getJoinedSelection(term);
      if (sel && sel.length > 0) {
        copyText(sel);
        return false;
      }
    }
    if (e.type === 'keydown' && (e.key === 'F5' || (e.ctrlKey && (e.key === 'r' || e.key === 'R')))) {
      return false;
    }
    return true;
  });
  term.open(host);
  // Canvas renderer draws box-drawing/block glyphs itself (customGlyphs), so
  // table borders connect with no inter-row gaps. Load after open().
  fit.fit();
  return { term, fit };
}

// --- Hangul composer (unified path on all platforms) ---
// Composed Hangul syllables (from desktop OS IME) pass through unchanged;
// compatibility jamos (from mobile keyboards that don't compose) are merged
// locally and re-emitted as full syllables.
// Some mobile IMEs (notably iOS Korean) send compatibility-jamo characters
// (U+3131-U+318F) one at a time without firing composition events, so xterm's
// term.onData yields "ㅎ", "ㅏ", "ㄴ" instead of "한". This composer combines
// them on the wire by sending DEL (\x7f) to erase the partial syllable and
// resending the merged one. Composed Hangul / ASCII / other chars pass through
// unchanged.
const CHO = ['ㄱ','ㄲ','ㄴ','ㄷ','ㄸ','ㄹ','ㅁ','ㅂ','ㅃ','ㅅ','ㅆ','ㅇ','ㅈ','ㅉ','ㅊ','ㅋ','ㅌ','ㅍ','ㅎ'];
const JUNG = ['ㅏ','ㅐ','ㅑ','ㅒ','ㅓ','ㅔ','ㅕ','ㅖ','ㅗ','ㅘ','ㅙ','ㅚ','ㅛ','ㅜ','ㅝ','ㅞ','ㅟ','ㅠ','ㅡ','ㅢ','ㅣ'];
const JONG = ['','ㄱ','ㄲ','ㄳ','ㄴ','ㄵ','ㄶ','ㄷ','ㄹ','ㄺ','ㄻ','ㄼ','ㄽ','ㄾ','ㄿ','ㅀ','ㅁ','ㅂ','ㅄ','ㅅ','ㅆ','ㅇ','ㅈ','ㅊ','ㅋ','ㅌ','ㅍ','ㅎ'];
const JUNG_COMBINE = { 'ㅗㅏ':'ㅘ', 'ㅗㅐ':'ㅙ', 'ㅗㅣ':'ㅚ', 'ㅜㅓ':'ㅝ', 'ㅜㅔ':'ㅞ', 'ㅜㅣ':'ㅟ', 'ㅡㅣ':'ㅢ' };
const JONG_COMBINE = { 'ㄱㅅ':'ㄳ', 'ㄴㅈ':'ㄵ', 'ㄴㅎ':'ㄶ', 'ㄹㄱ':'ㄺ', 'ㄹㅁ':'ㄻ', 'ㄹㅂ':'ㄼ', 'ㄹㅅ':'ㄽ', 'ㄹㅌ':'ㄾ', 'ㄹㅍ':'ㄿ', 'ㄹㅎ':'ㅀ', 'ㅂㅅ':'ㅄ' };
const JONG_SPLIT = Object.fromEntries(Object.entries(JONG_COMBINE).map(([k, v]) => [v, k]));
function isCompatJamo(c) { return c >= 'ㄱ' && c <= 'ㆎ'; }
// Audit 1.1: some Android IMEs (GBoard variants) send MODERN Hangul Jamo
// (U+1100-11FF) instead of compatibility jamo. Map them to the compat
// forms the composer already understands.
const MODERN_TO_COMPAT = {};
(function () {
  const modernCho = '\u1100\u1101\u1102\u1103\u1104\u1105\u1106\u1107\u1108\u1109\u110A\u110B\u110C\u110D\u110E\u110F\u1110\u1111\u1112';
  const modernJung = '\u1161\u1162\u1163\u1164\u1165\u1166\u1167\u1168\u1169\u116A\u116B\u116C\u116D\u116E\u116F\u1170\u1171\u1172\u1173\u1174\u1175';
  const modernJong = '\u11A8\u11A9\u11AA\u11AB\u11AC\u11AD\u11AE\u11AF\u11B0\u11B1\u11B2\u11B3\u11B4\u11B5\u11B6\u11B7\u11B8\u11B9\u11BA\u11BB\u11BC\u11BD\u11BE\u11BF\u11C0\u11C1\u11C2';
  for (let i = 0; i < modernCho.length; i++) MODERN_TO_COMPAT[modernCho[i]] = CHO[i];
  for (let i = 0; i < modernJung.length; i++) MODERN_TO_COMPAT[modernJung[i]] = JUNG[i];
  for (let i = 0; i < modernJong.length; i++) MODERN_TO_COMPAT[modernJong[i]] = JONG[i + 1];
})();
function normalizeJamo(c) { return MODERN_TO_COMPAT[c] || c; }

function makeHangulComposer(send) {
  let cho = -1, jung = -1, jong = 0; // jong=0 means none
  let lastLen = 0; // chars of current syllable already sent

  function build() {
    if (cho >= 0 && jung >= 0) {
      return String.fromCharCode(0xAC00 + (cho * 21 + jung) * 28 + jong);
    }
    if (cho >= 0) return CHO[cho];
    if (jung >= 0) return JUNG[jung];
    return '';
  }
  function flushDisplay() {
    const s = build();
    if (lastLen > 0) send('\x7f'.repeat(lastLen));
    if (s) send(s);
    lastLen = s ? 1 : 0;
  }
  function commit() { cho = -1; jung = -1; jong = 0; lastLen = 0; }

  return {
    feed(text) {
      for (const ch0 of text) {
        // Audit 1.1: normalize modern jamo (U+1100-11FF) to compat forms.
        const ch = normalizeJamo(ch0);
        if (!isCompatJamo(ch)) {
          // Non-jamo (composed Hangul, ASCII, control). Finalize and pass.
          commit();
          send(ch);
          continue;
        }
        const choIdx = CHO.indexOf(ch);
        const jungIdx = JUNG.indexOf(ch);
        const jongIdx = JONG.indexOf(ch);
        const isVowel = jungIdx >= 0;

        if (isVowel) {
          if (cho < 0) {
            // No leading consonant: emit jung standalone, no syllable to merge
            commit();
            send(ch);
          } else if (jung < 0) {
            jung = jungIdx;
            flushDisplay();
          } else if (jong === 0) {
            // cho+jung, new jung: try vowel combine, else split → new syllable
            const combined = JUNG_COMBINE[JUNG[jung] + ch];
            if (combined) {
              jung = JUNG.indexOf(combined);
              flushDisplay();
            } else {
              commit();
              send(ch); // standalone vowel (no preceding cho)
            }
          } else {
            // cho+jung+jong + new vowel → jong becomes cho of next syllable
            const split = JONG_SPLIT[JONG[jong]];
            let movedChoChar;
            if (split) {
              // Compound jong: keep the first jamo, move the second
              jong = JONG.indexOf(split[0]);
              movedChoChar = split[1];
            } else {
              movedChoChar = JONG[jong];
              jong = 0;
            }
            flushDisplay();
            const newChoIdx = CHO.indexOf(movedChoChar);
            commit();
            if (newChoIdx >= 0) {
              cho = newChoIdx;
              jung = jungIdx;
              flushDisplay();
            } else {
              send(ch);
            }
          }
        } else if (choIdx >= 0) {
          // Consonant (which may also be a valid jong)
          if (cho < 0) {
            cho = choIdx;
            flushDisplay();
          } else if (jung < 0) {
            // Two consonants in a row → finalize previous, start fresh
            commit();
            cho = choIdx;
            flushDisplay();
          } else if (jong === 0 && jongIdx > 0) {
            jong = jongIdx;
            flushDisplay();
          } else if (jong > 0) {
            // Try to combine into a compound jong
            const combined = JONG_COMBINE[JONG[jong] + ch];
            if (combined) {
              jong = JONG.indexOf(combined);
              flushDisplay();
            } else {
              commit();
              cho = choIdx;
              flushDisplay();
            }
          } else {
            commit();
            cho = choIdx;
            flushDisplay();
          }
        } else {
          // jong-only jamo (rare)
          commit();
          send(ch);
        }
      }
    },
    flush() { commit(); }
  };
}

function connectSocket(entry, session, attempt = 0) {
  if (live.get(session.id) !== entry) return; // entry was disposed during retry
  try { entry.socket?.close(); } catch {}
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  // No ?token= in the URL — the webpty_token cookie travels with the
  // handshake (Issue 3.3 keeps the token out of logs/history/Referer).
  const ws = new WebSocket(`${proto}//${location.host}/ws/sessions/${encodeURIComponent(session.id)}`);
  ws.binaryType = 'arraybuffer';
  ws.onopen = () => {
    if (attempt > 0) showHint('', 0); // reconnected — clear the offline hint
    attempt = 0;
    ws.send(JSON.stringify({ type: 'resize', __ctl: true, cols: entry.term.cols, rows: entry.term.rows }));
  };
  ws.onmessage = (event) => {
    if (typeof event.data === 'string' && event.data.startsWith('{')) {
      try {
        const msg = JSON.parse(event.data);
        if (msg.type === 'state') { applySessionState(msg.session); return; }
        if (msg.type === 'resync') {
          // Server dropped output frames (backgrounded tab etc.) and sent a
          // full buffer snapshot. Reset the terminal and replay it so
          // incremental TUI state (reasonix repaints, cursor) is rebuilt
          // instead of showing a garbled, misaligned screen.
          try {
            entry.term.reset();
            if (msg.data) {
              const bin = Uint8Array.from(atob(msg.data), (c) => c.charCodeAt(0));
              // Chunked write (audit F1): a 128KB snapshot parsed in one
              // term.write() is a ~50-100ms main-thread task; 8KB chunks
              // let the browser yield between parses.
              for (let off = 0; off < bin.length; off += 8192) {
                entry.term.write(bin.subarray(off, off + 8192));
              }
            }
          } catch {}
          return;
        }
        // Audit S1b: any OTHER recognized JSON (future protocol messages)
        // is silently ignored — never falls through to term.write() which
        // would print control JSON into the terminal.
        return;
      } catch {}
    }
    // Large frames are chunked into ~8KB writes. term.write() parses
    // synchronously; a 32KB+ frame (e.g. a TUI full repaint) would block
    // the main thread for a long task on low-end phones. Chunking lets the
    // browser yield between writes so rendering stays smooth.
    const data = event.data instanceof ArrayBuffer
      ? new Uint8Array(event.data)
      : event.data;
    if (typeof data === 'string') {
      if (data.length <= 8192) { entry.term.write(data); return; }
      for (let i = 0; i < data.length; i += 8192) {
        entry.term.write(data.slice(i, i + 8192));
      }
    } else if (data instanceof Uint8Array) {
      if (data.length <= 8192) { entry.term.write(data); return; }
      for (let i = 0; i < data.length; i += 8192) {
        entry.term.write(data.subarray(i, i + 8192));
      }
    } else {
      entry.term.write(data);
    }
  };
  ws.onerror = () => {
    // Audit L2: distinguish network-level failure from a clean close.
    // onclose always follows, so the reconnect logic stays there.
    entry.lastWsError = 'network';
  };
  ws.onclose = () => {
    if (entry.socket === ws) entry.socket = null;
    if (live.get(session.id) !== entry) return;
    // Audit T5: repeated handshake failures (attempt>=3) with a token set
    // usually mean the token was revoked/rotated — stop hammering and
    // re-prompt for the token instead of a forever-reconnect loop.
    if (attempt >= 3 && (localStorage.getItem('webpty.token') || document.cookie.includes('webpty_token'))) {
      showHint('连接被拒绝——令牌可能已失效，请重新解锁', 6000);
      setTimeout(() => showTokenGate(), 1500);
      return;
    }
    // Connection-loss visibility (audit V1): without this, input typed
    // while disconnected vanished silently. Hint shows until the next
    // successful open.
    showHint('连接断开，正在重连…', 10000);
    const delay = Math.min(5000, 250 * 2 ** attempt);
    setTimeout(() => connectSocket(entry, session, attempt + 1), delay);
  };
  entry.socket = ws;

  // Composer + onData are wired once per entry. The composer reads
  // entry.socket dynamically so it keeps working across reconnects.
  if (!entry.composer) {
    entry.composer = makeHangulComposer((s) => {
      if (entry.socket?.readyState === WebSocket.OPEN) entry.socket.send(s);
    });
    // Note: no imeComposing() gate here. xterm's CompositionHelper already
    // buffers until compositionend, so partial-jamo emissions during composition
    // aren't expected. Gating on imeDepth caused a race on iOS where the next
    // syllable's compositionstart raised imeDepth before the previous syllable's
    // onData fired — dropping all but the last syllable of each word.
    // Audit 1.2: TUI sessions (vim, reasonix...) bypass the composer — its
    // DEL-and-resend merge would fight the TUI's own backspace handling and
    // corrupt input; pass through raw.
    const session = sessions.find((s) => s.id === entry.sessionId) || sessions.find((s) => s.id === entry.id);
    if (session && TUI_TOOLS.has(session.tool)) {
      entry.term.onData((data) => {
        if (entry.socket?.readyState === WebSocket.OPEN) entry.socket.send(data);
      });
    } else {
      entry.term.onData((data) => entry.composer.feed(data));
    }
  }
}

// Track active composition so partial jamos emitted by xterm during composition
// can be ignored — the composer handles final text via term.onData on compositionend.
let imeDepth = 0;
function imeComposing() { return imeDepth > 0; }
function isXtermTextarea(target) {
  return target && target.classList && target.classList.contains('xterm-helper-textarea');
}
document.addEventListener('compositionstart', (ev) => {
  if (!isXtermTextarea(ev.target)) return;
  imeDepth++;
}, true);
document.addEventListener('compositionend', (ev) => {
  if (!isXtermTextarea(ev.target)) return;
  imeDepth = Math.max(0, imeDepth - 1);
}, true);

// Diagnostic overlay — enable with `?debug=ime`. Shows recent composition /
// input events so we can see what an iOS keyboard actually fires.
if (new URLSearchParams(location.search).get('debug') === 'ime') {
  const dbg = document.createElement('div');
  dbg.style.cssText = 'position:fixed;left:6px;right:6px;bottom:6px;max-height:40vh;overflow:auto;padding:6px 8px;font:11px/1.3 monospace;background:rgba(0,0,0,0.85);color:#0f0;z-index:200;border-radius:8px;pointer-events:auto;';
  document.body.appendChild(dbg);
  const log = (s) => {
    const line = document.createElement('div');
    line.textContent = s;
    dbg.appendChild(line);
    while (dbg.children.length > 30) dbg.removeChild(dbg.firstChild);
    dbg.scrollTop = dbg.scrollHeight;
  };
  for (const t of ['compositionstart', 'compositionupdate', 'compositionend', 'beforeinput', 'input']) {
    document.addEventListener(t, (ev) => {
      log(`${t} data=${JSON.stringify(ev.data ?? null)} iType=${ev.inputType ?? '-'} isC=${ev.isComposing ?? '-'}`);
    }, true);
  }
  const origSend = WebSocket.prototype.send;
  WebSocket.prototype.send = function (d) {
    if (typeof d === 'string' && !d.startsWith('{')) log(`ws.send ${JSON.stringify(d)}`);
    return origSend.call(this, d);
  };
  log('IME debug overlay active');
}

function buildSessionPage(session) {
  if (isAgent(session)) return buildChatPage(session);
  const page = tplSession.content.firstElementChild.cloneNode(true);
  page.dataset.id = session.id;
  page.dataset.tool = session.tool;
  const host = page.querySelector('.term-host');
  const composeInput = page.querySelector('.compose-input');
  const composeSubmit = page.querySelector('.compose-submit');
  const scrollBottomBtn = page.querySelector('.scroll-bottom-btn');

  const entry = { page, host, composeInput, composeSubmit, scrollBottomBtn, term: null, fit: null, socket: null, id: session.id };
  live.set(session.id, entry);

  // Jump-to-bottom button: tap to snap xterm scrollback to the latest line.
  // Don't steal focus from the terminal (mobile keyboard would dismiss).
  scrollBottomBtn.addEventListener('pointerdown', (ev) => ev.preventDefault());
  scrollBottomBtn.addEventListener('mousedown', (ev) => ev.preventDefault());
  scrollBottomBtn.addEventListener('click', (ev) => {
    ev.preventDefault();
    try { entry.term?.scrollToBottom(); } catch {}
  });

  // Right-edge scroll strip: drag vertically to page through xterm scrollback.
  // Position of the thumb = fraction of scrollback; drag maps to scrollLines.
  const strip = page.querySelector('.scroll-strip');
  const stripThumb = strip ? strip.querySelector('.strip-thumb') : null;
  let stripDrag = null;
  const updateStripThumb = () => {
    if (!stripThumb || !entry.term) return;
    const b = entry.term.buffer.active;
    const maxY = Math.max(b.baseY, 0);
    const frac = maxY ? Math.min(b.viewportY / maxY, 1) : 1;
    stripThumb.style.top = `${frac * 100}%`;
    stripThumb.style.height = `${Math.max(6, 100 / (maxY + 1) * 30)}%`;
  };
  try { entry.term?.onScroll(updateStripThumb); } catch {}
  if (strip && entry.term) {
    const dragScroll = (clientY) => {
      const rect = strip.getBoundingClientRect();
      const frac = Math.min(Math.max((clientY - rect.top) / Math.max(rect.height, 1), 0), 1);
      const b = entry.term.buffer.active;
      const maxY = Math.max(b.baseY, 0);
      const target = Math.round(frac * maxY);
      const delta = target - b.viewportY;
      if (delta) { try { entry.term.scrollLines(delta); } catch {} }
      // TUI sessions (alternate screen, mouse tracking): scrollLines has
      // nothing to scroll — forward a synthetic wheel so the app scrolls.
      if (entry.term.element.classList.contains('enable-mouse-events')) {
        try {
          entry.term.element.dispatchEvent(new WheelEvent('wheel', {
            deltaY: delta > 0 ? 100 : -100,
            deltaMode: WheelEvent.DOM_DELTA_PIXEL,
            bubbles: true,
            cancelable: true,
          }));
        } catch {}
      }
      updateStripThumb();
    };
    // rAF-throttle pointer/touch drag: pointermove can fire faster than the
    // display; coalesce to one dragScroll per frame (last position wins).
    let dragRaf = 0;
    let dragY = 0;
    const queueDrag = (clientY) => {
      dragY = clientY;
      if (dragRaf) return;
      dragRaf = requestAnimationFrame(() => {
        dragRaf = 0;
        dragScroll(dragY);
      });
    };
    strip.addEventListener('pointerdown', (ev) => {
      ev.preventDefault();
      strip.setPointerCapture?.(ev.pointerId);
      stripDrag = true;
      dragScroll(ev.clientY);
    });
    strip.addEventListener('pointermove', (ev) => {
      if (!stripDrag) return;
      ev.preventDefault();
      queueDrag(ev.clientY);
    });
    strip.addEventListener('pointerup', () => { stripDrag = false; });
    strip.addEventListener('pointercancel', () => { stripDrag = false; });
    // Touch fallback for browsers without pointer capture support.
    strip.addEventListener('touchstart', (ev) => { stripDrag = true; dragScroll(ev.touches[0].clientY); }, { passive: true });
    strip.addEventListener('touchmove', (ev) => { if (stripDrag) queueDrag(ev.touches[0].clientY); }, { passive: true });
    strip.addEventListener('touchend', () => { stripDrag = false; });
    updateStripThumb();
  }

  // Auto-grow textarea
  const resizeCompose = () => {
    composeInput.style.height = 'auto';
    composeInput.style.height = Math.min(composeInput.scrollHeight, 140) + 'px';
  };
  composeInput.addEventListener('input', resizeCompose);

  const submit = () => {
    const text = composeInput.value;
    if (!text) return;
    const sendBytes = (b) => {
      if (entry.socket?.readyState === WebSocket.OPEN) entry.socket.send(b);
      else api(`/api/sessions/${session.id}/input`, { method: 'POST', body: JSON.stringify({ bytes: b }) }).catch(() => {});
    };
    if (text.includes('\n')) {
      // Multi-line: deliver as a bracketed paste so the TUI keeps embedded
      // newlines as message content, then submit with a separate Enter.
      sendBytes(`\x1b[200~${text}\x1b[201~`);
      setTimeout(() => sendBytes('\r'), 40);
    } else {
      sendBytes(`${text}\r`);
    }
    composeInput.value = '';
    composeInput.style.height = '';
  };
  composeSubmit.onclick = (ev) => { ev.preventDefault(); submit(); };
  wireComposerSend(composeInput, submit);

  // Key row — quick taps for common control sequences. Suppress focus on
  // pointer/mousedown so tapping a key doesn't steal focus from the terminal
  // (or composer) and dismiss the soft keyboard on mobile.
  for (const btn of page.querySelectorAll('.key-btn')) {
    btn.addEventListener('pointerdown', (ev) => ev.preventDefault());
    btn.addEventListener('mousedown', (ev) => ev.preventDefault());
    btn.addEventListener('click', (ev) => {
      ev.preventDefault();
      sendKey(entry, btn.dataset.key);
    });
  }

  // Mouse wheel anywhere on the terminal scrolls xterm scrollback
  host.addEventListener('wheel', (ev) => {
    if (!entry.term) return;
    ev.preventDefault();
    const lines = Math.round(ev.deltaY / 24);
    if (lines !== 0) {
      try { entry.term.scrollLines(lines); } catch {}
    }
  }, { passive: false });

  // Touch scroll in TUI sessions (reasonix etc.): they run in the alternate
  // screen buffer (no scrollback) and enable mouse tracking, so xterm's own
  // touch handler has nothing to scroll. On desktop the mouse wheel reaches
  // the app because xterm forwards wheel → mouse events when tracking is
  // active. Touch produces no wheel event, so here we synthesize one and
  // dispatch it to the terminal element — xterm forwards it to the TUI,
  // which scrolls its own UI. Non-TUI sessions keep xterm's native touch
  // scrolling (the xterm.js patch made it unconditional).
  //
  // Throttling: touchmove fires at 60-120Hz; forwarding every event makes
  // the TUI repaint its whole screen per frame (stutter). Accumulate the
  // finger travel and emit one wheel per ~36px (≈ one desktop wheel notch),
  // at most every 40ms, with the direction locked for the gesture so a
  // slightly wobbly finger doesn't flip-flop the scroll.
  const TUI_WHEEL_STEP_PX = 36;
  const TUI_WHEEL_MIN_MS = 40;
  let touchScroll = null;
  let inertiaTimer = null;
  host.addEventListener('touchstart', (ev) => {
    const t = ev.touches[0];
    if (!t) return;
    if (inertiaTimer) { clearTimeout(inertiaTimer); inertiaTimer = null; }
    touchScroll = { y: t.clientY, last: t.clientY, prev: t.clientY, active: false, acc: 0, dir: 0, lastSent: 0 };
  }, { passive: true });
  host.addEventListener('touchmove', (ev) => {
    if (!touchScroll || !entry.term) return;
    const t = ev.touches[0];
    if (!t) return;
    const dy = t.clientY - touchScroll.last;
    touchScroll.prev = touchScroll.last;
    touchScroll.last = t.clientY;
    if (!touchScroll.active) {
      if (Math.abs(t.clientY - touchScroll.y) < 8) return; // still a tap
      touchScroll.active = true;
    }
    if (dy === 0) return;
    if (!entry.term.element.classList.contains('enable-mouse-events')) return;
    // Accumulate travel; lock direction on first significant movement.
    if (touchScroll.dir === 0 && Math.abs(dy) >= 4) touchScroll.dir = dy > 0 ? 1 : -1;
    if (touchScroll.dir === 0) return;
    if (dy * touchScroll.dir < 0) return; // opposite movement ignored this frame
    touchScroll.acc += dy;
    const now = performance.now();
    if (Math.abs(touchScroll.acc) >= TUI_WHEEL_STEP_PX
        && now - touchScroll.lastSent >= TUI_WHEEL_MIN_MS) {
      // Synthesize a wheel event; xterm (mouse tracking active) forwards it
      // to the app, e.g. reasonix scrolls its UI. One notch per step.
      try {
        entry.term.element.dispatchEvent(new WheelEvent('wheel', {
          deltaY: touchScroll.acc > 0 ? 100 : -100,
          deltaMode: WheelEvent.DOM_DELTA_PIXEL,
          bubbles: true,
          cancelable: true,
        }));
      } catch {}
      touchScroll.acc = 0;
      touchScroll.lastSent = now;
    }
  }, { passive: true });
  host.addEventListener('touchend', () => {
    // Fling inertia: if the finger was moving fast when it lifted, keep
    // sending a few more scroll notches with decay so the TUI scroll feels
    // continuous instead of stopping dead (perceived stutter).
    if (touchScroll && touchScroll.active && touchScroll.dir !== 0
        && entry.term && entry.term.element.classList.contains('enable-mouse-events')) {
      const speed = Math.abs(touchScroll.last - (touchScroll.prev || touchScroll.last));
      if (speed >= 14) { // px per last event — a real flick
        let n = Math.min(6, Math.round(speed / 10));
        const dir = touchScroll.dir;
        const step = () => {
          if (n-- <= 0) { inertiaTimer = null; return; }
          try {
            entry.term.element.dispatchEvent(new WheelEvent('wheel', {
              deltaY: dir > 0 ? 100 : -100,
              deltaMode: WheelEvent.DOM_DELTA_PIXEL,
              bubbles: true,
              cancelable: true,
            }));
          } catch {}
          inertiaTimer = setTimeout(step, 40);
        };
        step();
      }
    }
    touchScroll = null;
  }, { passive: true });
  host.addEventListener('touchcancel', () => { touchScroll = null; }, { passive: true });

  // Right-click: copy selection if there is one, otherwise paste the clipboard.
  // Suppress the browser context menu in both cases.
  host.addEventListener('contextmenu', (ev) => {
    ev.preventDefault();
    const sel = getJoinedSelection(entry.term);
    if (sel) {
      copyText(sel);
      try { entry.term.clearSelection(); } catch {}
    } else {
      pasteToSession(entry);
    }
  });

  // Triple-click: extend xterm's default single-row line selection to the full
  // logical line — i.e. across visually-wrapped continuation rows.
  host.addEventListener('click', (ev) => {
    // Clicking anywhere in the terminal area must give the terminal focus so
    // subsequent keystrokes reach the PTY (xterm's own textarea focus can be
    // missed when the click lands on the .term-host padding or a wrapped
    // region — the symptom was "prompt visible, but typing does nothing").
    if (entry.term) {
      try { entry.term.focus(); } catch {}
    }
    if (ev.detail !== 3 || !entry.term) return;
    // Defer so xterm's own triple-click handler runs first; we then expand.
    setTimeout(() => expandSelectionToLogicalLine(entry.term), 0);
  });

  return entry;
}

// Terms whose current selection was made via triple-click → full-logical-line
// expansion. We trim leading/trailing whitespace on copy in that case, since
// the user picked a row not a precise range.
const tripleClickTerms = new WeakSet();

// Build the selection text with visually-wrapped rows joined (no \n between
// them). The terminal wraps lines at the screen width when a logical line is
// longer than cols; those wraps aren't real newlines and shouldn't pollute the
// clipboard.
function getJoinedSelection(term) {
  if (!term) return '';
  const range = term.getSelectionPosition?.();
  if (!range) return '';
  const buffer = term.buffer.active;
  let out = '';
  for (let y = range.start.y; y <= range.end.y; y++) {
    const line = buffer.getLine(y);
    if (!line) continue;
    const startCol = (y === range.start.y) ? range.start.x : 0;
    const endCol = (y === range.end.y) ? range.end.x : undefined;
    out += line.translateToString(false, startCol, endCol);
    if (y < range.end.y) {
      const next = buffer.getLine(y + 1);
      if (!next?.isWrapped) out += '\n';
    }
  }
  if (tripleClickTerms.has(term)) {
    out = out.split('\n').map((s) => s.trim()).join('\n');
  }
  return out;
}

function expandSelectionToLogicalLine(term) {
  const range = term.getSelectionPosition?.();
  if (!range) return;
  const buffer = term.buffer.active;
  let startY = range.start.y;
  while (startY > 0 && buffer.getLine(startY)?.isWrapped) startY--;
  let endY = range.end.y;
  while (endY + 1 < buffer.length && buffer.getLine(endY + 1)?.isWrapped) endY++;
  try { term.selectLines(startY, endY); } catch {}
  tripleClickTerms.add(term);
}

// Wire Enter-to-send on a composer textarea, robust to mobile keyboards and
// IMEs. Desktop: plain Enter submits, Shift/Alt+Enter inserts a newline. With
// an IME active (Korean, Japanese, etc.), Chrome reports the commit-Enter as
// `key='Process'`/keyCode=229 — we detect it via `ev.code === 'Enter'` and
// defer submit until compositionend so the IME finalizes first. Mobile "send"
// keys often fire only `beforeinput` (insertLineBreak) with no keydown, so
// that path also submits.
function wireComposerSend(composeInput, submit) {
  let composing = false;
  let pendingSubmit = false;
  let lastEnterAt = 0;

  composeInput.addEventListener('compositionstart', () => { composing = true; });
  composeInput.addEventListener('compositionend', () => {
    composing = false;
    if (pendingSubmit) {
      pendingSubmit = false;
      submit();
    }
  });

  // Submit on blur — e.g., the iOS keyboard's send / hide-keyboard button.
  // Tab switches set _suppressBlurSubmit beforehand so they don't fire this.
  // The 40ms defer lets iOS commit any in-flight IME composition (compositionend
  // can fire just AFTER blur on Safari) before we read the final value, and
  // also lets a Send-button click on the compose-submit fall through to clear
  // the value first (its click runs before the timer).
  composeInput.addEventListener('blur', () => {
    if (composeInput._suppressBlurSubmit) {
      composeInput._suppressBlurSubmit = false;
      return;
    }
    setTimeout(() => {
      if (composeInput.value.trim()) submit();
    }, 40);
  });

  composeInput.addEventListener('keydown', (ev) => {
    // `ev.code` is the physical key — stays 'Enter' even when an IME rewrites
    // `ev.key` to 'Process'. Check both so we don't miss the IME-commit case.
    const isEnter = ev.code === 'Enter' || ev.key === 'Enter' || ev.keyCode === 13;
    if (!isEnter) return;
    if (ev.shiftKey || ev.altKey) return; // newline
    lastEnterAt = Date.now();
    if (composing || ev.isComposing || ev.keyCode === 229) {
      // IME is composing. Don't preventDefault here — the browser needs the
      // Enter to commit the IME — but flag a submit to run on compositionend.
      pendingSubmit = true;
      return;
    }
    ev.preventDefault();
    submit();
  });

  composeInput.addEventListener('beforeinput', (ev) => {
    if (ev.inputType !== 'insertLineBreak') return;
    if (composing) return;
    // Within 500ms of an Enter keydown, the newline is the browser's fallback
    // for that Enter — suppress it (keydown or compositionend already submitted
    // or will submit). Past that, this is a mobile soft-keyboard send button.
    if (Date.now() - lastEnterAt < 500) {
      ev.preventDefault();
      return;
    }
    ev.preventDefault();
    submit();
  });
}

// ===== HTML chat view (agent engine: claude stream-json) ====================

function buildChatPage(session) {
  const page = tplChat.content.firstElementChild.cloneNode(true);
  page.dataset.id = session.id;
  page.dataset.tool = session.tool;
  const scrollEl = page.querySelector('.chat-scroll');
  const logEl = page.querySelector('.chat-log');
  const composeInput = page.querySelector('.compose-input');
  const composeSubmit = page.querySelector('.compose-submit');

  const entry = { page, kind: 'chat', scrollEl, logEl, composeInput, composeSubmit, socket: null, term: null, fit: null, render: null, pendingEl: null };
  live.set(session.id, entry);
  resetChat(entry);

  // Audit C1: 'stop' button interrupts the current turn (SIGINT) — visible
  // only while a turn is active (server sets turnActive in state pushes).
  const stopBtn = page.querySelector('.compose-stop');
  stopBtn.onclick = async () => {
    try {
      await api(`/api/sessions/${session.id}/interrupt`, { method: 'POST' });
      showHint('已发送停止信号…');
    } catch (e) {
      showHint('停止失败: ' + e.message);
    }
  };
  const updateStopBtn = () => {
    const s = sessions.find((x) => x.id === session.id);
    const active = !!(s && s.turnActive) || !entry.pendingEl?.hidden;
    stopBtn.hidden = !active;
  };
  entry.updateStopBtn = updateStopBtn;

  // Audit A1: 'new chat' clears the resume id so the next start is a
  // fresh conversation.
  const newChatBtn = page.querySelector('.compose-newchat');
  newChatBtn.onclick = async () => {
    if (!confirm('开始新对话？将不再续接当前上下文。')) return;
    try {
      await api(`/api/sessions/${session.id}/reset`, { method: 'POST' });
      resetChat(entry);
      showHint('已开始新对话（下次启动为新会话）');
    } catch (e) {
      showHint('操作失败: ' + e.message);
    }
  };

  const resizeCompose = () => {
    composeInput.style.height = 'auto';
    composeInput.style.height = Math.min(composeInput.scrollHeight, 140) + 'px';
  };
  composeInput.addEventListener('input', resizeCompose);

  const submit = () => {
    const text = composeInput.value.trim();
    if (!text) return;
    // Audit C1: stopped sessions can't accept input — restart + hint
    // (aligned with the pty path) instead of silently dropping.
    const sess = sessions.find((s) => s.id === session.id);
    if (sess && sess.state !== 'running') {
      showHint('会话未运行，正在重启…');
      api(`/api/sessions/${session.id}/start`, { method: 'POST' }).catch(() => {});
      composeInput.value = '';
      return;
    }
    // Audit A2: don't queue into a busy turn (server also rejects).
    if (sess && sess.turnActive) {
      showHint('上一回合仍在进行，请等待或点 ■ 停止');
      return;
    }
    // Optimistic local echo: render the user bubble immediately; the
    // server's echo (same session id) lands in the transcript and the
    // rAF replay renders it — duplicates are avoided because renderChatItem
    // for 'user' just sets textContent (idempotent for identical text).
    renderChatItem(entry, { t: 'user', text, ts: Date.now() });
    const payload = JSON.stringify({ type: 'user', __ctl: true, text });
    if (entry.socket?.readyState === WebSocket.OPEN) {
      entry.socket.send(payload);
    } else {
      ensureChat(entry, session);
      setTimeout(() => { if (entry.socket?.readyState === WebSocket.OPEN) entry.socket.send(payload); }, 300);
    }
    composeInput.value = '';
    composeInput.style.height = '';
  };
  composeSubmit.onclick = (ev) => { ev.preventDefault(); submit(); };
  wireComposerSend(composeInput, submit);

  return entry;
}

function resetChat(entry) {
  entry.logEl.innerHTML = '';
  entry.render = { curTextEl: null, curTextId: null, toolCards: new Map(), systemShown: false, curTextBuf: '', curTextRenderedLen: 0, _snapBuf: '' };
  setChatPending(entry, false);
}

function ensureChat(entry, session) {
  if (entry.socket && entry.socket.readyState <= WebSocket.OPEN) return;
  connectChatSocket(entry, session);
}

function connectChatSocket(entry, session, attempt = 0) {
  if (live.get(session.id) !== entry) return;
  try { entry.socket?.close(); } catch {}
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  // No ?token= in the URL — the webpty_token cookie travels with the
  // handshake (Issue 3.3 keeps the token out of logs/history/Referer).
  const ws = new WebSocket(`${proto}//${location.host}/ws/sessions/${encodeURIComponent(session.id)}`);
  ws.onopen = () => { attempt = 0; };
  ws.onmessage = (event) => {
    if (typeof event.data !== 'string') return;
    let msg;
    try { msg = JSON.parse(event.data); } catch { return; }
    if (msg.type === 'snapshot') {
      if (msg.chunk !== undefined) {
        // Chunked snapshot (audit 8.1): accumulate string chunks until
        // done, then parse ONCE (single JSON.parse for the whole
        // transcript) and replay.
        if (!r._snapBuf) { r._snapBuf = ''; resetChat(entry); }
        r._snapBuf += msg.chunk;
        if (!msg.done) return;
        let items = [];
        try { items = JSON.parse(r._snapBuf); }
        catch {
          // Audit S1: chunked snapshot frames can be dropped by the outbox
          // on a slow connection — a partial buffer won't parse. Force a
          // reconnect so the whole snapshot is re-sent instead of leaving
          // the chat silently empty.
          r._snapBuf = '';
          try { ws.close(); } catch {}
          showHint('快照不完整，正在重新同步…', 5000);
          return;
        }
        r._snapBuf = '';
        replaySnapshot(entry, items);
        return;
      }
      resetChat(entry);
      replaySnapshot(entry, msg.transcript || []);
      return;
    } else if (msg.type === 'agent') {
      renderChatItem(entry, msg.item);
    } else if (msg.type === 'state') {
      applySessionState(msg.session);
    }
  };
  ws.onclose = () => {
    if (entry.socket === ws) entry.socket = null;
    if (live.get(session.id) !== entry) return;
    // Audit T5 (same as pty path): repeated failures with a token set →
    // re-prompt instead of an endless reconnect loop.
    if (attempt >= 3 && (localStorage.getItem('webpty.token') || document.cookie.includes('webpty_token'))) {
      showHint('连接被拒绝——令牌可能已失效，请重新解锁', 6000);
      setTimeout(() => showTokenGate(), 1500);
      return;
    }
    showHint('连接断开，正在重连…', 10000);
    const delay = Math.min(5000, 250 * 2 ** attempt);
    setTimeout(() => connectChatSocket(entry, session, attempt + 1), delay);
  };
  entry.socket = ws;
}

function setChatPending(entry, on) {
  if (on) {
    if (!entry.pendingEl) {
      const el = document.createElement('div');
      el.className = 'chat-pending';
      el.innerHTML = '<i></i><i></i><i></i>';
      entry.pendingEl = el;
    }
    entry.scrollEl.appendChild(entry.pendingEl); // keep below the log
    entry.scrollEl.scrollTop = entry.scrollEl.scrollHeight;
  } else if (entry.pendingEl) {
    entry.pendingEl.remove();
    entry.pendingEl = null;
  }
}

function renderChatItem(entry, item) {
  const sc = entry.scrollEl;
  const atBottom = sc.scrollHeight - sc.scrollTop - sc.clientHeight < 80;
  const r = entry.render;
  const breakText = () => { r.curTextEl = null; r.curTextId = null; };

  // DOM cap (audit V5): long agent sessions accumulate thousands of
  // .chat-msg nodes — keep the newest CHAT_DOM_CAP, drop the oldest.
  const CHAT_DOM_CAP = 1500;
  if (entry.logEl.childElementCount > CHAT_DOM_CAP) {
    const over = entry.logEl.childElementCount - CHAT_DOM_CAP;
    for (let i = 0; i < over; i++) {
      const first = entry.logEl.firstElementChild;
      if (first && !first.classList.contains('tool-card')) first.remove();
      else break;
    }
  }

  switch (item.t) {
    case 'system':
      if (!r.systemShown) {
        const el = document.createElement('div');
        el.className = 'chat-sys';
        el.textContent = [item.model, item.permissionMode].filter(Boolean).join(' · ');
        entry.logEl.appendChild(el);
        r.systemShown = true;
      }
      break;
    case 'user': {
      breakText();
      // Audit C1 dedup: submit() renders the bubble optimistically; the
      // server echoes the same user item over the WS — skip when the last
      // rendered user bubble has identical text.
      const prevUser = entry.logEl.lastElementChild;
      const prevBubble = prevUser && prevUser.querySelector('.bubble');
      if (prevUser && prevUser.classList.contains('chat-msg')
          && prevUser.classList.contains('user')
          && prevBubble && prevBubble.textContent === item.text) {
        return;
      }
      const el = document.createElement('div');
      el.className = 'chat-msg user';
      const b = document.createElement('div');
      b.className = 'bubble';
      b.textContent = item.text;
      el.appendChild(b);
      entry.logEl.appendChild(el);
      setChatPending(entry, true);
      break;
    }
    case 'text': {
      if (!item.text) break;
      if (!r.curTextEl || r.curTextId !== item.id) {
        const el = document.createElement('div');
        el.className = 'chat-msg assistant';
        const b = document.createElement('div');
        b.className = 'bubble md';
        el.appendChild(b);
        entry.logEl.appendChild(el);
        r.curTextEl = b;
        r.curTextId = item.id;
        r.curTextBuf = '';
        clearTimeout(r.curTextTimer); // flush any pending render of the old block
      }
      // Debounced full-block render (audit V5): deltas arrive token-wise and
      // may split markdown syntax (```, **bold**, lists) — rendering each
      // delta standalone leaves unclosed/glitched blocks. Accumulate the
      // full text and re-render once 200ms passes without new deltas.
      // (Audit F1) Past TEXT_RENDER_CAP, full re-renders become O(n²):
      // switch to append-only for the tail so the main thread stays smooth.
      r.curTextBuf += item.text;
      clearTimeout(r.curTextTimer);
      r.curTextTimer = setTimeout(() => {
        if (!r.curTextEl) return;
        const buf = r.curTextBuf;
        const prevLen = r.curTextRenderedLen || 0;
        if (buf.length > 65536 && prevLen > 0) {
          // Append the tail delta (no full DOM rebuild).
          const delta = buf.slice(prevLen);
          r.curTextEl.insertAdjacentHTML('beforeend', renderMarkdown(delta));
          r.curTextRenderedLen = buf.length;
        } else {
          r.curTextEl.innerHTML = renderMarkdown(buf);
          r.curTextRenderedLen = buf.length;
        }
      }, 200);
      break;
    }
    case 'thinking': {
      breakText();
      const el = document.createElement('div');
      el.className = 'chat-think';
      el.textContent = item.text ? `✻ ${item.text}` : '✻ Thinking…';
      entry.logEl.appendChild(el);
      break;
    }
    case 'tool_use': {
      breakText();
      const card = makeToolCard(item);
      entry.logEl.appendChild(card.el);
      r.toolCards.set(item.toolId, card);
      break;
    }
    case 'tool_result': {
      const card = r.toolCards.get(item.toolId);
      if (card) attachToolResult(card, item);
      else {
        const el = document.createElement('div');
        el.className = 'chat-tool';
        const pre = document.createElement('pre');
        pre.className = 'tool-out' + (item.isError ? ' err' : '');
        pre.textContent = item.content;
        el.appendChild(pre);
        entry.logEl.appendChild(el);
      }
      break;
    }
    case 'result': {
      breakText();
      setChatPending(entry, false);
      const el = document.createElement('div');
      el.className = 'chat-result' + (item.isError ? ' err' : '');
      const parts = [item.isError ? (item.text || 'error') : 'done'];
      if (typeof item.durationMs === 'number') parts.push(`${(item.durationMs / 1000).toFixed(1)}s`);
      if (typeof item.numTurns === 'number') parts.push(`${item.numTurns} turns`);
      if (typeof item.costUsd === 'number') parts.push(`$${item.costUsd.toFixed(4)}`);
      el.textContent = parts.join('  ·  ');
      entry.logEl.appendChild(el);
      break;
    }
    case 'error': {
      breakText();
      setChatPending(entry, false);
      const el = document.createElement('div');
      el.className = 'chat-result err';
      el.textContent = item.message;
      entry.logEl.appendChild(el);
      break;
    }
    case 'exit': {
      breakText();
      setChatPending(entry, false);
      const el = document.createElement('div');
      el.className = 'chat-sys';
      el.textContent = '— session ended —';
      entry.logEl.appendChild(el);
      break;
    }
  }
  if (atBottom) sc.scrollTop = sc.scrollHeight;
}

function makeToolCard(item) {
  const el = document.createElement('div');
  el.className = 'chat-tool';
  const head = document.createElement('button');
  head.type = 'button';
  head.className = 'tool-head';
  const summary = toolSummary(item.name, item.input);
  head.innerHTML = `<span class="tool-name">${escapeHtml(item.name || 'tool')}</span>`
    + (summary ? `<span class="tool-sum">${escapeHtml(summary)}</span>` : '')
    + `<span class="tool-status">▸</span>`;
  const body = document.createElement('div');
  body.className = 'tool-body';
  body.hidden = true;
  const pre = document.createElement('pre');
  pre.className = 'tool-in';
  pre.textContent = prettyInput(item.input);
  body.appendChild(pre);
  const out = document.createElement('pre');
  out.className = 'tool-out';
  out.hidden = true;
  body.appendChild(out);
  head.onclick = () => { body.hidden = !body.hidden; };
  el.appendChild(head);
  el.appendChild(body);
  return { el, head, body, out };
}

function attachToolResult(card, item) {
  card.out.hidden = false;
  card.out.textContent = item.content || (item.isError ? '(error)' : '(no output)');
  card.out.classList.toggle('err', Boolean(item.isError));
  const st = card.head.querySelector('.tool-status');
  if (st) {
    st.textContent = item.isError ? '✗' : '✓';
    st.className = 'tool-status ' + (item.isError ? 'err' : 'ok');
  }
}

function toolSummary(name, input) {
  if (!input || typeof input !== 'object') return '';
  switch (name) {
    case 'Bash':
    case 'PowerShell': return input.command || '';
    case 'Read':
    case 'Write':
    case 'Edit': return input.file_path || '';
    case 'NotebookEdit': return input.notebook_path || '';
    case 'Grep': return input.pattern || '';
    case 'Glob': return input.pattern || '';
    case 'Task': return input.description || '';
    case 'WebFetch': return input.url || '';
    case 'WebSearch': return input.query || '';
    case 'TaskCreate': return input.subject || '';
    default: {
      const first = Object.values(input).find((v) => typeof v === 'string');
      return first ? String(first).slice(0, 80) : '';
    }
  }
}

function prettyInput(input) {
  try { return JSON.stringify(input, null, 2); } catch { return String(input); }
}

// --- minimal, safe markdown → HTML (escapes first, then limited formatting) -
function escapeHtml(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}
function renderInline(s) {
  s = s.replace(/`([^`]+)`/g, (_, c) => `<code>${c}</code>`);
  s = s.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  s = s.replace(/(^|[^*])\*([^*\n]+)\*(?!\*)/g, '$1<em>$2</em>');
  // Link URLs: exclude quote/angle/backtick chars so the href attribute can
  // never be escaped (stored-XSS via onmouseover=... — Issue 2.3), and keep
  // the protocol whitelist (http/https only).
  s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^\s"'<>`]+)\)/g, (_, t, u) => `<a href="${u}" target="_blank" rel="noopener">${t}</a>`);
  return s;
}
function renderMarkdown(text) {
  const lines = String(text).split('\n');
  let html = '';
  let inFence = false, fenceBuf = [], inList = false, listType = '';
  const closeList = () => { if (inList) { html += `</${listType}>`; inList = false; listType = ''; } };
  for (const raw of lines) {
    if (/^\s*```/.test(raw)) {
      if (!inFence) { inFence = true; fenceBuf = []; }
      else { inFence = false; closeList(); html += `<pre class="md-code"><code>${escapeHtml(fenceBuf.join('\n'))}</code></pre>`; }
      continue;
    }
    if (inFence) { fenceBuf.push(raw); continue; }
    if (!raw.trim()) { closeList(); continue; }
    const h = raw.match(/^(#{1,6})\s+(.*)$/);
    if (h) { closeList(); const lvl = h[1].length; html += `<h${lvl}>${renderInline(escapeHtml(h[2]))}</h${lvl}>`; continue; }
    const ul = raw.match(/^\s*[-*]\s+(.*)$/);
    const ol = raw.match(/^\s*\d+\.\s+(.*)$/);
    if (ul || ol) {
      const type = ul ? 'ul' : 'ol';
      if (!inList || listType !== type) { closeList(); inList = true; listType = type; html += `<${type}>`; }
      html += `<li>${renderInline(escapeHtml((ul ? ul[1] : ol[1])))}</li>`;
      continue;
    }
    closeList();
    html += `<p>${renderInline(escapeHtml(raw))}</p>`;
  }
  if (inFence) html += `<pre class="md-code"><code>${escapeHtml(fenceBuf.join('\n'))}</code></pre>`;
  closeList();
  return html;
}

const KEY_SEQ = {
  esc: '\x1b',
  tab: '\t',
  enter: '\r',
  break: '\x03',
  up: '\x1b[A',
  down: '\x1b[B',
  left: '\x1b[D',
  right: '\x1b[C',
  home: '\x1b[H',
  end: '\x1b[F',
  pgup: '\x1b[5~',
  pgdn: '\x1b[6~',
  // Quick menu-selection keys: claude's numbered prompts select on the digit;
  // codex / other TUIs sometimes use letter shortcuts.
  1: '1',
  2: '2',
  3: '3',
  a: 'a',
  b: 'b',
  c: 'c'
};

// navigator.clipboard exists only in a secure context (HTTPS or localhost).
// Over plain HTTP (e.g. a Tailscale IP/host) it is undefined, so writeText was
// silently a no-op. Fall back to a synchronous execCommand('copy') run inside
// the click gesture, which works in insecure contexts too.
function copyText(text) {
  if (!text) return false;
  if (navigator.clipboard?.writeText) {
    navigator.clipboard.writeText(text).catch(() => execCommandCopy(text));
    return true;
  }
  return execCommandCopy(text);
}
function execCommandCopy(text) {
  try {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.setAttribute('readonly', '');
    ta.style.cssText = 'position:fixed;top:-1000px;left:0;opacity:0;';
    document.body.appendChild(ta);
    ta.focus();
    ta.select();
    ta.setSelectionRange(0, text.length);
    const ok = document.execCommand('copy');
    ta.remove();
    return ok;
  } catch { return false; }
}
// Chunk size for large pastes over WS (Issue/plan Task 9): keep frames well
// under limits and give the receiver time to drain between chunks.
const PASTE_CHUNK = 32 * 1024;

async function sendTextChunked(entry, text) {
  // UTF-8 byte length (Chinese = 3 bytes each) decides chunking.
  const bytes = new TextEncoder().encode(text);
  if (bytes.length <= PASTE_CHUNK) {
    if (entry.socket?.readyState === WebSocket.OPEN) entry.socket.send(text);
    return;
  }
  const totalKB = Math.ceil(bytes.length / 1024);
  showHint(`正在粘贴 ${totalKB} KB…`, 10000);
  try {
    for (let i = 0; i < bytes.length; i += PASTE_CHUNK) {
      if (entry.socket?.readyState !== WebSocket.OPEN) break;
      const part = bytes.subarray(i, i + PASTE_CHUNK);
      // Send as binary for pty sessions (bytes), text otherwise.
      if (entry.socket.binaryType === 'arraybuffer' || entry.kind === 'session') {
        entry.socket.send(part);
      } else {
        entry.socket.send(new TextDecoder().decode(part));
      }
      await new Promise((r) => setTimeout(r, 50));
    }
  } finally {
    showHint('', 0); // clear immediately
  }
}

function pasteToSession(entry) {
  if (navigator.clipboard?.readText) {
    navigator.clipboard.readText()
      .then((text) => { if (text && entry.socket?.readyState === WebSocket.OPEN) sendTextChunked(entry, text); })
      .catch(() => focusComposeForPaste(entry));
    return;
  }
  // Insecure context: the clipboard can't be read programmatically, so put the
  // cursor in the composer and let the user paste manually, then Send.
  focusComposeForPaste(entry);
}
function focusComposeForPaste(entry) {
  entry.composeInput?.focus();
}

function sendKey(entry, key) {
  if (key === 'copy') {
    copyText(getJoinedSelection(entry.term));
    return;
  }
  if (key === 'paste') {
    pasteToSession(entry);
    return;
  }
  // `text:...` shortcut: type a literal word and submit with Enter (used by the
  // 보고/진행/확인/완료 quick-reply buttons).
  if (key.startsWith('text:')) {
    const text = key.slice(5);
    if (text && entry.socket?.readyState === WebSocket.OPEN) entry.socket.send(text + '\r');
    return;
  }
  const seq = KEY_SEQ[key];
  if (seq && entry.socket?.readyState === WebSocket.OPEN) entry.socket.send(seq);
}

// Shared tab bar — one tab per session. Folder name + a tiny tool-color dot.
// Tab indicator: blink = working, solid = running & waiting for input,
// border = exited. CLI tools draw a tall cursor bar; agent (web) tools draw a
// globe-like circle. busy is server-derived (agent: turn in flight; pty: output
// still repainting).
function dotStatus(s) {
  if (s.state === 'stopped') return 'exited';
  if (s.busy) return 'busy';
  return 'ready';
}

let tabDragInProgress = false;

function renderTabs() {
  // Suppressed during an active tab drag — wiping tabsEl.innerHTML mid-drag
  // would orphan the dragged element held in the pointermove closure, and the
  // next insertBefore would reattach it alongside a freshly rendered duplicate.
  if (tabDragInProgress) return;
  // Keyed diff (audit F2): keep existing tab nodes, only touch the parts
  // that changed. Full innerHTML rebuild on every 3s poll / state push /
  // tab switch churned the whole strip and re-created listeners.
  const existing = new Map();
  for (const el of tabsEl.children) {
    if (el.dataset && el.dataset.id) existing.set(el.dataset.id, el);
  }
  sessions.forEach((s, idx) => {
    let tab = existing.get(s.id);
    if (!tab) {
      tab = document.createElement('button');
      tab.type = 'button';
      tab.className = 'tab';
      tab.dataset.id = s.id;
      tab.draggable = false; // we use pointer events, not native HTML5 DnD
      const name = document.createElement('span');
      name.className = 'tab-name';
      tab.appendChild(name);
      const dot = document.createElement('span');
      dot.className = 'tab-dot';
      dot.dataset.tool = '';
      tab.appendChild(dot);
      const closeBtn = document.createElement('button');
      closeBtn.type = 'button';
      closeBtn.className = 'tab-close';
      closeBtn.title = '关闭会话';
      closeBtn.setAttribute('aria-label', '关闭会话');
      closeBtn.innerHTML = '×';
      closeBtn.onclick = async (ev) => {
        ev.stopPropagation();
        ev.preventDefault();
        if (!confirm(`关闭会话「${s.name}」？${isAgent(s) ? '（对话上下文仍可通过重新打开项目恢复）' : '（进程将被终止，输出历史不可恢复）'}`)) return;
        try {
          await api(`/api/sessions/${s.id}`, { method: 'DELETE' });
        } catch (e) {
          alert(e.message);
          return;
        }
        await refreshSessions();
      };
      tab.appendChild(closeBtn);
      tab.onclick = () => scrollToIndex(idx);
      tabsEl.appendChild(tab);
      existing.set(s.id, tab);
    }
    // Update only the volatile parts.
    tab.classList.toggle('active', idx === activeIndex);
    tab.querySelector('.tab-name').textContent = s.name;
    const dot = tab.querySelector('.tab-dot');
    const cls = `tab-dot ${isAgent(s) ? 'web' : 'cli'} ${dotStatus(s)}`;
    if (dot.className !== cls) dot.className = cls;
    if (dot.dataset.tool !== s.tool) dot.dataset.tool = s.tool;
  });
  // Remove tabs for sessions that no longer exist.
  for (const el of Array.from(tabsEl.children)) {
    if (!sessions.some((s) => s.id === el.dataset.id)) el.remove();
  }
  scrollActiveTabIntoView();
}

// Keep the active tab visible in the horizontal tab strip. Auto-snaps on
// initial load and when the user explicitly navigates (tab tap, drawer). Once
// the user has scrolled the strip by hand we leave it alone — periodic polls
// shouldn't yank the strip back under their finger. The next compose-input
// keystroke resets that and re-snaps to the active tab.
let tabStripUserScrolled = false;
let tabStripProgrammaticScroll = false;
tabsEl.addEventListener('scroll', () => {
  if (tabStripProgrammaticScroll) return;
  tabStripUserScrolled = true;
});
function scrollActiveTabIntoView(force = false) {
  if (!force && tabStripUserScrolled) return;
  const active = tabsEl.children[activeIndex];
  if (!active) return;
  // Defer one frame: on first render the tab strip's own layout / scrollWidth
  // isn't settled yet, so scrollIntoView would no-op.
  requestAnimationFrame(() => {
    tabStripProgrammaticScroll = true;
    try { active.scrollIntoView({ behavior: 'instant', inline: 'nearest', block: 'nearest' }); }
    catch { active.scrollIntoView(); }
    // Release the suppression after the resulting scroll event has settled.
    setTimeout(() => { tabStripProgrammaticScroll = false; }, 100);
  });
}
// Typing in any composer means the user is back to "work on the active tab"
// mode — re-snap the strip so the current tab is visible again.
document.addEventListener('input', (ev) => {
  if (!ev.target?.classList?.contains?.('compose-input')) return;
  if (!tabStripUserScrolled) return;
  tabStripUserScrolled = false;
  scrollActiveTabIntoView(true);
}, true);

// Tab drag-and-drop reorder. Pointer-events based so the same code path covers
// mouse + touch. Touch arms after a ~300ms hold (so a tap still activates the
// tab and a horizontal swipe still scrolls the tab strip); mouse arms after a
// small movement threshold. Reorders DOM live as the dragged tab crosses each
// sibling's midpoint, then persists the new order to the server on release.
function setupTabDrag() {
  const HOLD_MS = 300;
  const MOVE_THRESHOLD = 6;

  tabsEl.addEventListener('pointerdown', (ev) => {
    if (ev.button !== undefined && ev.button !== 0) return;
    const tab = ev.target.closest('.tab');
    if (!tab || !tabsEl.contains(tab)) return;
    if (ev.target.closest('.tab-close')) return;
    if (tabsEl.querySelectorAll('.tab').length < 2) return;

    const isTouch = ev.pointerType === 'touch';
    const startX = ev.clientX;
    const startY = ev.clientY;
    const pointerId = ev.pointerId;
    let armed = !isTouch;
    let dragging = false;
    let baseX = startX;
    let holdTimer = null;
    let cancelled = false;

    const cleanup = () => {
      clearTimeout(holdTimer);
      tabDragInProgress = false;
      tab.classList.remove('drag-armed', 'dragging');
      tab.style.transform = '';
      try { tab.releasePointerCapture(pointerId); } catch {}
      window.removeEventListener('pointermove', onMove);
      window.removeEventListener('pointerup', onUp);
      window.removeEventListener('pointercancel', onCancel);
    };

    const swallowNextClick = () => {
      const swallow = (e) => {
        e.stopPropagation();
        e.preventDefault();
        window.removeEventListener('click', swallow, true);
      };
      window.addEventListener('click', swallow, true);
      // Fallback in case no click fires (e.g. touch released outside)
      setTimeout(() => window.removeEventListener('click', swallow, true), 250);
    };

    if (isTouch) {
      holdTimer = setTimeout(() => {
        if (cancelled) return;
        armed = true;
        tab.classList.add('drag-armed');
        try { tab.setPointerCapture(pointerId); } catch {}
      }, HOLD_MS);
    }

    const onMove = (e) => {
      if (e.pointerId !== pointerId) return;
      const dx = e.clientX - startX;
      const dy = e.clientY - startY;

      if (!dragging) {
        if (!armed) {
          // Touch: significant horizontal movement before hold completed →
          // user is scrolling the tab strip, abort drag. Vertical wobble is
          // ignored — there's nothing vertical to scroll on the header.
          if (Math.abs(dx) > MOVE_THRESHOLD) {
            cancelled = true;
            cleanup();
          }
          return;
        }
        // Engage drag on horizontal movement only — vertical movement should
        // never start a drag and never moves the tab off its row.
        if (Math.abs(dx) < MOVE_THRESHOLD) return;
        dragging = true;
        tabDragInProgress = true;
        tab.classList.remove('drag-armed');
        tab.classList.add('dragging');
        try { tab.setPointerCapture(pointerId); } catch {}
      }

      e.preventDefault();
      tab.style.transform = `translateX(${e.clientX - baseX}px)`;

      const siblings = [...tabsEl.querySelectorAll('.tab')];
      for (const other of siblings) {
        if (other === tab) continue;
        const r = other.getBoundingClientRect();
        const mid = r.left + r.width / 2;
        const pos = tab.compareDocumentPosition(other);
        const otherIsAfter = (pos & Node.DOCUMENT_POSITION_FOLLOWING) !== 0;
        const otherIsBefore = (pos & Node.DOCUMENT_POSITION_PRECEDING) !== 0;
        if (otherIsAfter && e.clientX > mid) {
          tabsEl.insertBefore(tab, other.nextSibling);
          baseX = e.clientX;
          tab.style.transform = '';
          break;
        }
        if (otherIsBefore && e.clientX < mid) {
          tabsEl.insertBefore(tab, other);
          baseX = e.clientX;
          tab.style.transform = '';
          break;
        }
      }
    };

    const onUp = (e) => {
      if (e.pointerId !== pointerId) return;
      const didDrag = dragging;
      const draggedId = tab.dataset.id;
      cleanup();
      if (didDrag) {
        swallowNextClick();
        commitTabOrder(draggedId);
      }
    };

    const onCancel = (e) => {
      if (e.pointerId !== pointerId) return;
      cleanup();
    };

    window.addEventListener('pointermove', onMove, { passive: false });
    window.addEventListener('pointerup', onUp);
    window.addEventListener('pointercancel', onCancel);
  });
}

async function commitTabOrder(draggedId) {
  const newIds = [...tabsEl.querySelectorAll('.tab')].map((t) => t.dataset.id);
  const byId = new Map(sessions.map((s) => [s.id, s]));
  const next = [];
  for (const id of newIds) {
    const s = byId.get(id);
    if (s) { next.push(s); byId.delete(id); }
  }
  for (const s of byId.values()) next.push(s); // defensive: keep any stragglers
  sessions = next;

  // Reorder the page DOM in-place so existing terminals/sockets are preserved.
  for (const s of sessions) {
    const page = track.querySelector(`.page[data-id="${s.id}"]`);
    if (page) track.appendChild(page);
  }
  // The dragged tab becomes the active one — matches the user's gesture.
  const ni = sessions.findIndex((s) => s.id === draggedId);
  if (ni >= 0) activeIndex = ni;
  scrollToIndex(activeIndex, true);

  try {
    await api('/api/sessions/order', {
      method: 'PUT',
      body: JSON.stringify({ ids: sessions.map((s) => s.id) })
    });
  } catch (e) {
    console.error('reorder persist failed', e);
  }
}

const TOOL_ICON = { claude: 'C', 'claude-chat': 'C', codex: 'O', reasonix: 'R', opencode: 'K', aider: 'A', gemini: 'G', qwen: 'Q', 'cursor-agent': 'X', copilot: 'P', agy: 'Y', powershell: 'P', bash: 'B' };
const TOOL_LABEL = {
  claude: 'Claude',
  'claude-chat': 'Claude (chat)',
  codex: 'Codex',
  reasonix: 'Reasonix',
  opencode: 'OpenCode',
  aider: 'Aider',
  gemini: 'Gemini',
  qwen: 'Qwen Code',
  'cursor-agent': 'Cursor Agent',
  copilot: 'Copilot',
  agy: 'Antigravity',
  powershell: 'PowerShell',
  bash: 'Bash'
};

async function exitSession(session) {
  if (!session) return;
  if (!confirm(`Close session "${session.name}"?`)) return;
  try { await api(`/api/sessions/${session.id}`, { method: 'DELETE' }); }
  catch (e) { alert(e.message); return; }
  await refreshSessions();
}

// Transient status hint chip — show a message, auto-hide after ms.
let _hintTimer = null;
function showHint(msg, ms = 2000) {
  const hint = document.getElementById('status-hint');
  if (!hint) return;
  hint.textContent = msg;
  hint.hidden = false;
  clearTimeout(_hintTimer);
  _hintTimer = setTimeout(() => { hint.hidden = true; hint.textContent = ''; }, ms);
}

function sendToSession(id, text) {
  const entry = live.get(id);
  const session = sessions.find((s) => s.id === id);
  // Input to a stopped session was silently dropped (server write() returns
  // false, WS path is a no-op) — auto-restart + brief hint instead.
  if (session && session.state !== 'running') {
    showHint('会话未运行，正在重启…');
    startSession(id).catch(() => {});
    return false;
  }
  if (isAgent(session)) {
    // Agent sessions speak the stream-json user-message protocol, not raw bytes.
    if (entry?.socket?.readyState === WebSocket.OPEN) {
      entry.socket.send(JSON.stringify({ type: 'user', __ctl: true, text: text.replace(/\r$/, '') }));
      return true;
    }
    return false;
  }
  if (entry?.socket?.readyState === WebSocket.OPEN) {
    entry.socket.send(text);
    return true;
  }
  // Fallback to HTTP input endpoint
  api(`/api/sessions/${id}/input`, { method: 'POST', body: JSON.stringify({ bytes: text }) }).catch(() => {});
  return false;
}

function closeMenu() {
  menuBackdrop.hidden = true;
  menuPop.innerHTML = '';
}

function addMenuItem(label, onClick, opts = {}) {
  const b = document.createElement('button');
  b.className = 'menu-item' + (opts.className ? ' ' + opts.className : '');
  b.textContent = label;
  if (opts.badge) {
    const s = document.createElement('span');
    s.className = 'badge';
    s.textContent = opts.badge;
    b.appendChild(s);
  }
  b.onclick = (ev) => {
    ev.stopPropagation();
    closeMenu();
    Promise.resolve().then(onClick);
  };
  menuPop.appendChild(b);
}

function addMenuSep() {
  const s = document.createElement('div');
  s.className = 'menu-sep';
  menuPop.appendChild(s);
}

async function spawnShell(tool, label, cwd) {
  if (!config.tools[tool]) {
    alert(`工具「${tool}」未在服务器上配置。`);
    return;
  }
  try {
    const created = await api('/api/sessions', {
      method: 'POST',
      body: JSON.stringify({
        name: label,
        cwd,
        tool,
        args: '',
        autostart: false,
        start: true
      })
    });
    await refreshSessions();
    const idx = sessions.findIndex((s) => s.id === created.id);
    if (idx >= 0) scrollToIndex(idx);
  } catch (e) {
    alert(e.message);
  }
}

function addMenuLabel(text) {
  const div = document.createElement('div');
  div.className = 'menu-label';
  div.textContent = text;
  menuPop.appendChild(div);
}

function addToolMenuItem(tool, project, session) {
  const cwd = (session.cwd || '').toLowerCase();
  // Find an existing session for this folder+tool (running OR stopped), preferring running.
  let sess = null;
  for (const s of sessions) {
    if ((s.cwd || '').toLowerCase() === cwd && s.tool === tool) {
      if (!sess || s.state === 'running') sess = s;
    }
  }
  const b = document.createElement('button');
  b.className = 'menu-item menu-tool';
  b.dataset.tool = tool;

  const dot = document.createElement('span');
  dot.className = 'menu-tool-dot';
  b.appendChild(dot);

  const label = document.createElement('span');
  label.className = 'menu-tool-label';
  label.textContent = TOOL_LABEL[tool] || tool;
  b.appendChild(label);

  let stateText = null;
  if (sess) stateText = sess.id === session.id ? 'current' : (sess.state === 'running' ? 'active' : 'resume');
  else if (tool === 'claude' && project.claudeMtime > 0) stateText = 'history';
  if (stateText) {
    const badge = document.createElement('span');
    badge.className = 'menu-state';
    badge.textContent = stateText;
    b.appendChild(badge);
  }

  b.onclick = (ev) => {
    ev.stopPropagation();
    closeMenu();
    activateTool(project, tool, sess);
  };
  menuPop.appendChild(b);
}

function openMenu(sessionId) {
  menuPop.innerHTML = '';
  const session = sessions.find((s) => s.id === sessionId);
  if (!session) return;

  addMenuLabel('命令');
  addMenuItem('退出', () => exitSession(session), { className: 'danger' });
  addMenuItem('清屏', () => sendToSession(session.id, '/clear\r'));
  addMenuItem('压缩上下文', () => sendToSession(session.id, '/compact\r'));

  addMenuSep();
  addMenuLabel('工作流');
  addMenuItem('汇报进度', () => sendToSession(session.id, '请总结当前进度并汇报。\r'));
  addMenuItem('提交并推送', () => sendToSession(session.id, '请提交更改并推送。\r'));
  addMenuItem('部署', () => sendToSession(session.id, '请部署。\r'));

  addMenuSep();
  addMenuLabel('扩展');
  addMenuItem('Agent 管理', openAgentsPanel);
  addMenuItem('通知中心', openNotifyPanel);
  addMenuItem('成本账单', openCostPanel);
  addMenuItem('备份管理', openBackupPanel);
  addMenuItem('迁移向导', openMigratePanel);

  addMenuSep();
  addMenuLabel('工具');
  const cwd = (session.cwd || '').toLowerCase();
  const project = (projects || []).find((p) => p.path.toLowerCase() === cwd)
    || { path: session.cwd, name: session.name, claudeMtime: 0 };
  for (const t of Object.keys(config.tools)) {
    addToolMenuItem(t, project, session);
  }

  menuBackdrop.hidden = false;
}

menuBackdrop.addEventListener('click', (ev) => {
  if (ev.target === menuBackdrop) closeMenu();
});

function openDrawer() {
  populateFolders();
  drawer.hidden = false;
  requestAnimationFrame(() => drawer.classList.add('open'));
}

function closeDrawer() {
  drawer.classList.remove('open');
  setTimeout(() => { drawer.hidden = true; }, 220);
}

drawerBackdrop.addEventListener('click', closeDrawer);

// --- Add Folder picker ---------------------------------------------------
// Server-driven filesystem browser. `currentPath` is the directory we're
// looking at (empty string = platform roots). Selecting confirms the path
// itself, not an entry inside the list. Clicking a row navigates into it.
let pickerCurrentPath = '';

function parentDirOf(p) {
  if (!p) return '';
  if (/^[A-Za-z]:[\\/]?$/.test(p)) return '';
  if (p === '/') return '';
  const cleaned = p.replace(/[\\/]+$/, '');
  const sep = Math.max(cleaned.lastIndexOf('/'), cleaned.lastIndexOf('\\'));
  if (sep <= 0) return '';
  const parent = cleaned.slice(0, sep);
  if (/^[A-Za-z]:$/.test(parent)) return parent + '\\';
  if (!parent) return '/';
  return parent;
}

async function loadPickerDir(p) {
  pickerCurrentPath = p || '';
  folderPickerPath.textContent = pickerCurrentPath || '(根目录)';
  folderPickerUp.disabled = !pickerCurrentPath;
  folderPickerSelect.disabled = !pickerCurrentPath;
  folderPickerList.innerHTML = '<li class="folder-picker-empty">加载中…</li>';
  try {
    const entries = await api(`/api/fs/list?path=${encodeURIComponent(pickerCurrentPath)}`);
    renderPickerEntries(entries);
  } catch (err) {
    folderPickerList.innerHTML = `<li class="folder-picker-empty">无法列出: ${err.message}</li>`;
  }
}

function renderPickerEntries(entries) {
  folderPickerList.innerHTML = '';
  if (!entries.length) {
    folderPickerList.innerHTML = '<li class="folder-picker-empty">(无子文件夹)</li>';
    return;
  }
  for (const e of entries) {
    const li = document.createElement('li');
    const btn = document.createElement('button');
    btn.className = 'folder-picker-row';
    btn.innerHTML = `
      <span class="icon"><svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg></span>
      <span class="name"></span>`;
    btn.querySelector('.name').textContent = e.name;
    btn.onclick = () => loadPickerDir(e.path);
    li.appendChild(btn);
    folderPickerList.appendChild(li);
  }
}

function openFolderPicker() {
  folderPickerBackdrop.hidden = false;
  loadPickerDir('');
}

function closeFolderPicker() {
  folderPickerBackdrop.hidden = true;
}

addFolderBtn.onclick = openFolderPicker;
folderPickerUp.onclick = () => loadPickerDir(parentDirOf(pickerCurrentPath));
folderPickerClose.onclick = closeFolderPicker;
folderPickerCancel.onclick = closeFolderPicker;
folderPickerBackdrop.addEventListener('click', (ev) => {
  if (ev.target === folderPickerBackdrop) closeFolderPicker();
});

// --- Create project -------------------------------------------------------
async function createProject() {
  const name = newProjectName.value.trim();
  if (!name) return;
  newProjectBtn.disabled = true;
  try {
    const proj = await api('/api/projects/create', {
      method: 'POST',
      body: JSON.stringify({ name, gitInit: true })
    });
    newProjectName.value = '';
    projects = await api('/api/projects');
    populateFolders();
    // Jump straight into the freshly created project with the default tool.
    closeDrawer();
    await activateTool(proj, defaultToolFor(proj), null);
  } catch (err) {
    alert(`创建项目失败: ${err.message}`);
  } finally {
    newProjectBtn.disabled = false;
  }
}
newProjectBtn.onclick = createProject;
newProjectName.addEventListener('keydown', (e) => {
  if (e.key === 'Enter') createProject();
});
folderPickerSelect.onclick = async () => {
  if (!pickerCurrentPath) return;
  folderPickerSelect.disabled = true;
  try {
    projects = await api('/api/projects', {
      method: 'POST',
      body: JSON.stringify({ path: pickerCurrentPath })
    });
    populateFolders();
    closeFolderPicker();
  } catch (err) {
    alert(`添加文件夹失败: ${err.message}`);
    folderPickerSelect.disabled = false;
  }
};

function sortProjects(list, activeTools) {
  const arr = [...list];
  const byName = (a, b) => a.name.localeCompare(b.name);
  if (folderSort === 'name') return arr.sort(byName);
  if (folderSort === 'date') {
    return arr.sort((a, b) => {
      const d = (b.mtime || 0) - (a.mtime || 0);
      return d !== 0 ? d : byName(a, b);
    });
  }
  // 'active' (default): running > claude history > others, then by name within tier
  const tier = (p) => {
    if (activeTools.has(p.path.toLowerCase())) return 2;
    if (p.claudeMtime) return 1;
    return 0;
  };
  return arr.sort((a, b) => {
    const t = tier(b) - tier(a);
    if (t !== 0) return t;
    return byName(a, b);
  });
}

function populateFolders() {
  drawerFolders.innerHTML = '';

  const activeTools = new Map();
  for (const s of sessions) {
    if (s.state !== 'running') continue;
    const key = (s.cwd || '').toLowerCase();
    if (!activeTools.has(key)) activeTools.set(key, new Set());
    activeTools.get(key).add(s.tool);
  }
  const currentCwd = (sessions[activeIndex]?.cwd || '').toLowerCase();

  if (!projects.length) {
    const empty = document.createElement('li');
    empty.className = 'drawer-empty';
    empty.textContent = '未找到项目';
    drawerFolders.appendChild(empty);
    return;
  }

  const sorted = sortProjects(projects, activeTools);

  for (const p of sorted) {
    const pathKey = p.path.toLowerCase();
    const li = document.createElement('li');
    const btn = document.createElement('button');
    btn.className = 'folder-row';
    if (pathKey === currentCwd) btn.classList.add('current');

    const name = document.createElement('span');
    name.className = 'folder-row-name';
    name.textContent = p.name;
    btn.appendChild(name);

    const dots = document.createElement('span');
    dots.className = 'active-dots';
    const active = activeTools.get(pathKey);
    if (active) {
      for (const t of active) {
        const d = document.createElement('span');
        d.className = 'active-dot ' + (toolEngine(t) === 'agent' ? 'web' : 'cli');
        d.dataset.tool = t;
        d.title = t;
        dots.appendChild(d);
      }
    }
    // Outlined claude dot when history exists but no running claude session
    if (p.claudeMtime && !(active && active.has('claude'))) {
      const d = document.createElement('span');
      d.className = 'active-dot cli history';
      d.dataset.tool = 'claude';
      d.title = 'claude history available';
      dots.appendChild(d);
    }
    btn.appendChild(dots);

    const chev = document.createElement('span');
    chev.className = 'chev';
    chev.innerHTML = '<svg viewBox="0 0 24 24" width="18" height="18" fill="currentColor"><path d="M9.3 6.3a1 1 0 0 1 1.4 0l5 5a1 1 0 0 1 0 1.4l-5 5a1 1 0 1 1-1.4-1.4l4.3-4.3-4.3-4.3a1 1 0 0 1 0-1.4z"/></svg>';
    btn.appendChild(chev);

    btn.onclick = () => navigateToFolder(p);
    li.appendChild(btn);
    drawerFolders.appendChild(li);
  }
}

// Preferred default tool when opening a folder that has no session yet:
// user preference (localStorage) → reasonix (modern agent CLI) → claude.
function defaultToolFor(project) {
  const cwd = (project.path || '').toLowerCase();
  const last = localStorage.getItem(`webpty.lastTool.${cwd}`);
  if (last && config.tools[last]) return last;
  if (config.tools.reasonix) return 'reasonix';
  if (config.tools.claude) return 'claude';
  return Object.keys(config.tools)[0] || 'claude';
}

function navigateToFolder(project) {
  const cwd = project.path.toLowerCase();
  const folderSessions = sessions
    .map((s, idx) => ({ s, idx }))
    .filter(({ s }) => (s.cwd || '').toLowerCase() === cwd);

  if (folderSessions.length === 0) {
    // No session yet — default to the preferred tool (server-side auto-resumes if history)
    activateTool(project, defaultToolFor(project), null);
    return;
  }

  // Prefer last-used tool stored per folder; fall back to most recently created
  const lastTool = localStorage.getItem(`webpty.lastTool.${cwd}`);
  let target = null;
  if (lastTool) {
    const match = folderSessions.find(({ s }) => s.tool === lastTool);
    if (match) target = match;
  }
  if (!target) target = folderSessions[folderSessions.length - 1];

  closeDrawer();
  scrollToIndex(target.idx);
}

async function activateTool(project, tool, existing) {
  // Existing session (running or stopped) → switch to it; restart if stopped.
  // Resumes the same session instead of spawning a duplicate (e.g. openecg-2).
  if (existing) {
    closeDrawer();
    const idx = sessions.findIndex((s) => s.id === existing.id);
    if (idx >= 0) scrollToIndex(idx);
    if (existing.state === 'stopped') await startSession(existing.id);
    return;
  }
  closeDrawer();
  try {
    // Multi-open support: name the session "<tool>-<project>" so several
    // sessions of the same agent (even the same project) are distinguishable
    // in the tab bar; append a counter when the name would collide.
    let name = `${tool}-${project.name}`;
    const same = sessions.filter((s) => s.tool === tool && s.name === name);
    if (same.length > 0) name = `${tool}-${project.name}-${same.length + 1}`;
    const created = await api('/api/sessions', {
      method: 'POST',
      body: JSON.stringify({
        name,
        cwd: project.path,
        tool,
        args: '',
        autostart: false,
        start: true
      })
    });
    await refreshSessions();
    const idx = sessions.findIndex((s) => s.id === created.id);
    if (idx >= 0) scrollToIndex(idx);
  } catch (e) {
    alert(e.message);
  }
}

// Keyboard session switching (desktop): Alt+1..9 jumps to the Nth session.
// Capture phase so it runs before xterm forwards the keys to the PTY.
window.addEventListener('keydown', (e) => {
  if (e.altKey && !e.ctrlKey && !e.metaKey && /^[1-9]$/.test(e.key)) {
    const idx = Number(e.key) - 1;
    if (idx < pageCount()) { e.preventDefault(); e.stopPropagation(); scrollToIndex(idx); }
  }
}, true);

// Left-edge swipe to open drawer
let edgeTouch = null;
window.addEventListener('touchstart', (e) => {
  if (!drawer.hidden) return;
  const t = e.touches[0];
  if (t.clientX <= 24) edgeTouch = { x: t.clientX, y: t.clientY };
}, { passive: true });
window.addEventListener('touchmove', (e) => {
  if (!edgeTouch) return;
  const t = e.touches[0];
  const dx = t.clientX - edgeTouch.x;
  const dy = Math.abs(t.clientY - edgeTouch.y);
  if (dx > 40 && dy < 40) { edgeTouch = null; openDrawer(); }
}, { passive: true });
window.addEventListener('touchend', () => { edgeTouch = null; }, { passive: true });

function ensureTerminal(entry, session) {
  if (entry.term) return;
  const { term, fit } = makeTerminal(session, entry.host);
  entry.term = term;
  entry.fit = fit;
  // Reflect xterm scrollback position to the floating jump-to-bottom button.
  // viewportY === baseY means we're already at the latest line.
  const updateScrollBtn = () => {
    if (!entry.scrollBottomBtn) return;
    const b = term.buffer.active;
    const atBottom = b.viewportY >= b.baseY;
    entry.scrollBottomBtn.hidden = atBottom;
  };
  try { term.onScroll(updateScrollBtn); } catch {}
  updateScrollBtn();
  connectSocket(entry, session);
}

function buildAddPage() {
  const page = tplAdd.content.firstElementChild.cloneNode(true);
  const selProject = page.querySelector('select[name="project"]');
  const selTool = page.querySelector('select[name="tool"]');
  const hint = page.querySelector('#add-hint');
  for (const p of projects) {
    const o = document.createElement('option');
    o.value = p.path;
    o.textContent = p.name;
    selProject.appendChild(o);
  }
  for (const t of Object.keys(config.tools)) {
    const o = document.createElement('option');
    o.value = t;
    o.textContent = t;
    selTool.appendChild(o);
  }
  page.querySelector('.add-form').onsubmit = async (e) => {
    e.preventDefault();
    const form = new FormData(e.currentTarget);
    const cwd = form.get('project');
    const proj = projects.find((p) => p.path === cwd);
    hint.textContent = '创建中…';
    try {
      const created = await api('/api/sessions', {
        method: 'POST',
        body: JSON.stringify({
          name: proj?.name || '',
          cwd,
          tool: form.get('tool'),
          args: '',
          autostart: true,
          start: true,
          // Audit 2.1: per-session permission mode (empty = tool default).
          permissionMode: form.get('permissionMode') || undefined
        })
      });
      hint.textContent = '';
      await refreshSessions();
      const idx = sessions.findIndex((s) => s.id === created.id);
      if (idx >= 0) scrollToIndex(idx);
    } catch (err) {
      hint.textContent = err.message;
    }
  };
  // Show the permission picker only for agent-engine tools.
  const permField = page.querySelector('#perm-field');
  const updatePermVisibility = () => {
    const t = (config && config.tools) ? config.tools[selTool.value] : null;
    permField.hidden = !(t && t.engine === 'agent');
  };
  selTool.addEventListener('change', updatePermVisibility);
  updatePermVisibility();
  return { page, project: selProject, tool: selTool };
}

let trackInitialized = false;
function rebuildTrack(preserveId) {
  // On subsequent rebuilds, keep the current page in view.
  if (!preserveId && trackInitialized && sessions[activeIndex]) {
    preserveId = sessions[activeIndex].id;
  }
  // On the first build (after refresh), prefer the last-active session.
  if (!preserveId) {
    const last = localStorage.getItem('webpty.lastSessionId');
    if (last && sessions.some((s) => s.id === last)) preserveId = last;
  }
  trackInitialized = true;
  // Dispose old terminals / close their sockets BEFORE wiping the track.
  // Without this, every session delete leaked an xterm instance (canvas,
  // renderer, listeners) plus a live WebSocket (the server-side _ws_session
  // task and outbox stayed resident too) — unbounded growth on multi-open.
  for (const [, entry] of live) {
    try { entry.socket?.close(); } catch {}
    try { entry.term?.dispose(); } catch {}
  }
  track.innerHTML = '';
  live.clear();
  for (const s of sessions) {
    const entry = buildSessionPage(s);
    track.appendChild(entry.page);
  }
  if (!sessions.length) {
    const empty = document.createElement('section');
    empty.className = 'page page-empty';
    empty.innerHTML = '<div class="empty-hint"><h2>暂无会话</h2><p>点击 ☰ 打开项目列表，选择一个文件夹开始。</p><button class="empty-cta" id="empty-open-drawer">打开项目列表</button></div>';
    track.appendChild(empty);
    empty.querySelector('#empty-open-drawer').onclick = () => openDrawer();
  }

  let nextIdx = sessions.findIndex((s) => s.id === preserveId);
  if (nextIdx < 0) nextIdx = Math.min(activeIndex, Math.max(0, pageCount() - 1));
  activeIndex = Math.max(0, nextIdx);
  renderTabs();
  requestAnimationFrame(() => {
    scrollToIndex(activeIndex, false);
    onActivate(activeIndex);
    // Eager-init every other entry so the first switch to each tab doesn't
    // show the blank-then-replay flicker. Off-screen pages still have full
    // layout (.page is flex: 0 0 100%), so term.open + fit measure correctly.
    for (let i = 0; i < sessions.length; i++) {
      if (i === activeIndex) continue;
      const s = sessions[i];
      const e = live.get(s.id);
      if (!e) continue;
      if (isAgent(s)) ensureChat(e, s);
      else ensureTerminal(e, s);
    }
  });
}

function updatePageMeta() {
  renderTabs();
}

async function startSession(id) {
  try { await api(`/api/sessions/${id}/start`, { method: 'POST', body: '{}' }); }
  catch (e) { console.warn('start failed:', e.message); }
  schedulePoll(0);
}

function onActivate(idx) {
  const session = sessions[idx];
  if (!session) return;
  const entry = live.get(session.id);
  if (!entry) return;
  if (isAgent(session)) {
    ensureChat(entry, session);
    if (session.state === 'stopped') startSession(session.id);
  } else {
    ensureTerminal(entry, session);
    if (session.state === 'stopped') startSession(session.id);
    if (entry.fit) {
      try { entry.fit.fit(); } catch {}
      if (entry.socket?.readyState === WebSocket.OPEN) {
        entry.socket.send(JSON.stringify({ type: 'resize', __ctl: true, cols: entry.term.cols, rows: entry.term.rows }));
      }
    }
  }
  // Remember last-used tool per folder for drawer navigation
  if (session.cwd) {
    localStorage.setItem(`webpty.lastTool.${session.cwd.toLowerCase()}`, session.tool);
  }
  // Remember last-active session globally so refresh returns to it
  localStorage.setItem('webpty.lastSessionId', session.id);
  // Close any open soft keyboard from the previous tab. Setting the suppress
  // flag tells the composer's blur handler NOT to auto-submit — only a user
  // dismissing the keyboard (e.g., iOS ▾ button) should send the draft.
  const focused = document.activeElement;
  if (focused && focused.classList?.contains('compose-input')) {
    focused._suppressBlurSubmit = true;
    focused.blur();
  }
  // Don't auto-focus the new tab's composer — it would just re-open the
  // keyboard we just dismissed. When there's no composer at all (no-input-bar
  // layout), focus the terminal so keys still reach the PTY instead of <body>.
  if (!entry.composeInput || entry.composeInput.offsetParent === null) {
    try { entry.term?.focus(); } catch {}
  }
}

async function loadConfig() { return (config = await api('/api/config')); }
async function loadProjects() { return (projects = await api('/api/projects')); }
async function loadSessionsRaw() { return (sessions = await api('/api/sessions')); }

async function refreshSessions() {
  const before = sessions.map((s) => s.id).join(',');
  await loadSessionsRaw();
  const after = sessions.map((s) => s.id).join(',');
  if (before !== after) rebuildTrack();
  else updatePageMeta();
}

function schedulePoll(delay = 3000) {
  clearTimeout(pollTimer);
  // Pause polling while the tab is hidden (low-footprint: backgrounded tabs
  // don't need 3s session refreshes) and back off on repeated failures.
  if (document.hidden) {
    pollTimer = setTimeout(schedulePoll, 15000); // 15s wake-check, cheap
    return;
  }
  pollTimer = setTimeout(async () => {
    try {
      await refreshSessions();
      pollFailures = 0;
    } catch (e) {
      console.error(e);
      pollFailures = (pollFailures || 0) + 1;
    }
    const base = pollFailures > 2 ? Math.min(30000, 3000 * 2 ** (pollFailures - 2)) : 3000;
    schedulePoll(base);
  }, delay);
}

function applyViewport() {
  // visualViewport.height excludes the on-screen keyboard area on iOS / Android,
  // unlike window.innerHeight which stays at the layout-viewport size.
  const vv = window.visualViewport;
  const h = vv?.height ?? window.innerHeight;
  document.documentElement.style.setProperty('--vvh', h + 'px');
  // Audit C2: iPad floating keyboards / toolbars offset the viewport —
  // reserve the offset as bottom padding so the compose bar stays visible.
  const offsetTop = vv?.offsetTop ?? 0;
  document.documentElement.style.setProperty('--vvt', offsetTop + 'px');
  // Only refit + resize when the height actually changed — the keyboard
  // raising/lowering fires many visualViewport resize events, and every one
  // would re-fit all terminals and spam the PTY with resize frames (each
  // triggering a TUI repaint), which is a big part of the "slow to respond
  // on mobile" problem.
  if (applyViewport._lastH !== h) {
    applyViewport._lastH = h;
    // Audit C2: keyboard raise/lower — snap the active chat page to the
    // bottom so the newest message stays visible above the keyboard.
    const active = sessions[activeIndex];
    const activeId = active ? active.id : null;
    const activeEntry = activeId ? live.get(activeId) : null;
    if (activeEntry && activeEntry.kind === 'chat' && activeEntry.scrollEl) {
      activeEntry.scrollEl.scrollTop = activeEntry.scrollEl.scrollHeight;
    }
    // Only the ACTIVE session is refit + resized immediately (keyboard
    // raise/lower). Backgrounded sessions' TUIs must not repaint per frame;
    // they'll fit when activated.
    for (const [id, entry] of live) {
      if (id !== activeId) continue;
      if (!entry.term) continue;
      try { entry.fit.fit(); } catch {}
      if (entry.socket?.readyState === WebSocket.OPEN) {
        entry.socket.send(JSON.stringify({ type: 'resize', __ctl: true, cols: entry.term.cols, rows: entry.term.rows }));
      }
    }
    scrollToIndex(activeIndex, false);
  }
}

// Returning to the tab: refresh sessions immediately instead of waiting for
// the next (possibly 15s-delayed) poll tick.
document.addEventListener('visibilitychange', () => {
  if (!document.hidden) {
    pollFailures = 0;
    schedulePoll(0);
  }
});

// Set --vvh immediately (not only after bootstrap) so the layout has the
// correct viewport height even if a later await stalls.
try { applyViewport(); } catch {}

window.addEventListener('resize', applyViewport);
if (window.visualViewport) {  window.visualViewport.addEventListener('resize', applyViewport);
  window.visualViewport.addEventListener('scroll', applyViewport);
}
applyViewport();

// Debug handles (consumed by .playwright-*.mjs tests; harmless in prod)
window.__webpty = {
  get sessions() { return sessions; },
  get live() { return live; },
  get activeIndex() { return activeIndex; },
  get projects() { return projects; }
};

// Full bootstrap: fetch config/projects/sessions in parallel with the font
// preload so first paint isn't serialized behind three sequential fetches.
async function bootstrap() {
  const fontP = (async () => {
    if (!document.fonts?.load) return;
    try {
      await Promise.race([
        Promise.all([
          document.fonts.load('15px "D2Coding"'),
          document.fonts.load('bold 15px "D2Coding"')
        ]),
        // Never let a slow/missing font block the first fit + viewport —
        // the fallback font stack renders fine while D2Coding loads.
        new Promise((res) => setTimeout(res, 1500))
      ]);
    } catch {}
  })();
  const [cfg, proj, sess] = await Promise.all([loadConfig(), loadProjects(), loadSessionsRaw()]);
  config = cfg;
  projects = proj;
  sessions = sess;
  rebuildTrack();
  setupTabDrag();
  schedulePoll();
  await fontP; // let the metrics catch up before the first fit
  requestAnimationFrame(applyViewport);
}

(async () => {
  try {
    await bootstrap();
  } catch (e) {
    if (e.message === 'forbidden') {
      // Token gate is on and we have no (valid) token — prompt for it.
      showTokenGate();
    } else {
      alert(e.message);
    }
  }
})();

// ---- Global error visibility (diagnostics) ----
// Surface any uncaught runtime error on the page so problems are visible
// instead of silently breaking input/rendering.
window.addEventListener('error', (ev) => {
  showFatal(ev.error || ev.message || 'unknown error');
});
window.addEventListener('unhandledrejection', (ev) => {
  showFatal((ev.reason && (ev.reason.message || ev.reason)) || 'unhandled rejection');
});
function showFatal(msg) {
  let el = document.getElementById('webpty-fatal');
  if (!el) {
    el = document.createElement('div');
    el.id = 'webpty-fatal';
    el.style.cssText = 'position:fixed;left:8px;bottom:8px;right:8px;z-index:99999;'
      + 'background:#2a0f0f;color:#ff8a8a;font:12px/1.5 monospace;'
      + 'padding:8px 10px;border:1px solid #a33;border-radius:6px;'
      + 'white-space:pre-wrap;word-break:break-all;max-height:40vh;overflow:auto;';
    const close = document.createElement('button');
    close.textContent = '× 清空';
    close.style.cssText = 'position:absolute;top:4px;right:6px;background:none;'
      + 'border:none;color:#ff8a8a;font-size:12px;cursor:pointer;';
    close.onclick = () => { el.innerHTML = ''; el.hidden = true; };
    el.appendChild(close);
    document.body.appendChild(el);
    el.hidden = false;
  }
  const text = '⚠ ' + String(msg).slice(0, 500);
  // Audit 6.1: dedupe identical messages (merge into a counter), cap at 50
  // lines so an error storm can't flood the bar, and the × button clears.
  const existing = el.querySelectorAll('.fatal-line');
  for (const line of existing) {
    if (line.dataset.msg === text) {
      const n = (parseInt(line.dataset.count, 10) || 1) + 1;
      line.dataset.count = String(n);
      line.textContent = `${text} (×${n})`;
      return;
    }
  }
  if (existing.length >= 50) {
    el.removeChild(el.querySelector('.fatal-line')); // drop oldest
  }
  const line = document.createElement('div');
  line.className = 'fatal-line';
  line.dataset.msg = text;
  line.dataset.count = '1';
  line.textContent = text;
  el.appendChild(line);
  el.hidden = false;
}

// ---- Notification center panel (ext) ----
function openNotifyPanel() {
  closeMenu();
  notifyBackdrop.hidden = false;
  refreshNotifyPanel();
}
async function refreshNotifyPanel() {
  notifyPage = 1;
  const [rules, msgs] = await Promise.all([
    api('/api/notify/rules').catch(() => ({ rules: [] })),
    api('/api/notify/messages?page=1').catch(() => ({ items: [] })),
  ]);
  const ruleList = rules.rules || [];
  const msgList = msgs.items || [];
  document.getElementById('notify-rules-count').textContent = ruleList.length;
  document.getElementById('notify-messages-count').textContent = msgs.total ?? msgList.length;
  notifyRules.innerHTML = ruleList.map((r) =>
    `<div class="panel-item">
       <span class="dot" style="background:${r.enabled ? 'var(--accent)' : 'var(--dot)'}"></span>
       <div class="item-main">
         <div class="item-title">${esc(r.name)} <span class="badge">${esc(r.event_type)}</span></div>
         <div class="item-sub">level: ${esc(r.level)} · action: ${esc(r.action)}
           ${r.quiet_start ? ` · 静默 ${esc(r.quiet_start)}–${esc(r.quiet_end)}` : ''}
           ${r.enabled ? '' : ' · <span style="color:var(--muted)">已停用</span>'}</div>
       </div>
     </div>`).join('') ||
    `<div class="empty-tip">暂无规则 — 点击「添加规则」创建第一条通知规则</div>`;
  notifyMessages.innerHTML = msgList.map((m) => {
    const t = new Date((m.ts || 0) * 1000);
    const when = isNaN(t) ? '' : t.toLocaleString('zh-CN', { hour12: false });
    const lvlCls = m.level === 'critical' ? 'err' : (m.level === 'warn' ? 'warn' : 'ok');
    return `<div class="panel-item ${m.level === 'critical' ? 'critical' : (m.level === 'warn' ? 'warn' : '')}">
      <span class="dot" style="background:${m.level === 'critical' ? 'var(--danger)' : (m.level === 'warn' ? '#d29922' : 'var(--accent)')}"></span>
      <div class="item-main">
        <div class="item-title">${esc(m.title)} <span class="badge ${lvlCls}">${esc(m.level)}</span></div>
        <div class="item-sub">${esc(m.tool || '')} ${esc(m.project || '')} · ${when} · ${m.delivered ? '已发送' : '待重试'}</div>
      </div>
    </div>`;
  }).join('') ||
    `<div class="empty-tip">暂无消息 — 会话事件触发后将显示在这里</div>`;
  // Audit F1: pagination — the API pages at 20; show a load-more button
  // when more pages exist.
  const total = msgs.total ?? msgList.length;
  const hasMore = total > (notifyPage * 20);
  const moreEl = document.getElementById('notify-load-more');
  if (moreEl) moreEl.hidden = !hasMore;
}
let notifyPage = 1;
async function loadMoreNotify() {
  notifyPage += 1;
  try {
    const msgs = await api(`/api/notify/messages?page=${notifyPage}`).catch(() => ({ items: [] }));
    const items = msgs.items || [];
    const html = items.map((m) => {
      const t = new Date((m.ts || 0) * 1000);
      const when = isNaN(t) ? '' : t.toLocaleString('zh-CN', { hour12: false });
      const lvlCls = m.level === 'critical' ? 'err' : (m.level === 'warn' ? 'warn' : 'ok');
      return `<div class="panel-item ${m.level === 'critical' ? 'critical' : (m.level === 'warn' ? 'warn' : '')}">
        <span class="dot" style="background:${m.level === 'critical' ? 'var(--danger)' : (m.level === 'warn' ? '#d29922' : 'var(--accent)')}"></span>
        <div class="item-main">
          <div class="item-title">${esc(m.title)} <span class="badge ${lvlCls}">${esc(m.level)}</span></div>
          <div class="item-sub">${esc(m.tool || '')} ${esc(m.project || '')} · ${when} · ${m.delivered ? '已发送' : '待重试'}</div>
        </div>
      </div>`;
    }).join('');
    notifyMessages.insertAdjacentHTML('beforeend', html);
    const total = msgs.total ?? (notifyPage * 20);
    const moreEl = document.getElementById('notify-load-more');
    if (moreEl) moreEl.hidden = !(total > (notifyPage * 20));
  } catch (e) {
    notifyPage -= 1;
  }
}
document.getElementById('notify-load-more').onclick = loadMoreNotify;
notifyBackdrop.addEventListener('click', (ev) => {
  if (ev.target === notifyBackdrop) notifyBackdrop.hidden = true;
});
document.getElementById('notify-rule-add').onclick = async () => {
  try {
    const type = document.getElementById('notify-rule-type').value;
    // Audit 3.1: matcher_json is now editable in the UI; validate it as
    // an object before sending (the server also tolerates bad JSON).
    const matcherRaw = document.getElementById('notify-rule-matcher').value.trim();
    let matcher = '{}';
    if (matcherRaw) {
      const parsed = JSON.parse(matcherRaw);
      if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
        throw new Error('matcher 必须是 JSON 对象');
      }
      matcher = JSON.stringify(parsed);
    }
    await api('/api/notify/rules', { method: 'POST', body: JSON.stringify({
      name: 'rule-' + Date.now(), event_type: type, matcher_json: matcher,
      action: 'email', level: 'warn', quiet_start: '', quiet_end: '', enabled: 1 }) });
    refreshNotifyPanel();
  } catch (e) {
    alert(e.message);
  }
};
document.getElementById('notify-test').onclick = async () => {
  try {
    const r = await api('/api/notify/test', { method: 'POST' });
    alert(r.ok ? '测试邮件已发送' : 'SMTP 未配置');
  } catch (e) {
    alert(e.message);
  }
};

// ---- Cost dashboard panel (ext) ----
function openCostPanel() {
  closeMenu();
  costBackdrop.hidden = false;
  refreshCostPanel();
}
async function refreshCostPanel() {
  const period = document.getElementById('cost-period').value;
  const [sum, byTool, alerts] = await Promise.all([
    api(`/api/cost/summary?period=${period}`).catch(() => ({})),
    api(`/api/cost/by-tool?period=${period}`).catch(() => []),
    api('/api/cost/alerts').catch(() => []),
  ]);
  const alertsArr = Array.isArray(alerts) ? alerts : [];
  const over = alertsArr.some((a) => a.active);
  document.getElementById('cost-cards').innerHTML =
    `<div class="cost-card${over ? ' over' : ''}"><div class="v">$${Number(sum.cost || 0).toFixed(4)}</div><div class="l">实际成本(工具上报)</div></div>` +
    `<div class="cost-card"><div class="v">$${Number(sum.estimated || 0).toFixed(4)}</div><div class="l">估算(未上报时)</div></div>` +
    `<div class="cost-card"><div class="v">${esc(sum.tokens_in ?? 0)}</div><div class="l">输入 tokens</div></div>` +
    `<div class="cost-card"><div class="v">${esc(sum.tokens_out ?? 0)}</div><div class="l">输出 tokens</div></div>` +
    `<div class="cost-card${over ? ' over' : ''}"><div class="v">${over ? '超限' : '正常'}</div><div class="l">预算状态</div></div>`;
  document.getElementById('cost-groups').innerHTML =
    ((byTool || []).map((g) =>
      `<div class="panel-item">
         <span class="dot" style="background:var(--tool-codex)"></span>
         <div class="item-main">
           <div class="item-title">${esc(g.name)} <span class="badge">$${Number(g.cost || 0).toFixed(4)}</span></div>
           <div class="item-sub">${esc(g.tokens_in ?? 0)} in / ${esc(g.tokens_out ?? 0)} out</div>
         </div>
       </div>`).join('') ||
    `<div class="empty-tip">暂无数据 — Agent 使用后成本将实时统计</div>`);
}
document.getElementById('cost-close').onclick = () => { costBackdrop.hidden = true; };
costBackdrop.addEventListener('click', (ev) => {
  if (ev.target === costBackdrop) costBackdrop.hidden = true;
});
document.getElementById('cost-period').onchange = refreshCostPanel;
document.getElementById('cost-budget-set').onclick = async () => {
  const n = parseFloat(document.getElementById('cost-budget').value || '0');
  if (!Number.isFinite(n)) { alert('请输入有效预算'); return; }
  try {
    await api('/api/cost/budget', { method: 'PUT', body: JSON.stringify({ limit: n }) });
    refreshCostPanel();
  } catch (e) {
    alert(e.message);
  }
};
document.getElementById('cost-reconcile').onclick = async () => {
  try {
    const r = await api('/api/cost/reconcile', { method: 'POST' });
    alert(`日志校对完成，补录 ${r?.added ?? 0} 条`);
    refreshCostPanel();
  } catch (e) {
    alert(e.message);
  }
};

// Audit 4.1: CSV export of the current period (uses the active period
// selector; download via <a download> like migrate packages).
document.getElementById('cost-export').onclick = async () => {
  try {
    const period = document.getElementById('cost-period').value;
    const res = await fetch(`/api/cost/export?period=${period}`);
    if (!res.ok) throw new Error((await res.json()).error || res.status);
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `webpty-cost-${period}.csv`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 5000);
  } catch (e) {
    alert('导出失败: ' + e.message);
  }
};

// ---- Backup management panel (ext) ----
function openBackupPanel() {
  closeMenu();
  backupBackdrop.hidden = false;
  refreshBackupPanel();
}
async function refreshBackupPanel() {
  const r = await api('/api/backup/list').catch(() => ({ backups: [] }));
  const list = r.backups || [];
  document.getElementById('backup-list-count').textContent = list.length;
  document.getElementById('backup-list').innerHTML = list.map((b) => {
    const t = new Date((b.created_at || 0) * 1000);
    const when = isNaN(t) ? '' : t.toLocaleString('zh-CN', { hour12: false });
    return `<div class="panel-item">
      <span class="dot" style="background:${b.kind === 'migrate-export' ? '#d29922' : 'var(--accent)'}"></span>
      <div class="item-main">
        <div class="item-title">${esc(b.filename)} ${b.encrypted ? '<span class="badge warn">加密</span>' : ''}${b.kind === 'migrate-export' ? '<span class="badge warn">迁移包</span>' : ''}</div>
        <div class="item-sub">${esc((Number(b.size_bytes || 0) / 1024).toFixed(1))}KB · ${when} · SHA256 ${esc((b.sha256 || '').slice(0, 10))}…</div>
      </div>
      <div class="item-side">
        ${b.kind === 'migrate-export' ? '' : `<button class="btn sm" data-restore="${esc(b.id)}" type="button">恢复</button>`}
      </div>
    </div>`;
  }).join('') ||
    `<div class="empty-tip">暂无备份 — 点击「立即备份」创建第一个快照（默认每 24h 自动备份）</div>`;
  // Re-bind restore handlers after every refresh (innerHTML replaced above).
  document.querySelectorAll('#backup-list [data-restore]').forEach((btn) => {
    btn.onclick = async () => {
      if (!confirm('恢复该备份将覆盖当前配置，继续？')) return;
      try {
        const r = await api(`/api/backup/restore/${btn.dataset.restore}`, { method: 'POST' });
        alert(r.ok ? '恢复成功' : '恢复失败: ' + r.message);
      } catch (e) {
        alert('恢复失败: ' + e.message);
      }
      refreshBackupPanel();
    };
  });
}
document.getElementById('backup-close').onclick = () => { backupBackdrop.hidden = true; };
backupBackdrop.addEventListener('click', (ev) => {
  if (ev.target === backupBackdrop) backupBackdrop.hidden = true;
});
document.getElementById('backup-create').onclick = async () => {
  try {
    await api('/api/backup/create', { method: 'POST' });
    alert('备份已创建');
  } catch (e) {
    alert('备份失败: ' + e.message);
  }
  refreshBackupPanel();
};

// ---- Migration wizard panel (ext) ----
function openMigratePanel() {
  closeMenu();
  migrateBackdrop.hidden = false;
}
document.getElementById('migrate-close').onclick = () => { migrateBackdrop.hidden = true; };
migrateBackdrop.addEventListener('click', (ev) => {
  if (ev.target === migrateBackdrop) migrateBackdrop.hidden = true;
});
document.getElementById('migrate-export').onclick = async () => {
  try {
    const r = await api('/api/migrate/export', { method: 'POST' });
    const a = document.createElement('a');
    a.href = `/api/migrate/download/${encodeURIComponent(r.filename)}`;
    a.download = r.filename;
    a.click();
    document.getElementById('migrate-result').innerHTML = `<p>已导出 ${esc(r.filename)}，开始下载</p>`;
  } catch (e) {
    alert('导出失败: ' + e.message);
  }
};
document.getElementById('migrate-do').onclick = async () => {
  const file = document.getElementById('migrate-file').files[0];
  if (!file) { alert('请先选择 .tar.gz 包'); return; }
  const mode = document.getElementById('migrate-mode').value;
  const fd = new FormData();
  fd.append('file', file);
  fd.append('mode', mode);
  try {
    const res = await fetch('/api/migrate/import', {
      method: 'POST', body: fd,
      headers: { authorization: `Bearer ${localStorage.getItem('webpty.token') || ''}` },
    });
    const out = await res.json().catch(() => ({}));
    const statusOk = out.status === 'done';
    const badgeCls = out.status === 'error' ? 'err' : (out.status === 'dry-run' ? 'warn' : 'ok');
    document.getElementById('migrate-result').innerHTML =
      `<div class="panel-item ${out.status === 'error' ? 'critical' : ''}">
         <span class="dot" style="background:${statusOk ? 'var(--accent)' : (out.status === 'error' ? 'var(--danger)' : '#d29922')}"></span>
         <div class="item-main">
           <div class="item-title">导入 <span class="badge ${badgeCls}">${esc(out.status)}</span></div>
           <div class="item-sub">${esc(out.message || '')} · 模式 ${esc(mode)}</div>
         </div>
       </div>` +
      (out.changes && typeof out.changes === 'object' && Object.keys(out.changes).length
        ? `<div class="result-pre">${esc(JSON.stringify(out.changes, null, 2))}</div>` : '');
  } catch (e) {
    alert('导入失败: ' + e.message);
  }
};

// ---- Agent management panel (ext) ----
const agentsBackdrop = document.getElementById('agents-backdrop');
const agentsList = document.getElementById('agents-list');

function openAgentsPanel() {
  closeMenu();
  agentsBackdrop.hidden = false;
  refreshAgentsPanel();
}

const AGENT_TOOL_COLORS = {
  claude: 'var(--tool-claude)', 'claude-chat': 'var(--tool-claude)',
  codex: 'var(--tool-codex)', reasonix: 'var(--tool-accent, var(--accent))',
  opencode: 'var(--tool-accent, var(--accent))', agy: 'var(--tool-agy)',
  powershell: 'var(--tool-powershell)', bash: 'var(--dot)',
};

async function refreshAgentsPanel() {
  const cfg = await api('/api/config').catch(() => ({ tools: {} }));
  // Keep the module-level config in sync: the session menu's tool list and
  // the "new session" page read config.tools. Without this, edits made here
  // (add/disable/change) wouldn't show up there until a full page reload.
  config = cfg;
  const tools = cfg.tools || {};
  const providers = cfg.providers || {};
  const names = Object.keys(tools);
  document.getElementById('agents-count').textContent = names.length;
  agentsList.innerHTML = names.map((name) => {
    const t = tools[name] || {};
    const color = AGENT_TOOL_COLORS[name] || 'var(--accent)';
    const isAgent = t.engine === 'agent' || name === 'claude-chat';
    const prov = t.provider ? providers[t.provider] : null;
    const effectiveUrl = t.apiBaseUrl || prov?.baseUrl || '';
    const hasKey = !!(t.apiKey || prov?.apiKey);
    return `<div class="panel-item agent-row" data-agent="${esc(name)}">
      <span class="dot" style="background:${color}"></span>
      <div class="item-main">
        <div class="item-title">${esc(TOOL_LABEL[name] || name)}
          <span class="badge ${isAgent ? 'ok' : ''}">${isAgent ? 'agent' : 'pty'}</span>
          ${t.provider ? `<span class="badge ok">${esc(t.provider)}</span>` : ''}
          ${hasKey ? '<span class="badge">🔑</span>' : ''}</div>
        <div class="item-sub agent-cmd">${esc(t.command || name)} ${esc(t.defaultArgs || '')}</div>
        <div class="item-sub">${effectiveUrl ? esc(effectiveUrl) : ''}${hasKey ? ' · 密钥已配置' : ''}</div>
        <div class="agent-edit">
          <div class="row">
            <label>command <input class="inp" data-field="command" value="${esc(t.command || '')}"></label>
            <label>defaultArgs <input class="inp" style="min-width:200px" data-field="defaultArgs" value="${esc(t.defaultArgs || '')}"></label>
            <label>nameFlag <input class="inp" data-field="nameFlag" placeholder="(无)" value="${esc(t.nameFlag || '')}"></label>
          </div>
          <div class="row">
            <label>engine
              <select class="sel" data-field="engine">
                <option value="">pty</option>
                <option value="agent" ${t.engine === 'agent' ? 'selected' : ''}>agent</option>
              </select>
            </label>
            <label>permissionMode <input class="inp" data-field="permissionMode" placeholder="(默认)" value="${esc(t.permissionMode || '')}"></label>
            <label>label <input class="inp" data-field="label" placeholder="(默认名)" value="${esc(t.label || '')}"></label>
          </div>
          <div class="row">
            <label>provider
              <select class="sel" data-field="provider">
                ${Object.keys(providers).map((p) =>
                  `<option value="${esc(p)}" ${t.provider === p ? 'selected' : ''}>${esc(p)}</option>`).join('')}
                <option value="" ${!t.provider ? 'selected' : ''}>(无)</option>
              </select>
            </label>
            <label>apiBaseUrl <input class="inp" style="min-width:180px" data-field="apiBaseUrl" placeholder="(用预设)" value="${esc(t.apiBaseUrl || '')}"></label>
            <label>apiKey <input class="inp" type="password" style="min-width:160px" data-field="apiKey" placeholder="${hasKey ? '已配置（留空不改）' : '(用预设或填)'}" value=""></label>
          </div>
          <div class="row">
            <button class="btn sm primary" data-act="save">保存</button>
            <button class="btn sm" data-act="cancel">取消</button>
          </div>
        </div>
      </div>
      <div class="item-side agent-actions">
        <label class="field">供应商
          <select class="sel" data-act="provider-switch" style="max-width:110px">
            <option value="">(无)</option>
            ${Object.keys(providers).map((p) =>
              `<option value="${esc(p)}" ${t.provider === p ? 'selected' : ''}>${esc(p)}</option>`).join('')}
          </select>
        </label>
        <button class="btn sm" data-act="edit" type="button">编辑</button>
        <button class="btn sm danger" data-act="disable" type="button">禁用</button>
      </div>
    </div>`;
  }).join('') ||
    `<div class="empty-tip">没有可用 Agent — 在顶部输入工具名与命令添加</div>`;

  // ---- row actions (re-bound after every refresh) ----
  agentsList.querySelectorAll('.agent-row').forEach((row) => {
    const name = row.dataset.agent;
    row.querySelector('[data-act="edit"]').onclick = () => {
      row.classList.add('editing');
    };
    row.querySelector('[data-act="cancel"]').onclick = () => {
      row.classList.remove('editing');
    };
    row.querySelector('[data-act="save"]').onclick = async () => {
      const patch = {};
      row.querySelectorAll('[data-field]').forEach((el) => {
        const f = el.dataset.field;
        const v = f === 'engine' ? el.value : el.value.trim();
        if (f === 'apiKey') {
          // empty OR masked (****...) = keep the existing key (don't wipe it)
          if (v !== '' && !v.startsWith('****')) patch[f] = v;
          return;
        }
        if (f === 'nameFlag' || f === 'provider') {
          // empty → no flag / no provider preset
          patch[f] = v === '' ? null : v;
        } else if (f === 'engine') {
          if (v !== '') patch[f] = v;
        } else if (v === '') {
          patch[f] = null; // clear optional fields instead of storing ''
        } else {
          patch[f] = v;
        }
      });
      try {
        await api('/api/config/tools', {
          method: 'PUT', body: JSON.stringify({ tools: { [name]: patch } }) });
        row.classList.remove('editing');
        refreshAgentsPanel();
      } catch (e) {
        alert('保存失败: ' + e.message);
      }
    };
    // Quick provider switch: selecting a preset in the row header saves
    // tool.provider immediately (and clears per-tool apiBaseUrl/apiKey so
    // the preset's values take effect).
    const pswitch = row.querySelector('[data-act="provider-switch"]');
    if (pswitch) {
      pswitch.onchange = async () => {
        const p = pswitch.value;
        try {
          await api('/api/config/tools', {
            method: 'PUT', body: JSON.stringify({
              tools: { [name]: { provider: p === '' ? null : p,
                                 apiBaseUrl: null, apiKey: null } } }) });
          refreshAgentsPanel();
        } catch (e) {
          alert('切换失败: ' + e.message);
        }
      };
    }
    row.querySelector('[data-act="disable"]').onclick = async () => {
      if (!confirm(`禁用 Agent「${name}」？（可从配置重新启用）`)) return;
      try {
        await api('/api/config/tools', {
          method: 'PUT', body: JSON.stringify({ tools: { [name]: null } }) });
        refreshAgentsPanel();
      } catch (e) {
        alert('禁用失败: ' + e.message);
      }
    };
  });
}

document.getElementById('agents-close').onclick = () => { agentsBackdrop.hidden = true; };
agentsBackdrop.addEventListener('click', (ev) => {
  if (ev.target === agentsBackdrop) agentsBackdrop.hidden = true;
});
document.getElementById('agents-new-add').onclick = async () => {
  const name = document.getElementById('agents-new-name').value.trim();
  const cmd = document.getElementById('agents-new-cmd').value.trim();
  if (!name || !cmd) { alert('请填写工具名与命令'); return; }
  try {
    await api('/api/config/tools', {
      method: 'PUT', body: JSON.stringify({
        tools: { [name]: { command: cmd, defaultArgs: '', nameFlag: null } } }) });
    document.getElementById('agents-new-name').value = '';
    document.getElementById('agents-new-cmd').value = '';
    refreshAgentsPanel();
  } catch (e) {
    alert('添加失败: ' + e.message);
  }
};

// ---- Provider presets panel (ext) ----
const providersBackdrop = document.getElementById('providers-backdrop');
const providersList = document.getElementById('providers-list');

function openProvidersPanel() {
  providersBackdrop.hidden = false;
  refreshProvidersPanel();
}

async function refreshProvidersPanel() {
  const cfg = await api('/api/config').catch(() => ({ providers: {} }));
  config = cfg; // keep module-level in sync (menu tool list reads tools)
  const providers = cfg.providers || {};
  const names = Object.keys(providers);
  document.getElementById('providers-count').textContent = names.length;
  providersList.innerHTML = names.map((name) => {
    const p = providers[name] || {};
    const models = Array.isArray(p.models) ? p.models.join(', ') : '';
    return `<div class="panel-item provider-row" data-provider="${esc(name)}">
      <span class="dot" style="background:var(--accent)"></span>
      <div class="item-main">
        <div class="item-title">${esc(name)} ${p.apiKey ? '<span class="badge ok">🔑</span>' : '<span class="badge warn">无密钥</span>'}</div>
        <div class="item-sub agent-cmd">${esc(p.baseUrl || '')} ${models ? '· ' + esc(models) : ''}</div>
        <div class="provider-edit">
          <div class="row">
            <label>baseUrl <input class="inp" style="min-width:240px" data-pfield="baseUrl" value="${esc(p.baseUrl || '')}"></label>
            <label>apiKey <input class="inp" type="password" style="min-width:160px" data-pfield="apiKey" placeholder="${p.apiKey ? '已配置（留空不改）' : '(填密钥)'}" value=""></label>
          </div>
          <div class="row">
            <label>models <input class="inp" style="min-width:280px" data-pfield="models" placeholder="逗号分隔，如 gpt-5.4,o4-mini" value="${esc(models)}"></label>
          </div>
          <div class="row">
            <button class="btn sm primary" data-pact="save">保存</button>
            <button class="btn sm" data-pact="cancel">取消</button>
          </div>
        </div>
      </div>
      <div class="item-side agent-actions">
        <button class="btn sm" data-pact="edit" type="button">编辑</button>
        <button class="btn sm danger" data-pact="delete" type="button">删除</button>
      </div>
    </div>`;
  }).join('') ||
    `<div class="empty-tip">暂无供应商预设 — 点击「添加」创建</div>`;

  providersList.querySelectorAll('.provider-row').forEach((row) => {
    const name = row.dataset.provider;
    row.querySelector('[data-pact="edit"]').onclick = () => row.classList.add('editing');
    row.querySelector('[data-pact="cancel"]').onclick = () => row.classList.remove('editing');
    row.querySelector('[data-pact="save"]').onclick = async () => {
      const patch = {};
      row.querySelectorAll('[data-pfield]').forEach((el) => {
        const f = el.dataset.pfield;
        const v = el.value.trim();
        if (f === 'apiKey') {
          // empty or masked (****...) = keep existing key
          if (v !== '' && !v.startsWith('****')) patch[f] = v;
        } else if (f === 'models') {
          patch[f] = v ? v.split(',').map((s) => s.trim()).filter(Boolean) : [];
        } else {
          patch[f] = v;
        }
      });
      try {
        await api('/api/config/providers', {
          method: 'PUT', body: JSON.stringify({ providers: { [name]: patch } }) });
        row.classList.remove('editing');
        refreshProvidersPanel();
        refreshAgentsPanel(); // provider badges in agent list change too
      } catch (e) {
        alert('保存失败: ' + e.message);
      }
    };
    row.querySelector('[data-pact="delete"]').onclick = async () => {
      if (!confirm(`删除供应商预设「${name}」？（引用它的 Agent 将回到无预设）`)) return;
      try {
        await api('/api/config/providers', {
          method: 'PUT', body: JSON.stringify({ providers: { [name]: null } }) });
        refreshProvidersPanel();
        refreshAgentsPanel();
      } catch (e) {
        alert('删除失败: ' + e.message);
      }
    };
  });
}

document.getElementById('providers-close').onclick = () => { providersBackdrop.hidden = true; };
providersBackdrop.addEventListener('click', (ev) => {
  if (ev.target === providersBackdrop) providersBackdrop.hidden = true;
});
document.getElementById('providers-new-add').onclick = async () => {
  const name = document.getElementById('providers-new-name').value.trim();
  if (!name) { alert('请填写预设名'); return; }
  try {
    await api('/api/config/providers', {
      method: 'PUT', body: JSON.stringify({
        providers: { [name]: { baseUrl: '', apiKey: '', models: [] } } }) });
    document.getElementById('providers-new-name').value = '';
    refreshProvidersPanel();
    refreshAgentsPanel();
  } catch (e) {
    alert('添加失败: ' + e.message);
  }
};
document.getElementById('agents-providers-btn').onclick = openProvidersPanel;

// ---- Agent native config files panel (ext) ----
// Reads & precisely edits each agent CLI's own config (codex config.toml,
// claude settings.json, reasonix config.toml, ...) via /api/agent-config/*.
const acfgBackdrop = document.getElementById('acfg-backdrop');
const acfgTool = document.getElementById('acfg-tool');
const acfgPath = document.getElementById('acfg-path');
const acfgFields = document.getElementById('acfg-fields');
const acfgRaw = document.getElementById('acfg-raw');

// 每工具可编辑字段 → 显示名/类型/占位
const ACFG_FIELD_META = {
  model: { label: '模型 model', ph: 'gpt-5.4 / deepseek-v4-flash' },
  base_url: { label: 'API 地址 base_url', ph: 'https://api.example.com/v1' },
  api_key: { label: 'API 密钥 api_key', ph: 'sk-...（留空不改）', secret: true },
  language: { label: '语言 language', ph: 'zh / en' },
  effort: { label: '推理强度 effort', ph: 'low / high / max' },
  theme: { label: '主题 theme', ph: 'dark / light' },
  model_provider: { label: '模型供应商 model_provider', ph: 'openai / anthropic' },
  temperature: { label: '温度 temperature', ph: '0.0 - 1.0' },
  proxy: { label: '代理 proxy', ph: 'http://host:port' },
};

let acfgList = null;

async function openAcfgPanel() {
  acfgBackdrop.hidden = false;
  const r = await api('/api/agent-config/list').catch(() => ({ tools: {} }));
  acfgList = r.tools || {};
  acfgTool.innerHTML = Object.keys(acfgList).map((t) => {
    const info = acfgList[t];
    const state = info.exists ? (info.editable ? '' : '（只读）') : '（无配置）';
    return `<option value="${esc(t)}">${esc(t)} ${state}</option>`;
  }).join('');
  acfgTool.onchange = loadAcfgTool;
  loadAcfgTool();
}

async function loadAcfgTool() {
  const tool = acfgTool.value;
  const info = acfgList[tool] || {};
  acfgPath.textContent = info.exists ? (info.path || '') : '未找到配置文件';
  acfgRaw.hidden = true;
  if (!info.exists) {
    acfgFields.innerHTML = `<div class="empty-tip">该工具暂无配置文件（webpty 只在发现文件时展示）</div>`;
    return;
  }
  const r = await api(`/api/agent-config/read?tool=${encodeURIComponent(tool)}`).catch(() => ({}));
  const content = r.content || '';
  acfgRaw.textContent = content;
  const fields = await deriveAcfgFields(tool, content);
  renderAcfgFields(tool, fields, content);
}

// 从原文提取当前值（TOML 行级 / JSON 扁平）用于表单回显
async function deriveAcfgFields(tool, content) {
  const isToml = /^[a-zA-Z_]+\s*=/.test(content.trim().split('\n')[0] || '');
  const out = {};
  if (isToml) {
    for (const line of content.split('\n')) {
      const m = line.match(/^([a-zA-Z_]+)\s*=\s*"([^"]*)"/);
      if (m) out[m[1]] = m[2];
    }
  } else {
    try {
      const obj = JSON.parse(content);
      const flat = {};
      const walk = (o, prefix) => {
        if (o && typeof o === 'object') {
          for (const [k, v] of Object.entries(o)) {
            if (v !== null && typeof v === 'object') walk(v, prefix + k + '.');
            else flat[prefix + k] = v;
          }
        }
      };
      walk(obj, '');
      // 映射 claude 的 env 键为通用字段名
      if (flat['env.ANTHROPIC_BASE_URL'] !== undefined) out.base_url = flat['env.ANTHROPIC_BASE_URL'];
      if (flat['env.ANTHROPIC_AUTH_TOKEN'] !== undefined) out.api_key = flat['env.ANTHROPIC_AUTH_TOKEN'];
      if (flat['theme'] !== undefined) out.theme = flat['theme'];
    } catch {}
  }
  return out;
}

function renderAcfgFields(tool, values, content) {
  const editable = (acfgList[tool] || {}).editable;
  if (!editable) {
    acfgFields.innerHTML = `<div class="empty-tip">该工具配置当前为只读（YAML 等格式暂不支持编辑）</div>`;
    return;
  }
  const keys = Object.keys(ACFG_FIELD_META).filter((k) => k !== 'theme' || values.theme !== undefined || tool === 'claude');
  acfgFields.innerHTML = keys.map((k) => {
    const meta = ACFG_FIELD_META[k];
    const cur = values[k];
    const secret = meta.secret && cur;
    return `<div class="panel-item">
      <span class="dot" style="background:var(--accent)"></span>
      <div class="item-main">
        <div class="item-title">${meta.label} ${cur !== undefined ? `<span class="badge">${secret ? '已配置' : esc(String(cur))}</span>` : '<span class="badge warn">未设置</span>'}</div>
        <div class="row" style="margin-top:6px">
          <input class="inp" data-akey="${k}" type="${meta.secret ? 'password' : 'text'}"
            placeholder="${meta.secret && cur ? '已配置（留空不改）' : meta.ph}" value="">
        </div>
      </div>
    </div>`;
  }).join('') +
  `<div class="panel-toolbar" style="margin-top:10px">
     <button id="acfg-save" class="btn primary" type="button">保存修改</button>
     <span class="muted" style="font-size:12px">只替换上面字段对应行/键，其余内容与注释原样保留</span>
   </div>`;
  const saveBtn = document.getElementById('acfg-save');
  if (saveBtn) {
    saveBtn.onclick = async () => {
      const values2 = {};
      acfgFields.querySelectorAll('[data-akey]').forEach((el) => {
        const v = el.value.trim();
        if (v !== '') values2[el.dataset.akey] = v;
      });
      if (!Object.keys(values2).length) { alert('没有输入任何修改'); return; }
      try {
        const r = await api('/api/agent-config/update', {
          method: 'PUT', body: JSON.stringify({ tool, values: values2 }) });
        alert(r.ok ? `已更新：${(r.changed || []).join(', ')}` : '更新失败: ' + (r.error || ''));
        loadAcfgTool();
      } catch (e) {
        alert('保存失败: ' + e.message);
      }
    };
  }
}

document.getElementById('acfg-close').onclick = () => { acfgBackdrop.hidden = true; };
acfgBackdrop.addEventListener('click', (ev) => {
  if (ev.target === acfgBackdrop) acfgBackdrop.hidden = true;
});
document.getElementById('acfg-toggle-raw').onclick = () => {
  acfgRaw.hidden = !acfgRaw.hidden;
};
document.getElementById('agents-acfg-btn').onclick = openAcfgPanel;
