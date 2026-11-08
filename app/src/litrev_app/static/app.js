/* Litrev App: the page core (window.LR), the reviews list, the new-review form and Settings.
   Vanilla JS, no build step. Every element is built with LR.h and text nodes (no HTML strings are ever parsed), so
   text that came from papers or from Claude can't become markup. */
(function () {
  'use strict';

  const LR = (window.LR = {
    pages: {}, reviewTabs: [], askForms: {}, features: {}, app: null,
    _cleanup: [], _listeners: {},
  });
  // The CSRF secret comes once, in /auth's redirect to /#k=..., never in GET /. localStorage keeps it for reloads: its
  // origin includes the port, the cookie's doesn't, so a listener on another loopback port can't read it (security F4).
  // Read before boot() routes: '#k=...' is not a page.
  const CSRF = (function () {
    const m = /^#k=([^&]+)/.exec(location.hash);
    if (!m) {
      try { return localStorage.getItem('litrev-csrf') || ''; } catch (e) { return ''; }
    }
    let k = m[1];
    try { k = decodeURIComponent(k); } catch (e) { /* a bad %-escape: keep it as it is */ }
    try { localStorage.setItem('litrev-csrf', k); } catch (e) { /* blocked storage: this load still works */ }
    history.replaceState(null, '', location.pathname + location.search);
    return k;
  })();

  // ---- building elements ------------------------------------------------------------------------------------
  const BOOL = new Set(['checked', 'disabled', 'hidden', 'selected', 'required', 'open', 'multiple']);

  function h(tag, attrs, ...children) {
    const el = document.createElement(tag);
    const a = attrs || {};
    for (const [k, v] of Object.entries(a)) {
      if (k === 'value' || v === null || v === undefined || v === false) continue;
      if (k === 'class') el.className = v;
      else if (k === 'text') el.textContent = v;
      else if (k === 'dataset') Object.assign(el.dataset, v);
      else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2), v);
      else if (BOOL.has(k)) el[k] = true;
      else el.setAttribute(k, v === true ? '' : String(v));
    }
    appendAll(el, children);
    if ('value' in a && a.value !== null && a.value !== undefined) el.value = a.value;   // after <option>s exist
    return el;
  }
  function appendAll(el, kids) {
    for (const c of kids.flat(Infinity)) {
      if (c === null || c === undefined || c === false) continue;
      el.append(c instanceof Node ? c : document.createTextNode(String(c)));
    }
  }
  LR.h = h;

  // A small, safe markdown: paragraphs, - / 1. lists, # headings, **bold**, *italic*, `code`, http(s) links.
  function inline(s) {
    const out = [];
    const re = /\*\*([^*]+)\*\*|`([^`]+)`|\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)|\*([^*\s][^*]*)\*/g;
    let last = 0;
    let m;
    while ((m = re.exec(s))) {
      if (m.index > last) out.push(s.slice(last, m.index));
      if (m[1]) out.push(h('strong', null, m[1]));
      else if (m[2]) out.push(h('code', null, m[2]));
      else if (m[3]) out.push(h('a', { href: m[4], target: '_blank', rel: 'noopener noreferrer' }, m[3]));
      else if (m[5]) out.push(h('em', null, m[5]));
      last = re.lastIndex;
    }
    if (last < s.length) out.push(s.slice(last));
    return out;
  }
  LR.md = function md(text) {
    const frag = document.createDocumentFragment();
    let para = [];
    let list = null;
    const flushPara = () => { if (para.length) { frag.append(h('p', null, inline(para.join(' ')))); para = []; } };
    const flushList = () => { if (list) { frag.append(list); list = null; } };
    for (const raw of String(text || '').replace(/\r\n?/g, '\n').split('\n')) {
      const line = raw.trimEnd();
      const head = /^(#{1,4})\s+(.*)$/.exec(line);
      const bullet = /^\s*[-*]\s+(.*)$/.exec(line);
      const num = /^\s*\d+[.)]\s+(.*)$/.exec(line);
      if (!line.trim()) { flushPara(); flushList(); continue; }
      if (head) { flushPara(); flushList(); frag.append(h(head[1].length <= 2 ? 'h4' : 'h5', null, inline(head[2]))); continue; }
      if (bullet || num) {
        flushPara();
        const tag = bullet ? 'ul' : 'ol';
        if (!list || list.tagName.toLowerCase() !== tag) { flushList(); list = h(tag); }
        list.append(h('li', null, inline((bullet || num)[1])));
        continue;
      }
      flushList();
      para.push(line.trim());
    }
    flushPara();
    flushList();
    return frag;
  };

  LR.field = function field(id, label, input, hint) {
    return h('div', { class: 'field' }, h('label', { for: id }, label), input,
      typeof hint === 'string' ? h('p', { class: 'hint' }, hint) : hint || null);
  };
  LR.errorBox = (err) => h('div', { class: 'banner bad', role: 'alert' }, h('p', null, (err && err.message) || String(err)));

  // ---- talking to the server --------------------------------------------------------------------------------
  function signedOut() {
    const main = document.getElementById('main');
    main.replaceChildren(h('div', { class: 'empty' }, h('h1', null, 'Open Litrev again'),
      h('p', null, 'Litrev restarted, or this page was opened without the app. Double-click Litrev in your ' +
        'Applications folder (or run litrev-app open in Terminal) to get a fresh link.')));
  }
  LR.api = async function api(method, path, body) {
    const opt = { method, headers: {}, credentials: 'same-origin' };
    opt.headers['X-CSRF'] = CSRF;                      // every method: the cookie alone reads nothing (security F4)
    if (body !== undefined) { opt.headers['Content-Type'] = 'application/json'; opt.body = JSON.stringify(body); }
    let r;
    try { r = await fetch(path, opt); } catch (e) { throw new Error('Litrev is not answering. Is the app still running?'); }
    let data = null;
    try { data = await r.json(); } catch (e) { data = null; }
    if (r.status === 401) signedOut();
    if (!r.ok) {
      const err = new Error((data && data.error) || `Request failed (${r.status})`);
      err.status = r.status;
      err.data = data;
      throw err;
    }
    return data;
  };
  // Live streams. Browsers allow 6 connections per host and every stream holds one, so a hidden tab closes its
  // streams and reopens them when shown again (M1b review: a few open tabs froze every request).
  const streams = new Set();
  LR.sse = function sse(slug, onEvent, opts) {
    const o = opts || {};
    const s = { last: o.after || 0, es: null, closed: false };
    s.open = () => {
      if (s.es || s.closed || document.hidden) return;
      const url = slug ? `/api/reviews/${encodeURIComponent(slug)}/events` : '/api/events';
      s.es = new EventSource(`${url}?k=${encodeURIComponent(CSRF)}`);    // EventSource can't send X-CSRF
      s.es.onmessage = (m) => {
        let ev;
        try { ev = JSON.parse(m.data); } catch (e) { return; }
        if (ev.seq && ev.seq <= s.last) return;        // the stream replays its ring buffer on every reconnect
        if (ev.seq) s.last = ev.seq;
        onEvent(ev);
      };
    };
    s.pause = () => { if (s.es) { s.es.close(); s.es = null; } };
    s.close = () => { s.closed = true; s.pause(); streams.delete(s); };
    streams.add(s);
    s.open();
    if (!o.keep) LR.onLeave(() => s.close());
    return s;
  };
  document.addEventListener('visibilitychange', () => {
    for (const s of streams) { if (document.hidden) s.pause(); else s.open(); }
    if (!document.hidden) LR.emit('visible');
  });

  // ---- small shared pieces ----------------------------------------------------------------------------------
  LR.on = function on(name, fn) {
    (LR._listeners[name] = LR._listeners[name] || new Set()).add(fn);
    return () => LR._listeners[name].delete(fn);
  };
  LR.emit = function emit(name, data) {
    for (const fn of LR._listeners[name] || []) {
      try { fn(data); } catch (e) { console.error(e); }
    }
  };
  LR.onLeave = (fn) => { LR._cleanup.push(fn); };
  LR.toast = function toast(msg, kind) {
    const box = document.getElementById('toasts');
    const t = h('div', { class: `toast ${kind || ''}` }, msg);
    box.append(t);
    setTimeout(() => t.remove(), kind === 'bad' ? 9000 : 5000);
  };

  const fmt = (LR.fmt = {
    money: (usd) => `$${(Number(usd) || 0).toFixed(2)}`,
    duration(sec) {
      const s = Math.max(0, Math.round(Number(sec) || 0));
      const hr = Math.floor(s / 3600);
      const mn = Math.floor((s % 3600) / 60);
      if (hr) return `${hr} h ${mn} min`;
      if (mn) return `${mn} min`;
      return `${s} s`;
    },
    clock: (ts) => (ts ? new Date(ts * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : ''),
    day: (ts) => (ts ? new Date(ts * 1000).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : ''),
    until(ts) {
      const s = ts - Date.now() / 1000;
      return s <= 0 ? 'any moment now' : `in ${fmt.duration(s)}`;
    },
    stepTitle(n) {
      const t = (LR.app && LR.app.step_titles) || {};
      return t[n] || t[String(n)] || '';
    },
    estimate: (cap) => Math.round(30 + 0.75 * (Number(cap) || 0)),
    phase(st, o) {
      const opt = o || {};
      const step = `step ${st.step}`;
      if (opt.asking || st.phase === 'waiting_user') return { dot: 'ask', text: 'Waiting for your answer' };
      switch (st.phase) {
        case 'running':
          return st.busy ? { dot: 'running', text: `Running ${step}: ${fmt.stepTitle(st.step)}` }
            : { dot: '', text: `Queued to run ${step}` };
        case 'paused':
          return { dot: 'warn', text: st.paused_until ? `Paused until ${fmt.clock(st.paused_until)} (${fmt.until(st.paused_until)})` : 'Paused' };
        case 'failed':
          return { dot: 'bad', text: `Stopped with a problem in ${step}` };
        case 'done':
          return { dot: 'ok', text: 'Done: every step has run' };
        default:
          if (opt.madeInClaudeCode) return { dot: '', text: 'Made in Claude Code. Press Run to continue it here.' };
          return { dot: '', text: st.message === 'Stopped' ? `Stopped at ${step}` : `Not running (at ${step})` };
      }
    },
  });

  // ---- router -----------------------------------------------------------------------------------------------
  LR.route = function route() {
    for (const f of LR._cleanup.splice(0)) {
      try { f(); } catch (e) { console.error(e); }
    }
    const parts = location.hash.replace(/^#\/?/, '').split('/').filter(Boolean).map(decodeURIComponent);
    const name = parts[0] || 'reviews';
    const page = LR.pages[name] || LR.pages.missing;
    const main = document.getElementById('main');
    main.replaceChildren();
    document.querySelectorAll('[data-nav]').forEach((a) => a.classList.toggle('on', a.dataset.nav === name));
    try { page(main, parts.slice(1)); } catch (e) { console.error(e); main.append(LR.errorBox(e)); }
  };

  function showProblems(problems) {
    const box = document.getElementById('problems');
    if (!problems || !problems.length) { box.hidden = true; box.replaceChildren(); return; }
    box.hidden = false;
    box.replaceChildren(h('div', { class: 'banner bad', role: 'alert' },
      problems.map((p) => h('p', null, p)),
      h('p', null, 'Reviews can be read, but no step can run until this is fixed. ', h('a', { href: '#/settings' }, 'Open Settings'))));
  }
  LR.refreshApp = async function refreshApp() {
    LR.app = await LR.api('GET', '/api/app');
    showProblems(LR.app.problems);
    return LR.app;
  };

  function applyTheme(choice) {
    let theme = choice;
    if (theme === undefined) {
      try { theme = localStorage.getItem('litrev-theme'); } catch (e) { theme = null; }
    }
    if (theme) document.documentElement.dataset.theme = theme;
    else delete document.documentElement.dataset.theme;
    const dark = theme ? theme === 'dark' : window.matchMedia('(prefers-color-scheme: dark)').matches;
    const btn = document.getElementById('theme');
    if (btn) btn.textContent = dark ? 'Light' : 'Dark';
    return dark;
  }

  function notifyBrowser(ev) {
    LR.toast(ev.body);
    if (document.hidden && 'Notification' in window && Notification.permission === 'granted') {
      try { new Notification(ev.title, { body: ev.body }); } catch (e) { /* not allowed here */ }
    }
  }

  async function boot() {
    applyTheme();
    document.getElementById('theme').addEventListener('click', () => {
      const next = document.getElementById('theme').textContent === 'Dark' ? 'dark' : 'light';
      try { localStorage.setItem('litrev-theme', next); } catch (e) { /* private window */ }
      applyTheme(next);
    });
    try { await LR.refreshApp(); } catch (e) { LR.app = { problems: [], step_titles: {} }; }
    LR.sse(null, (ev) => {
      LR.emit('global', ev);
      if (ev.type === 'notify') notifyBrowser(ev);
    }, { keep: true });
    window.addEventListener('hashchange', LR.route);
    LR.route();
  }
  document.addEventListener('DOMContentLoaded', boot);

  // ---- page: reviews ----------------------------------------------------------------------------------------
  function meter(st, asking) {
    const m = h('span', { class: 'meter', role: 'img', 'aria-label': `At step ${st.step} of 7` });
    for (let i = 0; i < 8; i++) {
      let cls = '';
      if (st.phase === 'done' || i < st.step) cls = 'done';
      else if (i === st.step) cls = asking || st.phase === 'waiting_user' ? 'ask' : 'cur';
      m.append(h('i', { class: cls }));
    }
    return m;
  }

  function entry(r) {
    const ph = fmt.phase(r.state, { asking: r.asking, madeInClaudeCode: r.made_in_claude_code });
    return h('li', null, h('a', { class: 'entry', href: `#/r/${encodeURIComponent(r.slug)}` },
      h('span', { class: 'entry-title' }, r.seed),
      h('span', { class: 'entry-purpose' }, r.purpose),
      h('span', { class: 'sentence' }, h('span', { class: `dot ${ph.dot}` }), ph.text),
      h('span', { class: 'entry-side' },
        r.made_in_claude_code ? null : meter(r.state, r.asking),
        h('span', { class: 'tags' },
          r.asking ? h('span', { class: 'tag marker' }, 'Question waiting') : null,
          r.made_in_claude_code ? h('span', { class: 'tag' }, 'Made in Claude Code') : null,
          r.hub ? h('span', { class: 'tag' }, 'Hub ready') : null))));
  }

  LR.pages.reviews = function reviewsPage(root) {
    const box = h('div', null, h('p', { class: 'hint' }, 'Loading your reviews…'));
    root.append(h('div', { class: 'pagehead' }, h('h1', null, 'Reviews'),
      h('a', { class: 'btn primary', href: '#/new' }, 'New review')), box);
    let timer = null;
    async function load() {
      try {
        const data = await LR.api('GET', '/api/reviews');
        if (!data.reviews.length) {
          box.replaceChildren(h('div', { class: 'empty' },
            h('p', null, 'No reviews yet. Name a topic or a paper and say what the review is for. Claude finds, ' +
              'screens and reads the papers. It asks you one question, the scope, and makes the other calls itself.'),
            h('a', { class: 'btn primary', href: '#/new' }, 'Start a review'),
            h('p', { class: 'hint' }, `Reviews are kept in ${data.root}/reviews.`)));
        } else {
          box.replaceChildren(h('ul', { class: 'catalogue' }, data.reviews.map(entry)));
        }
      } catch (e) {
        box.replaceChildren(LR.errorBox(e));
      }
    }
    LR.onLeave(LR.on('global', (ev) => {
      if (ev.type === 'state') { clearTimeout(timer); timer = setTimeout(load, 400); }
    }));
    const every = setInterval(load, 30000);         // keeps "paused until … (in 12 min)" current
    LR.onLeave(() => { clearInterval(every); clearTimeout(timer); });
    load();
  };

  // ---- page: new review -------------------------------------------------------------------------------------
  LR.pages.new = function newPage(root) {
    const topic = h('textarea', { id: 'topic', rows: 2, placeholder: 'Depth Anything; monocular depth foundation models' });
    const purpose = h('textarea', { id: 'purpose', rows: 3, placeholder: 'Decide which models to evaluate for zero-shot metric depth on dashcam video' });
    const cap = h('input', { id: 'cap', type: 'number', min: 5, max: 300, step: 1, value: 40 });
    const years = h('input', { id: 'years', type: 'number', min: 1, max: 30, step: 1, value: 3 });
    const est = h('p', { class: 'hint' });
    // step 8: the person answers the video question here, so the run never stops to ask it
    const video = h('input', { id: 'video', type: 'checkbox' });
    const minutes = h('input', { id: 'video_minutes', type: 'number', min: 3, max: 60, step: 1, value: 20, disabled: true });
    video.addEventListener('change', () => { minutes.disabled = !video.checked; });
    const showEst = () => {
      est.textContent = `About $${fmt.estimate(cap.value)} API-equivalent (~$30 + ~$0.75 per paper). Your Claude ` +
        "plan's usage limits apply: when you reach one, the review pauses and carries on by itself after the reset.";
    };
    cap.addEventListener('input', showEst);
    showEst();
    const uses = h('p', { class: 'hint' });
    LR.api('GET', '/api/settings').then((s) => {
      uses.replaceChildren(`Runs with your settings: ${s.parallel} subagents at once, step-5 model ` +
        `${s.step5_model || "the skill's default"}, effort ${s.effort || "Claude Code's default"}, OpenAlex key ${s.keys.openalex ? 'saved' : 'not set'}. `,
        h('a', { href: '#/settings' }, 'Change settings'));
      // without a key, OpenAlex's 1,000 daily credits (shared by the network) run out during one review
      if (!s.keys.openalex) uses.append('. A review needs more OpenAlex credits than the 1,000 a day without a key: get a free key at ',
        h('a', { href: 'https://openalex.org/settings/api', target: '_blank', rel: 'noopener' }, 'openalex.org/settings/api'),
        ' and save it in Settings.');
    }).catch(() => {});
    const err = h('p', { class: 'err', role: 'alert' });
    const go = h('button', { class: 'btn primary', type: 'submit' }, 'Start review');
    const form = h('form', {
      novalidate: true,
      onsubmit: async (e) => {
        e.preventDefault();
        err.textContent = '';
        go.disabled = true;
        go.textContent = 'Creating the review…';
        try {
          const r = await LR.api('POST', '/api/reviews', {
            topic: topic.value, purpose: purpose.value, cap: Number(cap.value), years: Number(years.value),
            video: video.checked, video_minutes: Number(minutes.value),
          });
          location.hash = `#/r/${encodeURIComponent(r.slug)}`;
        } catch (ex) {
          err.textContent = ex.message;
          go.disabled = false;
          go.textContent = 'Start review';
        }
      },
    },
    LR.field('topic', 'What should the review cover?', topic,
      'A topic, a method or a paper title. Claude starts from it and finds the related work.'),
    LR.field('purpose', 'What will you use it for?', purpose,
      'Claude uses this to judge which papers matter and what to compare.'),
    h('div', { class: 'row2' },
      LR.field('cap', 'Papers to include, at most', cap, est),
      LR.field('years', 'Years to look back', years, 'Older papers come in only when they are seminal.')),
    h('div', { class: 'field check' }, h('label', { for: 'video' }, video, ' Also make a narrated video of the review'),
      h('p', { class: 'hint' }, 'Step 8: a chapter plan, a script, a voice and an MP4. It covers the ten sections of the review and costs about USD 18-34 of extra ' +
        'usage for 20 minutes; the voice and the video render on this Mac. You can also add it later with Make video.')),
    h('div', { class: 'row2' }, LR.field('video_minutes', 'Video length, minutes', minutes, 'Default 20.')),
    uses, err, h('div', null, go));
    root.append(h('div', { class: 'pagehead' }, h('h1', null, 'New review')), form);
    topic.focus();
  };

  // ---- page: settings ---------------------------------------------------------------------------------------
  const MODELS = [['opus', 'Opus'], ['sonnet', 'Sonnet'], ['haiku', 'Haiku']];
  const EFFORTS = [['low', 'Low'], ['medium', 'Medium'], ['high', 'High'], ['xhigh', 'Extra high'], ['max', 'Max']];

  function select(id, value, first, choices = MODELS) {
    return h('select', { id, value: value || '' }, h('option', { value: '' }, first),
      choices.map(([v, label]) => h('option', { value: v }, label)));
  }

  function settingsForm(st, box) {
    const f = {
      root: h('input', { id: 'root', type: 'text', value: st.root }),
      parallel: h('input', { id: 'parallel', type: 'number', min: 1, max: 6, value: st.parallel }),
      step5: select('step5', st.step5_model, "The skill's default"),
      model: select('model', st.model, "Claude Code's default"),
      effort: select('effort', st.effort, "Claude Code's default", EFFORTS),
      wait: h('input', { id: 'wait', type: 'number', min: 1, max: 120, value: Math.round(st.wait_s / 60) }),
      mailto: h('input', { id: 'mailto', type: 'email', value: st.mailto || '', placeholder: 'you@example.org' }),
      oa: h('input', { id: 'oa', type: 'password', autocomplete: 'off',
        placeholder: st.keys.openalex ? 'Saved in your Keychain; type to replace it' : 'Not set' }),
      s2: h('input', { id: 's2', type: 'password', autocomplete: 'off',
        placeholder: st.keys.s2 ? 'Saved in your Keychain; type to replace it' : 'Not set' }),
      skill: h('input', { id: 'skill', type: 'text', value: st.skill_override || '',
        placeholder: 'Empty: use the installed plugin' }),
    };
    const clearOa = h('input', { type: 'checkbox', id: 'clear-oa' });
    const clearS2 = h('input', { type: 'checkbox', id: 'clear-s2' });
    const notifState = h('p', { class: 'hint' });
    const showNotif = () => {
      if (!('Notification' in window)) notifState.textContent = 'This browser cannot show notifications.';
      else notifState.textContent = { granted: 'Browser notifications are on.', denied: 'Browser notifications are blocked in this browser\'s settings.', default: 'Browser notifications are off.' }[Notification.permission];
    };
    showNotif();
    const err = h('p', { class: 'err', role: 'alert' });
    const save = h('button', { class: 'btn primary', type: 'submit' }, 'Save settings');
    const app = LR.app || {};
    return h('form', {
      class: 'settings',
      novalidate: true,
      onsubmit: async (e) => {
        e.preventDefault();
        err.textContent = '';
        save.disabled = true;
        const body = {
          root: f.root.value, parallel: Number(f.parallel.value), step5_model: f.step5.value, model: f.model.value,
          effort: f.effort.value, wait_s: Math.round(Number(f.wait.value) * 60), mailto: f.mailto.value, skill_override: f.skill.value,
        };
        if (f.oa.value) body.openalex_key = f.oa.value;
        if (f.s2.value) body.s2_key = f.s2.value;
        if (clearOa.checked) body.clear_openalex_key = true;
        if (clearS2.checked) body.clear_s2_key = true;
        try {
          const saved = await LR.api('POST', '/api/settings', body);
          await LR.refreshApp();
          LR.toast('Settings saved');
          box.replaceChildren(settingsForm(saved, box));
        } catch (ex) {
          err.textContent = ex.message;
          save.disabled = false;
        }
      },
    },
    h('section', null, h('h2', null, 'Where reviews live'),
      LR.field('root', 'Reviews folder', f.root, 'Each review is a folder inside its reviews/ subfolder. Claude Code sessions use the same folder.')),
    h('section', null, h('h2', null, 'How steps run'),
      LR.field('parallel', 'Subagents at once', f.parallel, 'Readers working in parallel inside one step. More is faster but hits Claude rate limits sooner; 3 is safe.'),
      LR.field('step5', 'Model for reading method details (step 5)', f.step5, 'Step 5 reads every included paper in full and is the most expensive step.'),
      LR.field('model', 'Model for the step sessions', f.model),
      LR.field('effort', 'Effort', f.effort, 'How much Claude thinks in every session and subagent. Lower effort costs less; Medium is the default.'),
      LR.field('wait', 'Keep a question open for (minutes)', f.wait, 'After this, Claude saves its work and stops; your answer starts the next session.')),
    h('section', null, h('h2', null, 'Paper sources'),
      LR.field('mailto', 'Your email for OpenAlex', f.mailto, 'OpenAlex answers faster for requests that carry a contact address.'),
      LR.field('oa', 'OpenAlex API key', f.oa, st.keys.openalex ? h('label', { class: 'hint' }, clearOa, ' Remove the saved key') : 'Optional. Stored in your macOS Keychain.'),
      LR.field('s2', 'Semantic Scholar API key', f.s2, st.keys.s2 ? h('label', { class: 'hint' }, clearS2, ' Remove the saved key') : 'Optional. Gives citation lookups their own rate limit.')),
    h('section', null, h('h2', null, 'Notifications'),
      h('p', null, 'macOS notifications appear when Claude asks you something, a step fails or a review is done.'),
      notifState,
      'Notification' in window && Notification.permission === 'default'
        ? h('button', { class: 'btn', type: 'button', onclick: async () => { await Notification.requestPermission(); showNotif(); } }, 'Also show browser notifications')
        : null),
    h('section', null, h('details', null, h('summary', null, 'Advanced'),
      LR.field('skill', 'Skill folder', f.skill, 'Only for testing a different copy of the reviewing-literature skill.'))),
    h('section', null, h('h2', null, 'About this copy of Litrev'),
      h('dl', { class: 'kv' },
        h('dt', null, 'Version'), h('dd', null, app.version || ''),
        h('dt', null, 'Claude Code'), h('dd', null, app.claude || ''),
        h('dt', null, 'Skill'), h('dd', null, app.skill || ''),
        h('dt', null, 'Address'), h('dd', null, app.port ? `127.0.0.1:${app.port}` : ''))),
    err, h('div', null, save));
  }

  LR.pages.settings = function settingsPage(root) {
    const box = h('div', null, h('p', { class: 'hint' }, 'Loading settings…'));
    root.append(h('div', { class: 'pagehead' }, h('h1', null, 'Settings')), box);
    LR.api('GET', '/api/settings').then((st) => box.replaceChildren(settingsForm(st, box)))
      .catch((e) => box.replaceChildren(LR.errorBox(e)));
  };

  LR.pages.missing = function missingPage(root) {
    root.append(h('div', { class: 'empty' }, h('h1', null, 'Nothing here'),
      h('p', null, 'This address is not a Litrev page. '), h('a', { href: '#/reviews' }, 'Go to your reviews')));
  };
})();
