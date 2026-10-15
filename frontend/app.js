// ═══════════════════════════════════════════════════════════════════
// CONFIG
// ═══════════════════════════════════════════════════════════════════
const API = window.CARTWISE_API_URL || 'https://YOUR_API_GATEWAY_URL';

// ── v9.5: notice when the session ends elsewhere ──────────────────────────────
// Deleting the account, an admin email change, or a PIN reset revokes sessions on
// the server. Before, other open tabs kept showing the app (and failing quietly)
// until a full reload. Any 401 from a user endpoint now returns to sign-in, and
// the session is re-checked when the tab regains focus and every 5 minutes.
const _cwFetch = window.fetch.bind(window);
window.fetch = async function(input, init) {
  const res = await _cwFetch(input, init);
  try {
    const url = typeof input === 'string' ? input : (input && input.url) || '';
    if (res.status === 401 && url.startsWith(API) && !/\/(auth|admin)\/|\/user\/unsubscribe/.test(url)) {
      sessionEnded();
    }
  } catch (_) {}
  return res;
};
let _lastSessionCheck = 0;
function checkSession(force) {
  if (typeof userEmail === 'undefined' || !userEmail) return;
  const now = Date.now();
  if (!force && now - _lastSessionCheck < 60000) return;   // at most once a minute
  _lastSessionCheck = now;
  fetch(API + '/user/prefs', {credentials:'include', cache:'no-store'}).catch(() => {});  // 401 → sessionEnded()
}
document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible') checkSession(); });
window.addEventListener('focus', () => checkSession());
setInterval(() => checkSession(true), 5 * 60 * 1000);
function sessionEnded() {
  if (typeof userEmail === 'undefined' || !userEmail) return;   // already signed out
  try { closeDealModal(); } catch (_) {}
  try { toggleFilterSheet(false); } catch (_) {}
  document.querySelectorAll('.overlay.vis').forEach(o => o.classList.remove('vis'));
  doLogout({ local: true });
  setMsg('auth-msg', 'Your session has ended. Please sign in again.', 'err');
}

// PDC-05: session token travels in HttpOnly cookie (SameSite=Lax; Secure).
// Browser sends it automatically on all requests to *.cartwise.shopping.
let userEmail = localStorage.getItem('cw_email') || null;
let prefs     = null;
// PDC-21: admin token now in HttpOnly cookie — no JS-accessible storage.
let _adminLoggedIn = false;


// PDC-05: session travels in HttpOnly cookie — sent automatically by browser.
function authHeaders() {
  return {'Content-Type': 'application/json'};
}

// Deal state
let _allDeals      = [];       // raw from API
let _dealTokenCache= new Map(); // pre-tokenized title+brand tokens keyed by deal id
let _badgeCache    = new Map(); // pre-computed history badges keyed by deal id
const DEALS_PAGE_SIZE = 50;     // cards per "page" in All Deals grid
let _dealsPageLimit   = DEALS_PAGE_SIZE; // how many deal cards to render (grows on Load More)
let _dealCounts    = {};       // type counts
let _deptCounts    = {};       // dept counts
let _dealsUpdated  = '';       // WeeklyAdLatestUpdatedDateTime from last fetch
let _dealsStoreId  = '';       // which store these deals are for
let _allMatches    = [];       // computed matches
let _dealHistory   = null;    // history summary { num_weeks, badges, freq, ... } for current store
let _corpusTitles  = [];      // global autocomplete corpus
let _matchDeptCts  = {};
let _savingTypes   = [];       // dynamic saving_type values from API
let _tprLoaded     = false;    // whether TPR deals have been fetched and merged
let _tprLoading    = false;    // true while a TPR fetch is in flight (informational)
// Load generations (v9.2 fix for bug #4): core and TPR deals are fetched in
// parallel and can arrive in either order. Each loadDeals() bumps _dealsGen;
// a TPR response for the current generation that beats the core response is
// parked in _pendingTpr and merged as soon as core deals are applied.
let _dealsGen      = 0;        // incremented on every deal (re)load, store change and logout
let _coreGen       = 0;        // generation whose core deals are currently in _allDeals
let _pendingTpr    = null;     // {gen, data} TPR response waiting for its core deals
let _totalAllDeals = 0;        // total deal count across all types (from API total_all)

// ── Item shape helpers ────────────────────────────────────────────────────────
// prefs.items is an array of plain strings.
// Legacy {name, mode} dicts are handled transparently for backward compat.

function itemName(item)  { return typeof item === 'string' ? item : (item?.name  || ''); }

// Return all item names as a plain string array (for display, autocomplete, etc.)
function itemNames() { return (prefs?.items || []).map(itemName); }

// Returns false if the date string is a known null/placeholder value (e.g. 1/1–1/1 from Publix API)
function isValidDate(s) {
  if (!s || !s.trim()) return false;
  const clean = s.trim().replace(/–/g, '-'); // normalize en-dash
  // Publix returns 1/1 as a null placeholder when no date is available
  return !/^1\/1$/.test(clean);
}
function hasValidDates(d) {
  return isValidDate(d.valid_from) || isValidDate(d.valid_thru);
}



// ── History badge thresholds ──────────────────────────────────────────────────
const HIST_MIN = {
  bestYear:   8,
  best6weeks: 6,
  repeat:     2,
  rareOnSale: 12,
  freqHint:   4,
  banner:     4,
};

function parseSavingsAmount(str) {
  if (!str) return null;
  const m = str.match(/\$(\d+(?:\.\d+)?)/);
  return m ? parseFloat(m[1]) : null;
}



// ── Sort by actual savings amount, NOT by price ─────────────────────────────
// Uses savingsDisplay() output so BOGO, multi-buy, and standard deals all
// sort by their computed save amount. Deals without savings sort last.
function dealSortSavings(d) {
  if (d.offer && typeof d.offer.save === 'number') return d.offer.save;   // v9.3: per-unit savings
  const sv = savingsDisplay(d);
  if (sv.save) {
    const m = sv.save.match(/\$(\d+(?:\.\d+)?)/);
    if (m) return parseFloat(m[1]);
  }
  return -Infinity;
}

// ── History lookups (v9.2) ───────────────────────────────────────────────────
// The server (lambda/history.py) now does the history work and returns a small
// summary: badges keyed by normalized title, and past-week counts for list items.
// The old version shipped 4.7 MB of past deals and fuzzy-matched every current
// deal against all of them in the browser (~190 s of CPU after each login).

// KEEP IN SYNC with norm_title() in lambda/history.py
function cwHistKey(title) {
  return (title || '').toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim();
}

const HIST_BADGE_LABEL = { best: 'Biggest discount', repeat: 'Repeat deal', rare: 'Rarely on sale' };
let _badgeIndex = null;   // Map: cwHistKey(title) → badge, built once per history load

function buildBadgeIndex(history) {
  const m = new Map();
  const b = (history && history.badges) || {};
  const weeks = (history && history.num_weeks) || 0;
  for (const type of Object.keys(HIST_BADGE_LABEL)) {
    // v9.8.2: "best" = this week's stated saving is the biggest in the loaded history
    const label = type === 'best' && weeks ? `Biggest discount in ${weeks} wks` : HIST_BADGE_LABEL[type];
    for (const key of (b[type] || [])) if (!m.has(key)) m.set(key, { type, label });
  }
  return m;
}

function computeBadge(deal, history) {
  if (!history || !history.badges) return null;
  if (!_badgeIndex) _badgeIndex = buildBadgeIndex(history);
  return _badgeIndex.get(cwHistKey(deal.title)) || null;
}

function freqLabel(count, weeksOfHistory) {
  const annualRate = Math.round((count / Math.max(weeksOfHistory || 1, 1)) * 52);
  if (annualRate === 0)  return 'rarely on sale';
  if (annualRate <= 2)   return `on sale ~${annualRate}x/year`;
  if (annualRate <= 8)   return 'on sale a few times a year';
  if (annualRate <= 20)  return 'on sale most months';
  return 'on sale most weeks';
}

function computeFreqHint(itemName, history) {
  if (!history || (history.num_weeks || 0) < HIST_MIN.freqHint || !history.freq) return '';
  const count = history.freq[(itemName || '').trim().toLowerCase()];
  if (count === undefined) return '';   // item added since the summary loaded; refreshes shortly
  return freqLabel(count, history.weeks_of_history);
}

// ── Badge pre-computation ─────────────────────────────────────────────────────
// Now a dictionary lookup per deal (microseconds), so it runs in one pass.
let _badgeComputeTimer = null;  // retained so older call sites that clear it stay harmless

function precomputeBadges() {
  _badgeCache = new Map();
  if (!_allDeals.length || !_dealHistory) return;
  for (const d of _allDeals) {
    const b = computeBadge(d, _dealHistory);
    if (b) _badgeCache.set(d.id, b);
  }
}

function buildQualityBanner(matches, history) {
  if (!history || (history.num_weeks || 0) < HIST_MIN.banner) return null;
  const bestItems = [], repeatItems = [];
  matches.forEach(deal => {
    const badge = _badgeCache.get(deal.id) || null;
    if (!badge) return;
    const name = deal.my_item || deal.title || '';
    if (badge.type === 'best' || badge.type === 'recent') bestItems.push(name);
    else if (badge.type === 'repeat') repeatItems.push(name);
  });
  // v9.4: one mention per list item (two cereal deals both matching "cheerios")
  const uniq = arr => [...new Set(arr.map(x => x.toLowerCase()))];
  bestItems.splice(0, bestItems.length, ...uniq(bestItems));
  repeatItems.splice(0, repeatItems.length, ...uniq(repeatItems));
  if (!bestItems.length && !repeatItems.length) return null;
  const parts = [];
  if (bestItems.length)   parts.push(`🔥 <strong>${bestItems.slice(0,3).map(escH).join(' &amp; ')}</strong> ${bestItems.length===1?'is':'are'} at the biggest discount in ${escH(String(history.num_weeks))} weeks.`);
  if (repeatItems.length) parts.push(`🔁 <strong>${repeatItems.slice(0,2).map(escH).join(' &amp; ')}</strong> ${repeatItems.length===1?'is a repeat':'are repeats'} from last week.`);
  return parts.join(' ');
}

let _historyLoading = false;

async function loadDealHistory() {
  const storeId = prefs?.store_id;
  if (!storeId || _historyLoading) return;
  _historyLoading = true;
  try {
    const res = await fetch(API + `/deals/history?store_id=${encodeURIComponent(storeId)}`, {credentials:'include',headers:authHeaders()});
    if (!res.ok) return;
    _dealHistory = await res.json();
    _badgeIndex = null;  // rebuilt lazily from the new summary
    precomputeBadges();  // dictionary lookups — fast
    renderDeals();
    renderFreqHints();
    // Show badge legends if enough history
    document.querySelectorAll('.badge-legend').forEach(el => {
      el.style.display = (_dealHistory && _dealHistory.num_weeks >= HIST_MIN.repeat) ? '' : 'none';
    });
  } catch (e) { console.warn('History load failed:', e); }
  finally { _historyLoading = false; }
}

// Re-fetch the history summary shortly after list edits so frequency hints
// cover newly added items (the summary is computed per user on the server).
let _historyRefreshTimer = null;
function scheduleHistoryRefresh() {
  clearTimeout(_historyRefreshTimer);
  _historyRefreshTimer = setTimeout(loadDealHistory, 1500);
}

async function loadCorpus() {
  if (_corpusTitles.length) return;
  // v2 key: forces all browsers to discard old &amp;-poisoned corpus cache.
  const CORPUS_KEY = 'cw_corpus_v2';
  const cached = sessionStorage.getItem(CORPUS_KEY);
  // Always decode entities — safety net for any data stored before the Python fix
  if (cached) { _corpusTitles = JSON.parse(cached).map(decodeEntities); return; }
  try {
    const res = await fetch(API + '/deals/corpus', {credentials:'include',headers:authHeaders()});
    if (!res.ok) return;
    const data = await res.json();
    // Decode HTML entities as a safety net (Python fix handles new data; this covers transition)
    _corpusTitles = (data.titles || []).map(decodeEntities);
    sessionStorage.setItem(CORPUS_KEY, JSON.stringify(_corpusTitles));
  } catch (e) { console.warn('Corpus load failed:', e); }
}

function renderFreqHints() {
  if (!_dealHistory || (_dealHistory.num_weeks || 0) < HIST_MIN.freqHint) return;
  document.querySelectorAll('.list-item-freq').forEach(el => {
    const item = el.dataset.item || '';
    el.textContent = computeFreqHint(item, _dealHistory);
  });
}
// ═══════════════════════════════════════════════════════════════════
// PIN INPUTS
// ═══════════════════════════════════════════════════════════════════
function initPins(sel, onSubmit) {
  const ds = document.querySelectorAll(sel);
  ds.forEach((el,i) => {
    el.addEventListener('input', () => {
      el.value = el.value.replace(/\D/g,'').slice(-1);
      if (el.value && i < ds.length-1) ds[i+1].focus();
    });
    el.addEventListener('keydown', e => {
      if (e.key==='Backspace' && !el.value && i>0) ds[i-1].focus();
      if (e.key==='Enter' && onSubmit) onSubmit();
    });
    el.addEventListener('focus', () => el.select());
  });
}
function getPin(sel) { return [...document.querySelectorAll(sel)].map(e=>e.value).join(''); }
function clrPin(sel) { document.querySelectorAll(sel).forEach(e=>e.value=''); const f=document.querySelector(sel); if(f) f.focus(); }

function initPinInputs(selector) {
  document.querySelectorAll(selector).forEach((inp, i, all) => {
    inp.addEventListener('keydown', e => {
      if (e.key === 'Backspace' && !inp.value && i > 0) {
        all[i-1].focus();
        all[i-1].value = '';
        e.preventDefault();
      }
    });
    inp.addEventListener('input', e => {
      // strip non-digits
      inp.value = inp.value.replace(/\D/g, '').slice(-1);
      if (inp.value && i < all.length - 1) all[i+1].focus();
      // if last digit filled and it's the auth form, attempt login
      if (inp.value && i === all.length - 1 && selector === '.auth-pin') {
        const allFilled = Array.from(all).every(d => d.value);
        if (allFilled) doAuth();
      }
    });
    inp.addEventListener('focus', () => inp.select());
    inp.addEventListener('paste', e => {
      e.preventDefault();
      const digits = (e.clipboardData.getData('text').replace(/\D/g, '')).slice(0, all.length);
      digits.split('').forEach((d, j) => { if (all[i+j]) all[i+j].value = d; });
      const next = all[Math.min(i + digits.length, all.length - 1)];
      next.focus();
    });
  });
}


initPins('.auth-pin', doAuth);
initPins('.cur-pin', null);
initPins('.new-pin', null);
initPins('.ap-pin', null);
initPins('.ac-pin', null);

// Paste support on auth pins
document.querySelectorAll('.auth-pin').forEach((el,i,arr) => {
  el.addEventListener('paste', e => {
    const t=(e.clipboardData||window.clipboardData).getData('text').replace(/\D/g,'').slice(0,4);
    if(t.length===4){e.preventDefault();arr.forEach((d,j)=>d.value=t[j]||'');arr[3].focus();}
  });
});

// ═══════════════════════════════════════════════════════════════════
// AUTH
// ═══════════════════════════════════════════════════════════════════
let authMode = 'login';
function switchTab(m) {
  authMode=m;
  document.querySelectorAll('.auth-tab').forEach((t,i)=>t.classList.toggle('active',(m==='login'&&i===0)||(m==='register'&&i===1)));
  document.getElementById('auth-btn').textContent = m==='login'?'Sign In':'Create Account';
  setMsg('auth-msg','','');
}
function setMsg(id,txt,cls){ const el=document.getElementById(id); if(!el) return; el.textContent=txt; el.className='msg'+(cls?' '+cls:''); }

