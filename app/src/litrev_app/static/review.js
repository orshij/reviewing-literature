/* Litrev App: the review page. Header (status sentence, facts, buttons), banners, the question card, and tabs:
   Progress (step rail + live activity), Decisions, Sessions, Hub. Feature scripts add tabs through LR.reviewTabs. */
(function () {
  'use strict';
  const LR = window.LR;
  const { h, fmt } = LR;

  const FEED_MAX = 200;
  const QUIET = new Set(['start', 'result', 'retry', 'denied']);
  // plain shell commands (ls, wc, --help) are how agents look around; they bury the meaningful rows (live check)
  const NOISE = (ev) => ev.kind === 'tool' && ev.title === 'Shell command';
  const REFRESH_KINDS = new Set(['ask', 'ask_done', 'result', 'agent_end', 'litrev_done', 'limit']);
  const OUTCOME = {
    ok: 'Finished', max_turns: 'Turn limit', usage_limit: 'Usage limit', transient: 'Temporary Claude error',
    auth: 'Not logged in', error: 'Error', startup_failed: 'Did not start', interrupted: 'Interrupted',
    stopped: 'Stopped by you', watchdog: 'Stopped: no progress',
  };
  const DONE_TOAST = { run: 'Queued to run', resume: 'Resuming now', retry: 'Retrying the step', stop: 'Stopping' };
  let current = null;                       // the open review page: {slug, feed, review}

  // ---- activity feed: one row per tool call; start/progress/end merge by tool_use_id ----------------------------
  class Feed {
    constructor() { this.items = new Map(); this.order = []; this.lastSeq = 0; }

    add(ev) {
      if (ev.seq && ev.seq <= this.lastSeq) return false;
      if (NOISE(ev)) { if (ev.seq) this.lastSeq = ev.seq; return false; }
      if (ev.seq) this.lastSeq = ev.seq;
      const key = ev.tool_use_id ? `t:${ev.tool_use_id}` : `s:${ev.seq}`;
      const old = this.items.get(key);
      if (!old) {
        this.items.set(key, { ...ev, first: ev.ts });
        this.order.push(key);
        while (this.order.length > FEED_MAX) this.items.delete(this.order.shift());
        return true;
      }
      if (ev.kind === 'agent_progress') { if (ev.detail) old.detail = ev.detail; return true; }   // what it is doing now
      old.kind = ev.kind;
      old.status = ev.status;
      if (ev.kind === 'agent_end') { if (ev.title) old.detail = ev.title; return true; }
      if (ev.kind !== 'litrev_done' && ev.title) old.title = ev.title;
      if (ev.detail) old.detail = ev.detail;
      return true;
    }

    list() { return this.order.map((k) => this.items.get(k)).filter(Boolean); }
  }

  function feedRow(it, live) {
    const running = live && it.status === 'running';
    const dot = it.status === 'failed' ? 'bad' : running ? 'running' : it.kind === 'limit' ? 'warn' : 'ok';
    const cls = [it.subagent ? 'sub' : '', it.status === 'failed' ? 'failed' : '', QUIET.has(it.kind) ? 'quiet' : '']
      .filter(Boolean).join(' ');
    return h('li', { class: cls }, h('span', { class: `dot ${dot}` }), h('time', null, fmt.clock(it.first)),
      h('span', { class: 'what' }, it.title, it.detail ? h('span', { class: 'detail' }, it.detail) : null));
  }

  // ---- header, buttons, banners -----------------------------------------------------------------------------
  const hasOpenStep = (r) => r.steps.some((s) => s.state === 'todo' || s.state === 'stale');

  async function publish(r, refresh) {
    try {
      const res = await LR.api('POST', `/api/reviews/${encodeURIComponent(r.slug)}/publish`);
      LR.toast((res && res.message) || 'Publishing: approve it in the Claude Code window that opened');
      if (typeof LR.features.publish === 'function') LR.features.publish(r, res);
    } catch (e) {
      LR.toast(e.message, 'bad');
    }
    refresh();
  }

  function buttons(r, refresh) {
    const st = r.state;
    const warn = r.outside && r.outside.message ? `${r.outside.message}\n\nRun anyway?` : null;
    const act = (label, action, cls, confirmText) => h('button', {
      class: `btn ${cls}`, type: 'button',
      onclick: async (e) => {
        if (confirmText && !window.confirm(confirmText)) return;
        e.currentTarget.disabled = true;
        try {
          await LR.api('POST', `/api/reviews/${encodeURIComponent(r.slug)}/${action}`);
          LR.toast(DONE_TOAST[action]);
        } catch (ex) {
          LR.toast(ex.message, 'bad');
        }
        refresh();
      },
    }, label);
    const out = [];
    if (st.phase === 'idle' || (st.phase === 'done' && hasOpenStep(r))) {
      out.push(act(r.made_in_claude_code ? 'Run here' : 'Run', 'run', 'primary', warn));
    }
    if (st.phase === 'paused') out.push(act('Resume now', 'resume', 'primary', warn));
    if (st.phase === 'failed') out.push(act('Retry', 'retry', 'primary', warn));
    if (st.busy || ['running', 'paused', 'waiting_user'].includes(st.phase)) {
      out.push(act('Stop', 'stop', 'danger', st.busy
        ? 'Stop the running session? Its work so far stays on disk, and Run continues from there.' : null));
    }
    const v = r.video || {};
    if (!v.wanted && !st.busy && ['done', 'idle', 'failed'].includes(st.phase)) {
      out.push(h('button', {
        class: 'btn', type: 'button',
        onclick: async (e) => {
          if (!window.confirm(`Make a narrated 20-minute video of this review (step 8)? ${v.cost || ''}.`)) return;
          e.currentTarget.disabled = true;
          try {
            await LR.api('POST', `/api/reviews/${encodeURIComponent(r.slug)}/video`, { minutes: 30 });
            LR.toast('Making the video: step 8 is queued');
          } catch (ex) {
            LR.toast(ex.message, 'bad');
          }
          refresh();
        },
      }, 'Make video'));
    }
    if (LR.features.publish && r.hub) {
      out.push(h('button', { class: 'btn', type: 'button', onclick: () => LR.features.publish(r.slug) }, 'Publish'));   // publish.js POSTs and shows the waiting state
    }
    return out;
  }

  function header(r, refresh) {
    const st = r.state;
    const ph = fmt.phase(st, { asking: !!r.ask, madeInClaudeCode: r.made_in_claude_code });
    const facts = [];
    if (r.elapsed_s) facts.push(`Ran for ${fmt.duration(r.elapsed_s)}`);
    facts.push(`${fmt.money(r.cost.spent_usd)} so far (counted when each session ends); about $${r.cost.estimate_usd} for the whole review ` +
      `(${r.cost.label}, ${r.cost.formula})`);
    if (st.phase === 'paused' && st.message) facts.push(st.message);
    return h('div', { class: 'rhead' },
      h('h1', null, r.seed),
      r.purpose ? h('p', { class: 'purpose' }, r.purpose) : null,
      h('p', { class: 'sentence', role: 'status' }, h('span', { class: `dot ${ph.dot}` }), ph.text),
      h('div', { class: 'facts' }, facts.map((f) => h('span', null, f)),
        r.made_in_claude_code ? h('span', { class: 'tag' }, 'Made in Claude Code') : null),
      h('div', { class: 'actions' }, buttons(r, refresh)));
  }

  function banners(r) {
    const out = [];
    if (r.outside && r.outside.message) out.push(h('div', { class: 'banner', role: 'note' }, h('p', null, r.outside.message)));
    if (r.state.phase === 'failed' && r.state.message) {
      out.push(h('div', { class: 'banner bad', role: 'alert' }, h('p', null, r.state.message),
        h('p', { class: 'hint' }, 'Retry runs the step again; its work so far stays on disk.')));
    }
    if (r.status_error) {
      out.push(h('div', { class: 'banner bad' }, h('p', null, `Couldn't read where the review stands: ${r.status_error}`)));
    }
    if (r.ask && r.ask.status === 'parked') {
      out.push(h('p', { class: 'parked' }, 'Claude stopped waiting and saved its work. Your answer starts the next session.'));
    }
    return out;
  }

  // ---- tab: progress ------------------------------------------------------------------------------------------
  const cap1 = (s) => (s ? s[0].toUpperCase() + s.slice(1) : s);

  function rowNote(s) {
    if (s.state === 'stale') return `Needs updating: ${s.rows.filter((x) => x.state === 'stale').map((x) => x.key).join(', ')}`;
    if (s.state === 'warn') return s.rows.filter((x) => x.state === 'warn').map((x) => x.detail).join('; ').slice(0, 140);
    if (s.state === 'done') return s.rows.map((x) => x.detail).filter(Boolean).join('; ').slice(0, 90);
    return '';
  }

  function bar(p) {
    const fill = h('span');
    fill.style.width = `${p.total ? Math.round((100 * p.done) / p.total) : 0}%`;   // CSSOM, allowed by the CSP
    return h('div', null,
      h('div', { class: 'bar', role: 'progressbar', 'aria-valuemin': 0, 'aria-valuemax': p.total, 'aria-valuenow': p.done }, fill),
      h('span', { class: 'step-note' }, `${p.done} of ${p.total} papers read`));
  }

  function rail(r) {
    const st = r.state;
    const asking = !!r.ask;
    return h('ol', { class: 'rail', 'aria-label': 'Steps' }, r.steps.map((s) => {
      let cls = { done: 'done', stale: 'stale', warn: 'warn' }[s.state] || '';
      const here = !r.made_in_claude_code && s.step === st.step && !['done', 'idle'].includes(st.phase);
      if (here) {
        cls = asking || st.phase === 'waiting_user' ? 'asking'
          : { failed: 'failed', paused: 'paused' }[st.phase] || 'current';
      }
      const note = here ? fmt.phase(st, { asking }).text : rowNote(s);
      return h('li', { class: cls, 'aria-label': `Step ${s.step}, ${s.title}. ${note || s.state}` },
        h('span', { class: 'num', 'aria-hidden': 'true' }, s.state === 'done' && !here ? '✓' : String(s.step)),
        h('span', { class: 'step-title' }, cap1(s.title)),
        note ? h('span', { class: 'step-note' }, note) : null,
        s.step === 5 && r.step5 ? h('span', { class: 'step-note' }, bar(r.step5)) : null);
    }));
  }

  function lastNotes(r) {
    const s = [...r.sessions].reverse().find((x) => x.report && (x.report.notes_for_user || []).length);
    if (!s) return null;
    return h('div', { class: 'now' }, h('h3', null, "Notes from Claude's last report"),
      h('ul', null, s.report.notes_for_user.map((n) => h('li', null, n))));
  }

  const refs = new WeakMap();

  function paintFeed(root) {
    const ref = refs.get(root);
    if (!ref || !current || !current.review) return;
    const live = !!current.review.state.busy;
    const items = current.feed.list();
    const subs = live ? items.filter((it) => it.status === 'running' && ['agent_start', 'agent_progress'].includes(it.kind)) : [];
    const cmd = live ? items.filter((it) => it.status === 'running' && it.kind === 'litrev').slice(-1)[0] : null;
    ref.now.replaceChildren(subs.length || cmd ? h('div', { class: 'now' },
      h('h3', null, subs.length ? `${subs.length} subagent${subs.length > 1 ? 's' : ''} working` : 'Working on'),
      h('ul', null, cmd ? h('li', null, cmd.title, cmd.detail ? `: ${cmd.detail}` : '') : null,
        subs.map((s) => h('li', null, s.title, s.detail ? h('span', { class: 'detail' }, ` · ${s.detail}`) : null)))) : '');
    ref.list.replaceChildren(...items.slice().reverse().map((it) => feedRow(it, live)));
    if (!items.length) ref.list.append(h('li', { class: 'quiet' }, h('span'), h('span'), h('span', { class: 'what' }, 'Nothing has run here yet.')));
  }

  function renderProgress(root, r) {
    const now = h('div');
    const list = h('ol', { class: 'feed', 'aria-label': 'What Claude did, newest first' });
    const step5 = r.step5 && r.state.step === 5 && r.state.phase !== 'done'
      ? h('div', { class: 'now' }, h('h3', null, 'Method details (step 5)'), bar(r.step5)) : null;
    root.replaceChildren(h('div', { class: 'progress' }, rail(r),
      h('div', null, lastNotes(r), step5, now, h('h2', { class: 'label' }, 'Activity'), list)));
    refs.set(root, { now, list });
    paintFeed(root);
  }

  // ---- tab: sessions ------------------------------------------------------------------------------------------
  function renderSessions(root, r) {
    const rows = [...r.sessions].reverse();
    if (!rows.length) {
      root.replaceChildren(h('p', { class: 'hint' }, 'No sessions yet. Each step runs as one or more Claude Code sessions; they are listed here.'));
      return;
    }
    root.replaceChildren(h('div', { class: 'table-wrap' }, h('table', { class: 'data' },
      h('thead', null, h('tr', null, ['Ended', 'Step', 'How it ended', 'Took', 'Cost (API-equivalent)', 'Turns', 'Summary']
        .map((t) => h('th', { scope: 'col' }, t)))),
      h('tbody', null, rows.map((s) => h('tr', null,
        h('td', null, fmt.day(s.end)),
        h('td', null, String(s.step)),
        h('td', null, OUTCOME[s.outcome] || s.outcome || '', s.denials ? h('span', { class: 'detail' }, `${s.denials} command(s) blocked`) : null),
        h('td', null, s.start && s.end ? fmt.duration(s.end - s.start) : ''),
        h('td', null, s.cost_usd !== null && s.cost_usd !== undefined ? fmt.money(s.cost_usd) : ''),
        h('td', null, s.turns !== null && s.turns !== undefined ? String(s.turns) : ''),
        h('td', null, (s.report && s.report.summary) || s.note || s.result || '',
          s.report && (s.report.notes_for_user || []).length
            ? h('ul', null, s.report.notes_for_user.map((n) => h('li', null, n))) : null)))))));
  }

  // ---- tab: decisions (Claude makes every call but the scope itself, so the person sees and reverses them here) ----
  const KIND = { scope: 'Scope', screen: 'Screening', cap: 'Paper cap', kpi: 'KPI boards', protocol: 'Protocol', other: 'Other' };

  function renderDecisions(root, r) {
    const rows = r.decisions || [];
    const mine = rows.filter((d) => d.by === 'agent').length;
    const intro = h('p', { class: 'hint' }, 'Claude makes these calls itself, so the review does not stop to ask you. ',
      'To change one, ask in the ', h('a', { href: `#/r/${encodeURIComponent(r.slug)}/chat` }, 'Ask Claude'),
      ' tab, for example \u201cundo DEC041\u201d.');
    if (!rows.length) {
      root.replaceChildren(intro, h('p', { class: 'hint' }, 'No decisions yet.'));
      return;
    }
    root.replaceChildren(intro, h('p', { class: 'hint' }, `${mine} of ${rows.length} made by Claude, newest first.`),
      h('div', { class: 'table-wrap' }, h('table', { class: 'data' },
        h('thead', null, h('tr', null, ['ID', 'Date', 'By', 'About', 'Decision'].map((t) => h('th', { scope: 'col' }, t)))),
        h('tbody', null, rows.map((d) => h('tr', null,
          h('td', { class: 'nowrap' }, d.id || ''),
          h('td', { class: 'nowrap' }, d.date || ''),
          h('td', null, d.by === 'agent' ? h('span', { class: 'tag marker' }, 'Claude') : 'You'),
          h('td', null, KIND[d.kind] || d.kind || ''),
          h('td', null, d.decision || '', d.reason ? h('span', { class: 'detail' }, d.reason) : null)))))));
  }

  // ---- tab: hub -----------------------------------------------------------------------------------------------
  function renderHub(root, r) {
    if (!r.hub) {
      root.replaceChildren(h('p', { class: 'hint' }, 'The hub is made in step 7. It shows up here once the review gets there.'));
      return;
    }
    const src = `/r/${encodeURIComponent(r.slug)}/hub`;
    root.replaceChildren(
      h('p', { class: 'hint' }, 'This is the review as your teammates will see it once it is published. ',
        h('a', { href: src, target: '_blank', rel: 'noopener' }, 'Open it in its own tab')),
      h('iframe', { class: 'hubframe', src, title: `Hub: ${r.seed}`, sandbox: 'allow-scripts allow-popups allow-downloads', referrerpolicy: 'no-referrer' }));
  }

  // ---- tab: video (step 8) --------------------------------------------------------------------------------------
  function renderVideo(root, r) {
    const v = r.video || {};
    if (!v.exists) {
      root.replaceChildren(h('p', { class: 'hint' }, v.wanted
        ? `The ${v.minutes || 20}-minute video is made in step 8, after the review itself. It shows up here when it is ready.`
        : 'No video yet. Press Make video to add a narrated video of this review (step 8).'));
      return;
    }
    const src = `/r/${encodeURIComponent(r.slug)}/video`;
    const checks = Object.entries(v.checks || {});
    const bad = checks.filter(([, ok]) => !ok).map(([k]) => k.replace(/_/g, ' '));
    const player = h('video', { class: 'videoframe', controls: true, preload: 'metadata', src });
    if (v.captions) player.append(h('track', { kind: 'captions', srclang: 'en', label: 'English', src: `${src}.vtt`, default: true }));
    root.replaceChildren(
      h('p', { class: 'hint' }, `${fmt.duration(Math.round(v.duration_s || 0))}, ${v.size_mb || '?'} MB. `,
        bad.length ? `Checks that failed: ${bad.join(', ')}.` : `All ${checks.length} checks passed: the slides follow the voice.`, ' ',
        h('a', { href: src, target: '_blank', rel: 'noopener' }, 'Open it in its own tab'),
        v.slides ? ' · ' : '', v.slides ? h('a', { href: `${src}.pptx`, download: '' }, 'Download the slides (PowerPoint)') : ''),
      player);
  }

  LR.reviewTabs.push({ id: 'progress', title: 'Progress', render: renderProgress, update: renderProgress });
  LR.reviewTabs.push({ id: 'decisions', title: 'Decisions', render: renderDecisions, update: renderDecisions });
  LR.reviewTabs.push({ id: 'sessions', title: 'Sessions', render: renderSessions, update: renderSessions });
  LR.reviewTabs.push({ id: 'hub', title: 'Hub', render: renderHub });
  LR.reviewTabs.push({ id: 'video', title: 'Video', render: renderVideo, update: renderVideo });

  // ---- the page -----------------------------------------------------------------------------------------------
  LR.pages.r = function reviewPage(root, params) {
    const slug = params[0];
    if (!slug) { location.hash = '#/reviews'; return; }
    const tabId = params[1] || 'progress';
    const ctx = (current = { slug, feed: new Feed(), review: null, tab: null, askId: undefined, sse: null });
    const parts = { head: h('div'), banners: h('div'), ask: h('div'), tabs: h('nav', { class: 'tabs', 'aria-label': 'Review sections' }), panel: h('div') };
    root.append(parts.head, parts.banners, parts.ask, parts.tabs, parts.panel);
    let timer = null;
    let loading = false;
    let again = false;
    const schedule = (ms) => { clearTimeout(timer); timer = setTimeout(load, ms); };
    const refresh = () => schedule(0);

    function paint() {
      const r = ctx.review;
      parts.head.replaceChildren(header(r, refresh));
      parts.banners.replaceChildren(...banners(r));
      const askId = r.ask ? r.ask.id : null;
      if (askId !== ctx.askId) {                  // rebuilt only for a new question, so typing isn't lost on refresh
        ctx.askId = askId;
        parts.ask.replaceChildren(r.ask ? LR.askCard(r.ask, r, refresh) : '');
      }
      const active = LR.reviewTabs.find((t) => t.id === tabId) || LR.reviewTabs[0];
      parts.tabs.replaceChildren(...LR.reviewTabs.map((t) => h('a', {
        href: `#/r/${encodeURIComponent(slug)}/${t.id}`, 'aria-current': t === active ? 'page' : null,
      }, t.title)));
      if (ctx.tab !== active) {
        ctx.tab = active;
        parts.panel.replaceChildren();
        active.render(parts.panel, r);
      } else if (active.update) {
        active.update(parts.panel, r);
      }
    }

    function onEvent(ev) {
      if (ev.type === 'activity') {
        if (ctx.feed.add(ev) && ctx.tab && ctx.tab.id === 'progress') paintFeed(parts.panel);
        if (REFRESH_KINDS.has(ev.kind)) schedule(1200);
      } else if (ev.type === 'state') {
        schedule(250);
      }
    }

    async function load() {
      if (loading) { again = true; return; }
      loading = true;
      try {
        const r = await LR.api('GET', `/api/reviews/${encodeURIComponent(slug)}`);
        for (const a of r.activities) ctx.feed.add(a);
        ctx.review = r;
        paint();
        if (!ctx.sse) ctx.sse = LR.sse(slug, onEvent, { after: ctx.feed.lastSeq });   // after the backlog, not before
      } catch (e) {
        if (!ctx.review) root.replaceChildren(LR.errorBox(e));
        else LR.toast(e.message, 'bad');
      } finally {
        loading = false;
        if (again) { again = false; schedule(300); }
      }
    }

    const clock = setInterval(() => { if (ctx.review) parts.head.replaceChildren(header(ctx.review, refresh)); }, 30000);
    // while idle no events arrive, so re-check (outside sessions, paused-until) every 10 s when the tab is visible
    const poll = setInterval(() => { if (!document.hidden && ctx.review && !ctx.review.state.busy) load(); }, 10000);
    const offVisible = LR.on('visible', () => load());
    LR.onLeave(() => { clearTimeout(timer); clearInterval(clock); clearInterval(poll); offVisible(); if (current === ctx) current = null; });
    load();
  };
})();
