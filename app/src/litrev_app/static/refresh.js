// Litrev App M2: the review tab "New papers". Check a finished review for new papers (the dry run shows how many
// searches the check runs), let Claude recommend which to add, choose, and the app runs steps 4-7 for them.
// Titles, abstracts and Claude's notes are untrusted text: set as text only; links only when they are http(s).
(function () {
  "use strict";
  const LR = window.LR;
  let sub = null;
  let offVisible = null;     // reload when the browser tab is shown again: hidden tabs close their streams
  let timer = null;

  function h(tag, attrs, ...kids) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === null || v === undefined || v === false) continue;
      if (k === "class") el.className = v;
      else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
      else el.setAttribute(k, v === true ? "" : String(v));
    }
    for (const kid of kids.flat()) if (kid !== null && kid !== undefined && kid !== false) el.append(kid);
    return el;
  }

  function css() {
    if (document.getElementById("m2-css")) return;
    const l = document.createElement("link");
    l.id = "m2-css";
    l.rel = "stylesheet";
    l.href = "/static/m2.css";          // a file: the app's CSP (style-src 'self') blocks inline <style>
    document.head.append(l);
  }

  const errText = (e) => String((e && e.message) || e);
  const clock = (ts) => (ts ? new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "");
  const httpUrl = (u) => (typeof u === "string" && /^https?:\/\//i.test(u) ? u : null);

  async function render(root, review) {
    css();
    const slug = review.slug;
    const base = `/api/reviews/${slug}/refresh`;
    if (sub && typeof sub.close === "function") sub.close();
    if (offVisible) { offVisible(); offVisible = null; }
    clearInterval(timer);
    const box = h("div", { class: "m2-refresh" });
    const acts = h("ul", { class: "m2-acts-list" });
    root.replaceChildren(box);
    let s = null;

    async function load() {
      try { s = await LR.api("GET", base); } catch (e) { box.replaceChildren(h("p", {}, errText(e))); return; }
      box.replaceChildren(...view());
    }

    async function act(path, body) {
      try { await LR.api("POST", base + path, body || {}); } catch (e) { LR.toast(errText(e)); }
      await load();
    }

    function findButton() {
      return h("button", { class: "m2-primary", disabled: s.busy ? true : null, onclick: () => act("/plan") },
        "Find new papers");
    }

    function planView() {
      const p = s.plan || {};
      return h("div", {},
        h("p", {}, `This check replays ${p.requests} searches (${p.keyword} keyword searches, ${p.citing} citation walks) ` +
          "for papers newer than each one's last run. It takes a few minutes and uses the review's search-API quota. " +
          "Nothing is added until you approve it."),
        p.skipped ? h("p", { class: "m2-muted" }, `${p.skipped} searches are skipped; REFRESH.md lists why.`) : null,
        h("div", { class: "m2-row" }, h("button", { class: "m2-primary", disabled: s.busy ? true : null,
          onclick: () => act("/start") }, "Check now")));
    }

    let ran = false;          // the queued steps 4-7 have finished (or were stopped): the person can check again
    function queuedView() {
      const run = s.run || {};
      const done = (s.approved || []).join(", ");
      const parts = [h("p", {}, ran ? `Steps 4–7 ran for ${done || "the new papers"}.` : (s.message || "Steps 4–7 are queued.")),
        h("p", { class: "m2-muted" }, `Review: step ${run.step}, ${run.phase}${run.message ? ` (${run.message})` : ""}. ` +
          "Follow the steps on the review page.")];
      if (s.publish_ready) {
        parts.push(h("p", {}, "The updated hub is ready to publish."));
        if (LR.features.publish) {
          parts.push(h("button", { class: "m2-primary", onclick: () => LR.features.publish(slug) }, "Publish"));
        } else {
          parts.push(h("p", { class: "m2-muted" }, "Use Publish on the review page."));
        }
      }
      return h("div", {}, ...parts);
    }

    function formView() {
      const cands = s.candidates || [];
      const picked = new Set(cands.filter((c) => c.recommend === "include").map((c) => c.id));
      const capLine = h("p", {});
      const raise = h("input", { type: "checkbox", id: "m2-raise", checked: true });
      const raiseText = h("span", {});
      const raiseRow = h("label", { class: "m2-row", for: "m2-raise" }, raise, raiseText);
      const note = h("input", { type: "text", class: "m2-wide", placeholder: "Note for the decision log (optional)" });
      const submit = h("button", { class: "m2-primary" });

      function update() {
        const after = s.cap.used + picked.size;
        const over = after > s.cap.max;
        capLine.textContent = `Paper slots: ${s.cap.used} of ${s.cap.max} in use; with ${picked.size} selected: ${after}.`;
        raiseRow.hidden = !over;
        raiseText.textContent = ` Raise the cap to ${after} (recorded as your decision). Without it, the final validation fails.`;
        submit.textContent = picked.size ? `Add ${picked.size} paper(s) and run steps 4–7` : "Leave all of them out";
        submit.disabled = over && !raise.checked;
      }

      function row(c) {
        const box = h("input", { type: "checkbox", checked: picked.has(c.id) ? true : null, "aria-label": `Add ${c.id}` });
        box.addEventListener("change", () => { if (box.checked) picked.add(c.id); else picked.delete(c.id); update(); });
        const url = httpUrl(c.url);
        const rec = c.recommend ? `Claude: ${c.recommend}${c.why ? ` (${c.why})` : ""}` : "No recommendation from Claude";
        return h("li", { class: "m2-cand" },
          h("label", { class: "m2-row" }, box, h("strong", {}, c.id), h("span", {}, `${c.title || "(no title)"} (${c.year || "?"})`)),
          h("div", { class: "m2-muted" }, `Found by: ${(c.paths || []).join(", ") || "no recorded path"}`),
          h("div", { class: c.recommend === "include" ? "m2-rec m2-rec-in" : "m2-rec" }, rec),
          c.abstract ? h("details", {}, h("summary", {}, "Abstract"), h("p", {}, c.abstract)) : null,
          url ? h("a", { href: url, target: "_blank", rel: "noopener noreferrer" }, "Open the paper") : null);
      }

      raise.addEventListener("change", update);
      submit.addEventListener("click", async () => {
        submit.disabled = true;
        try {
          await LR.api("POST", base + "/answer", { approve: [...picked], raise_cap: raise.checked, note: note.value });
          LR.toast("Recorded");
        } catch (e) { LR.toast(errText(e)); }
        await load();
      });
      const q = s.question || {};
      const el = h("div", {},
        h("h4", {}, q.question || "Which of these new papers should join the review?"),
        q.context ? h("p", { class: "m2-muted" }, q.context) : null,
        capLine, raiseRow,
        h("ul", { class: "m2-cands" }, cands.map(row)),
        note, h("div", { class: "m2-row" }, submit));
      update();
      return el;
    }

    function view() {
      const out = [h("h3", {}, "New papers")];
      const stage = s.stage || "idle";
      ran = stage === "queued" && !["running", "waiting_user", "paused"].includes((s.run || {}).phase);
      if (s.busy && !["fetching", "screening", "queued"].includes(stage)) out.push(h("p", { class: "m2-note" }, `Not now: ${s.busy}.`));
      if (stage === "planned") out.push(planView());
      else if (stage === "fetching") out.push(h("p", {}, `${s.message || "Checking for new papers"}… started ${clock(s.started_at)}. ` +
        "This takes a few minutes; the page updates when it is done."));
      else if (stage === "screening") out.push(h("p", {}, s.message || "Claude is reading the new papers…"), acts);
      else if (stage === "queued") out.push(queuedView());
      else {
        if (s.message) out.push(h("p", {}, s.message));
        if (s.result) out.push(h("p", { class: "m2-muted" }, `Last check ${s.date || ""}: ${s.result.new} new candidate(s) from ` +
          `${s.result.replays} searches${s.result.failed ? `, ${s.result.failed} failed (retried next time)` : ""}.`));
      }
      if (s.question) out.push(formView());
      else if (["idle", "done", "failed"].includes(stage) || ran) {
        out.push(h("div", { class: "m2-row" }, findButton(),
          stage === "failed" && (s.new || []).length ? h("button", { onclick: () => act("/screen") }, "Retry screening") : null));
      }
      return out;
    }

    sub = LR.sse(slug, (ev) => {
      if (!ev || ev.type !== "refresh") return;
      if (ev.activity) {
        const a = ev.activity;
        acts.append(h("li", {}, `${a.title}${a.detail ? `: ${a.detail}` : ""}`));
        if (a.kind === "ask") load();
        return;
      }
      if (ev.state && !(s && s.question)) load();
    });
    timer = setInterval(() => {
      if (s && !s.question && ["fetching", "screening", "queued"].includes(s.stage)) load();
    }, 15000);
    offVisible = LR.on("visible", () => load());
    LR.onLeave(() => { if (offVisible) { offVisible(); offVisible = null; } });
    await load();
  }

  LR.reviewTabs.push({ id: "refresh", title: "New papers", render });
  // The question card on the review page: the refresh choice is made in the New papers tab, not in the card.
  LR.askForms.refresh = function refreshCard(root, ask, review) {
    root.append(LR.h("p", null, "Choose which of the new papers join the review in the New papers tab."),
      LR.h("a", { class: "btn primary", href: `#/r/${encodeURIComponent(review.slug)}/refresh` }, "Open New papers"));
  };
})();