// Strip any character that isn't valid in an ASCII email address.
// Allowed: a-z A-Z 0-9 and !#$%&'*+/=?^_`{|}~.- and @
// Called oninput on every email field to prevent non-ASCII / junk entry.
function sanitizeEmailInput(el) {
  const cleaned = el.value.replace(/[^a-zA-Z0-9!#$%&'*+/=?^_`{|}~.@-]/g, '');
  if (cleaned !== el.value) { el.value = cleaned; }
}
let _lockoutTimer = null;
function _startLockoutCountdown(retryAfterSeconds) {
  clearInterval(_lockoutTimer);
  const btn = document.getElementById('auth-btn');
  btn.disabled = true;
  let remaining = retryAfterSeconds;
  function tick() {
    const m = Math.floor(remaining / 60), s = remaining % 60;
    const display = m > 0 ? `${m}m ${String(s).padStart(2,'0')}s` : `${s}s`;
    setMsg('auth-msg', `Account locked. Try again in ${display}.`, 'err');
    btn.textContent = `Locked (${display})`;
    if (remaining <= 0) {
      clearInterval(_lockoutTimer);
      _lockoutTimer = null;
      btn.disabled = false;
      btn.textContent = authMode === 'login' ? 'Sign In' : 'Create Account';
      setMsg('auth-msg', 'Account unlocked. You may try again.', 'ok');
    }
    remaining--;
  }
  tick();
  _lockoutTimer = setInterval(tick, 1000);
}

let _authInFlight = false;
async function doAuth() {
  if (_authInFlight) return;  // prevent double-fire from pin input + initPins callback
  const em = document.getElementById('auth-email').value.trim().toLowerCase();
  const pin = getPin('.auth-pin');
  if(!em){ setMsg('auth-msg','Please enter your email.','err'); return; }
  if(!/^[\x00-\x7F]+$/.test(em) || !/^[a-zA-Z0-9!#$%&'*+/=?^_`{|}~.\-]+@[a-zA-Z0-9][a-zA-Z0-9\-]*(\.[a-zA-Z0-9][a-zA-Z0-9\-]*)*\.[a-zA-Z]{2,}$/.test(em)){
    setMsg('auth-msg','Please enter a valid email address.','err'); return;
  }
  if(pin.length!==4){ setMsg('auth-msg','Please enter your 4-digit PIN.','err'); return; }
  // v9.2.2 (#1): claim the flag only after validation passes; the early returns
  // above used to leave it set, so the button never worked again until reload.
  _authInFlight = true;
  const btn=document.getElementById('auth-btn');
  btn.disabled=true; btn.textContent=authMode==='login'?'Signing in…':'Creating account…';
  try{
    const res=await fetch(API+(authMode==='login'?'/auth/login':'/auth/register'),{method:'POST',credentials:'include',headers:{'Content-Type':'application/json'},body:JSON.stringify({email:em,pin})});
    const data=await res.json();
    if(!res.ok){
      if(res.status===429 && data.retry_after_seconds){
        _startLockoutCountdown(data.retry_after_seconds);
        return;
      }
      setMsg('auth-msg',data.error||'Something went wrong.','err');
      return;
    }
    if(authMode==='register'){
      // Auto-login with the credentials just used — no re-prompting required.
      // If email is unverified we still sign in; the app shows a banner prompting verification.
      // v9.5: the server answers the same way for new and existing emails (no account
      // enumeration), so don't claim an account was created — just try to sign in.
      setMsg('auth-msg','Signing you in…','ok');
      const loginRes = await fetch(API+'/auth/login', {
        method:'POST', credentials:'include',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify({email: em, pin})
      });
      const loginData = await loginRes.json();
      if(!loginRes.ok){
        // Either this email already has an account with a different PIN, or sign-in failed
        switchTab('login');
        setMsg('auth-msg','If this email is new, check your inbox to verify it, then sign in. If you already have an account, sign in with your PIN.','ok');
        return;
      }
      clearInterval(_lockoutTimer); _lockoutTimer=null;
      userEmail = loginData.email;
      localStorage.setItem('cw_email', userEmail);
      prefs = loginData.prefs;
      enterApp({isFirstLogin: true, emailVerified: loginData.email_verified !== false});
      return;
    }
    clearInterval(_lockoutTimer); _lockoutTimer=null;
    userEmail=data.email;
    localStorage.setItem('cw_email',userEmail);
    prefs=data.prefs;
    const _isNew = authMode==='register' || !(prefs && prefs.store_id);
    enterApp({isFirstLogin: _isNew, emailVerified: data.email_verified !== false});
  }catch(e){setMsg('auth-msg','Could not reach the server.','err');}
  finally{_authInFlight=false;if(!_lockoutTimer){btn.disabled=false;btn.textContent=authMode==='login'?'Sign In':'Create Account';}}
}

// ═══════════════════════════════════════════════════════════════════
// APP ENTRY
// ═══════════════════════════════════════════════════════════════════
// EMAIL VERIFICATION BANNER
// ═══════════════════════════════════════════════════════════════════
function showVerifyBanner(show) {
  let banner = document.getElementById('verify-banner');
  if (!show) { if (banner) banner.style.display = 'none'; return; }
  if (!banner) {
    banner = document.createElement('div');
    banner.id = 'verify-banner';
    banner.style.cssText = 'background:#fefce8;border-bottom:1px solid #fde68a;padding:10px 16px;display:flex;align-items:center;gap:10px;font-size:13px;color:#92400e;flex-wrap:wrap;';
    banner.innerHTML = `
      <span style="flex:1;min-width:200px">
        📧 <strong>Verify your email</strong> to receive weekly deal alerts.
        Check your inbox for a verification link.
      </span>
      <button data-action="resend-verification" id="btn-resend-verify"
        style="background:#f59e0b;color:#fff;border:none;border-radius:6px;padding:5px 12px;font-size:12px;font-weight:600;cursor:pointer;white-space:nowrap">
        Resend email
      </button>
      <button data-action="hide-verify-banner"
        style="background:none;border:none;font-size:16px;cursor:pointer;color:#92400e;padding:0 4px" title="Dismiss">×</button>`;
    // Insert at top of app-screen, before the topbar
    const app = document.getElementById('app-screen');
    app.insertBefore(banner, app.firstChild);
  }
  banner.style.display = 'flex';
}

async function resendVerification() {
  const btn = document.getElementById('btn-resend-verify');
  if (btn) { btn.disabled = true; btn.textContent = 'Sending…'; }
  try {
    const res = await fetch(API + '/auth/resend-verification', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({email: userEmail})
    });
    const d = await res.json();
    if (btn) { btn.textContent = d.message || 'Sent!'; }
    setTimeout(() => { if (btn) { btn.disabled = false; btn.textContent = 'Resend email'; }}, 5000);
  } catch(e) {
    if (btn) { btn.disabled = false; btn.textContent = 'Resend email'; }
  }
}

// ═══════════════════════════════════════════════════════════════════
function enterApp(opts) {
  // opts.isFirstLogin = true means brand-new account (no store set yet)
  const isFirstLogin = (opts && opts.isFirstLogin) ||
    (!localStorage.getItem('cw_welcomed_'+userEmail) && !(prefs && prefs.store_id));
  document.getElementById('auth-screen').style.display='none';
  document.getElementById('app-screen').classList.add('visible');
  // Show verification banner for unverified accounts
  const _verified = (opts && opts.emailVerified !== undefined) ? opts.emailVerified : true;
  showVerifyBanner(!_verified);
  document.getElementById('topbar-email').textContent=userEmail;
  document.getElementById('acct-email-disp').textContent=userEmail;
  document.getElementById('acct-avatar').textContent=(userEmail[0]||'?').toUpperCase();
  loadPrefs();
  if(isFirstLogin){
    // Show welcome wizard; on dismiss it will navigate to Store
    showPanel('store');
    setTimeout(()=>wlcShow(), 120);
  } else {
    // Returning user: go straight to My Matches
    showPanel('deals');   // showPanel('deals') already calls preloadDeals()
  }
  // preloadDeals() removed — showPanel('deals') handles it; calling both caused a duplicate /deals fetch
  loadDealHistory();
  loadCorpus();
  // #admin hash — reveal admin nav and go straight to admin panel (from staging, March 2026)
  _checkAdminHash();
}

function _checkAdminHash() {
  if (window.location.hash === '#admin') {
    document.querySelectorAll('#nav-admin,#mnav-admin,#nav-admin-sep,#mnav-admin-sep')
      .forEach(el => el.style.display = '');
    showPanel('admin');
  }
}
window.addEventListener('hashchange', _checkAdminHash);

// v9.6: the weekly run is cron(0 12 ? * WED *) in UTC, which is 8 AM ET in summer
// and 7 AM ET in winter. Show it in the viewer's own time zone instead of a fixed "8:00 AM ET".
function weeklyRunLabel(now) {
  now = now || new Date();
  const d = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate(), 12, 0, 0));
  d.setUTCDate(d.getUTCDate() + ((3 - d.getUTCDay() + 7) % 7));   // next (or this) Wednesday
  const t = d.toLocaleTimeString([], {hour:'numeric', minute:'2-digit', timeZoneName:'short'});
  const day = d.toLocaleDateString([], {weekday:'long'});
  return `Every ${day} at about ${t}`;
}
document.addEventListener('DOMContentLoaded', () => {
  const el = document.getElementById('sched-time');
  if (el) el.textContent = weeklyRunLabel();
});
async function doLogout(opts){
  // PDC-05: browser sends HttpOnly cookie automatically with credentials:'include'
  // v9.5: {local:true} when the server already ended the session (sessionEnded)
  if (!(opts && opts.local)) {
    try{await fetch(API+'/auth/logout',{method:'POST',credentials:'include',headers:authHeaders()});}catch(_){}
  }
  userEmail=null;prefs=null;_allDeals=[];_allMatches=[];_dealsStoreId='';_storeData=null;_badgeCache=new Map();_tprLoaded=false;_totalAllDeals=0;_dealHistory=null;_badgeIndex=null;_dealsGen++;_coreGen=0;_pendingTpr=null;
  // v9.5: nothing from this user should be visible to the next person on this browser
  _dealTokenCache=new Map();_deptCounts={};_dealsShownTotal=null;_matchesDirty=true;
  ['matches-grid','deals-grid','deals-meta','deals-chips','quality-banner'].forEach(id=>{const el=document.getElementById(id); if(el) el.innerHTML='';});
  const _vb=document.getElementById('deals-vbar'); if(_vb){_vb.innerHTML='';_vb.classList.remove('vis');}
  try{ Object.keys(localStorage).filter(k=>k.startsWith('cw_deals_ts_')).forEach(k=>localStorage.removeItem(k)); }catch(_){}
  localStorage.removeItem('cw_email');
  // Reset store UI so it doesn't bleed into the next session
  document.getElementById('store-card-wrap').style.display='none';
  document.getElementById('store-search-ui').style.display='';
  document.getElementById('store-card-el').innerHTML='';
  document.getElementById('store-id-input').value='';
  document.getElementById('store-results').innerHTML='';
  document.getElementById('app-screen').classList.remove('visible');
  document.getElementById('auth-screen').style.display='';
  clrPin('.auth-pin');
}
function showPanel(name) {
  document.querySelectorAll('.panel').forEach(p=>p.classList.remove('active'));
  document.querySelectorAll('.nav-item').forEach(n=>n.classList.remove('active'));
  document.getElementById('panel-'+name).classList.add('active');
  const sideNav=document.getElementById('nav-'+name);
  if(sideNav) sideNav.classList.add('active');
  const mobileNav=document.getElementById('mnav-'+name);
  if(mobileNav) mobileNav.classList.add('active');
  if(name==='admin') initAdmin();
  // Deals: load deals + matches together (unified panel)
  // List: always re-fetch so inbound email imports appear without a page reload
  if(name==='deals') { preloadDeals(); }
  else if(name==='list') fetchAndLoadPrefs();
}
function openMobileNav(){
  document.getElementById('mobile-nav').classList.add('open');
  document.body.style.overflow='hidden';
}
function closeMobileNav(){
  document.getElementById('mobile-nav').classList.remove('open');
  document.body.style.overflow='';
}

// ═══════════════════════════════════════════════════════════════════
// PREFS
// ═══════════════════════════════════════════════════════════════════
async function fetchAndLoadPrefs() {
  try {
    const res = await fetch(API+'/user/prefs', {credentials:'include', headers:authHeaders()});
    if (!res.ok) return;
    const data = await res.json();
    prefs = data.prefs;
    loadPrefs();
  } catch(_) {}
}
function loadPrefs() {
  if(!prefs) return;
  const sid=prefs.store_id||'';
  if(sid){
    // Set basic info immediately, then enrich with full store details in background.
    // Capture email now so a logout before the fetch returns can't corrupt the new session.
    const _emailAtLoad = userEmail;
    setStoreSel({id:sid, name:prefs.store_name||'', address:prefs.store_address||'', street:prefs.store_address||''});
    fetch(API+'/stores/search?q='+encodeURIComponent(sid), {credentials:'include',headers:authHeaders()})
      .then(r=>r.json()).then(data=>{
        if(userEmail !== _emailAtLoad) return; // session changed — discard stale result
        const s=(data.stores||[])[0];
        if(s && s.id===sid) setStoreSel(s);
      }).catch(()=>{});
  } else {
    // No store set — ensure the UI shows the search form, not a card left over from a previous session
    document.getElementById('store-card-wrap').style.display = 'none';
    document.getElementById('store-search-ui').style.display = '';
  }
  const emailEnabled = prefs.email_enabled !== false; // default true
  document.getElementById('email-enabled').checked = emailEnabled;
  document.getElementById('email-settings-wrap').style.display = emailEnabled ? '' : 'none';
  document.getElementById('notify-email-display').textContent = prefs.notify_email || userEmail || '';
  renderItems();
  const t=cwGetThreshold(prefs.matching||{});   // v9.2.2 (#11): same value the matcher uses
  document.getElementById('threshold').value=t;
  document.getElementById('threshold-val').textContent=t;
}
function collectPrefs(ctx){
  return {
    store_id:      prefs?.store_id||'',
    store_name:    prefs?.store_name||'',
    store_address: prefs?.store_address||'',
    bogo_mode:     prefs?.bogo_mode||'auto',      // v9.8
    notify_email:  userEmail,
    email_enabled: document.getElementById('email-enabled').checked,
    items:         prefs?.items||[],
    // v9.2.2 (#11): only the slider writes a threshold; other saves keep what is stored
    matching:      ctx==='matching'
                     ? {threshold:parseInt(document.getElementById('threshold').value)||cwGetThreshold(prefs?.matching||{})}
                     : (prefs?.matching||{}),
  };
}
let _saveToastTimer=null;
function showSaveToast(){
  const t=document.getElementById('save-toast');
  if(!t) return;
  t.classList.add('show');
  clearTimeout(_saveToastTimer);
  _saveToastTimer=setTimeout(()=>t.classList.remove('show'),2000);
}
async function savePrefs(ctx){
  if(ctx === 'list' || ctx === 'matching') _matchesDirty = true;
  const p=collectPrefs(ctx);
  if(prefs) p.items=prefs.items||[];
  try{
    const res=await fetch(API+'/user/prefs',{method:'PUT',credentials:'include',headers:{...authHeaders(),'Content-Type':'application/json'},body:JSON.stringify({prefs:p})});
    const data=await res.json();
    if(!res.ok) throw new Error(data.error||'Save failed');
    prefs=data.prefs;
    showSaveToast();
    if(ctx === 'list') scheduleHistoryRefresh();
  }catch(e){console.error('Save failed:',e.message);}
}

// ═══════════════════════════════════════════════════════════════════
// STORE
// ═══════════════════════════════════════════════════════════════════
// ── Store panel ────────────────────────────────────────────────────
let _storeData = null; // full store object from API

function fmtPhone(p){
  if(!p) return '';
  const d=p.replace(/\D/g,'');
  if(d.length===10) return `(${d.slice(0,3)}) ${d.slice(3,6)}-${d.slice(6)}`;
  return p;
}

function storeCardHtml(s){
  const addr = [s.street, s.city, s.state, s.zip].filter(Boolean).join(', ');
  const mapsUrl = `https://www.google.com/maps/search/?api=1&query=${encodeURIComponent(addr)}`;
  const hero = s.img_hero
    ? `<img class="store-card-hero" src="${escH(s.img_hero)}" alt="${escH(s.name)}" data-img-fallback>`
    + `<div class="store-card-hero-ph" style="display:none">🏪</div>`
    : `<div class="store-card-hero-ph">🏪</div>`;

  // Hours grid
  const days = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];
  const todayIdx = new Date().getDay();
  let hoursGrid = '';
  if(s.hours_raw && s.hours_raw.length){
    // hours_raw[0] = today, build forward
    hoursGrid = `<div class="store-hours-grid">`;
    for(let i=0;i<Math.min(7,s.hours_raw.length);i++){
      const h = s.hours_raw[i];
      const dt = new Date(h.openTime||h.closeTime||'');
      const dayIdx = dt.getDay ? dt.getDay() : (todayIdx+i)%7;
      const isToday = i===0;
      let timeStr = '';
      if(h.isClosed) timeStr='Closed';
      else if(h.isOpen24Hours) timeStr='24h';
      else{
        const fmt=iso=>{try{const t=new Date(iso);return t.toLocaleTimeString('en-US',{hour:'numeric',minute:'2-digit',hour12:true}).replace(':00','').replace(' ','');}catch{return'';}};
        timeStr=`${fmt(h.openTime)}<br>${fmt(h.closeTime)}`;
      }
      hoursGrid+=`<div class="store-hour-day${isToday?' today':''}">
        <div class="store-hour-day-name">${days[dayIdx]}</div>
        <div class="store-hour-time">${timeStr}</div>
      </div>`;
    }
    hoursGrid+='</div>';
  }

  const phone = fmtPhone(s.phone);
  const pharmPhone = fmtPhone(s.pharmacy_phone);

  return `<div>
    ${hero}
    <div class="store-card-body">
      <div class="store-card-name">${escH(s.name)}</div>
      <div class="store-card-id">Store #${escH(s.id)}</div>
      ${addr?`<div class="store-card-row"><span class="store-card-row-icon">📍</span>
        <span><a href="${escH(mapsUrl)}" target="_blank">${escH(addr)}</a></span></div>`:''}
      ${phone?`<div class="store-card-row"><span class="store-card-row-icon">📞</span>
        <span><a href="tel:+1${escH(s.phone)}">${escH(phone)}</a></span></div>`:''}
      ${pharmPhone?`<div class="store-card-row"><span class="store-card-row-icon">💊</span>
        <span>Pharmacy: <a href="tel:+1${escH(s.pharmacy_phone)}">${escH(pharmPhone)}</a></span></div>`:''}
      ${s.hours_today?`<div class="store-card-row"><span class="store-card-row-icon">🕐</span>
        <span>Today: <strong>${escH(s.hours_today)}</strong></span></div>`:''}
      ${hoursGrid?`<div style="margin-top:10px">${hoursGrid}</div>`:''}
    </div>
    <div class="store-card-footer">
      <span style="font-size:12px;color:var(--ink-soft)">
        <a href="https://www.publix.com/locations/${escH(s.name.toLowerCase().replace(/[^a-z0-9]+/g,'-').replace(/^-|-$/g,''))}" target="_blank" style="color:var(--green)">View on Publix.com ↗</a>
      </span>
      <button class="btn-change" data-action="clear-store">Change Store</button>
    </div>
  </div>`;
}

function setStoreSel(s){
  if(!prefs) prefs={};
  prefs.store_id      = s.id||'';
  prefs.store_name    = s.name||'';
  prefs.store_address = s.address||s.street||'';
  _storeData = s;
  if(s.id){
    document.getElementById('store-card-el').innerHTML = storeCardHtml(s);
    const bw = document.getElementById('bogo-card-wrap');
    if (bw) { bw.innerHTML = bogoCardHtml(); bw.style.display = ''; }
    renderBogoBanner();
    document.getElementById('store-card-wrap').style.display = '';
    document.getElementById('store-search-ui').style.display = 'none';
  }
}

// ── v9.8: BOGO store rule ────────────────────────────────────────────────────
// KEEP IN SYNC with bogo_mode_for_state / apply_bogo_mode in shared/deal_parser.py.
// Most Publix areas let you buy ONE BOGO item at half price; Florida and the southern
// parts of AL/GA/SC require buying two. AL/GA/SC are split, so we ask there.
const BOGO_HALF_STATES = new Set(['TN','NC','VA','KY']);
const BOGO_TWO_STATES  = new Set(['FL']);
const BOGO_ASK_STATES  = new Set(['AL','GA','SC']);
function stateFromAddress(addr){ const m = /,\s*([A-Z]{2})\s+\d{5}/.exec(addr||''); return m ? m[1] : ''; }
function bogoModeForState(st){ return BOGO_HALF_STATES.has(st) ? 'half' : BOGO_TWO_STATES.has(st) ? 'two' : null; }
function effectiveBogoMode(p){
  if (p && (p.bogo_mode === 'half' || p.bogo_mode === 'two')) return p.bogo_mode;
  return bogoModeForState(stateFromAddress(p && p.store_address)) || 'two';
}
function bogoNeedsAnswer(p){
  return !!(p && p.store_id && (!p.bogo_mode || p.bogo_mode === 'auto') && !bogoModeForState(stateFromAddress(p.store_address)));
}
function fmtMoney2(x){ return '$' + Number(x).toFixed(2); }
function applyBogoMode(offer, mode){
  if (!offer || offer.kind !== 'bogo') return offer;
  const b = parseInt(offer.buy || 1, 10), g = parseInt(offer.get || 1, 10);
  if (b !== g) return offer;                       // Buy 2 Get 1 etc.: no half-price rule
  const o = Object.assign({}, offer);
  const lb = o.unit === 'lb', unit = lb ? '/lb' : ' ea', upto = o.save_is_max ? 'up to ' : '';
  if (mode === 'half') {
    o.context = o.price ? `Half price: ${fmtMoney2(o.price)}${unit}, even if you buy 1` : 'Half price each, even if you buy 1';
    if (o.save) o.save_text = `Save ${upto}${fmtMoney2(o.save)}${lb ? '/lb' : ' each'}`;
  } else {
    const n = b + g;
    if (o.price) o.context = `≈ ${fmtMoney2(o.price)}${unit} when you buy ${n}`;
    // keep Publix's own total save text (doubling the rounded per-item figure drifts by a cent)
  }
  if (o.coupon_required && !/coupon/i.test(o.context || '')) o.context += ' · with digital coupon';
  return o;
}
function bogoCardHtml(){
  const st = stateFromAddress(prefs && prefs.store_address);
  const auto = bogoModeForState(st);
  const cur = (prefs && prefs.bogo_mode) || 'auto';
  const autoLabel = auto === 'half' ? `Automatic: ${escH(st)} stores sell one BOGO item at half price`
                  : auto === 'two'  ? `Automatic: ${escH(st)} stores require buying two`
                  : `Automatic: not sure for ${escH(st || 'this store')}, so we assume you must buy two`;
  const opt = (v, label) => `<label class="bogo-opt"><input type="radio" name="bogo-mode" value="${v}" data-action="bogo-mode"${cur===v?' checked':''}> ${label}</label>`;
  return `<div class="bogo-card${bogoNeedsAnswer(prefs) ? ' needs-answer' : ''}" id="bogo-card">
    <div class="bogo-card-hd">How do BOGO deals work at your store?</div>
    <div class="bogo-card-sub">Some Publix areas let you buy one BOGO item at half price; others (Florida and parts of Alabama, Georgia and South Carolina) require buying two. Prices on cards and in emails follow this setting.</div>
    ${opt('auto', autoLabel)}
    ${opt('half', 'I can buy one at half price')}
    ${opt('two', 'I must buy two to get one free')}
  </div>`;
}
function setBogoMode(mode){
  if (!prefs) return;
  prefs.bogo_mode = mode;
  savePrefs('bogo').catch(()=>{});
  const card = document.getElementById('bogo-card');
  if (card) card.outerHTML = bogoCardHtml();
  renderBogoBanner();
  if (_allDeals.length) renderDeals(true);
}
function renderBogoBanner(){
  const el = document.getElementById('bogo-banner');
  if (!el) return;
  if (!bogoNeedsAnswer(prefs)) { el.style.display = 'none'; el.innerHTML = ''; return; }
  const st = stateFromAddress(prefs.store_address);
  el.innerHTML = `<div class="bogo-banner-q"><strong>Quick question:</strong> at your ${escH(st)} Publix, can you buy one BOGO item at half price?</div>
    <div class="bogo-banner-a"><button class="btn-load" data-action="bogo-answer" data-mode="half">Yes, one at half price</button>
    <button class="btn-sec" data-action="bogo-answer" data-mode="two">No, I must buy two</button></div>`;
  el.style.display = '';
}

function clearStore(){
  document.getElementById('store-card-wrap').style.display = 'none';
  document.getElementById('store-search-ui').style.display = '';
  document.getElementById('store-id-input').value = '';
  document.getElementById('store-results').innerHTML = '';
  if(prefs){prefs.store_id='';prefs.store_name='';prefs.store_address='';prefs.bogo_mode='auto';}
  _storeData = null;
  const _bw=document.getElementById('bogo-card-wrap'); if(_bw){_bw.innerHTML='';_bw.style.display='none';}
  renderBogoBanner();
  _dealsStoreId='';_allDeals=[];_allMatches=[];_dealTokenCache=new Map();_badgeCache=new Map();_tprLoaded=false;_totalAllDeals=0;_dealsGen++;_coreGen=0;_pendingTpr=null;
  document.getElementById('deals-browser').style.display='none';
  const _ms=document.getElementById('matches-section');if(_ms)_ms.style.display='none';
}

function onStoreInputType(){
  // typing — do nothing until Search is clicked or Enter pressed
}

function onStoreInput(){
  doStoreSearch();
}

