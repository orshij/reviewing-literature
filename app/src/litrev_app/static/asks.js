/* Litrev App: the question card and one form per question kind (app-mode.md). Each form starts from Claude's
   proposal, lets the person change it, and posts the answer shape the engine records (M1a checkpoints).
   LR.askShape[kind](proposal) is the answer that accepts the proposal unchanged; a test feeds it to the engine.
   Nothing here touches the document at load time, so the shapes also load in node. */
(function () {
  'use strict';
  const LR = window.LR;

  function kpiRow(k) {
    const row = { metric: k.metric, metric_variant: k.metric_variant || null, splits: [...(k.splits || [])],
      rationale: k.rationale || '' };
    if (k.benchmark) row.benchmark = k.benchmark;
    else row.dataset = k.dataset;
    return row;
  }

  const shape = (LR.askShape = {
    scope: (p) => ({ in: [...(p.in || [])], out: [...(p.out || [])], protocol: [...(p.protocol || [])], notes: '' }),
    boundaries: (p) => ({ boundaries: (p.boundaries || []).map((b) => ({
      area: b.area, aliases: [...(b.aliases || [])], decision: b.recommend === 'in' ? 'in' : 'out', reason: b.reason || '' })) }),
    included_off_topic: (p) => ({ papers: (p.papers || []).map((x) => ({
      id: x.id, decision: x.recommend === 'keep' ? 'keep' : 'drop', reason: x.why || '' })) }),
    cap: (p) => ({ proposed: Number(p.proposed), reason: p.reason || '' }),
    kpi: (p) => ({ kpis: (p.kpis || []).map(kpiRow), remove: [], reason: '' }),
    free: (p) => ({ text: (p.options || [])[0] || '' }),
  });

  // ---- shared pieces --------------------------------------------------------------------------------------------
  let uid = 0;

  function listEditor(items, placeholder) {
    const { h } = LR;
    const box = h('div', { class: 'listed' });
    const addBtn = h('button', { class: 'linkish', type: 'button' }, 'Add another');
    const add = (v) => {
      const input = h('input', { type: 'text', value: v || '', placeholder });
      const row = h('div', { class: 'row' }, input,
        h('button', { class: 'btn quiet small', type: 'button', onclick: () => row.remove() }, 'Remove'));
      box.insertBefore(row, addBtn);
      return input;
    };
    addBtn.addEventListener('click', () => add('').focus());
    box.append(addBtn);
    (items || []).forEach((v) => add(v));
    return { el: box, value: () => [...box.querySelectorAll('input')].map((i) => i.value.trim()).filter(Boolean) };
  }

  function choice(options, value, label) {
    const { h } = LR;
    const name = `choice-${++uid}`;
    const el = h('div', { class: 'choice', role: 'radiogroup', 'aria-label': label });
    for (const [v, text] of options) {
      el.append(h('label', null, h('input', { type: 'radio', name, value: v, checked: v === value }), text));
    }
    return { el, value: () => { const c = el.querySelector('input:checked'); return c ? c.value : null; } };
  }

  function group(legend, el, hint) {
    const { h } = LR;
    return h('fieldset', null, h('legend', null, legend), hint ? h('p', { class: 'hint' }, hint) : null, el);
  }

  function labelled(text, input) {
    const { h } = LR;
    return h('label', { class: 'field' }, h('span', { class: 'label' }, text), input);
  }

  function actions(button) {
    return LR.h('div', { class: 'ask-actions' }, button);
  }

  // ---- the card ---------------------------------------------------------------------------------------------------
  LR.askCard = function askCard(ask, review, onAnswered) {
    const { h } = LR;
    const err = h('div', { class: 'err', role: 'alert' });
    const body = h('div');
    const card = h('section', { class: 'ask', 'aria-labelledby': 'ask-q' },
      h('span', { class: 'ask-head' }, 'Claude needs your answer'),
      h('h2', { class: 'ask-q', id: 'ask-q' }, ask.question || 'Claude has a question'),
      ask.context ? h('div', { class: 'prose' }, LR.md(ask.context)) : null,
      body, err);
    const form = LR.askForms[ask.kind];
    if (!form) {
      body.append(h('p', null, `This version of Litrev can't show a "${ask.kind}" question. Update Litrev, or ` +
        'answer it from a Claude Code session in this review.'));
      return card;
    }
    async function submit(answer, button) {
      err.replaceChildren();
      if (button) button.disabled = true;
      try {
        const res = await LR.api('POST', `/api/reviews/${encodeURIComponent(review.slug)}/answer`, answer);
        LR.toast(`Answer recorded${res.summary ? `: ${res.summary}` : ''}`);
        onAnswered();
      } catch (e) {
        err.append(h('p', null, e.message));
        const failed = ((e.data && e.data.outputs) || []).filter((o) => o.exit !== 0).slice(-1)[0];
        if (failed) err.append(h('p', { class: 'hint' }, 'litrev said: ', h('code', null, (failed.stderr || failed.stdout || '').trim().slice(-400))));
        if (button) button.disabled = false;
      }
    }
    form(body, ask, review, submit);
    return card;
  };

  // ---- scope ------------------------------------------------------------------------------------------------------
  LR.askForms.scope = function scopeForm(root, ask, review, submit) {
    const { h } = LR;
    const p = ask.proposal || {};
    const lists = {
      in: listEditor(p.in, 'An area the review covers'),
      out: listEditor(p.out, 'An area it leaves out'),
      protocol: listEditor(p.protocol, 'A rule for which results count, e.g. zero-shot only'),
    };
    const notes = h('textarea', { id: 'scope-notes', rows: 2, placeholder: 'Optional' });
    const btn = h('button', { class: 'btn primary', type: 'button' }, 'Record this scope');
    btn.addEventListener('click', () => {
      const a = { in: lists.in.value(), out: lists.out.value(), protocol: lists.protocol.value(), notes: notes.value.trim() };
      if (!a.in.length) { LR.toast('Keep at least one area in scope', 'bad'); return; }
      const base = shape.scope(p);
      const edited = JSON.stringify([a.in, a.out, a.protocol]) !== JSON.stringify([base.in, base.out, base.protocol]);
      if (!a.notes && edited) a.notes = "Edited Claude's proposal (Litrev App)";
      submit(a, btn);
    });
    root.append(
      p.rationale ? h('div', { class: 'prose' }, h('p', null, h('strong', null, 'Why Claude proposes this')), LR.md(p.rationale)) : null,
      group('In scope', lists.in.el, 'Papers about these areas are included.'),
      group('Out of scope', lists.out.el, 'Papers only about these are left out.'),
      group('Evaluation protocol', lists.protocol.el, 'Rules for which results count. Leave empty if there are none.'),
      LR.field('scope-notes', 'Anything Claude should know', notes),
      actions(btn));
  };

  // ---- boundaries -------------------------------------------------------------------------------------------------
  LR.askForms.boundaries = function boundariesForm(root, ask, review, submit) {
    const { h } = LR;
    const rows = ((ask.proposal || {}).boundaries || []).map((b) => {
      const pick = choice([['in', 'In scope'], ['out', 'Out of scope']], b.recommend === 'in' ? 'in' : 'out', b.area);
      const reason = h('input', { type: 'text', value: b.reason || '' });
      const n = (b.papers || []).length;
      const el = h('div', { class: 'item' },
        h('h4', null, b.area),
        (b.aliases || []).length ? h('p', { class: 'hint' }, `Also called: ${b.aliases.join(', ')}`) : null,
        h('p', { class: 'recommend' }, `${n} paper${n === 1 ? '' : 's'}. Claude suggests ` +
          `${b.recommend === 'in' ? 'including' : 'leaving out'} this area${b.reason ? `: ${b.reason}` : '.'}`),
        (b.examples || []).length ? h('ul', { class: 'examples' }, b.examples.slice(0, 5).map((t) => h('li', null, t))) : null,
        pick.el,
        labelled('Reason (kept with the decision)', reason));
      return { el, value: () => ({ area: b.area, aliases: [...(b.aliases || [])], decision: pick.value(), reason: reason.value.trim() }) };
    });
    const btn = h('button', { class: 'btn primary', type: 'button' }, 'Record these decisions');
    btn.addEventListener('click', () => submit({ boundaries: rows.map((r) => r.value()) }, btn));
    root.append(...rows.map((r) => r.el), actions(btn));
  };

  // ---- included but off topic -------------------------------------------------------------------------------------
  LR.askForms.included_off_topic = function offTopicForm(root, ask, review, submit) {
    const { h } = LR;
    const rows = ((ask.proposal || {}).papers || []).map((x) => {
      const keep = x.recommend === 'keep';
      const pick = choice([['keep', 'Keep it'], ['drop', 'Drop it']], keep ? 'keep' : 'drop', `${x.id} ${x.title || ''}`);
      const reason = h('input', { type: 'text', value: x.why || '' });
      const el = h('div', { class: 'item' },
        h('h4', null, `${x.id}: ${x.title || ''}`),
        x.screen_reason ? h('p', { class: 'hint' }, `Screened as: ${x.screen_reason}`) : null,
        h('p', { class: 'recommend' }, `Claude suggests ${keep ? 'keeping' : 'dropping'} it${x.why ? `: ${x.why}` : '.'}`),
        pick.el,
        labelled('Reason (kept with the decision)', reason));
      return { el, value: () => ({ id: x.id, decision: pick.value(), reason: reason.value.trim() }) };
    });
    const btn = h('button', { class: 'btn primary', type: 'button' }, 'Record these decisions');
    btn.addEventListener('click', () => submit({ papers: rows.map((r) => r.value()) }, btn));
    root.append(...rows.map((r) => r.el), actions(btn));
  };

  // ---- cap --------------------------------------------------------------------------------------------------------
  LR.askForms.cap = function capForm(root, ask, review, submit) {
    const { h } = LR;
    const p = ask.proposal || {};
    const n = h('input', { id: 'cap-n', type: 'number', min: 5, max: 300, step: 1, value: p.proposed });
    const est = h('p', { class: 'hint' });
    const show = () => { est.textContent = `About $${LR.fmt.estimate(n.value)} API-equivalent for the whole review.`; };
    n.addEventListener('input', show);
    show();
    const reason = h('textarea', { id: 'cap-reason', rows: 2, value: p.reason || '' });
    const btn = h('button', { class: 'btn primary', type: 'button' }, 'Set the cap');
    btn.addEventListener('click', () => {
      const v = Number(n.value);
      if (!Number.isInteger(v) || v < 5 || v > 300) { LR.toast('The cap is a whole number from 5 to 300', 'bad'); return; }
      submit({ proposed: v, reason: reason.value.trim() }, btn);
    });
    root.append(
      h('p', { class: 'recommend' }, `The cap is ${p.current} papers now; Claude proposes ${p.proposed}.`),
      LR.field('cap-n', 'Papers to include, at most', n, est),
      LR.field('cap-reason', 'Reason (kept with the decision)', reason),
      actions(btn));
  };

  // ---- KPIs -------------------------------------------------------------------------------------------------------
  LR.askForms.kpi = function kpiForm(root, ask, review, submit) {
    const { h } = LR;
    const p = ask.proposal || {};
    const names = {};
    const rows = [];
    const rowsBox = h('div');

    function addRow(k, papers) {
      const target = h('input', { type: 'text', value: k.benchmark || k.dataset || '' });
      const metric = h('input', { type: 'text', value: k.metric || '' });
      const variant = h('input', { type: 'text', value: k.metric_variant || '', placeholder: 'none' });
      const splits = listEditor(k.splits || [], 'A split label exactly as litrev lists it, or none');
      const rationale = h('textarea', { rows: 2, value: k.rationale || '' });
      const label = h('p', { class: 'recommend wide' });
      const row = {};
      row.showNames = () => {
        const named = [names[target.value.trim()], names[metric.value.trim()]].filter(Boolean).join(', ');
        label.textContent = `${named || 'KPI'}${papers ? ` (${papers} papers)` : ''}`;
      };
      target.addEventListener('input', row.showNames);
      metric.addEventListener('input', row.showNames);
      row.value = () => {
        const t = target.value.trim();
        const out = { metric: metric.value.trim(), metric_variant: variant.value.trim() || null, splits: splits.value(),
          rationale: rationale.value.trim() };
        if (/^D\d+$/.test(t)) out.dataset = t;
        else out.benchmark = t;
        return out;
      };
      row.el = h('div', { class: 'kpi-row' }, label,
        labelled('Benchmark (B…) or dataset (D…)', target),
        labelled('Metric (M…)', metric),
        labelled('Metric variant', variant),
        h('div', { class: 'wide' }, h('span', { class: 'label' }, 'Splits counted as one'), splits.el),
        h('div', { class: 'wide' }, labelled('Why this board', rationale)),
        h('div', { class: 'wide' }, h('button', {
          class: 'btn quiet small', type: 'button',
          onclick: () => { rows.splice(rows.indexOf(row), 1); row.el.remove(); },
        }, 'Remove this KPI')));
      rows.push(row);
      rowsBox.append(row.el);
      row.showNames();
    }
    (p.kpis || []).forEach((k) => addRow(k, k.papers));

    const existing = ((review.meta && review.meta.kpis) || []).map((k) => ({ k, box: h('input', { type: 'checkbox' }) }));
    const picker = h('div');
    const pickBtn = h('button', { class: 'btn', type: 'button' }, 'Show the boards litrev found');

    function board(b) {
      const checks = (b.splits || []).map((s) => ({ split: s.split === null ? 'none' : String(s.split), papers: s.papers, box: h('input', { type: 'checkbox' }) }));
      const variant = h('select', null, h('option', { value: '' }, 'No variant'),
        (b.metric_variants || []).map((v) => h('option', { value: v }, v)));
      const why = h('input', { type: 'text', placeholder: 'Why this board, and why these splits count as one' });
      const dir = b.higher_is_better === false ? 'lower is better' : b.higher_is_better ? 'higher is better' : 'direction not set';
      const add = h('button', { class: 'btn small', type: 'button' }, 'Add this KPI');
      add.addEventListener('click', () => {
        const chosen = checks.filter((c) => c.box.checked).map((c) => c.split);
        if (!chosen.length) { LR.toast('Pick at least one split', 'bad'); return; }
        names[b.target] = b.target_name || b.target;
        names[b.metric_id] = b.metric_name || b.metric_id;
        addRow({ [String(b.target).startsWith('D') ? 'dataset' : 'benchmark']: b.target, metric: b.metric_id,
          metric_variant: variant.value || null, splits: chosen, rationale: why.value }, b.papers);
        LR.toast('Added to the KPI list above');
      });
      return h('details', { class: 'board' },
        h('summary', null, `${b.target_name || b.target}, ${b.metric_name || b.metric_id}: ${b.papers} papers`),
        h('p', { class: 'hint' }, `${b.target} ${b.metric_id}, ${dir}`),
        h('div', { class: 'splits' }, checks.map((c) => h('label', null, c.box, ` ${c.split} (${c.papers})`))),
        labelled('Metric variant', variant),
        labelled('Why', why),
        add);
    }

    pickBtn.addEventListener('click', async () => {
      pickBtn.disabled = true;
      picker.replaceChildren(h('p', { class: 'hint' }, 'Asking litrev for the boards…'));
      try {
        const res = await LR.api('GET', `/api/reviews/${encodeURIComponent(review.slug)}/kpi-suggest`);
        Object.assign(names, res.names || {});
        rows.forEach((r) => r.showNames());
        picker.replaceChildren(...(res.boards.length ? res.boards.slice(0, 40).map(board)
          : [h('p', { class: 'hint' }, 'litrev found no boards with results yet.')]));
      } catch (e) {
        picker.replaceChildren(LR.errorBox(e));
        pickBtn.disabled = false;
      }
    });

    const reason = h('textarea', { id: 'kpi-reason', rows: 2, placeholder: 'Optional' });
    const btn = h('button', { class: 'btn primary', type: 'button' }, 'Record these KPIs');
    btn.addEventListener('click', () => {
      const kpis = rows.map((r) => r.value());
      if (!kpis.length) { LR.toast('Keep at least one KPI', 'bad'); return; }
      const bad = kpis.find((k) => !/^[BD]\d+$/.test(k.benchmark || k.dataset || '') || !/^M\d+$/.test(k.metric) || !k.splits.length);
      if (bad) { LR.toast('Each KPI needs a B… or D… id, an M… metric and at least one split', 'bad'); return; }
      submit({ kpis, remove: existing.filter((x) => x.box.checked).map((x) => x.k.id), reason: reason.value.trim() }, btn);
    });

    root.append(
      group('KPIs Claude proposes', rowsBox, 'Each KPI is one leaderboard: a benchmark or dataset, a metric, and the split labels that count as the same split.'),
      existing.length ? group('KPIs already in the review', h('div', { class: 'listed' }, existing.map((x) => h('label', null, x.box,
        ` Remove ${x.k.id}: ${x.k.benchmark_id || x.k.dataset_id} ${x.k.metric_id}${x.k.metric_variant ? ` ${x.k.metric_variant}` : ''} (${(x.k.splits || []).join(', ')})`)))) : null,
      group('Add a board', h('div', null, pickBtn, picker), 'Boards are ranked by how many included papers report them.'),
      LR.field('kpi-reason', 'Anything to note about this choice', reason),
      actions(btn));
  };

  // ---- free question ----------------------------------------------------------------------------------------------
  LR.askForms.free = function freeForm(root, ask, review, submit) {
    const { h } = LR;
    const options = (ask.proposal || {}).options || [];
    const pick = options.length ? choice(options.map((o) => [o, o]), null, ask.question || 'Options') : null;
    const text = h('textarea', { id: 'free-text', rows: 3, placeholder: options.length ? 'Optional' : '' });
    const btn = h('button', { class: 'btn primary', type: 'button' }, 'Send my answer');
    btn.addEventListener('click', () => {
      const chosen = pick ? pick.value() : null;
      const t = text.value.trim();
      const answer = chosen ? (t ? `${chosen}: ${t}` : chosen) : t;
      if (!answer) { LR.toast('Choose an option or write an answer', 'bad'); return; }
      submit({ text: answer }, btn);
    });
    root.append(pick ? group('Choose one', pick.el) : null,
      LR.field('free-text', options.length ? 'Anything to add' : 'Your answer', text), actions(btn));
  };
})();
