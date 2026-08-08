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
  const res = await fetch(url, { ...opts, headers });
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
  requestAnimationFrame(() => tokenGateInput.focus());
}

async function unlockToken() {
  const token = tokenGateInput.value.trim();
  if (!token) return;
  tokenGateBtn.disabled = true;
  tokenGateErr.textContent = '验证中…';
  try {
    localStorage.setItem('webpty.token', token);
    const c = await api('/api/config');
    config = c;
    tokenGate.hidden = true;
    tokenGateBtn.disabled = false;
    await bootstrap();
  } catch (e) {
    localStorage.removeItem('webpty.token');
    tokenGateBtn.disabled = false;
    tokenGateErr.textContent = e.message.includes('forbidden') ? '令牌错误，请重试' : e.message;
  }
}

tokenGateBtn.onclick = unlockToken;
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

const TUI_TOOLS = new Set(['claude', 'codex', 'agy']);
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
}

function makeTerminal(session, host) {
  const isTUI = TUI_TOOLS.has(session.tool);
  const term = new Terminal({
    cursorBlink: !isTUI,
    cursorStyle: 'bar',
    convertEol: true,
    scrollback: 30000,
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
  // Unicode 11 width tables — better CJK / emoji cell-width agreement so
  // box-drawing tables line up.
  if (window.Unicode11Addon?.Unicode11Addon) {
    term.loadAddon(new Unicode11Addon.Unicode11Addon());
    try { term.unicode.activeVersion = '11'; } catch {}
  }
  // Make http(s) URLs in terminal output clickable (open in a new tab)
  if (window.WebLinksAddon?.WebLinksAddon) {
    term.loadAddon(new WebLinksAddon.WebLinksAddon((ev, uri) => window.open(uri, '_blank', 'noopener')));
  }
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
  if (window.CanvasAddon?.CanvasAddon) {
    try { term.loadAddon(new CanvasAddon.CanvasAddon()); } catch {}
  }
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
      for (const ch of text) {
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
  const token = localStorage.getItem('webpty.token');
  const q = token ? `?token=${encodeURIComponent(token)}` : '';
  const ws = new WebSocket(`${proto}//${location.host}/ws/sessions/${encodeURIComponent(session.id)}${q}`);
  ws.binaryType = 'arraybuffer';
  ws.onopen = () => {
    attempt = 0;
    ws.send(JSON.stringify({ type: 'resize', cols: entry.term.cols, rows: entry.term.rows }));
  };
  ws.onmessage = (event) => {
    if (typeof event.data === 'string' && event.data.startsWith('{')) {
      try {
        const msg = JSON.parse(event.data);
        if (msg.type === 'state') { applySessionState(msg.session); return; }
      } catch {}
    }
    if (event.data instanceof ArrayBuffer) entry.term.write(new Uint8Array(event.data));
    else entry.term.write(event.data);
  };
  ws.onclose = () => {
    if (entry.socket === ws) entry.socket = null;
    if (live.get(session.id) !== entry) return;
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
    entry.term.onData((data) => entry.composer.feed(data));
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

  const entry = { page, host, composeInput, composeSubmit, scrollBottomBtn, term: null, fit: null, socket: null };
  live.set(session.id, entry);

  // Jump-to-bottom button: tap to snap xterm scrollback to the latest line.
  // Don't steal focus from the terminal (mobile keyboard would dismiss).
  scrollBottomBtn.addEventListener('pointerdown', (ev) => ev.preventDefault());
  scrollBottomBtn.addEventListener('mousedown', (ev) => ev.preventDefault());
  scrollBottomBtn.addEventListener('click', (ev) => {
    ev.preventDefault();
    try { entry.term?.scrollToBottom(); } catch {}
  });

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

  const resizeCompose = () => {
    composeInput.style.height = 'auto';
    composeInput.style.height = Math.min(composeInput.scrollHeight, 140) + 'px';
  };
  composeInput.addEventListener('input', resizeCompose);

  const submit = () => {
    const text = composeInput.value.trim();
    if (!text) return;
    const payload = JSON.stringify({ type: 'user', text });
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
  entry.render = { curTextEl: null, curTextId: null, toolCards: new Map(), systemShown: false };
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
  const token = localStorage.getItem('webpty.token');
  const q = token ? `?token=${encodeURIComponent(token)}` : '';
  const ws = new WebSocket(`${proto}//${location.host}/ws/sessions/${encodeURIComponent(session.id)}${q}`);
  ws.onopen = () => { attempt = 0; };
  ws.onmessage = (event) => {
    if (typeof event.data !== 'string') return;
    let msg;
    try { msg = JSON.parse(event.data); } catch { return; }
    if (msg.type === 'snapshot') {
      resetChat(entry);
      for (const it of msg.transcript) renderChatItem(entry, it);
      const last = msg.transcript[msg.transcript.length - 1];
      const done = !last || last.t === 'result' || last.t === 'exit' || last.t === 'error';
      setChatPending(entry, !done);
    } else if (msg.type === 'agent') {
      renderChatItem(entry, msg.item);
    } else if (msg.type === 'state') {
      applySessionState(msg.session);
    }
  };
  ws.onclose = () => {
    if (entry.socket === ws) entry.socket = null;
    if (live.get(session.id) !== entry) return;
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
      }
      r.curTextEl.insertAdjacentHTML('beforeend', renderMarkdown(item.text));
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
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}
function renderInline(s) {
  s = s.replace(/`([^`]+)`/g, (_, c) => `<code>${c}</code>`);
  s = s.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  s = s.replace(/(^|[^*])\*([^*\n]+)\*(?!\*)/g, '$1<em>$2</em>');
  s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, (_, t, u) => `<a href="${u}" target="_blank" rel="noopener">${t}</a>`);
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
function pasteToSession(entry) {
  if (navigator.clipboard?.readText) {
    navigator.clipboard.readText()
      .then((text) => { if (text && entry.socket?.readyState === WebSocket.OPEN) entry.socket.send(text); })
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
  tabsEl.innerHTML = '';
  sessions.forEach((s, idx) => {
    const tab = document.createElement('button');
    tab.type = 'button';
    tab.className = 'tab' + (idx === activeIndex ? ' active' : '');
    tab.dataset.id = s.id;
    tab.draggable = false; // we use pointer events, not native HTML5 DnD

    const name = document.createElement('span');
    name.className = 'tab-name';
    name.textContent = s.name;
    tab.appendChild(name);

    const dot = document.createElement('span');
    dot.className = `tab-dot ${isAgent(s) ? 'web' : 'cli'} ${dotStatus(s)}`;
    dot.dataset.tool = s.tool;
    tab.appendChild(dot);

    tab.onclick = () => scrollToIndex(idx);
    tabsEl.appendChild(tab);
  });
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

function sendToSession(id, text) {
  const entry = live.get(id);
  const session = sessions.find((s) => s.id === id);
  if (isAgent(session)) {
    // Agent sessions speak the stream-json user-message protocol, not raw bytes.
    if (entry?.socket?.readyState === WebSocket.OPEN) {
      entry.socket.send(JSON.stringify({ type: 'user', text: text.replace(/\r$/, '') }));
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
    alert(`Tool "${tool}" is not configured on the server.`);
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
  addMenuItem('通知中心', openNotifyPanel);
  addMenuItem('成本账单', () => { closeMenu(); alert('成本面板（M3 实现）'); });
  addMenuItem('备份管理', () => { closeMenu(); alert('备份面板（M4 实现）'); });
  addMenuItem('迁移向导', () => { closeMenu(); alert('迁移面板（M5 实现）'); });

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
    const created = await api('/api/sessions', {
      method: 'POST',
      body: JSON.stringify({
        name: project.name,
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
          start: true
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
        entry.socket.send(JSON.stringify({ type: 'resize', cols: entry.term.cols, rows: entry.term.rows }));
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
  pollTimer = setTimeout(async () => {
    try { await refreshSessions(); } catch (e) { console.error(e); }
    schedulePoll();
  }, delay);
}

function applyViewport() {
  // visualViewport.height excludes the on-screen keyboard area on iOS / Android,
  // unlike window.innerHeight which stays at the layout-viewport size.
  const h = window.visualViewport?.height ?? window.innerHeight;
  document.documentElement.style.setProperty('--vvh', h + 'px');
  for (const [id, entry] of live) {
    if (!entry.term) continue;
    try { entry.fit.fit(); } catch {}
    if (entry.socket?.readyState === WebSocket.OPEN) {
      entry.socket.send(JSON.stringify({ type: 'resize', cols: entry.term.cols, rows: entry.term.rows }));
    }
  }
  scrollToIndex(activeIndex, false);
}

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
    document.body.appendChild(el);
  }
  const line = document.createElement('div');
  line.textContent = '⚠ ' + String(msg).slice(0, 500);
  el.appendChild(line);
}

// ---- Notification center panel (ext) ----
function openNotifyPanel() {
  closeMenu();
  notifyBackdrop.hidden = false;
  refreshNotifyPanel();
}
async function refreshNotifyPanel() {
  const [rules, msgs] = await Promise.all([
    api('/api/notify/rules').catch(() => ({ rules: [] })),
    api('/api/notify/messages?page=1').catch(() => ({ items: [] })),
  ]);
  notifyRules.innerHTML = '<h4>规则</h4>' + ((rules.rules || []).map((r) =>
    `<div class="notify-item">${esc(r.name)} — ${esc(r.event_type)}
     ${r.enabled ? '' : '(停用)'}</div>`).join('') || '<p>无规则</p>');
  notifyMessages.innerHTML = '<h4>消息记录</h4>' + ((msgs.items || []).slice(0, 20).map((m) =>
    `<div class="notify-item ${esc(m.level)}">[${esc(m.level)}] ${esc(m.title)}
     <span class="muted">${esc(m.tool || '')} ${esc(m.project || '')}</span></div>`
  ).join('') || '<p>暂无消息</p>');
}
document.getElementById('notify-close').onclick = () => { notifyBackdrop.hidden = true; };
notifyBackdrop.addEventListener('click', (ev) => {
  if (ev.target === notifyBackdrop) notifyBackdrop.hidden = true;
});
document.getElementById('notify-rule-add').onclick = async () => {
  try {
    const type = document.getElementById('notify-rule-type').value;
    await api('/api/notify/rules', { method: 'POST', body: JSON.stringify({
      name: 'rule-' + Date.now(), event_type: type, matcher_json: '{}',
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