let _storeSearchTimer = null;
async function doStoreSearch(){
  const q = (document.getElementById('store-id-input').value||'').trim();
  if(!q) return;
  const resultsEl = document.getElementById('store-results');
  resultsEl.innerHTML = '<div class="store-no-results">Searching…</div>';
  try{
    const res = await fetch(API+'/stores/search?q='+encodeURIComponent(q), {credentials:'include',headers:authHeaders()});
    const data = await res.json();
    if(!res.ok) throw new Error(data.error||'Search failed');
    const stores = data.stores||[];
    if(!stores.length){
      resultsEl.innerHTML = '<div class="store-no-results">No stores found. Try a different city, ZIP, or store #.</div>';
      return;
    }
    resultsEl.innerHTML = stores.map((s,i)=>{
      const thumb = s.img_thumb
        ? `<img class="store-result-thumb" src="${escH(s.img_thumb)}" alt="" data-img-fallback><div class="store-result-thumb-ph" style="display:none">🏪</div>`
        : `<div class="store-result-thumb-ph">🏪</div>`;
      const dist = s.distance!=null ? `<div class="store-result-dist">${(+s.distance).toFixed(1)} mi away</div>` : '';
      const addr = [s.street, s.city, s.state].filter(Boolean).join(', ');
      return `<div class="store-result-item" data-action="select-store" data-index="${i}">
        ${thumb}
        <div style="flex:1;min-width:0">
          <div class="store-result-name">${escH(s.name)} <span style="font-weight:400;color:var(--ink-soft);font-size:11px">#${escH(s.id)}</span></div>
          <div class="store-result-addr">${escH(addr)}</div>
          ${dist}
        </div>
        <span style="color:var(--green);font-size:18px">›</span>
      </div>`;
    }).join('');
    // cache stores for selectStore
    window._storeSearchResults = stores;
  }catch(e){
    resultsEl.innerHTML = `<div class="store-no-results" style="color:var(--red)">Search failed: ${escH(e.message)}</div>`;
  }
}

function selectStore(idx){
  const s = (window._storeSearchResults||[])[idx];
  if(!s) return;
  setStoreSel(s);
  savePrefs('store').catch(()=>{});
  if(s.id !== _dealsStoreId) preloadDeals();
}

// ═══════════════════════════════════════════════════════════════════
// ITEMS
// ═══════════════════════════════════════════════════════════════════
// Items stored as strings; deal-checkbox adds title string
function renderItems(){
  const items=(prefs&&prefs.items)||[];
  document.getElementById('items-ul').innerHTML=items.map((item,i)=>{
    const name=decodeEntities(itemName(item));
    return `<li class="item-row">
      <span class="item-name">${escH(name)}<br><span class="item-freq list-item-freq" data-item="${escH(name)}"></span></span>
      <div class="item-row-actions">
        <button class="btn-rm" data-action="remove-item" data-index="${i}">×</button>
      </div>
    </li>`;
  }).join('');
  document.getElementById('items-ct').textContent=items.length+' item'+(items.length!==1?'s':'');
  renderFreqHints();
}
function addItem(){
  const inp=document.getElementById('new-item');const v=decodeEntities(inp.value.trim());
  if(!v) return;if(!prefs) prefs={};if(!prefs.items) prefs.items=[];
  if(!itemNames().map(n=>n.toLowerCase()).includes(v.toLowerCase())){prefs.items.push(v);renderItems();savePrefs('list').catch(()=>{});}
  inp.value='';inp.focus();
}
function removeItem(i){if(!prefs||!prefs.items) return;prefs.items.splice(i,1);renderItems();syncDealCheckboxes();savePrefs('list').catch(()=>{});}
function bulkImport(){
  const lines=document.getElementById('bulk-ta').value.split('\n').map(l=>l.trim()).filter(Boolean);
  if(!prefs) prefs={};if(!prefs.items) prefs.items=[];
  const existing=new Set(itemNames().map(n=>n.toLowerCase()));
  lines.forEach(l=>{if(!existing.has(l.toLowerCase())){prefs.items.push(l);existing.add(l.toLowerCase());}});
  renderItems();document.getElementById('bulk-ta').value='';syncDealCheckboxes();
  savePrefs('list').catch(()=>{});
}
function copyInboundAddr(){
  const addr=document.getElementById('inbound-addr-display').textContent.trim();
  const btn=document.getElementById('copy-inbound-btn');
  const flash=()=>{btn.textContent='Copied!';btn.style.color='var(--green)';setTimeout(()=>{btn.textContent='Copy';btn.style.color='';},2000);};
  if(navigator.clipboard&&window.isSecureContext){
    navigator.clipboard.writeText(addr).then(flash).catch(()=>{});
  } else {
    const ta=document.createElement('textarea');
    ta.value=addr;ta.style.position='fixed';ta.style.opacity='0';
    document.body.appendChild(ta);ta.select();
    try{document.execCommand('copy');flash();}catch(_){}
    document.body.removeChild(ta);
  }
}
function exportListDownload(){
  const names=itemNames();
  if(!names.length){alert('Your list is empty.');return;}
  const text=names.join('\n');
  const blob=new Blob([text],{type:'text/plain'});
  const a=document.createElement('a');
  a.href=URL.createObjectURL(blob);
  a.download='shopping-list.txt';
  a.click();
  URL.revokeObjectURL(a.href);
}
async function exportListCopy(btn){
  const names=itemNames();
  if(!names.length){alert('Your list is empty.');return;}
  try{
    await navigator.clipboard.writeText(names.join('\n'));
    const orig=btn.textContent;
    btn.textContent='✓ Copied!';
    setTimeout(()=>btn.textContent=orig,2000);
  }catch(e){
    // Fallback for browsers that block clipboard without user gesture
    const ta=document.createElement('textarea');
    ta.value=names.join('\n');
    ta.style.position='fixed';ta.style.opacity='0';
    document.body.appendChild(ta);ta.select();
    document.execCommand('copy');
    document.body.removeChild(ta);
    const orig=btn.textContent;
    btn.textContent='✓ Copied!';
    setTimeout(()=>btn.textContent=orig,2000);
  }
}

// ═══════════════════════════════════════════════════════════════════
// ACCOUNT
// ═══════════════════════════════════════════════════════════════════
async function changePin(){
  const cur=getPin('.cur-pin'),nw=getPin('.new-pin');
  if(cur.length!==4||nw.length!==4){setMsg('pin-msg','Fill in both PINs.','err');return;}
  try{
    const res=await fetch(API+'/auth/change-pin',{method:'POST',credentials:'include',headers:{...authHeaders(),'Content-Type':'application/json'},body:JSON.stringify({current_pin:cur,new_pin:nw})});
    const data=await res.json();
    if(!res.ok) throw new Error(data.error||'Failed');
    setMsg('pin-msg','✓ PIN updated — signing you out…','ok');clrPin('.cur-pin');clrPin('.new-pin');
    // PDC-21: all sessions invalidated on PIN change — redirect to login
    setTimeout(()=>doLogout(), 1500);
  }catch(e){setMsg('pin-msg',e.message,'err');}
}
async function deleteAccount(){
  closeOv('del-overlay');
  try{await fetch(API+'/user/account',{method:'DELETE',credentials:'include',headers:authHeaders()});}catch(_){}
  doLogout();
}
async function resendWeeklyEmail(){
  const btn=document.getElementById('btn-resend-weekly');
  if(btn){btn.disabled=true;btn.textContent='Queuing…';}
  try{
    const res=await fetch(API+'/user/resend-weekly',{method:'POST',credentials:'include',headers:authHeaders()});
    const d=await res.json();
    if(res.ok){
      // Accurate timing: Lambda cold start + scrape + email delivery = 1–2 min realistically
      setMsg('test-email-msg',d.message||'Email queued — check your inbox in a few minutes.','ok');
      if(btn){btn.textContent='✓ Queued';setTimeout(()=>{btn.textContent='🛒 Resend This Week\'s Deals';btn.disabled=false;},4000);}
    } else {
      setMsg('test-email-msg',d.error||'Failed to queue email.','err');
      if(btn){btn.disabled=false;btn.textContent='🛒 Resend This Week\'s Deals';}
    }
  }catch(e){
    setMsg('test-email-msg','Network error.','err');
    if(btn){btn.disabled=false;btn.textContent='🛒 Resend This Week\'s Deals';}
  }
}

async function sendTestEmail(){
  const btn=document.getElementById('btn-test-email');
  const msgEl=document.getElementById('test-email-msg');
  btn.disabled=true;msgEl.textContent='Sending…';msgEl.className='';
  try{
    const res=await fetch(API+'/user/test-email',{method:'POST',credentials:'include',headers:authHeaders()});
    const data=await res.json();
    if(!res.ok) throw new Error(data.error||'Failed');
    msgEl.textContent='✓ '+data.message;msgEl.style.color='var(--green)';
    setTimeout(()=>{msgEl.textContent='';},5000);
  }catch(e){msgEl.textContent='✗ '+e.message;msgEl.style.color='var(--red)';}
  finally{btn.disabled=false;}
}

// ═══════════════════════════════════════════════════════════════════
// DEALS — PRELOAD + RENDER
// ═══════════════════════════════════════════════════════════════════
const DEPT_EMOJI={
  'Meat':'🥩','Produce':'🥦','Dairy':'🧀','Deli':'🥪','Bakery':'🍞',
  'Frozen':'🧊','Seafood':'🐟','Beverages':'🥤','Floral':'💐',
  'Pharmacy':'💊','Health & Beauty':'💄','Household':'🧹','Baby':'👶',
  'Pet':'🐾','Pets':'🐾','Natural Foods':'🌿','Snacks':'🍿',
  'Canned & Packaged':'🥫','Breakfast':'🥞','Condiments':'🫙',
  'International':'🌍','Pasta & Grains':'🍝','default':'🏷️'
};
function deptEmoji(d){
  if(!d) return DEPT_EMOJI.default;
  for(const[k,v] of Object.entries(DEPT_EMOJI)){if(d.toLowerCase().includes(k.toLowerCase()))return v;}
  return DEPT_EMOJI.default;
}

// Preload: called on login and on store change. Skips if same store & already loaded.
// Helper: is a deal a BOGO — trust server-side flag first, fall back to title/categories
function isBogo(d){
  if(d.is_bogo===true) return true;
  const combined=((d.title||'')+' '+(d.savings||'')).toLowerCase();
  return /\bbogo\b|b1g1|b2g1|buy \d.{0,8}get \d|buy one.{0,10}get one/.test(combined);
}
// Helper: is a deal "extra savings"
function isExtra(d){
  return d.saving_type==='ExtraSavings' || isStacked(d) || d.is_extra===true;
}
// Publix sends 'Stacked'; older code expected 'StackedDeals'. Accept both (v9.2.2 #7).
function isStacked(d){
  return d.saving_type==='Stacked' || d.saving_type==='StackedDeals' || d.is_stacked===true;
}
// Temporary price reductions ("Tpr", mixed case from Publix)
function isTpr(d){
  return String(d.saving_type||'').toLowerCase()==='tpr';
}
// v9.2.2 (#7): counts refresh after core load AND after the TPR merge
function updateFilterCounts(){
  const set=(id,n)=>{const el=document.getElementById(id); if(el) el.textContent=n||'';};
  set('fcnt-all',    Math.max(_totalAllDeals||0, _allDeals.length));
  set('fcnt-weekly', _allDeals.filter(d=>d.saving_type==='WeeklyAd'&&!isBogo(d)).length);
  set('fcnt-tpr',    _allDeals.filter(d=>isTpr(d)&&!isBogo(d)).length);
  set('fcnt-bogo',   _allDeals.filter(d=>isBogo(d)).length);
  set('fcnt-coupon', _allDeals.filter(d=>isCoupon(d)).length);
  set('fcnt-extra',  _allDeals.filter(d=>isExtra(d)).length);
}
// Helper: is a deal a digital coupon
function isCoupon(d){
  return d.saving_type==='DigitalCoupon' || d.has_coupon;
}

async function preloadDeals() {
  const sid=prefs?.store_id;
  if(!sid) return;
  // Only reload if we don't have deals yet for this store,
  // or if the server's updated_at timestamp is newer than what we last loaded.
  // We do a lightweight check: if _allDeals is empty or store changed, always load.
  // Otherwise, reload only when navigating to deals/matches if the server timestamp changed.
  if(_dealsStoreId !== sid || !_allDeals.length) {
    await loadDeals(false);
  } else {
    // Already have deals for this store — check if server has newer data
    const cachedTs = localStorage.getItem('cw_deals_ts_'+sid) || '';
    if(_dealsUpdated && cachedTs === _dealsUpdated) return; // still fresh
    await loadDeals(false);
  }
}

let _dealsLoading = false;  // guard against concurrent loadDeals() calls

async function loadDeals(forceRefresh=false){
  if (_dealsLoading) return;  // already in flight — skip duplicate
  _dealsLoading = true;
  const sid=prefs?.store_id;
  if(!sid){
    _dealsLoading = false;
    document.getElementById('deals-err').textContent='Set your store first (Store panel).';
    document.getElementById('deals-err').style.display='';
    return;
  }
  // Show loading state
  document.getElementById('deals-err').style.display='none';
  document.getElementById('deals-browser').style.display='none';
  document.getElementById('deals-loading').style.display='block';
  const refreshBtn=document.getElementById('btn-refresh-deals');
  if(refreshBtn) refreshBtn.disabled=true;

  try{
    _tprLoaded = false;  // new deal set — TPR needs re-fetching
    _pendingTpr = null;
    const gen = ++_dealsGen;
    // Fire TPR fetch in parallel — don't await it, let it resolve in background
    // while core deals load, render, and paint. Either response may arrive first.
    // v9.6: "↻ Refresh" bypasses the browser's 5-minute cache (it used to re-show the cached copy)
    const cacheMode = forceRefresh ? 'reload' : 'default';
    loadTprDeals(sid, gen, cacheMode);

    const res=await fetch(API+'/deals?store_id='+encodeURIComponent(sid)+'&types=core', {credentials:'include',headers:authHeaders(),cache:cacheMode});
    const data=await res.json();
    if(!res.ok) throw new Error(data.error||'Failed to load deals');

    // Decode HTML entities from API data at the source so all downstream
    // rendering (autocomplete, cards, modals) gets clean strings
    _allDeals     = (data.deals||[]).map(d => ({
      ...d,
      title:       decodeEntities(d.title       || ''),
      description: decodeEntities(d.description || ''),
      save_line:   decodeEntities(d.save_line   || ''),
      savings:     decodeEntities(d.savings     || ''),
      fine_print:  decodeEntities(d.fine_print  || ''),
      department:  decodeEntities(d.department  || ''),
      brand:       decodeEntities(d.brand       || ''),
    }));
    _dealCounts   = data.counts||{};
    _deptCounts   = data.dept_counts||{};
    _dealsUpdated = data.updated_at||'';
    _dealsStoreId = sid;
    _savingTypes  = data.saving_types||[];
    _totalAllDeals= data.total_all || _allDeals.length;

    // Cache the timestamp so preloadDeals can skip reloading when data hasn't changed
    if(_dealsUpdated) localStorage.setItem('cw_deals_ts_'+sid, _dealsUpdated);

    updateFilterCounts();

    // Validity sublabels on banner
    const wa=_allDeals.find(d=>d.saving_type==='WeeklyAd'&&hasValidDates(d));
    const ex=_allDeals.find(d=>d.is_extra&&hasValidDates(d));

    // Validity banner
    if(wa){
      const vb=document.getElementById('deals-vbar');
      vb.innerHTML=`📅 Weekly Ad: Valid <strong>${escH(wa.valid_from)}</strong> to <strong>${escH(wa.valid_thru)}</strong> &nbsp;·&nbsp; ${_totalAllDeals} total deals`;
      vb.classList.add('vis');
    }

    // Dept filters
    buildDeptFilters('dept-deals', _deptCounts, 'renderDeals');

    const storeName=prefs?.store_name||('Store #'+sid);
    const _dealsSubEl=document.getElementById('deals-sub'); if(_dealsSubEl) _dealsSubEl.textContent=`${storeName} · ${_totalAllDeals} all deals`;

    document.getElementById('deals-loading').style.display='none';
    document.getElementById('deals-browser').style.display='';
    document.getElementById('deals-q').value='';
    // Pre-tokenize all deals for fast search filtering
    _dealTokenCache = new Map();
    for (const d of _allDeals) {
      _dealTokenCache.set(d.id, [...cwTokenize(d.title||''), ...cwTokenize(d.brand||'')]);
    }
    precomputeBadges();   // recompute badges with fresh deals (history may already be loaded)
    _coreGen = gen;
    if (_pendingTpr && _pendingTpr.gen === gen) {
      // TPR beat the core response: merge it now (mergeTprDeals re-runs matches + render)
      const parked = _pendingTpr; _pendingTpr = null;
      mergeTprDeals(parked.data);
    } else {
      _matchesDirty = true;  // deals just refreshed — recompute matches
      loadMatches();          // computes matches then calls renderDeals()
    }

  }catch(e){
    document.getElementById('deals-loading').style.display='none';
    document.getElementById('deals-err').textContent=e.message;
    document.getElementById('deals-err').style.display='';
  }finally{
    _dealsLoading = false;
    if(refreshBtn) refreshBtn.disabled=false;
  }
}

function buildDeptFilters(containerId, deptCounts, renderFn){
  const el=document.getElementById(containerId);
  if(!el) return;
  const depts=Object.keys(deptCounts).sort();
  el.innerHTML=depts.map(dept=>{
    const label=decodeEntities(dept);
    return `<label class="filter-opt">
      <input type="checkbox" class="dept-cb" data-container="${containerId}" data-dept="${escH(dept)}" data-action="dept-filter" data-render-fn="${renderFn}">
      <div><div class="filter-opt-label">${escH(label)}</div></div>
      <span class="filter-opt-cnt">${deptCounts[dept]}</span>
    </label>`;
  }).join('');
}

function getCheckedDepts(cid){
  return [...document.querySelectorAll(`#${cid} .dept-cb:checked`)].map(c=>c.dataset.dept);
}
function clearDeptCB(cid, dept){
  const el=document.querySelector(`#${cid} .dept-cb[data-dept="${dept}"]`);
  if(el) el.checked=false;
}

// Savings filter: selecting one unchecks others; if none checked, default to Weekly ad
const SAVINGS_SUBFILTERS=['f-weekly','f-tpr','f-bogo','f-coupon','f-extra'];
function onSavingsFilter(changed){
  const ids=['f-all',...SAVINGS_SUBFILTERS];
  const els=Object.fromEntries(ids.map(id=>[id,document.getElementById(id)]));
  if(changed==='all' && els['f-all'].checked){
    // All deals — uncheck everything else
    SAVINGS_SUBFILTERS.forEach(id=>els[id].checked=false);
  } else if(changed!=='all'){
    els['f-all'].checked=false;
    // If nothing checked, revert to all defaults
    const anyChecked=SAVINGS_SUBFILTERS.some(id=>els[id].checked);
    if(!anyChecked) SAVINGS_SUBFILTERS.forEach(id=>els[id].checked=true);
  }
  renderDeals();
}

// ── Sort helper ───────────────────────────────────────────────────────────────
function applySortOrder(arr, sortVal) {
  const a = arr.slice(); // don't mutate original
  if (sortVal === 'name-az') {
    a.sort((x,y) => (x.title||'').localeCompare(y.title||''));
  } else if (sortVal === 'name-za') {
    a.sort((x,y) => (y.title||'').localeCompare(x.title||''));
  } else if (sortVal === 'savings-desc') {
    // Items with no parseable savings value (-Infinity) always sort last
    a.sort((x,y) => {
      const sx = dealSortSavings(x), sy = dealSortSavings(y);
      if (!isFinite(sx) && !isFinite(sy)) return 0;
      if (!isFinite(sx)) return 1;
      if (!isFinite(sy)) return -1;
      return sy - sx;
    });
  } else if (sortVal === 'pct-desc' || sortVal === 'price-asc') {
    // v9.4 (#8): % off (exact savings only) and lowest price you pay per unit.
    // Deals without the number sort last, in their original order.
    const key = sortVal === 'pct-desc'
      ? (d => (d.offer && d.offer.pct) || -Infinity)
      : (d => (d.offer && typeof d.offer.price === 'number') ? -d.offer.price : -Infinity);
    a.sort((x,y) => {
      const kx = key(x), ky = key(y);
      if (!isFinite(kx) && !isFinite(ky)) return 0;
      if (!isFinite(kx)) return 1;
      if (!isFinite(ky)) return -1;
      return ky - kx;
    });
  } else if (sortVal === 'score-desc') {
    a.sort((x,y) => (y.match_score||0) - (x.match_score||0));
  }
  // 'default' — leave in original order
  return a;
}

// ── CartWise Matcher (JS port of shared/matcher.py) ──────────────────────────
// KEEP IN SYNC with shared/matcher.py — identical algorithm, identical results.
// Changes to scoring logic must be mirrored in both files.

const CW_SENSITIVITY_THRESHOLDS = { strict: 80, normal: 65, loose: 50 };
const CW_NOISE = new Set([
  'a','an','the','and','or','of','for','with','in','on','to',
  'from','by','at','is','it','its','any','all','per','each',
  'oz','fl','lb','lbs','ct','pk','pt','qt','gal','ml','mg',
  'kg','liter','liters','ounce','ounces','pound','pounds',
  'count','pack','size',
]);

// v9.4 (#10): identical to normalize() in shared/matcher.py. Python DELETES
// punctuation ("Jerry's" -> "jerrys") and its \w is Unicode-aware; the old JS
// replaced it with a space ("jerry s") and was ASCII-only, so ~1.3% of scores
// differed between the web page and the email. tests/matcher_parity.test.js checks this.
function cwNormalize(text) {
  if (!text) return '';
  return text
    .replace(/[^\p{L}\p{N}\p{M}_\s-]/gu, '')   // strip specials, keep hyphens
    .replace(/\s+/g, ' ')
    .trim()
    .toLowerCase();
}

// Python's round(): halves go to the even neighbour (round(32.5) == 32)
function cwPyRound(x) {
  const r = Math.round(x);
  return (Math.abs(x % 1) === 0.5 && r % 2 !== 0) ? r - 1 : r;
}

function cwTokenize(text) {
  return cwNormalize(text).split(' ').filter(Boolean);
}

function cwCleanTokens(tokens) {
  return tokens.filter(t => !CW_NOISE.has(t) && !/^\d+$/.test(t));
}

function cwGetThreshold(matchingCfg) {
  const raw = matchingCfg && matchingCfg.threshold;
  if (typeof raw === 'number' && raw >= 40 && raw <= 100) return Math.round(raw);
  const sens = (matchingCfg && matchingCfg.sensitivity) || 'normal';
  return CW_SENSITIVITY_THRESHOLDS[sens] || CW_SENSITIVITY_THRESHOLDS.normal;
}

function cwMatchScore(itemStr, deal) {
  if (!itemStr) return 0;
  const title       = deal.title       || '';
  const brand       = deal.brand       || '';
  const description = deal.description || '';
  if (!title) return 0;

  const itemNorm = cwNormalize(itemStr);
  if (!itemNorm) return 0;

  // Build deduplicated composite: brand + title + description
  const seen = new Set();
  const compositeParts = [];
  for (const field of [brand, title, description]) {
    for (const t of cwTokenize(field)) {
      if (!seen.has(t)) { seen.add(t); compositeParts.push(t); }
    }
  }
  const compositeTokens = compositeParts;
  const titleTokens = cwTokenize(title);

  // Clean item tokens; fall back to raw if cleaning removes everything
  const itemTokensRaw = cwTokenize(itemStr);
  let itemTokens = cwCleanTokens(itemTokensRaw);
  if (!itemTokens.length) itemTokens = itemTokensRaw;
  if (!itemTokens.length || !compositeTokens.length) return 0;

  // Exact title match
  if (itemNorm === cwNormalize(title)) return 100;

  // Count matched item tokens (exact or prefix for plurals, min 5 chars)
  let matched = 0;
  for (const iw of itemTokens) {
    if (compositeTokens.includes(iw)) {
      matched++;
    } else if (iw.length >= 5 && compositeTokens.some(ct =>
      ct.length >= 5 && (iw.startsWith(ct) || ct.startsWith(iw))
    )) {
      matched++;
    }
  }
  if (!matched) return 0;

  const precision = matched / itemTokens.length;

  // Coverage: fraction of title words the item covers
  const titleClean = cwCleanTokens(titleTokens).length ? cwCleanTokens(titleTokens) : titleTokens;
  let titleMatched = 0;
  for (const tw of titleClean) {
    if (itemTokens.includes(tw)) {
      titleMatched++;
    } else if (tw.length >= 5 && itemTokens.some(iw =>
      iw.length >= 5 && (tw.startsWith(iw) || iw.startsWith(tw))
    )) {
      titleMatched++;
    }
  }
  const coverage = titleClean.length ? titleMatched / titleClean.length : 0;

  // Single-word items: broad match
  if (itemTokens.length === 1) return matched ? 100 : 0;

  // Brand boost/penalty
  const brandNorm = cwNormalize(brand);
  if (brandNorm) {
    const brandTokens = brandNorm.split(' ').filter(Boolean);
    const itemHasBrand = itemTokens.some(iw =>
      brandTokens.includes(iw) ||
      brandTokens.some(bt => iw.length >= 5 && bt.length >= 5 && (iw.startsWith(bt) || bt.startsWith(iw)))
    );
    const itemBrandCandidates = itemTokens.filter(t => t.length >= 4 && !brandTokens.includes(t));
    if (itemBrandCandidates.length && !itemHasBrand) {
      const firstCandidate = itemBrandCandidates[0];
      const inTitle = titleTokens.includes(firstCandidate) || titleTokens.some(tt =>
        firstCandidate.length >= 5 && tt.length >= 5 &&
        (firstCandidate.startsWith(tt) || tt.startsWith(firstCandidate))
      );
      if (!inTitle) return Math.min(45, cwPyRound((precision * 0.65 + coverage * 0.35) * 100));
    }
    // Brand match bonus
    // (precision already set; apply inline below)
    const boostedPrecision = itemHasBrand ? Math.min(1.0, precision * 1.1) : precision;
    const score = cwPyRound((boostedPrecision * 0.65 + coverage * 0.35) * 100);
    return Math.min(100, score);
  }

  return Math.min(100, cwPyRound((precision * 0.65 + coverage * 0.35) * 100));
}

function cwMatchDealsForUser(deals, items, matchingCfg) {
  const threshold = cwGetThreshold(matchingCfg);
  const matches = [];
  const seen = new Set();

  for (const deal of deals) {
    if (!deal.title) continue;
    for (const item of items) {
      const name = typeof item === 'string' ? item : (item && item.name) || '';
      if (!name) continue;

      const score = cwMatchScore(name, deal);
      if (score >= threshold) {
        const key = deal.id || deal.title;
        if (!seen.has(key)) {
          seen.add(key);
          matches.push({
            ...deal,
            my_item:     name,
            deal_name:   deal.title,
            match_score: score,
          });
        }
      }
    }
  }

  matches.sort((a, b) => b.match_score - a.match_score);
  return matches;
}
// ── End CartWise Matcher ──────────────────────────────────────────────────────


function renderDeals(keepPage){
  if (!keepPage) _dealsPageLimit = DEALS_PAGE_SIZE;  // reset on filter/search/sort change
  const q    =(document.getElementById('deals-q').value||'').toLowerCase();
  const fAll =document.getElementById('f-all').checked;
  const fW   =document.getElementById('f-weekly').checked;
  const fBogo=document.getElementById('f-bogo').checked;
  const fCo  =document.getElementById('f-coupon').checked;
  const fE   =document.getElementById('f-extra').checked;
  const fTpr =(document.getElementById('f-tpr')||{checked:true}).checked;
  const depts=getCheckedDepts('dept-deals');
  const showMatchesFirst = (document.getElementById('matches-toggle')||{}).checked !== false;

  let deals=_allDeals.filter(d=>{
    if(fAll) return true;
    if(fBogo && isBogo(d))   return true;
    if(fCo   && isCoupon(d)) return true;
    if(fE    && isExtra(d))  return true;
    if(fW    && d.saving_type==='WeeklyAd' && !isBogo(d)) return true;
    if(fTpr  && isTpr(d)) return true;
    return false;
  });
  if(depts.length) deals=deals.filter(d=>depts.includes(d.department||''));
  if(q) {
    const qTerms = cwTokenize(q);
    deals = deals.filter(d => {
      const hay = _dealTokenCache.get(d.id) || cwTokenize((d.title||'') + ' ' + (d.brand||''));
      return qTerms.every(t =>
        hay.includes(t) ||
        (t.length >= 5 && hay.some(ht => ht.length >= 5 && (ht.startsWith(t) || t.startsWith(ht))))
      );
    });
  }

  // Chips
  const chips=[];
  depts.forEach(d=>chips.push(decodeEntities(d)));
  document.getElementById('deals-chips').innerHTML=chips.map(lbl=>
    `<span class="chip" data-action="clear-dept-chip" data-container="dept-deals" data-dept="${escH(lbl)}">${escH(lbl)} <span class="chip-x">×</span></span>`
  ).join('');

  // ── Matches section ──────────────────────────────────────────────────────────
  const matchSection = document.getElementById('matches-section');
  const matchGrid    = document.getElementById('matches-grid');
  const matchIdsInView = new Set();

  if(showMatchesFirst && _allMatches.length && matchSection && matchGrid) {
    // Filter matches by the same search query (not by deal type filter — matches are always shown)
    let visibleMatches = _allMatches;
    if(q) {
      const qTerms = cwTokenize(q);
      visibleMatches = visibleMatches.filter(d => {
        const hay = _dealTokenCache.get(d.id) || cwTokenize((d.title||'') + ' ' + (d.my_item||''));
        return qTerms.every(t =>
          hay.includes(t) ||
          (t.length >= 5 && hay.some(ht => ht.length >= 5 && (ht.startsWith(t) || t.startsWith(ht))))
        );
      });
    }

    if(visibleMatches.length) {
      const sortedMatches = applySortOrder(visibleMatches, 'score-desc');
      matchGrid.innerHTML = sortedMatches.map(d=>matchCardHtml(d)).join('');
      matchSection.style.display = '';
      sortedMatches.forEach(d=>matchIdsInView.add(d.id));

      // Quality banner
      const bannerEl = document.getElementById('quality-banner');
      if(bannerEl){
        if(_dealHistory && (_dealHistory.num_weeks||0) >= HIST_MIN.banner && visibleMatches.length){
          const bannerText=buildQualityBanner(visibleMatches,_dealHistory);
          if(bannerText){
            bannerEl.innerHTML=`<div class="quality-banner-hd">This week's highlights</div>${bannerText}`;
            bannerEl.className='quality-banner';bannerEl.style.display='';
          } else { bannerEl.style.display='none'; }
        } else { bannerEl.style.display='none'; }
      }
    } else {
      matchSection.style.display = 'none';
    }
  } else if(matchSection) {
    matchSection.style.display = 'none';
  }

  // ── All Deals grid (exclude items already shown as matches) ─────────────────
  const dealsToShow = showMatchesFirst && matchIdsInView.size
    ? deals.filter(d=>!matchIdsInView.has(d.id))
    : deals;

  // Meta count
  const totalShown = dealsToShow.length + matchIdsInView.size;
  const visibleCount = Math.min(dealsToShow.length, _dealsPageLimit);
  const totalLabel = _totalAllDeals || _allDeals.length;
  document.getElementById('deals-meta').textContent =
    matchIdsInView.size
      ? `${matchIdsInView.size} match${matchIdsInView.size!==1?'es':''} · showing ${visibleCount} of ${dealsToShow.length} other deals`
      : dealsToShow.length > _dealsPageLimit
        ? `Showing ${visibleCount} of ${deals.length} deals`
        : `${deals.length} of ${totalLabel} deals`;

  const grid=document.getElementById('deals-grid');
  if(!dealsToShow.length && !matchIdsInView.size){
    grid.innerHTML='<div class="empty"><div class="empty-icon">🔍</div>No deals match your filters.</div>';
    syncDealCheckboxes(); attachDealCBListeners(); return;
  }
  if(!dealsToShow.length){
    grid.innerHTML='';
    syncDealCheckboxes(); attachDealCBListeners(); return;
  }
  const dealsSortVal = (document.getElementById('deals-sort')||{}).value || 'default';
  const sortedDeals = applySortOrder(dealsToShow, dealsSortVal);
  const pageDeals   = sortedDeals.slice(0, _dealsPageLimit);
  const remaining   = sortedDeals.length - pageDeals.length;
  _dealsShownTotal = totalShown;
  updateFilterSheetCount();
  grid.innerHTML = pageDeals.map(d=>dealCardHtml(d)).join('')
    + (remaining > 0
      ? `<div class="load-more-wrap"><button class="btn-load-more" data-action="load-more-deals">Show ${Math.min(remaining, DEALS_PAGE_SIZE)} more deal${Math.min(remaining, DEALS_PAGE_SIZE)!==1?'s':''} <span class="load-more-ct">(${remaining} remaining)</span></button></div>`
      : '');
  syncDealCheckboxes();
  attachDealCBListeners();
}

// v9.4: one card for matches and deals (design canvas "Unified deal card").
// Tag over the image (one per card, plus "+ Coupon"), headline colored by what it
// means, save line in red, and a footer with the matched item / history chip.
function matchCardHtml(d){ return dealCardHtml(d, true); }

// ── Deal type helpers ─────────────────────────────────────────────────────────
// v9.4: one tag per deal, same order and labels as the email (shared/email_render.py deal_tag)
// Sale = weekly ad price, price drop or $ off; the source feed moves to the modal.
function dealTypeKey(d) {
  const o = d.offer || {};
  if (o.kind === 'bogo' || isBogo(d)) return 'bogo';
  if (isCoupon(d) && !isStacked(d)) return 'coupon';
  if (isStacked(d) || isExtra(d)) return 'extra';
  if (o.kind === 'multi') return 'multi';
  return 'sale';
}
function dealTypeLabel(d, k) {
  if (k === 'multi') { const m = /^(\d+) for/.exec((d.offer||{}).headline||''); return m ? `${m[1]} for` : 'Multi-buy'; }
  return { bogo:'BOGO', coupon:'Coupon', extra:'Extra', sale:'Sale' }[k];
}
function dealTypeTag(d) {
  const k = dealTypeKey(d);
  return `<span class="deal-type-tag ${k}">${escH(dealTypeLabel(d, k))}</span>`;
}

// ── savingsDisplay: consistent card output for all deal types ────────────────
// Returns {text, pctx, save, isDiscount, isClip}
//   text       = main price line (green) or deal text (amber)
//   pctx       = gray price context on same line (e.g. "2 for $25.99")
//   save       = red save line (e.g. "Save $3.00") or null
//   isDiscount = true → text is amber, false → text is green
//   isClip     = true → save row shows "Clip coupon" instead
// v9.3: the server normalizes every deal into `offer` (shared/deal_parser.py
// classify_offer). Render that when present; the guessing below is only for
// responses cached before v9.3.
//   green  = what you pay (price, multi-buy)
//   amber  = a mechanic or an amount off (BOGO, $2 off, 20% off)
function savingsDisplay(d) {
  const o = d.offer ? applyBogoMode(d.offer, effectiveBogoMode(prefs)) : d.offer;   // v9.8
  if (o && o.headline) {
    const kind = o.kind || 'other';
    let save = o.save_text || null;
    if (save && o.pct) save += ` (${o.pct}%)`;
    return {
      text: o.headline,
      pctx: o.context || null,
      save,
      extra: o.extra || null,
      isDiscount: !(kind === 'price' || kind === 'multi'),
      isClip: !!(d.has_coupon && !save),
      kind,
    };
  }
  return savingsDisplayLegacy(d);
}

// Pre-v9.3 responses had no `offer`; the API now always sends one. Minimal fallback.
function savingsDisplayLegacy(d) {
  const sv = decodeEntities(d.savings || '');
  return { text: sv, pctx: null, save: decodeEntities(d.save_line || '') || null, extra: null,
           isDiscount: !/^\$\d/.test(sv), isClip: !!d.has_coupon, kind: null };
}

// Generate deal card HTML (checkbox overlay)
// ── Deal detail modal ─────────────────────────────────────────────────────────

// Human-readable labels for saving_type values from the Publix API
const SAVING_TYPE_LABEL = {
  WeeklyAd:        'Weekly Ad',
  AllDeals:        'All Deals',
  DigitalCoupon:   'Digital Coupon',
  PrintableCoupon: 'Printable Coupon',
  ExtraSavings:    'Extra Savings',
  StackedDeals:    'Stacked Deal',
  Stacked:         'Stacked Deal',
  Tpr:             'Price drop',
};

// v9.8.2: SAVINGS history chart for the deal modal.
// - One dot per week the item was on sale, at what Publix said it saved per item
//   (per lb for weighed items). Higher = bigger discount. No prices, no estimates.
// - Dot color = deal type, same colors as the card tags (KEEP IN SYNC with
//   .deal-type-tag in index.html and TAGS in shared/email_render.py).
// - The line only joins CONSECUTIVE on-sale weeks; off-sale weeks get a tick on
//   the baseline. A hollow dot on the baseline = on sale, amount not listed.
// - Hover, tap or focus a dot for the week, the amount and Publix's own text.
// points: [{week, savings, kind, save?, max?, unit?, pct?}] oldest first;
// weeks: the store's snapshot weeks oldest first; newest: this week.
const DEAL_KIND_STYLE = {
  sale:   { c: '#1a6b3c', l: 'Sale' },
  bogo:   { c: '#c2650a', l: 'BOGO' },
  multi:  { c: '#0e7490', l: 'Multi-buy' },
  coupon: { c: '#7e22ce', l: 'Coupon' },
  extra:  { c: '#1d4ed8', l: 'Extra Savings' },
};
const MONTHS = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
const fmtMoney = v => '$' + (Math.round(v * 100) % 100 ? v.toFixed(2) : v.toFixed(0));
const kindStyle = k => DEAL_KIND_STYLE[k] || DEAL_KIND_STYLE.sale;

function chartPoints(points) {
  const seen = new Set();
  return (points || []).filter(p => !seen.has(p.week) && seen.add(p.week));
}

function chartAxis(pts, weeks, newest) {
  const first = pts[0].week;
  const axis = (weeks || []).filter(w => w >= first && (!newest || w <= newest));
  return axis.length >= pts.length ? axis : pts.map(p => p.week);
}

function saveText(p) {
  const u = p.unit === 'lb' ? '/lb' : ' ea';
  if (typeof p.save === 'number') return `Save ${p.max ? 'up to ' : ''}${fmtMoney(p.save)}${u}`;
  return p.pct ? `${p.pct}% off` : 'Amount not listed';
}

function weekLabel(w) {
  const d = new Date(w + 'T12:00:00');
  return `Week of ${MONTHS[d.getMonth()]} ${d.getDate()}, ${d.getFullYear()}`;
}

function buildDealChart(svgEl, points, weeks, newest) {
  const pts = chartPoints(points);
  const W = Math.max(svgEl.getBoundingClientRect().width || 0, 260);
  const H = 132, PAD = { l: 44, r: 16, t: 14, b: 30 };
  const known = pts.filter(p => typeof p.save === 'number');
  if (pts.length < 2 || !known.length) {
    svgEl.setAttribute('viewBox', `0 0 ${W} ${H}`);
    svgEl.innerHTML = `<text x="${W/2}" y="${H/2}" text-anchor="middle" font-size="13" fill="#6b6b6b" dy=".35em">Not enough savings history yet</text>`;
    return [];
  }
  const axis = chartAxis(pts, weeks, newest);
  const pos = new Map(axis.map((w, i) => [w, i]));
  const byWeek = new Map(pts.map(p => [p.week, p]));
  const cx = w => PAD.l + (pos.get(w) / Math.max(axis.length - 1, 1)) * (W - PAD.l - PAD.r);
  const base = H - PAD.b;
  const max = Math.max(...known.map(p => p.save));
  const cy = v => base - (v / (max * 1.15)) * (base - PAD.t);     // bigger discount = higher
  const AX = '#6f6a60', GRID = '#d9d3c7';
  let svg = '';

  svg += `<line x1="${PAD.l}" y1="${base}" x2="${W-PAD.r}" y2="${base}" stroke="${GRID}" stroke-width="1.2"/>`;
  svg += `<text x="${PAD.l-7}" y="${base}" text-anchor="end" font-size="11.5" fill="${AX}" dy=".35em">$0</text>`;
  svg += `<text x="${PAD.l-7}" y="${cy(max)}" text-anchor="end" font-size="11.5" fill="${AX}" font-weight="700" dy=".35em">${fmtMoney(max)}</text>`;
  svg += `<line x1="${PAD.l}" y1="${cy(max)}" x2="${W-PAD.r}" y2="${cy(max)}" stroke="rgba(180,83,9,.4)" stroke-width="1" stroke-dasharray="3,4"/>`;

  const seenM = new Set();
  let lastLbl = -99;
  axis.forEach(w => {
    const d = new Date(w + 'T12:00:00'), x = cx(w), mk = `${d.getFullYear()}-${d.getMonth()}`;
    if (!seenM.has(mk) && x > PAD.l + 6 && x - lastLbl >= 34) {
      seenM.add(mk); lastLbl = x;
      svg += `<text x="${x}" y="${base+19}" text-anchor="middle" font-size="11.5" fill="${AX}">${MONTHS[d.getMonth()]}</text>`;
    }
    if (!byWeek.has(w)) svg += `<line x1="${x}" y1="${base-4}" x2="${x}" y2="${base+4}" stroke="#c4bdb0" stroke-width="1.6"/>`;
  });

  for (let i = 1; i < axis.length; i++) {
    const a = byWeek.get(axis[i-1]), b = byWeek.get(axis[i]);
    if (!a || !b || typeof a.save !== 'number' || typeof b.save !== 'number') continue;
    svg += `<line x1="${cx(a.week)}" y1="${cy(a.save)}" x2="${cx(b.week)}" y2="${cy(b.save)}" stroke="#bdb6a8" stroke-width="2"/>`;
  }

  const hits = [];
  pts.forEach((p, i) => {
    const x = cx(p.week), has = typeof p.save === 'number', y = has ? cy(p.save) : base - 5;
    const col = kindStyle(p.kind).c, cur = newest && p.week === newest;
    if (!has) svg += `<circle cx="${x}" cy="${y}" r="4" fill="#fff" stroke="#a39b8c" stroke-width="1.6"/>`;
    else if (cur) svg += `<circle cx="${x}" cy="${y}" r="11" fill="${col}" opacity=".16"/><circle cx="${x}" cy="${y}" r="7" fill="${col}" stroke="#fff" stroke-width="1.8"/>`;
    else svg += `<circle cx="${x}" cy="${y}" r="5.2" fill="${col}" stroke="#fff" stroke-width="1.4"/>`;
    // Invisible, larger hit target for hover / tap / keyboard focus
    svg += `<circle class="dm-hit" data-i="${i}" cx="${x}" cy="${y}" r="12" fill="transparent" tabindex="0" role="img"
      aria-label="${escH(weekLabel(p.week) + ': ' + saveText(p) + ', ' + kindStyle(p.kind).l)}"/>`;
    hits.push({ p, x, y });
  });

  svgEl.setAttribute('viewBox', `0 0 ${W} ${H}`);
  svgEl.innerHTML = svg;
  wireChartTips(svgEl, hits, W, H);
  return hits;
}

// Tooltip: hover on desktop, tap on phones, focus with the keyboard
function wireChartTips(svgEl, hits, W, H) {
  const wrap = svgEl.parentElement;
  let tip = wrap.querySelector('.dm-tip');
  if (!tip) { tip = document.createElement('div'); tip.className = 'dm-tip'; tip.setAttribute('role', 'tooltip'); wrap.appendChild(tip); }
  const hide = () => tip.classList.remove('vis');
  const show = el => {
    const h = hits[+el.dataset.i]; if (!h) return;
    const { p } = h, k = kindStyle(p.kind), txt = decodeEntities(p.savings || '').trim();
    tip.innerHTML = `<div class="dm-tip-wk">${escH(weekLabel(p.week))}</div>
      <div class="dm-tip-amt">${escH(saveText(p))}</div>
      <div class="dm-tip-kind"><i style="background:${k.c}"></i>${escH(k.l)}${txt && txt.toLowerCase() !== k.l.toLowerCase() ? ' · ' + escH(txt) : ''}</div>`;
    const sr = svgEl.getBoundingClientRect(), wr = wrap.getBoundingClientRect();
    const px = sr.left - wr.left + h.x * (sr.width / W), py = sr.top - wr.top + h.y * (sr.height / H);
    tip.classList.add('vis');
    const tw = tip.offsetWidth, th = tip.offsetHeight;
    tip.style.left = Math.min(Math.max(px - tw / 2, 0), wrap.clientWidth - tw) + 'px';
    tip.style.top = (py - th - 12 >= 0 ? py - th - 12 : py + 14) + 'px';
  };
  svgEl.querySelectorAll('.dm-hit').forEach(el => {
    el.addEventListener('mouseenter', () => show(el));
    el.addEventListener('focus', () => show(el));
    el.addEventListener('click', e => { e.stopPropagation(); show(el); });
    el.addEventListener('mouseleave', hide);
    el.addEventListener('blur', hide);
  });
  svgEl.addEventListener('click', e => { if (!e.target.classList.contains('dm-hit')) hide(); });
}

// "This week: save $1.70 ea · best in 26 weeks · on sale 9 of 26 weeks" + a key of what's shown
function chartFooterHtml(points, weeks, newest) {
  const pts = chartPoints(points);
  const known = pts.filter(p => typeof p.save === 'number');
  if (pts.length < 2 || !known.length) return '';
  const span = chartAxis(pts, weeks, newest).length;
  const cur = newest && pts.find(p => p.week === newest);
  const best = known.reduce((a, b) => b.save > a.save ? b : a);
  const parts = [];
  if (cur) parts.push(`<b>This week: ${escH(saveText(cur).replace(/^Save/, 'save'))}</b>`);
  if (cur && typeof cur.save === 'number' && cur.save >= best.save)
    parts.push(`<span class="dm-best">${best.week === cur.week ? 'best' : 'ties the best'} in ${span} weeks</span>`);
  else {
    const d = new Date(best.week + 'T12:00:00');
    parts.push(`best: ${escH(saveText(best).replace(/^Save /, ''))} (${MONTHS[d.getMonth()]})`);
  }
  parts.push(`on sale ${pts.length} of ${span} weeks`);
  const kinds = Object.keys(DEAL_KIND_STYLE).filter(k => known.some(p => p.kind === k));
  const keys = kinds.map(k => `<span class="dm-key"><i class="dm-dot" style="background:${kindStyle(k).c}"></i>${kindStyle(k).l}</span>`);
  if (pts.some(p => typeof p.save !== 'number')) keys.push(`<span class="dm-key"><i class="dm-dot unk"></i>Amount not listed</span>`);
  if (span > pts.length) keys.push(`<span class="dm-key"><i class="dm-tick"></i>Not on sale</span>`);
  return `<div class="dm-chart-foot">${parts.join(' · ')}</div><div class="dm-keys">${keys.join('')}</div>`;
}

let _modalDeal = null;

function openDealModal(dealId) {
  const d = _allDeals.find(x => x.id === dealId);
  if (!d) return;
  _modalDeal = d;

  const emoji   = deptEmoji(d.department);
  const title   = decodeEntities(d.title);
  const desc    = decodeEntities(d.description);
  const savings = decodeEntities(d.savings);
  const saveLine= decodeEntities(d.save_line);
  const fine    = decodeEntities(d.fine_print);

  // ── Image / placeholder ───────────────────────────────────────────────────
  const img = d.image_url
    ? `<img class="deal-modal-img" src="${escH(d.image_url)}" alt="${escH(title)}"
         data-img-fallback>
       <div class="deal-modal-img-ph" style="display:none">${emoji}</div>`
    : `<div class="deal-modal-img-ph">${emoji}</div>`;

  // ── Badges ────────────────────────────────────────────────────────────────
  // v9.4: the type tag is shown once at the top; history signal is a neutral chip
  const badges = [];
  const histBadge = _badgeCache.get(d.id) || null;
  if (histBadge) badges.push(`<span class="chip-hist chip-hist-${escH(histBadge.type)}">${escH(histBadge.label)}</span>`);

  // ── Structured data rows (Option C influence) ─────────────────────────────
  const rows = [];
  const dr = (lbl, val, cls='') =>
    `<div class="dm-data-row"><span class="dm-data-lbl">${lbl}</span><span class="dm-data-val${cls?' '+cls:''}">${val}</span></div>`;

  if (d.department)
    rows.push(dr('Department', escH(decodeEntities(d.department))));
  if (d.brand)
    rows.push(dr('Brand', escH(decodeEntities(d.brand))));
  if (hasValidDates(d))
    rows.push(dr('Valid', `${escH(decodeEntities(d.valid_from))} – ${escH(decodeEntities(d.valid_thru))}`));
  if (d.saving_type)
    rows.push(dr('Deal source', escH(SAVING_TYPE_LABEL[d.saving_type] || d.saving_type)
      + (d.is_extra && d.saving_type !== 'ExtraSavings' ? ' · Extra Savings' : '')));
  const match = (_allMatches || []).find(m => m.id === d.id);
  if (match && match.my_item)
    rows.push(dr('Matched', `${escH(match.my_item)} <span class="dm-score">(score ${escH(String(match.match_score))})</span>`));
  if (fine)
    rows.push(dr('Fine print', escH(fine), 'fine'));

  // Frequency hint — filled in from the per-title history fetch below
  if (_dealHistory && (_dealHistory.num_weeks || 0) >= HIST_MIN.freqHint)
    rows.push(`<div class="dm-data-row" id="dm-freq-row" style="display:none"><span class="dm-data-lbl">On sale</span><span class="dm-data-val freq" id="dm-freq-val"></span></div>`);

  // ── Actions ───────────────────────────────────────────────────────────────
  const actions = [];
  if (d.has_coupon) {
    const url = `https://www.publix.com/savings/digital-coupons${d.coupon_id ? '?cid=' + d.coupon_id : ''}`;
    actions.push(`<a class="btn-clip" href="${escH(url)}" target="_blank" rel="noopener">✂️ Clip Coupon</a>`);
  }
  const onList = itemNames().map(n => n.toLowerCase()).includes(title.toLowerCase());
  actions.push(`<button class="btn-load" style="padding:8px 16px;font-size:13px" data-action="modal-toggle-list"
    id="modal-list-btn">${onList ? '✓ On My List' : '+ Add to My List'}</button>`);

  // ── Timeline: only show section if history is loaded ──────────────────────
  const validCaption = hasValidDates(d)
    ? `📅 Week of ${escH(decodeEntities(d.valid_from))}${d.valid_thru ? ' – ' + escH(decodeEntities(d.valid_thru)) : ''}`
    : '';

  const timelineHtml = _dealHistory
    ? `<div class="dm-timeline">
        <div class="dm-timeline-hd">Savings history</div>
        <svg id="dm-chart-svg" class="dm-chart"></svg>
        <div id="dm-chart-foot"></div>
        ${validCaption ? `<div class="dm-timeline-caption">${validCaption}</div>` : ''}
      </div>`
    : '';

  // ── Render ────────────────────────────────────────────────────────────────
  document.getElementById('deal-modal-content').innerHTML = `
    ${img}
    <div class="deal-modal-body">
      <div class="dm-tagline">${dealTypeTag(d)}${couponPlusTag(d)}<span class="deal-modal-dept">${escH([d.is_publix_brand ? 'Publix' : '', decodeEntities(d.department || '')].filter(Boolean).join(' · '))}</span></div>
      <div class="deal-modal-title">${escH(title)}</div>
      ${desc ? `<div class="deal-modal-desc">${escH(desc)}</div>` : ''}
      ${modalPriceHtml(d, savings, saveLine)}
      ${badges.length ? `<div class="dm-chips">${badges.join('')}</div>` : ''}
      ${rows.length ? `<div class="dm-data-rows">${rows.join('')}</div>` : ''}
      ${actions.length ? `<div class="deal-modal-actions">${actions.join('')}</div>` : ''}
      ${timelineHtml}
    </div>`;

  document.getElementById('deal-modal-overlay').classList.add('vis');
  document.body.style.overflow = 'hidden';

  // Fetch this deal's history on demand, then draw after layout so the SVG has real dimensions
  if (_dealHistory && prefs?.store_id) {
    const dealId = d.id;
    fetch(API + `/deals/history?store_id=${encodeURIComponent(prefs.store_id)}&title=${encodeURIComponent(d.title || '')}`,
          {credentials:'include', headers:authHeaders()})
      .then(r => r.ok ? r.json() : null)
      .then(data => {
        if (!data || !_modalDeal || _modalDeal.id !== dealId) return;  // modal closed or changed
        const pastWeeks = (data.points || []).filter(p => p.week !== _dealHistory.newest_week).length;
        const fv = document.getElementById('dm-freq-val');
        if (fv && (data.num_weeks || 0) >= HIST_MIN.freqHint) {
          fv.textContent = freqLabel(pastWeeks, data.weeks_of_history);
          document.getElementById('dm-freq-row').style.display = '';
        }
        requestAnimationFrame(() => {
          const svgEl = document.getElementById('dm-chart-svg');
          const newest = data.newest_week || _dealHistory.newest_week;
          if (svgEl) buildDealChart(svgEl, data.points, data.weeks, newest);
          const foot = document.getElementById('dm-chart-foot');
          if (foot && chartPoints(data.points).length >= 2) foot.innerHTML = chartFooterHtml(data.points, data.weeks, newest);
        });
      })
      .catch(() => {});
  }
}

// v9.3: modal price block = same headline / context / save as the card
function modalPriceHtml(d, savings, saveLine) {
  const sv = savingsDisplay(d);
  if (!d.offer || !d.offer.headline)
    return `<div class="deal-modal-price">${escH(savings)}</div>${saveLine ? `<div class="deal-modal-save">${escH(saveLine)}</div>` : ''}`;
  return `<div class="dm-price-panel"><div class="dm-price-main">`
    + `<span class="deal-modal-price ${headlineClass(sv.kind, sv.isDiscount)}">${escH(sv.text)}</span>`
    + (sv.pctx ? `<span class="deal-modal-pctx">${escH(sv.pctx)}</span>` : '') + `</div>`
    + ((sv.save || sv.extra) ? `<div class="dm-price-sep"></div><div class="dm-price-save">`
        + (sv.save ? `<div class="deal-modal-save">${escH(sv.save)}</div>` : '')
        + (sv.extra ? `<div class="deal-modal-extra">${escH(sv.extra)}</div>` : '') + `</div>` : '')
    + `</div>`;
}

function closeDealModal() {
  document.getElementById('deal-modal-overlay').classList.remove('vis');
  document.body.style.overflow = '';
  _modalDeal = null;
}

function modalToggleList() {
  if (!_modalDeal) return;
  const title = decodeEntities(_modalDeal.title);
  if (!prefs) prefs = {};
  if (!prefs.items) prefs.items = [];
  const names = itemNames().map(n => n.toLowerCase());
  if (names.includes(title.toLowerCase())) {
    prefs.items = prefs.items.filter(i => itemName(i).toLowerCase() !== title.toLowerCase());
  } else {
    prefs.items.push(title);
  }
  renderItems();
  syncDealCheckboxes();
  savePrefs('list').catch(() => {});
  // Update button label in modal
  const onList = itemNames().map(n => n.toLowerCase()).includes(title.toLowerCase());
  const btn = document.getElementById('modal-list-btn');
  if (btn) btn.textContent = onList ? '✓ On My List' : '+ Add to My List';
}

function dealCardHtml(d, isMatch){
  const emoji=deptEmoji(d.department);
  const title=decodeEntities(d.title || d.deal_name);
  const sv=savingsDisplay(d);
  // All 50 cards per page load images eagerly — they're small (15-30KB)
  // and cached by Publix CDN (max-age=86400). Lazy loading caused a 20s+
  // gap where cards 9-37 showed placeholder emojis until scrolled.
  const img=d.image_url
    ?`<img class="deal-card-img" src="${escH(d.image_url)}" alt="${escH(title)}"
        data-img-fallback>
       <div class="deal-card-img-ph" style="display:none">${emoji}</div>`
    :`<div class="deal-card-img-ph">${emoji}</div>`;
  const tags = `<div class="deal-card-badges">${dealTypeTag(d)}${couponPlusTag(d)}</div>`;
  const dept = [d.is_publix_brand ? 'Publix' : '', decodeEntities(d.department||'')].filter(Boolean).join(' · ');
  const priceHtml = sv.text
    ? `<span class="deal-card-price ${headlineClass(sv.kind, sv.isDiscount)}">${escH(sv.text)}</span>`
      + (sv.pctx ? ` <span class="deal-card-pctx">${escH(sv.pctx)}</span>` : '')
    : '';
  let saveHtml = '';
  if (sv.save) saveHtml = `<span class="deal-card-save">${escH(sv.save)}</span>`;
  else if (sv.extra) saveHtml = `<span class="deal-card-extra">${escH(sv.extra)}</span>`;
  else if (sv.isClip || (d.has_coupon && d.coupon_id)) saveHtml = clipLinkHtml(d);
  if (sv.save && sv.extra) saveHtml += ` <span class="deal-card-extra">${escH(sv.extra)}</span>`;
  const histBadge = _badgeCache.get(d.id) || null;
  const chips = [];
  if (isMatch && d.my_item) chips.push(`<span class="chip-match" title="Match score ${escH(String(d.match_score||''))}%">${escH(d.my_item)}</span>`);
  if (histBadge) chips.push(`<span class="chip-hist chip-hist-${escH(histBadge.type)}">${escH(histBadge.label)}</span>`);
  const footer = chips.length ? chips.join('')
    : (hasValidDates(d) ? `<span class="deal-card-valid">Valid ${escH(decodeEntities(d.valid_from))} – ${escH(decodeEntities(d.valid_thru))}</span>` : '');
  const cbId=`${isMatch?'mcb':'dcb'}-${d.id}`;
  return `<div class="deal-card${isMatch?' is-match':''}" data-action="open-deal-modal" data-deal-id="${escH(d.id)}">
    <div class="deal-add-wrap">
      <input type="checkbox" class="deal-add-cb" id="${cbId}" data-title="${escH(title)}" data-id="${escH(d.id)}">
      <label class="deal-add-lbl" for="${cbId}" title="Add to My List"></label>
    </div>
    ${tags}
    ${img}
    <div class="deal-card-body">
      <div class="deal-card-row-dept"><span class="deal-card-dept">${escH(dept)}</span></div>
      <div class="deal-card-row-title"><span class="deal-card-title">${escH(title)}</span></div>
      <div class="deal-card-row-price">${priceHtml}</div>
      <div class="deal-card-row-save">${saveHtml || '<span class="deal-card-save-hidden">&nbsp;</span>'}</div>
      <div class="deal-card-row-valid">${footer}</div>
    </div></div>`;
}

// Green = what you pay; red = an amount off; ink = a mechanic (BOGO, % off)
function headlineClass(kind, isDiscount) {
  if (kind === 'price' || kind === 'multi') return 'k-pay';
  if (kind === 'amount_off' || kind === 'coupon' || kind === 'free') return 'k-off';
  if (kind) return 'k-mech';
  return isDiscount ? 'k-off' : 'k-pay';     // pre-v9.3 data
}
function couponPlusTag(d) {
  const k = dealTypeKey(d);
  const o = d.offer || {};
  if (k === 'coupon') return '';
  return (o.extra || (o.coupon_required && k !== 'coupon') || (d.has_coupon && k !== 'coupon'))
    ? `<span class="deal-type-tag plus">+ Coupon</span>` : '';
}
function clipLinkHtml(d) {
  const url = `https://www.publix.com/savings/digital-coupons${d.coupon_id ? '?cid=' + encodeURIComponent(d.coupon_id) : ''}`;
  return `<a class="deal-card-save is-clip" href="${escH(url)}" target="_blank" rel="noopener" data-stop>Clip coupon →</a>`;
}

// Sync checkbox states to match prefs.items
function syncDealCheckboxes(){
  const names=itemNames().map(s=>s.toLowerCase());
  document.querySelectorAll('.deal-add-cb').forEach(cb=>{
    const title=(cb.dataset.title||'').toLowerCase();
    cb.checked = names.includes(title);
  });
}

// Attach listeners to deal card checkboxes (called after render)
function attachDealCBListeners(){
  document.querySelectorAll('.deal-add-cb').forEach(cb=>{
    cb.addEventListener('change', ()=>{
      const title=cb.dataset.title||'';
      if(!prefs) prefs={};if(!prefs.items) prefs.items=[];
      if(cb.checked){
        if(!itemNames().map(n=>n.toLowerCase()).includes(title.toLowerCase())) prefs.items.push(title);
      } else {
        prefs.items=prefs.items.filter(i=>itemName(i).toLowerCase()!==title.toLowerCase());
      }
      renderItems();
      savePrefs('list').catch(()=>{});
    });
  });
}

// ═══════════════════════════════════════════════════════════════════
// MATCHES
// ═══════════════════════════════════════════════════════════════════
let _matchesLoading = false;
let _matchesDirty    = true;  // force fetch on first visit; set false after load, true when list changes

function loadMatches(){
  // Client-side matching: runs the JS port of shared/matcher.py against
  // _allDeals (already loaded) and the user's current prefs.items.
  // No network call — results are always in sync with the current list/sensitivity.
  if(!_matchesDirty && _allMatches.length) {
    renderDeals();
    return;
  }

  const items     = (prefs && prefs.items) || [];
  const matching  = (prefs && prefs.matching) || {};

  if(!_allDeals.length || !items.length) {
    _allMatches = [];
    _matchesDirty = false;
    renderDeals();
    return;
  }

  _allMatches = cwMatchDealsForUser(_allDeals, items, matching);
  _matchesDirty = false;

  // Update match count pill
  const pill = document.getElementById('match-count-pill');
  if(pill) {
    pill.textContent = _allMatches.length + ' match' + (_allMatches.length!==1?'es':'');
    pill.style.display = _allMatches.length ? '' : 'none';
  }

  renderDeals();
}
function onThresholdChange(){
  savePrefs('matching').then(()=>loadMatches()).catch(()=>{});
}

let _searchTimer = null;
function debouncedRenderDeals(){
  clearTimeout(_searchTimer);
  _searchTimer = setTimeout(renderDeals, 300);
}

function loadMoreDeals(){
  _dealsPageLimit += DEALS_PAGE_SIZE;
  renderDeals(true);  // true = keepPage — don't reset _dealsPageLimit
}

// ── Phased TPR loading ──────────────────────────────────────────────────────
// TPR deals (63% of all deals) are fetched in parallel with core deals.
// The fetch fires alongside the core request; by the time the TPR response
// arrives (~900ms), core has already rendered (~650ms). Matches update silently.
async function loadTprDeals(sid, gen, cacheMode){
  if(!sid) sid = prefs?.store_id;
  if(!sid) return;
  if(gen === undefined) gen = _dealsGen;
  _tprLoading=true;
  try{
    const res=await fetch(API+'/deals?store_id='+encodeURIComponent(sid)+'&types=tpr', {credentials:'include',headers:authHeaders(),cache:cacheMode||'default'});
    const data=await res.json();
    if(!res.ok) throw new Error(data.error||'Failed to load TPR deals');
    if(gen !== _dealsGen) return;                     // a newer load, store change or logout superseded this
    if(_coreGen !== gen){ _pendingTpr = {gen, data}; return; }  // core not applied yet — merge when it is
    mergeTprDeals(data);
  }catch(e){
    console.warn('TPR deal load failed:', e);
  }finally{
    _tprLoading=false;
  }
}

function mergeTprDeals(data){
  const tprDeals=(data.deals||[]).map(d=>({
    ...d,
    title:       decodeEntities(d.title       || ''),
    description: decodeEntities(d.description || ''),
    save_line:   decodeEntities(d.save_line   || ''),
    savings:     decodeEntities(d.savings     || ''),
    fine_print:  decodeEntities(d.fine_print  || ''),
    department:  decodeEntities(d.department  || ''),
    brand:       decodeEntities(d.brand       || ''),
  }));

  // Merge into _allDeals (skip duplicates)
  const existingIds=new Set(_allDeals.map(d=>d.id));
  const newDeals=tprDeals.filter(d=>!existingIds.has(d.id));
  _allDeals=_allDeals.concat(newDeals);

  // Extend token cache for new deals
  for(const d of newDeals){
    _dealTokenCache.set(d.id, [...cwTokenize(d.title||''), ...cwTokenize(d.brand||'')]);
  }

  // Extend badge cache for new deals (dictionary lookups — cheap)
  if(_dealHistory){
    for(const d of newDeals){
      const b=computeBadge(d,_dealHistory);
      if(b) _badgeCache.set(d.id, b);
    }
  }

  // Merge dept counts (TPR deals mostly have no department but handle gracefully)
  const tprDeptCounts=data.dept_counts||{};
  for(const [dept,cnt] of Object.entries(tprDeptCounts)){
    _deptCounts[dept]=(_deptCounts[dept]||0)+cnt;
  }
  buildDeptFilters('dept-deals', _deptCounts, 'renderDeals');
  updateFilterCounts();

  _tprLoaded=true;
  _matchesDirty=true;
  loadMatches();  // re-runs matcher with expanded deal set → renderDeals()
}

// ── v9.4: mobile filter sheet (same filter controls, shown as a bottom sheet <900px) ──
function toggleFilterSheet(open){
  const col = document.getElementById('deals-filter-col');
  const bd  = document.getElementById('filter-sheet-backdrop');
  if (!col) return;
  col.classList.toggle('sheet-open', !!open);
  if (bd) bd.classList.toggle('vis', !!open);
  document.body.style.overflow = open ? 'hidden' : '';
  if (open) updateFilterSheetCount();
}
function resetDealFilters(){
  document.getElementById('f-all').checked = false;
  SAVINGS_SUBFILTERS.forEach(id => { const el = document.getElementById(id); if (el) el.checked = true; });
  document.querySelectorAll('#dept-deals .dept-cb').forEach(cb => { cb.checked = false; });
  renderDeals();
}
let _dealsShownTotal = null;
function updateFilterSheetCount(){
  const btn = document.getElementById('filter-sheet-apply');
  if (btn) btn.textContent = _dealsShownTotal != null
    ? `Show ${_dealsShownTotal.toLocaleString()} deal${_dealsShownTotal === 1 ? '' : 's'}` : 'Show deals';
  const fb = document.querySelector('.btn-filters');
  if (fb) {
    const off = SAVINGS_SUBFILTERS.filter(id => { const el = document.getElementById(id); return el && !el.checked; }).length
      + document.querySelectorAll('#dept-deals .dept-cb:checked').length;
    fb.innerHTML = off ? `Filters <span class="cnt">${off}</span>` : 'Filters';
  }
}

function onMatchesToggle(){
  const on = document.getElementById('matches-toggle').checked;
  const lbl = document.getElementById('toggle-bar-label');
  if(lbl){ lbl.textContent = on ? 'My Matches first' : 'Browse all deals'; lbl.className='toggle-bar-label'+(on?'':' off'); }
  renderDeals();
}

// ═══════════════════════════════════════════════════════════════════
// ADMIN
// ═══════════════════════════════════════════════════════════════════
// ── Admin sidebar navigation ─────────────────────────────────────────────────
let _adminActivePage = 'users';
function adminNav(name){
  _adminActivePage = name;
  ['users','reports','logging','inbound'].forEach(t=>{
    document.getElementById('anav-'+t).classList.toggle('active', t===name);
    document.getElementById('apage-'+t).classList.toggle('active', t===name);
  });
  // Lazy-load on first visit
  if(name==='reports'){
    const sb=document.getElementById('admin-stats-body');
    if(sb && (sb.textContent.includes('Loading')||sb.textContent.trim()==='')) adminLoadStats();
  }
  if(name==='logging'){
    const le=document.getElementById('admin-logs-el');
    if(le && (le.textContent.includes('Loading')||le.textContent.trim()==='')) adminLoadLogs();
  }
  if(name==='inbound'){
    const iw=document.getElementById('inbound-log-wrap');
    if(iw && iw.textContent.includes('Click Refresh')) adminLoadInboundLogs();
  }
}

// ── Email Import (inbound) log viewer ────────────────────────────────────────
let _allInboundLogs = [];
const INBOUND_ACTION_LABELS = {
  'inbound-webhook-received':  {icon:'📨', label:'Webhook received',    color:'var(--ink)'},
  'inbound-sig-fail':          {icon:'🔐', label:'Signature failed',     color:'var(--red)'},
  'inbound-sender-extracted':  {icon:'📬', label:'Sender identified',    color:'var(--ink)'},
  'inbound-no-from':           {icon:'⚠️', label:'No sender',           color:'var(--amber)'},
  'inbound-wrong-dest':        {icon:'🎯', label:'Wrong destination',    color:'var(--amber)'},
  'inbound-user-matched':      {icon:'✅', label:'User matched',         color:'var(--green)'},
  'inbound-no-user':           {icon:'👤', label:'User not found',       color:'var(--red)'},
  'inbound-user-lookup-error': {icon:'💥', label:'Lookup error',         color:'var(--red)'},
  'inbound-notify-scan-error': {icon:'💥', label:'Scan error',           color:'var(--red)'},
  'inbound-fetching-body':     {icon:'⬇️', label:'Fetching email body',  color:'var(--ink)'},
  'inbound-fetch-failed':      {icon:'❌', label:'Fetch failed',          color:'var(--red)'},
  'inbound-config-error':      {icon:'⚙️', label:'Config error',         color:'var(--red)'},
  'inbound-html-parsed':       {icon:'🔍', label:'HTML parsed',          color:'var(--ink)'},
  'inbound-no-items':          {icon:'📭', label:'No items found',       color:'var(--amber)'},
  'inbound-list-imported':     {icon:'🛒', label:'Items imported',       color:'var(--green)'},
  'inbound-subject-rejected':  {icon:'🚫', label:'Subject not recognized', color:'var(--amber)'},
  'inbound-rate-limited':      {icon:'⏱️', label:'Rate limited',         color:'var(--amber)'},
  'inbound-rate-limit-error':  {icon:'💥', label:'Rate-limit error',     color:'var(--red)'},
};

async function adminLoadInboundLogs(){
  const wrap = document.getElementById('inbound-log-wrap');
  wrap.innerHTML = '<div style="color:var(--ink-soft);font-size:13px;padding:12px">Loading…</div>';
  const sender = (document.getElementById('inbound-sender-filter').value || '').trim();
  const qs = sender ? `?sender=${encodeURIComponent(sender)}&limit=300` : '?limit=300';
  try{
    const res  = await fetch(API+'/admin/inbound-logs'+qs, {credentials:'include',headers:adminHdrs()});
    const data = await res.json();
    if(!res.ok) throw new Error(data.error||'Failed');
    _allInboundLogs = data.logs || [];
    adminRenderInboundLogs(sender);
  }catch(e){
    wrap.innerHTML = `<div style="color:var(--red);font-size:13px;padding:12px">Error: ${escH(e.message)}</div>`;
  }
}

function adminRenderInboundLogs(senderFilter){
  const wrap = document.getElementById('inbound-log-wrap');
  const logs = _allInboundLogs;
  if(!logs.length){
    const hint = senderFilter
      ? `No inbound log entries found for <strong>${escH(senderFilter)}</strong>. Check the address matches the forwarding email exactly.`
      : 'No inbound email events logged yet. Forward a Publix My List email to trigger the webhook.';
    wrap.innerHTML = `<div style="color:var(--ink-soft);font-size:13px;padding:14px">${hint}</div>`;
    return;
  }

  // Group logs by email_id so each forwarded email is a collapsible chain
  const chains = {};
  const order  = [];
  logs.forEach(l => {
    const key = l.email_id || ('no-id-' + (l.sender || l.user || l.ts));
    if(!chains[key]){ chains[key]=[]; order.push(key); }
    chains[key].push(l);
  });

  const rows = order.map((key, ci) => {
    const entries = chains[key];
    const first   = entries[entries.length-1]; // oldest first
    const last    = entries[0];                // newest first
    const sender  = first.sender || last.sender || last.user || '?';
    const outcome = last.action || '';
    const outcomeInfo = INBOUND_ACTION_LABELS[outcome] || {icon:'•', label:outcome, color:'var(--ink)'};
    const dt = new Date(last.ts);
    const est = dt.toLocaleString('en-US',{timeZone:'America/New_York',month:'numeric',day:'numeric',
      hour:'numeric',minute:'2-digit',hour12:true});
    const chainId = 'ichain-'+ci;

    const stepRows = entries.slice().reverse().map(l => {
      const info = INBOUND_ACTION_LABELS[l.action] || {icon:'•', label:l.action||'?', color:'var(--ink)'};
      const extra = [];
      if(l.message) extra.push(`<span style="color:var(--ink-mid)">${escH(l.message)}</span>`);
      if(l.match_type) extra.push(`match: <strong>${escH(l.match_type)}</strong>`);
      if(l.items_found !== undefined) extra.push(`items found: <strong>${l.items_found}</strong>`);
      if(l.added !== undefined) extra.push(`added: <strong>${l.added}</strong>`);
      if(l.skipped_duplicates !== undefined) extra.push(`dupes skipped: <strong>${l.skipped_duplicates}</strong>`);
      if(l.added_items && l.added_items !== '[]') extra.push(`<span style="color:var(--green)">${escH(l.added_items)}</span>`);
      if(l.hint) extra.push(`<em style="color:var(--amber)">${escH(l.hint)}</em>`);
      if(l.html_bytes !== undefined) extra.push(`html: ${l.html_bytes}B`);
      if(l.html_snippet) extra.push(`<code style="font-size:10px;color:var(--ink-soft)">${escH((l.html_snippet||'').slice(0,120))}</code>`);
      if(l.has_svix_id !== undefined) extra.push(`svix-id present: ${l.has_svix_id}`);
      if(l.secret_configured !== undefined) extra.push(`secret configured: ${l.secret_configured}`);

      const stepTime = new Date(l.ts).toLocaleString('en-US',{timeZone:'America/New_York',
        hour:'numeric',minute:'2-digit',second:'2-digit',hour12:true});
      return `<div style="display:flex;gap:10px;padding:6px 14px;border-bottom:1px solid var(--border);font-size:12px;align-items:flex-start">
        <span style="white-space:nowrap;color:var(--ink-soft);min-width:72px">${stepTime}</span>
        <span style="min-width:18px">${info.icon}</span>
        <span style="min-width:160px;color:${info.color};font-weight:600">${escH(info.label)}</span>
        <span style="color:var(--ink-mid);flex:1">${extra.join(' &nbsp;·&nbsp; ')}</span>
      </div>`;
    }).join('');

    return `<div style="border-bottom:2px solid var(--border)">
      <div data-action="toggle-chain" data-chain="${chainId}"
        style="display:flex;gap:12px;align-items:center;padding:10px 14px;cursor:pointer;background:var(--green-bg);user-select:none">
        <span style="font-size:13px;font-weight:700;color:var(--ink);flex:1">
          ${escH(sender)}
          <span style="font-weight:400;color:var(--ink-soft);margin-left:8px">${est}</span>
        </span>
        <span style="color:${outcomeInfo.color};font-size:12px;font-weight:700">${outcomeInfo.icon} ${escH(outcomeInfo.label)}</span>
        <span style="color:var(--ink-soft);font-size:11px">${entries.length} step${entries.length!==1?'s':''} ▾</span>
      </div>
      <div id="${chainId}" style="display:none">${stepRows}</div>
    </div>`;
  }).join('');

  wrap.innerHTML = `<div style="font-size:12px;color:var(--ink-soft);padding:8px 14px;border-bottom:1px solid var(--border)">
    ${logs.length} event${logs.length!==1?'s':''} across ${order.length} email attempt${order.length!==1?'s':''}
    ${senderFilter ? ' — filtered to: <strong>'+escH(senderFilter)+'</strong>' : ''}
  </div>${rows}`;
}


async function adminSignOut(){
  // PDC-21: revoke the server-side admin session (cookie cleared by response)
  try{
    await fetch(API+'/admin/logout',{method:'POST',credentials:'include',headers:adminHdrs()});
  }catch(_){}
  _adminLoggedIn=false;
  _adminDataLoaded = false;  // allow re-fetch on next login
  document.getElementById('admin-dash').style.display='none';
  document.getElementById('admin-login-sec').style.display='';
  document.getElementById('admin-secret-inp').value='';
  setMsg('admin-login-msg','','');
}

// Called once per admin login (fresh login OR restored session) — not on every page visit.
// Pre-fetches all log panels so every tab is ready without a manual Refresh click.
// Reset _adminDataLoaded to false on adminSignOut() so next login re-fetches fresh data.
let _adminDataLoaded = false;
async function adminPostLogin() {
  if (_adminDataLoaded) return;
  _adminDataLoaded = true;
  // Fire all fetches in parallel — each populates its own panel independently
  adminLoadLogs();      // Logging tab: scrape jobs
  adminLoadStats();     // Reports tab
  adminLoadAuthLogs();  // Users tab: authentication log
  adminLoadAppLogs();   // Logging tab: application logs
}
async function initAdmin(){
  // PDC-21: admin token is in HttpOnly cookie — validate by making an authenticated call
  try{
    const res=await fetch(API+'/admin/scrape-logs',{credentials:'include',headers:adminHdrs()});
    if(res.ok){
      _adminLoggedIn=true;
      document.getElementById('admin-login-sec').style.display='none';
      document.getElementById('admin-dash').style.display='block';
      adminLoadUsers();
      adminPostLogin();
      return;
    }
  }catch(_){}
  // No valid session — show login form
  _adminLoggedIn=false;
}
async function adminLogin(){
  const s=document.getElementById('admin-secret-inp').value.trim();
  if(!s){setMsg('admin-login-msg','Enter the admin secret.','err');return;}
  const btn=document.querySelector('#admin-login-sec .btn-save');
  if(btn){btn.disabled=true;btn.textContent='Verifying…';}
  try{
    // PDC-04/21: exchange secret for HttpOnly cookie session
    const res=await fetch(API+'/admin/login',{
      method:'POST',
      credentials:'include',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({secret:s})
    });
    if(res.status===401){
      setMsg('admin-login-msg','Incorrect admin secret.','err');
      return;
    }
    if(!res.ok){
      setMsg('admin-login-msg','Server error — try again.','err');
      return;
    }
    _adminLoggedIn=true;
    document.getElementById('admin-secret-inp').value=''; // clear secret from DOM
    adminPostLogin();
    document.getElementById('admin-login-sec').style.display='none';
    document.getElementById('admin-dash').style.display='block';
    adminLoadUsers();
  }catch(e){
    setMsg('admin-login-msg','Could not reach the server.','err');
  }finally{
    if(btn){btn.disabled=false;btn.textContent='Authenticate';}
  }
}
// PDC-21: admin token in HttpOnly cookie — browser sends automatically with credentials:'include'
function adminHdrs(){return{'Content-Type':'application/json'};}
function adminFetchOpts(extra={}){return{credentials:'include',headers:adminHdrs(),...extra};}


async function adminLoadStats(){
  const el=document.getElementById('admin-stats-body');
  if(!el) return;
  el.innerHTML='<div style="color:var(--ink-soft);font-size:13px">Loading…</div>';
  try{
    const res=await fetch(API+'/admin/stats',{credentials:'include',headers:adminHdrs()});
    const data=await res.json();
    if(!res.ok) throw new Error(data.error||'Failed');

    const s=data;
    const totalUsers=s.total_users||0;
    const deletedUsers=s.deleted_users||0;
    const avgItems=s.avg_items||0;
    const noItems=s.no_items_count||0;
    const byMonth=s.by_month||[];
    const byGeo=s.by_geography||[];
    const popular=s.popular_items||[];

    // Monthly chart
    const maxMonth=Math.max(1,...byMonth.map(m=>m.count));
    const monthBars=byMonth.slice(-12).map(m=>{
      const pct=Math.round((m.count/maxMonth)*100);
      const lbl=m.month.slice(5); // "MM"
      return `<div class="smc-bar-wrap" title="${m.month}: ${m.count} users">
        <div class="smc-bar" style="height:${pct}%;flex:1"></div>
        <div class="smc-lbl">${lbl}</div>
      </div>`;
    }).join('');

    // Geography bars
    const maxGeo=Math.max(1,...byGeo.map(g=>g.count));
    const geoBars=byGeo.map(g=>{
      const pct=Math.round((g.count/maxGeo)*100);
      return `<li><span style="flex:0 0 130px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${escH(g.location)}">${escH(g.location)}</span>
        <div class="sl-bar-wrap"><div class="sl-bar" style="width:${pct}%"></div></div>
        <span class="sl-ct">${g.count}</span></li>`;
    }).join('');

    // Popular items bars
    const maxPop=Math.max(1,...popular.map(p=>p.count));
    const popBars=popular.map(p=>{
      const pct=Math.round((p.count/maxPop)*100);
      return `<li><span style="flex:0 0 160px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${escH(p.item)}">${escH(p.item)}</span>
        <div class="sl-bar-wrap"><div class="sl-bar" style="width:${pct}%;background:var(--amber)"></div></div>
        <span class="sl-ct">${p.count}</span></li>`;
    }).join('');

    el.innerHTML=`
      <div class="stats-grid">
        <div class="stat-card"><div class="stat-val">${totalUsers}</div><div class="stat-lbl">Total Users</div></div>
        <div class="stat-card"><div class="stat-val">${avgItems}</div><div class="stat-lbl">Avg List Items</div></div>
        <div class="stat-card"><div class="stat-val">${totalUsers-noItems}</div><div class="stat-lbl">Active Lists</div></div>
        <div class="stat-card"><div class="stat-val">${noItems}</div><div class="stat-lbl">Empty Lists</div></div>
        <div class="stat-card"><div class="stat-val">${deletedUsers}</div><div class="stat-lbl">Users Deleted</div></div>
      </div>

      <div style="margin-bottom:8px;font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.05em;color:var(--ink-soft)">Users Added by Month</div>
      ${byMonth.length
        ? `<div class="stat-month-chart">${monthBars}</div>`
        : '<div style="font-size:13px;color:var(--ink-soft)">No data yet</div>'}

      <div class="stats-two" style="margin-top:20px">
        <div>
          <div style="margin-bottom:8px;font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.05em;color:var(--ink-soft)">Users by Geography</div>
          ${byGeo.length
            ? `<ul class="stat-list">${geoBars}</ul>`
            : '<div style="font-size:13px;color:var(--ink-soft)">No store data</div>'}
        </div>
        <div>
          <div style="margin-bottom:8px;font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.05em;color:var(--ink-soft)">Most Popular List Items</div>
          ${popular.length
            ? `<ul class="stat-list">${popBars}</ul>`
            : '<div style="font-size:13px;color:var(--ink-soft)">No list data yet</div>'}
        </div>
      </div>`;
  }catch(e){
    el.innerHTML=`<div style="color:var(--red);font-size:13px">Failed to load stats: ${escH(e.message)}</div>`;
  }
}
async function adminLoadLogs(){
  document.getElementById('admin-logs-el').innerHTML='<div style="color:var(--ink-soft);font-size:13px;padding:12px 0">Loading…</div>';
  try{
    const res=await fetch(API+'/admin/scrape-logs',{credentials:'include',headers:adminHdrs()});
    const data=await res.json();
    if(!res.ok) throw new Error(data.error||'Failed');
    const logs=data.logs||[];
    const bar=document.getElementById('admin-scrape-bar');
    if(logs.length){
      const last=logs[0];
      const dt=new Date(last.started_at);
      const est=dt.toLocaleString('en-US',{timeZone:'America/New_York',month:'numeric',day:'numeric',year:'numeric',hour:'numeric',minute:'2-digit',hour12:true});
      bar.className='scrape-bar';
      const isFanOut = last.mode === 'fan-out';
      bar.innerHTML=isFanOut
        ? `<span>Last run: <span class="scrape-stat">${est} ET</span></span>
           <span><span class="scrape-stat">${last.stores_examined||0}</span> stores dispatched &nbsp;·&nbsp;
                 <span class="scrape-stat">${last.total_users||0}</span> users</span>`
        : `<span>Last import: <span class="scrape-stat">${est} ET</span></span>
           <span><span class="scrape-stat">${last.stores_examined||0}</span> stores examined &nbsp;·&nbsp;
                 <span class="scrape-stat">${last.total_deals||0}</span> deals imported &nbsp;·&nbsp;
                 <span class="scrape-stat">${last.emails_sent||0}</span> emails sent</span>`;
    } else {
      bar.className='scrape-bar loading';
      bar.innerHTML='<span>No scrape jobs recorded yet.</span>';
    }
    if(!logs.length){document.getElementById('admin-logs-el').innerHTML='<div style="color:var(--ink-soft);font-size:13px">No scrape jobs yet.</div>';return;}
    document.getElementById('admin-logs-el').innerHTML=logs.map(log=>{
      const dt=new Date(log.started_at);
      const est=dt.toLocaleString('en-US',{timeZone:'America/New_York',month:'numeric',day:'numeric',year:'numeric',hour:'numeric',minute:'2-digit',hour12:true});
      const dur=log.finished_at?Math.round((new Date(log.finished_at)-dt)/1000)+'s':'–';
      const errs=log.errors||[];
      const isFanOut = log.mode === 'fan-out';
      const summary = isFanOut
        ? `${log.stores_examined||0} stores dispatched · ${log.total_users||0} users`
        : `${log.stores_examined||0} stores · ${log.total_deals||0} deals · ${log.emails_sent||0} emails`;
      return `<div class="log-row">
        <div class="log-icon">${errs.length?'⚠️':'✅'}</div>
        <div class="log-detail">
          <div class="log-summary">${summary}</div>
          <div class="log-meta">${est} ET · ${dur}${isFanOut?' · fan-out':''}</div>
          ${errs.length?`<div class="log-errors">${errs.slice(0,3).map(escH).join(' | ')}</div>`:''}
        </div></div>`;
    }).join('');
  }catch(e){
    const bar=document.getElementById('admin-scrape-bar');
    bar.className='scrape-bar error';
    bar.innerHTML=`<span>Error: ${escH(e.message)}</span>`;
  }
}
async function adminRunScrape(btn){
  btn.disabled=true;btn.textContent='Invoking…';
  try{
    const res=await fetch(API+'/admin/scrape-now',{method:'POST',credentials:'include',headers:adminHdrs()});
    const data=await res.json();
    if(!res.ok) throw new Error(data.error||'Failed');
    btn.textContent='✓ Invoked!';
    setTimeout(()=>{btn.disabled=false;btn.textContent='▶ Run Now';},3000);
    setTimeout(adminLoadLogs,35000);
  }catch(e){alert('Error: '+e.message);btn.disabled=false;btn.textContent='▶ Run Now';}
}
async function adminLoadTail(){
  const termEl=document.getElementById('admin-tail');
  const loadEl=document.getElementById('admin-tail-loading');
  loadEl.textContent='Fetching logs…';termEl.style.display='none';
  try{
    const res=await fetch(API+'/admin/logs/tail',{credentials:'include',headers:adminHdrs()});
    const data=await res.json();
    if(!res.ok) throw new Error(data.error||'Failed');
    const lines=data.lines||[];
    if(!lines.length){loadEl.textContent='No log events found.';return;}
    loadEl.textContent='';termEl.style.display='block';
    termEl.innerHTML=lines.map(l=>{
      const e=l.msg.toLowerCase().includes('error');
      const o=l.msg.includes('Email sent')||l.msg.includes('Done.');
      return `<div><span class="ts">${(l.ts||'').replace('T',' ').replace(/\..*/,'')}</span><span class="${e?'terr':o?'tok':''}">${escH(l.msg)}</span></div>`;
    }).join('');
    termEl.scrollTop=termEl.scrollHeight;
  }catch(e){loadEl.textContent='Error: '+e.message;}
}
let _allAdminUsers=[];
let _adminUserPage=0;
// v9.8.3: click a header to sort; click again to reverse. Blank values always sort last.
let _adminUserSort={key:'last_login',dir:-1};
const ADMIN_USER_SORT={
  email:      u=>(u.email||'').toLowerCase(),
  notify:     u=>(u.notify_email||u.email||'').toLowerCase(),
  store:      u=>u.store_id?Number(u.store_id)||u.store_id:'',
  items:      u=>u.item_count||0,
  joined:     u=>u.created_at||'',
  last_login: u=>u.last_login||'',
};
const ADMIN_SORT_FIRST_DIR={email:1,notify:1,store:1,items:-1,joined:-1,last_login:-1};
function adminSortUsers(key){
  if(!ADMIN_USER_SORT[key]) return;
  _adminUserSort = _adminUserSort.key===key ? {key,dir:-_adminUserSort.dir} : {key,dir:ADMIN_SORT_FIRST_DIR[key]||1};
  _adminUserPage=0;
  adminRenderUsers();
}
function adminSortedUsers(list){
  const {key,dir}=_adminUserSort, get=ADMIN_USER_SORT[key];
  if(!get) return list;
  const blank=v=>v===''||v==null;
  return list.slice().sort((a,b)=>{
    const x=get(a), y=get(b);
    if(blank(x)||blank(y)) return blank(x)===blank(y) ? 0 : blank(x) ? 1 : -1;
    return (x<y?-1:x>y?1:0)*dir || (a.email||'').localeCompare(b.email||'');
  });
}
function adminLastLoginHtml(ts){
  if(!ts) return '<span style="color:var(--ink-soft)" title="No sign-in recorded (the auth log keeps 90 days)">—</span>';
  const d=new Date(ts); if(isNaN(d)) return '—';
  const days=Math.floor((Date.now()-d.getTime())/86400000);
  const ago=days<=0?'today':days===1?'yesterday':days<60?`${days} days ago`:`${Math.round(days/30)} months ago`;
  const full=d.toLocaleString([], {dateStyle:'medium', timeStyle:'short'});
  return `<span title="${escH(full)}">${escH(d.toLocaleDateString([], {month:'short',day:'numeric',year:'numeric'}))}</span><br><span style="color:var(--ink-soft)">${ago}</span>`;
}
const ADMIN_PAGE_SIZE=25;

async function adminLoadUsers(){
  try{
    let allUsers=[];
    let cursor='';
    // Fetch all pages (50 users each) — admin needs full list for search
    while(true){
      const url=API+'/admin/users'+(cursor?'?cursor='+encodeURIComponent(cursor):'');
      const res=await fetch(url,{credentials:'include',headers:adminHdrs()});
      const data=await res.json();
      if(!res.ok) throw new Error(data.error||'Failed');
      allUsers=allUsers.concat(data.users||[]);
      if(!data.next_cursor) break;
      cursor=data.next_cursor;
    }
    _allAdminUsers=allUsers;
    _adminUserPage=0;
    document.getElementById('admin-user-ct').textContent=`(${_allAdminUsers.length})`;
    document.getElementById('admin-user-search').value='';
    adminRenderUsers();
  }catch(e){document.getElementById('admin-tbody').innerHTML=`<tr><td colspan="7" style="color:var(--red);font-size:13px">Error: ${escH(e.message)}</td></tr>`;}
}

function adminFilterUsers(){
  _adminUserPage=0;
  adminRenderUsers();
}

function adminRenderUsers(){
  const q=(document.getElementById('admin-user-search').value||'').toLowerCase();
  document.querySelectorAll('#admin-users-head th.sortable').forEach(th=>{
    const on=th.dataset.sort===_adminUserSort.key;
    th.classList.toggle('sorted',on); th.classList.toggle('asc',on&&_adminUserSort.dir>0); th.classList.toggle('desc',on&&_adminUserSort.dir<0);
    th.setAttribute('aria-sort', on?(_adminUserSort.dir>0?'ascending':'descending'):'none');
  });
  const filtered=adminSortedUsers(_allAdminUsers).filter(u=>
    !q ||
    (u.email||'').toLowerCase().includes(q) ||
    (u.notify_email||'').toLowerCase().includes(q) ||
    (u.store_id||'').includes(q) ||
    (u.store_name||'').toLowerCase().includes(q)
  );

  const total=filtered.length;
  const totalPages=Math.max(1,Math.ceil(total/ADMIN_PAGE_SIZE));
  if(_adminUserPage>=totalPages) _adminUserPage=totalPages-1;
  const start=_adminUserPage*ADMIN_PAGE_SIZE;
  const page=filtered.slice(start, start+ADMIN_PAGE_SIZE);

  document.getElementById('admin-tbody').innerHTML=page.length
    ? page.map(u=>{
      const verified = u.email_verified !== false;
      const vBadge = verified
        ? '<span style="color:var(--green);font-size:11px" title="Email verified">✓ verified</span>'
        : '<span style="color:var(--red);font-size:11px" title="Email not verified">✗ unverified</span>';
      return `
      <tr>
        <td><strong>${escH(u.email)}</strong><br><span style="font-size:11px;color:var(--ink-soft)">${escH(u.created_at?.slice(0,10)||'')}</span></td>
        <td><span style="font-size:12px">${escH(u.notify_email||u.email)}</span><br>${vBadge}</td>
        <td>${u.store_id?`<span style="font-family:var(--mono);font-size:12px">#${escH(u.store_id)}</span><br><span style="font-size:11px;color:var(--ink-soft)">${escH(u.store_name||'')}</span>`:'<span style="color:var(--ink-soft);font-size:12px">—</span>'}</td>
        <td><button class="abn a" data-email="${escH(u.email)}" data-items="${escH(JSON.stringify(u.items))}" data-action="admin-show-items">${u.item_count} item${u.item_count!==1?'s':''}</button></td>
        <td style="font-size:11px;color:var(--ink-soft)">${escH(u.created_at?.slice(0,10)||'—')}</td>
        <td style="font-size:11px;white-space:nowrap">${adminLastLoginHtml(u.last_login)}</td>
        <td><div style="display:flex;gap:5px;flex-wrap:wrap">
          <button class="abn g" data-email="${escH(u.email)}" data-action="admin-show-pin">Reset PIN</button>
          <button class="abn a" data-email="${escH(u.email)}" data-action="admin-show-email">Reset Email</button>
          ${!verified?`<button class="abn a" data-email="${escH(u.email)}" data-action="admin-resend-verify">Resend Verify</button>`:''}
          <button class="abn r" data-email="${escH(u.email)}" data-action="admin-del-user">Delete</button>
        </div></td>
      </tr>`}).join('')
    : `<tr><td colspan="7" style="color:var(--ink-soft);font-size:13px;padding:16px">${q?'No users match your search.':'No users found.'}</td></tr>`;

  // Pagination controls
  const pages=document.getElementById('admin-user-pages');
  if(total<=ADMIN_PAGE_SIZE){pages.innerHTML='';return;}
  pages.innerHTML=`
    <span>Showing ${start+1}–${Math.min(start+ADMIN_PAGE_SIZE,total)} of ${total}</span>
    <div style="display:flex;gap:6px">
      <button class="abn g" data-action="admin-user-page" data-dir="-1" ${_adminUserPage===0?'disabled':''}>← Prev</button>
      <span style="padding:4px 8px">${_adminUserPage+1} / ${totalPages}</span>
      <button class="abn g" data-action="admin-user-page" data-dir="1" ${_adminUserPage>=totalPages-1?'disabled':''}>Next →</button>
    </div>`;
}

function adminUserPage(dir){
  _adminUserPage+=dir;
  adminRenderUsers();
}
function showCreateUser(){
  document.getElementById('ac-email').value='';clrPin('.ac-pin');setMsg('ac-msg','','');
  document.getElementById('admin-create-ov').classList.add('vis');
}
async function adminDoCreate(){
  const em=document.getElementById('ac-email').value.trim().toLowerCase();
  const pin=getPin('.ac-pin');
  if(!em||pin.length!==4){setMsg('ac-msg','Enter email and 4-digit PIN.','err');return;}
  try{
    const res=await fetch(API+'/admin/users',{method:'POST',credentials:'include',headers:adminHdrs(),body:JSON.stringify({email:em,pin})});
    const data=await res.json();
    if(!res.ok) throw new Error(data.error||'Failed');
    setMsg('ac-msg','✓ Created.','ok');
    setTimeout(()=>{closeOv('admin-create-ov');adminLoadUsers();},1000);
  }catch(e){setMsg('ac-msg',e.message,'err');}
}
async function adminDelUser(em){
  if(!confirm(`Delete ${em}? Cannot be undone.`)) return;
  try{
    const res=await fetch(API+'/admin/users/'+encodeURIComponent(em),{method:'DELETE',credentials:'include',headers:adminHdrs()});
    const data=await res.json();
    if(!res.ok) throw new Error(data.error||'Failed');
    _allAdminUsers=_allAdminUsers.filter(u=>u.email!==em);
    document.getElementById('admin-user-ct').textContent=`(${_allAdminUsers.length})`;
    adminRenderUsers();
  }catch(e){alert('Error: '+e.message);}
}
async function adminResendVerify(em){
  if(!confirm(`Resend verification email to ${em}?`)) return;
  try{
    const res=await fetch(API+'/admin/users/'+encodeURIComponent(em)+'/resend-verification',{method:'POST',credentials:'include',headers:adminHdrs()});
    const data=await res.json();
    if(!res.ok) throw new Error(data.error||'Failed');
    alert(data.message||'Verification email sent.');
  }catch(e){alert('Error: '+e.message);}
}
let _apEmail='';
function adminShowPin(em){_apEmail=em;document.getElementById('apoe').textContent=em;clrPin('.ap-pin');setMsg('ap-msg','','');document.getElementById('admin-pin-ov').classList.add('vis');}
async function adminDoResetPin(){
  const pin=getPin('.ap-pin');if(pin.length!==4){setMsg('ap-msg','Enter 4-digit PIN.','err');return;}
  try{
    const res=await fetch(API+'/admin/users/'+encodeURIComponent(_apEmail)+'/reset-pin',{method:'POST',credentials:'include',headers:adminHdrs(),body:JSON.stringify({new_pin:pin})});
    const data=await res.json();
    if(!res.ok) throw new Error(data.error||'Failed');
    setMsg('ap-msg','✓ PIN reset.','ok');setTimeout(()=>closeOv('admin-pin-ov'),1200);
  }catch(e){setMsg('ap-msg',e.message,'err');}
}
let _aeEmail='';
function adminShowEmail(em){_aeEmail=em;document.getElementById('aeoe').textContent=em;document.getElementById('ae-new-inp').value='';setMsg('ae-msg','','');document.getElementById('admin-email-ov').classList.add('vis');}
async function adminDoResetEmail(){
  const ne=document.getElementById('ae-new-inp').value.trim().toLowerCase();
  if(!ne){setMsg('ae-msg','Enter new email.','err');return;}
  try{
    const res=await fetch(API+'/admin/users/'+encodeURIComponent(_aeEmail)+'/reset-email',{method:'POST',credentials:'include',headers:adminHdrs(),body:JSON.stringify({new_email:ne})});
    const data=await res.json();
    if(!res.ok) throw new Error(data.error||'Failed');
    setMsg('ae-msg','✓ Updated.','ok');setTimeout(()=>{closeOv('admin-email-ov');adminLoadUsers();},1200);
  }catch(e){setMsg('ae-msg',e.message,'err');}
}
let _aiEmail='';
function adminShowItems(em,items){
  _aiEmail=em;document.getElementById('ai-email').textContent=em;setMsg('ai-msg','','');
  document.getElementById('ai-list').innerHTML=!items.length
    ?'<p style="color:var(--ink-soft);font-style:italic">No items on list.</p>'
    :`<ol style="padding-left:20px">${items.map(i=>`<li style="padding:3px 0">${escH(i)}</li>`).join('')}</ol>`;
  document.getElementById('admin-items-ov').classList.add('vis');
}
async function adminClearItems(){
  if(!confirm(`Clear all items for ${_aiEmail}?`)) return;
  try{
    const res=await fetch(API+'/admin/users/'+encodeURIComponent(_aiEmail)+'/items',{method:'DELETE',credentials:'include',headers:adminHdrs()});
    const data=await res.json();
    if(!res.ok) throw new Error(data.error||'Failed');
    setMsg('ai-msg','✓ Cleared.','ok');setTimeout(()=>{closeOv('admin-items-ov');adminLoadUsers();},1000);
  }catch(e){setMsg('ai-msg',e.message,'err');}
}


// ── Auth Log ──────────────────────────────────────────────────────────────────
let _allAuthLogs = [];
async function adminLoadAuthLogs(){
  document.getElementById('auth-log-wrap').innerHTML='<div style="color:var(--ink-soft);font-size:13px;padding:8px">Loading…</div>';
  try{
    const res=await fetch(API+'/admin/auth-logs?limit=200',{credentials:'include',headers:adminHdrs()});
    const data=await res.json();
    if(!res.ok) throw new Error(data.error||'Failed');
    _allAuthLogs=data.logs||[];
    adminRenderAuthLogs();
  }catch(e){
    document.getElementById('auth-log-wrap').innerHTML=`<div style="color:var(--red);font-size:13px;padding:8px">Error: ${escH(e.message)}</div>`;
  }
}
function adminRenderAuthLogs(){
  const filter=document.getElementById('auth-log-filter').value;
  let logs=_allAuthLogs;
  if(filter==='success') logs=logs.filter(l=>l.success);
  if(filter==='fail')    logs=logs.filter(l=>!l.success);
  if(!logs.length){
    document.getElementById('auth-log-wrap').innerHTML='<div style="color:var(--ink-soft);font-size:13px;padding:10px">No auth events found.</div>';
    return;
  }
  const rows=logs.map(l=>{
    const dt=new Date(l.ts);
    const est=dt.toLocaleString('en-US',{timeZone:'America/New_York',month:'numeric',day:'numeric',hour:'numeric',minute:'2-digit',hour12:true});
    const geo=[l.city,l.region,l.country].filter(Boolean).join(', ')||'—';
    const ua=(l.user_agent||'').replace(/Mozilla\/5\.0\s*/,'').slice(0,60);
    const badge=l.success
      ?'<span class="ok">✓ Login</span>'
      :'<span class="fail">✗ Fail</span>';
    return `<tr>
      <td style="white-space:nowrap">${est}</td>
      <td>${badge}</td>
      <td><strong style="font-size:12px">${escH(l.email)}</strong></td>
      <td style="font-family:var(--mono);font-size:11px">${escH(l.ip||'—')}</td>
      <td style="font-size:11px">${escH(geo)}</td>
      <td style="font-size:11px;color:var(--ink-soft);max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${escH(l.user_agent||'')}">${escH(ua)}</td>
    </tr>`;
  }).join('');
  document.getElementById('auth-log-wrap').innerHTML=`
    <table class="auth-log-tbl">
      <thead><tr><th>Time (ET)</th><th>Result</th><th>Email</th><th>IP</th><th>Location</th><th>Browser</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
}

// ── App / Frontend / API / Email / Cache Logs ────────────────────────────────
let _allAppLogs = [];
async function adminLoadAppLogs(){
  document.getElementById('app-log-wrap').innerHTML='<div style="color:var(--ink-soft);font-size:13px;padding:8px">Loading…</div>';
  try{
    const srcFilter=document.getElementById('app-log-filter').value;
    const srcParam=srcFilter!=='all'?`&source=${encodeURIComponent(srcFilter)}`:"";    
    const res=await fetch(API+`/admin/app-logs?limit=500${srcParam}`,{credentials:'include',headers:adminHdrs()});
    const data=await res.json();
    if(!res.ok) throw new Error(data.error||'Failed');
    _allAppLogs=data.logs||[];
    adminRenderAppLogs();
  }catch(e){
    document.getElementById('app-log-wrap').innerHTML=`<div style="color:var(--red);font-size:13px;padding:8px">Error: ${escH(e.message)}</div>`;
  }
}
function adminRenderAppLogs(){
  const filter=document.getElementById('app-log-filter').value;
  let logs=_allAppLogs.filter(l=>l.kind!=='stats');   // v9.3: daily cache counters live in the Cache widget
  if(filter!=='all') logs=logs.filter(l=>l.source===filter);
  const el=document.getElementById('app-log-wrap');
  if(!logs.length){
    el.innerHTML='<div style="color:var(--ink-soft);font-size:13px;padding:10px">No logs found.</div>';
    return;
  }
  const SOURCE_ICONS={'frontend':'💻','api':'⚡','email':'✉️','cache':'🗄️','scraper':'🛒'};
  const rows=logs.map(l=>{
    const dt=new Date(l.ts);
    const est=dt.toLocaleString('en-US',{timeZone:'America/New_York',month:'numeric',day:'numeric',
      hour:'numeric',minute:'2-digit',hour12:true});
    const lvl=l.level||'info';
    const src=l.source||'?';
    const icon=SOURCE_ICONS[src]||'•';
    let detail='';
    if(src==='email'){
      detail=`to: ${escH(l.to||'')} | subj: ${escH((l.subject||'').slice(0,40))} | ${l.ok?'✓ sent':'✗ failed'}`;
    } else if(src==='cache'){
      detail=`store ${escH(l.store_id||'?')} | ${l.hit?'● hit':'○ miss'} | ${escH(l.endpoint||'')}`;
    } else if(src==='api' && (l.action||'').startsWith('inbound-')){
      const parts=[];
      if(l.action) parts.push(`<strong>${escH(l.action)}</strong>`);
      if(l.sender) parts.push(`from: ${escH(l.sender)}`);
      if(l.user)   parts.push(`user: ${escH(l.user)}`);
      if(l.message) parts.push(escH(l.message.slice(0,100)));
      if(l.items_found!==undefined) parts.push(`items: ${l.items_found}`);
      if(l.added!==undefined) parts.push(`added: ${l.added}`);
      detail=parts.join(' · ');
    } else if(src==='api'){
      detail=`${escH(l.method||'?')} ${escH(l.path||'')} → ${l.status||'?'} | ${escH((l.message||'').slice(0,80))}`;
    } else {
      const msg=(l.message||'')+(l.stack?' │ '+l.stack.split('\n')[0]:'');
      detail=escH(msg.slice(0,200));
      if(l.url) detail+=` <span style="color:var(--ink-soft);font-size:10px">[${escH(l.url.slice(0,60))}]</span>`;
    }
    return `<div class="app-log-row lv-${escH(lvl)}">
      <span class="alts">${est}</span>
      <span class="alsrc">${icon} ${escH(src)}</span>
      <span class="almsg">${detail}</span>
    </div>`;
  }).join('');
  el.innerHTML=rows;
}

// ── Cache stats widget ────────────────────────────────────────────────────────
async function adminLoadCacheStats(){
  const el=document.getElementById('cache-stats-wrap');
  el.innerHTML='<div style="color:var(--ink-soft);font-size:13px">Loading…</div>';
  try{
    const res=await fetch(API+'/admin/app-logs?limit=500&source=cache',{credentials:'include',headers:adminHdrs()});
    const data=await res.json();
    if(!res.ok) throw new Error(data.error||'Failed');
    const all=(data.logs||[]).filter(l=>l.source==='cache');
    // v9.3: daily counter rows (kind=stats, hits_<store>/misses_<store>) replace one row per request.
    // Older per-request rows (hit=true/false) still count until they expire.
    const byStore={};
    const bump=(s,k,n)=>{ if(!byStore[s]) byStore[s]={hits:0,misses:0,store:s}; byStore[s][k]+=n; };
    for(const l of all){
      if(l.kind==='stats'){
        for(const [k,v] of Object.entries(l)){
          const m=/^(hits|misses)_(.+)$/.exec(k);
          if(m) bump(m[2], m[1], Number(v)||0);
        }
      } else {
        bump(l.store_id||'unknown', l.hit?'hits':'misses', 1);
      }
    }
    const stores=Object.values(byStore);
    const totalHits=stores.reduce((a,s)=>a+s.hits,0);
    const totalMiss=stores.reduce((a,s)=>a+s.misses,0);
    if(!totalHits && !totalMiss){
      el.innerHTML='<div style="color:var(--ink-soft);font-size:13px">No cache events recorded yet.</div>';
      return;
    }
    const hitPct=Math.round(totalHits/(totalHits+totalMiss)*100);
    const rows=Object.values(byStore).sort((a,b)=>(b.hits+b.misses)-(a.hits+a.misses)).map(s=>{
      const total=s.hits+s.misses;
      const pct=total?Math.round(s.hits/total*100):0;
      return `<tr>
        <td style="font-family:var(--mono);font-size:12px">#${escH(s.store)}</td>
        <td style="font-size:12px;color:var(--green)">${s.hits}</td>
        <td style="font-size:12px;color:var(--red)">${s.misses}</td>
        <td style="font-size:12px">${pct}%</td>
        <td style="min-width:100px"><div style="height:6px;background:var(--border);border-radius:3px;overflow:hidden">
          <div style="height:100%;width:${pct}%;background:var(--green);border-radius:3px"></div></div></td>
      </tr>`;
    }).join('');
    el.innerHTML=`
      <div class="stats-grid" style="margin-bottom:16px">
        <div class="stat-card"><div class="stat-val">${totalHits+totalMiss}</div><div class="stat-lbl">Total Requests</div></div>
        <div class="stat-card"><div class="stat-val" style="color:var(--green)">${totalHits}</div><div class="stat-lbl">Cache Hits</div></div>
        <div class="stat-card"><div class="stat-val" style="color:var(--amber)">${totalMiss}</div><div class="stat-lbl">Cache Misses</div></div>
        <div class="stat-card"><div class="stat-val">${hitPct}%</div><div class="stat-lbl">Hit Rate</div></div>
      </div>
      <table class="admin-tbl" style="font-size:13px">
        <thead><tr><th>Store</th><th>Hits</th><th>Misses</th><th>Rate</th><th style="min-width:100px"></th></tr></thead>
        <tbody>${rows}</tbody>
      </table>`;
  }catch(e){
    el.innerHTML=`<div style="color:var(--red);font-size:13px">Error: ${escH(e.message)}</div>`;
  }
}

// ── Frontend error reporting ───────────────────────────────────────────────────
// v9.8.1: only report errors from Cartwise's own code. Browser extensions
// (Safari shows their scripts as webkit-masked-url://hidden/), cross-origin
// "Script error." with no source, and the benign ResizeObserver notice were
// filling the log with entries nobody can act on.
const _ERR_NOISE = [/^Script error\.?$/i, /ResizeObserver loop/i];
const _EXT_SRC = /(webkit-masked-url|safari-(web-)?extension|chrome-extension|moz-extension|ms-browser-extension):\/\//i;
const _errSeen = new Map();
function _reportableError(msg, src, stack){
  msg = String(msg || '');
  if (_ERR_NOISE.some(r => r.test(msg))) return false;
  if (_EXT_SRC.test(String(src || '')) || _EXT_SRC.test(String(stack || ''))) return false;
  const key = msg.slice(0, 200), now = Date.now();                 // same error at most once a minute
  if (now - (_errSeen.get(key) || 0) < 60000) return false;
  _errSeen.set(key, now);
  return _errSeen.size <= 50;                                      // and no more than 50 kinds per page load
}
function _sendError(message, stack, url){
  if(!userEmail) return; // skip silently — /log/error requires auth (PDC-11)
  try{fetch(API+'/log/error',{method:'POST',credentials:'include',headers:{...authHeaders(),'Content-Type':'application/json'},
    body:JSON.stringify({level:'error',message,stack,url})});}catch(_){}
}
window.onerror = function(msg, src, line, col, err){
  const stack = err && err.stack || '';
  if (!_reportableError(msg, src, stack)) return;
  _sendError(String(msg), stack, src+'#L'+line+':'+col);
};
window.onunhandledrejection = function(e){
  const stack = e.reason && e.reason.stack || '';
  if (!_reportableError(e.reason && e.reason.message || e.reason, '', stack)) return;
  _sendError('Unhandled rejection: '+String(e.reason), stack, window.location.href);
};

// ═══════════════════════════════════════════════════════════════════
// WELCOME MODAL
// ═══════════════════════════════════════════════════════════════════
let _wlcStep = 0;
const WLC_STEPS = 3;

function wlcShow(){
  _wlcStep = 0;
  _wlcRender();
  document.getElementById('wlc-overlay').classList.add('vis');
}

function _wlcRender(){
  // Show correct step
  for(let i=0;i<WLC_STEPS;i++){
    document.getElementById('wlc-step-'+i).classList.toggle('active', i===_wlcStep);
    document.getElementById('wlc-dot-'+i).classList.toggle('active', i===_wlcStep);
  }
  // Update button label
  const btn = document.getElementById('wlc-next-btn');
  if(_wlcStep < WLC_STEPS-1){
    btn.textContent = 'Next →';
  } else {
    btn.textContent = "Let’s go →";
    btn.style.background = 'var(--green)';
  }
}

function wlcNext(){
  if(_wlcStep < WLC_STEPS-1){
    _wlcStep++;
    _wlcRender();
  } else {
    wlcDismiss();
  }
}

function wlcDismiss(){
  document.getElementById('wlc-overlay').classList.remove('vis');
  // Mark this user as welcomed so we never show again
  if(userEmail) localStorage.setItem('cw_welcomed_'+userEmail, '1');
  // Ensure we're on the store panel after dismissal
  showPanel('store');
}

// ═══════════════════════════════════════════════════════════════════
// UTILS
// ═══════════════════════════════════════════════════════════════════
function decodeEntities(s){
  if(!s) return '';
  // Use the browser's own HTML parser — handles every named entity (&euml; &rsquo; &ndash; etc.)
  // without maintaining a lookup table. The textarea trick is safe: no script execution.
  const ta=document.createElement('textarea');
  ta.innerHTML=String(s);
  return ta.value.replace(/[\r\n]+/g,' ').trim();
}
function escH(s){return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');}
function closeOv(id){document.getElementById(id).classList.remove('vis');}

// ═══════════════════════════════════════════════════════════════════
// AUTO-LOGIN
// ═══════════════════════════════════════════════════════════════════
(async function init(){
  // Check active session via token
  try{
    const res=await fetch(API+'/user/prefs', {credentials:'include',headers:authHeaders()});
    if(!res.ok){localStorage.removeItem('cw_email');return;}
    const data=await res.json();
    prefs=data.prefs;userEmail=data.email;
    enterApp({isFirstLogin: !localStorage.getItem('cw_welcomed_'+data.email) && !(prefs && prefs.store_id)});
  }catch(_){}
})();

document.querySelectorAll('.overlay').forEach(o=>{
  o.addEventListener('click',e=>{if(e.target===o) o.classList.remove('vis');});
});

// ═══════════════════════════════════════════════════════════════════
// AUTOCOMPLETE
// ═══════════════════════════════════════════════════════════════════
let _acIdx = -1;

function acGetTitles() {
  const seen = new Set();
  const titles = [];
  // Merge current-week deal titles with persistent corpus
  const allSources = [
    ..._allDeals.map(d => d.title || ''),
    ..._corpusTitles,
  ];
  // Exclude items already on the list
  const alreadyAdded = new Set(itemNames().map(n => n.toLowerCase()));
  for (const t of allSources) {
    const trimmed = t.trim(); // entities already decoded at load time in loadCorpus / _allDeals
    if (trimmed && !seen.has(trimmed.toLowerCase()) && !alreadyAdded.has(trimmed.toLowerCase())) {
      seen.add(trimmed.toLowerCase());
      titles.push(trimmed);
    }
  }
  return titles;
}

function acInput() {
  const q = (document.getElementById('new-item').value || '').trim();
  const drop = document.getElementById('ac-drop');
  if (!q) { drop.style.display = 'none'; return; }

  const qLow = q.toLowerCase();
  const all = acGetTitles();
  const words = qLow.split(/\s+/).filter(Boolean);
  const matches = all.filter(t => words.some(w => t.toLowerCase().includes(w)));

  matches.sort((a, b) => {
    const aStart = a.toLowerCase().startsWith(qLow) ? 0 : 1;
    const bStart = b.toLowerCase().startsWith(qLow) ? 0 : 1;
    return aStart - bStart || a.localeCompare(b);
  });

  const suggestions = matches.slice(0, 8);
  _acIdx = -1;

  drop.innerHTML = [
    `<div class="ac-item ac-item-free" data-val="${escH(q)}" data-action="ac-select">${escH(q)} <span style="font-size:10px;opacity:.55;margin-left:4px">add custom</span></div>`,
    ...suggestions.map(s =>
      `<div class="ac-item ac-item-deal" data-val="${escH(s)}" data-action="ac-select">${escH(s)}</div>`
    )
  ].join('');

  drop.style.display = q ? '' : 'none';
}

function acKeydown(e) {
  const drop = document.getElementById('ac-drop');
  const items = drop.querySelectorAll('.ac-item');
  if (drop.style.display === 'none' || !items.length) {
    if (e.key === 'Enter') addItem();
    return;
  }
  if (e.key === 'ArrowDown') {
    e.preventDefault();
    _acIdx = Math.min(_acIdx + 1, items.length - 1);
    items.forEach((el, i) => el.classList.toggle('ac-sel', i === _acIdx));
    if (_acIdx >= 0) document.getElementById('new-item').value = items[_acIdx].dataset.val || items[_acIdx].textContent.trim();
  } else if (e.key === 'ArrowUp') {
    e.preventDefault();
    _acIdx = Math.max(_acIdx - 1, 0);
    items.forEach((el, i) => el.classList.toggle('ac-sel', i === _acIdx));
    if (_acIdx >= 0) document.getElementById('new-item').value = items[_acIdx].dataset.val || items[_acIdx].textContent.trim();
  } else if (e.key === 'Enter') {
    e.preventDefault();
    if (_acIdx >= 0 && items[_acIdx]) {
      acSelect(items[_acIdx].dataset.val || items[_acIdx].textContent.trim());
    } else {
      addItem();
    }
  } else if (e.key === 'Escape') {
    acHide();
    closeDealModal();
  }
}

function acSelect(val) {
  document.getElementById('new-item').value = decodeEntities(val);
  acHide();
  addItem();
}

function acHide() {
  const drop = document.getElementById('ac-drop');
  if (drop) drop.style.display = 'none';
  _acIdx = -1;
}
document.addEventListener('DOMContentLoaded', () => {
  initPinInputs('.auth-pin');
  initPinInputs('.ac-pin');

  // ═══════════════════════════════════════════════════════════════════
  // PDC-21: EVENT DELEGATION — replaces all inline on* handlers
  // ═══════════════════════════════════════════════════════════════════

  // ── Image error fallback (must use capture phase) ─────────────────
  document.addEventListener('error', (e) => {
    if (e.target.tagName === 'IMG' && e.target.hasAttribute('data-img-fallback')) {
      e.target.style.display = 'none';
      const sib = e.target.nextElementSibling;
      if (sib) sib.style.display = 'flex';
    }
  }, true);

  // ── Auth form submit ──────────────────────────────────────────────
  const authForm = document.getElementById('auth-form');
  if (authForm) authForm.addEventListener('submit', (e) => { e.preventDefault(); doAuth(); });

  // ── Click delegation ──────────────────────────────────────────────
  document.addEventListener('click', (e) => {
    if (e.target.closest('a[data-stop]')) return;   // v9.4: links inside cards (Clip coupon)
    const el = e.target.closest('[data-action]');
    if (!el) return;
    const action = el.dataset.action;

    // -- v9.8 BOGO rule --
    if (action === 'bogo-answer') return setBogoMode(el.dataset.mode);

    // -- v9.4 mobile filter sheet --
    if (action === 'open-filter-sheet')  return toggleFilterSheet(true);
    if (action === 'close-filter-sheet') return toggleFilterSheet(false);
    if (action === 'reset-filters')      return resetDealFilters();

    // -- Auth / Nav --
    if (action === 'switch-tab')        return switchTab(el.dataset.tab);
    if (action === 'do-logout')         return doLogout();
    if (action === 'open-mobile-nav')   return openMobileNav();
    if (action === 'close-mobile-nav')  return closeMobileNav();
    if (action === 'nav-panel')         return showPanel(el.dataset.panel);
    if (action === 'nav-panel-mobile')  { showPanel(el.dataset.panel); closeMobileNav(); return; }

    // -- Verify banner --
    if (action === 'resend-verification') return resendVerification();
    if (action === 'hide-verify-banner')  return showVerifyBanner(false);

    // -- Store --
    if (action === 'do-store-search')   return doStoreSearch();
    if (action === 'clear-store')       return clearStore();
    if (action === 'select-store')      return selectStore(parseInt(el.dataset.index));

    // -- Deals --
    if (action === 'refresh-deals')     { _matchesDirty = true; return loadDeals(true); }
    if (action === 'load-more-deals')   return loadMoreDeals();
    if (action === 'clear-dept-chip')   { clearDeptCB(el.dataset.container, el.dataset.dept); renderDeals(); return; }
    if (action === 'open-deal-modal') {
      if (!e.target.closest('.deal-add-wrap,.btn-clip')) openDealModal(el.dataset.dealId);
      return;
    }

    // -- Deal modal --
    if (action === 'deal-modal-backdrop') { if (e.target === el) closeDealModal(); return; }
    if (action === 'close-deal-modal')  return closeDealModal();
    if (action === 'modal-toggle-list') return modalToggleList();

    // -- List --
    if (action === 'add-item')          return addItem();
    if (action === 'remove-item')       return removeItem(parseInt(el.dataset.index));
    if (action === 'bulk-import')       return bulkImport();
    if (action === 'copy-inbound')      return copyInboundAddr();
    if (action === 'export-download')   return exportListDownload();
    if (action === 'export-copy')       return exportListCopy(el);

    // -- Notifications --
    if (action === 'send-test-email')   return sendTestEmail();
    if (action === 'resend-weekly')     return resendWeeklyEmail();

    // -- Account --
    if (action === 'change-pin')        return changePin();
    if (action === 'wlc-show')          return wlcShow();
    if (action === 'show-delete-overlay') { document.getElementById('del-overlay').classList.add('vis'); return; }
    if (action === 'delete-account')    return deleteAccount();

    // -- Welcome tour --
    if (action === 'wlc-dismiss')       return wlcDismiss();
    if (action === 'wlc-next')          return wlcNext();

    // -- Overlays --
    if (action === 'close-overlay')     return closeOv(el.dataset.ov);

    // -- Admin auth --
    if (action === 'admin-login')       return adminLogin();
    if (action === 'admin-sign-out')    return adminSignOut();

    // -- Admin nav --
    if (action === 'admin-nav')         return adminNav(el.dataset.tab);
    if (action === 'show-create-user')  return showCreateUser();
    if (action === 'admin-refresh-users') return adminLoadUsers();
    if (action === 'admin-sort-users')    return adminSortUsers(el.dataset.sort);

    // -- Admin actions --
    if (action === 'admin-load-auth-logs')  return adminLoadAuthLogs();
    if (action === 'admin-load-stats')      return adminLoadStats();
    if (action === 'admin-run-scrape')      return adminRunScrape(el);
    if (action === 'admin-load-logs')       return adminLoadLogs();
    if (action === 'admin-load-tail')       return adminLoadTail();
    if (action === 'admin-load-app-logs')   return adminLoadAppLogs();
    if (action === 'admin-load-cache-stats') return adminLoadCacheStats();
    if (action === 'admin-load-inbound-logs') return adminLoadInboundLogs();
    if (action === 'admin-clear-inbound-filter') {
      document.getElementById('inbound-sender-filter').value = '';
      return adminLoadInboundLogs();
    }

    // -- Admin user table (delegated from dynamic content) --
    if (action === 'admin-show-items')  return adminShowItems(el.dataset.email, JSON.parse(el.dataset.items));
    if (action === 'admin-show-pin')    return adminShowPin(el.dataset.email);
    if (action === 'admin-show-email')  return adminShowEmail(el.dataset.email);
    if (action === 'admin-del-user')    return adminDelUser(el.dataset.email);
    if (action === 'admin-resend-verify') return adminResendVerify(el.dataset.email);
    if (action === 'admin-do-reset-pin') return adminDoResetPin();
    if (action === 'admin-do-reset-email') return adminDoResetEmail();
    if (action === 'admin-clear-items') return adminClearItems();
    if (action === 'admin-do-create')   return adminDoCreate();
    if (action === 'admin-user-page')   return adminUserPage(parseInt(el.dataset.dir));

    // -- Inbound chain toggle --
    if (action === 'toggle-chain') {
      const c = document.getElementById(el.dataset.chain);
      if (c) c.style.display = c.style.display === 'none' ? 'block' : 'none';
      return;
    }
  });

  // ── Change delegation ─────────────────────────────────────────────
  document.addEventListener('change', (e) => {
    const el = e.target.closest('[data-action]');
    if (!el) return;
    const action = el.dataset.action;

    if (action === 'matches-toggle')    return onMatchesToggle();
    if (action === 'bogo-mode')         return setBogoMode(el.value);
    if (action === 'threshold-slider')  return onThresholdChange();
    if (action === 'savings-filter')    return onSavingsFilter(el.dataset.filter);
    if (action === 'deals-sort')        return renderDeals();
    if (action === 'email-enabled-toggle') {
      document.getElementById('email-settings-wrap').style.display = el.checked ? '' : 'none';
      savePrefs('account');
      return;
    }
    if (action === 'dept-filter') {
      const fn = el.dataset.renderFn;
      if (fn === 'renderDeals')       renderDeals();
      return;
    }
    if (action === 'admin-render-auth-logs') return adminRenderAuthLogs();
    if (action === 'admin-render-app-logs')  return adminRenderAppLogs();
  });

  // ── Input delegation ──────────────────────────────────────────────
  document.addEventListener('input', (e) => {
    const el = e.target.closest('[data-action]');
    if (!el) return;
    const action = el.dataset.action;

    if (action === 'sanitize-email')    return sanitizeEmailInput(el);
    if (action === 'store-input-type' || action === 'store-search')  return onStoreInputType();
    if (action === 'threshold-slider') {
      document.getElementById('threshold-val').textContent = el.value;
      return;
    }
    if (action === 'deals-search-input') return debouncedRenderDeals();
    if (action === 'ac-input')          return acInput();
    if (action === 'admin-filter-users') return adminFilterUsers();
  });

  // ── Keydown delegation ────────────────────────────────────────────
  document.addEventListener('keydown', (e) => {
    const el = e.target.closest('[data-action]');
    if (!el) return;
    const action = el.dataset.action;

    if ((action === 'store-search-key' || action === 'store-search') && e.key === 'Enter')  return doStoreSearch();
    if (action === 'ac-input')           return acKeydown(e);
    if (action === 'admin-secret-key' && e.key === 'Enter')  return adminLogin();
  });

  // ── Mousedown delegation (v9.2.2 #3) ──────────────────────────────
  // Autocomplete picks fire on mousedown so they land before the input's blur
  // hides the list. The old inline onmousedown was blocked by the CSP.
  document.addEventListener('mousedown', (e) => {
    const el = e.target.closest('[data-action="ac-select"]');
    if (!el) return;
    e.preventDefault();                 // keep focus in the input
    acSelect(el.dataset.val || el.textContent.trim());
  });

  // ── Blur delegation ───────────────────────────────────────────────
  document.addEventListener('blur', (e) => {
    const el = e.target.closest('[data-action]');
    if (!el) return;
    const action = el.dataset.action;

    if (action === 'ac-input')           return setTimeout(acHide, 150);
  }, true); // blur doesn't bubble — use capture
});
