// Litrev App M2 toolbar: the Claude usage meter (GET /api/usage: the last rate_limit_event any app session saw),
// a warning before a new review when weekly usage is above 80%, and the Update button (POST /api/update).
// DOM is built with createElement and textContent only.
(function () {
  "use strict";
  const LR = window.LR;
  const NEW_REVIEW = /^\/api\/reviews\/?$/;          // M1b's create-review request (POST)
  const POLL_MS = 60000;
  const api = LR.api.bind(LR);                        // the unwrapped call, so the wrapper below never loops
  let usage = null;

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

  function style() {
    if (document.getElementById("m2-css")) return;
    const l = document.createElement("link");
    l.id = "m2-css";
    l.rel = "stylesheet";
    l.href = "/static/m2.css";          // a file: the app's CSP (style-src 'self') blocks inline <style>
    document.head.append(l);
  }

  const pct = (u) => `${Math.round(u * 100)}%`;
  const when = (ts) => new Date(ts * 1000).toLocaleString([], { weekday: "short", hour: "2-digit", minute: "2-digit" });

  function level(u, limited) {
    if (limited || u >= 0.95) return ["m2-crit", "limit"];
    if (u >= 0.8) return ["m2-warn", "high"];
    return ["m2-ok", ""];
  }

  function meterEl(label, w, limited) {
    if (!w || w.utilization === null || w.utilization === undefined) {
      const tip = w && w.reset ? "This window has reset since the last session" : "Known after the next Claude session";
      return h("span", { class: "m2-meter m2-muted", title: tip }, `${label} –`);
    }
    const u = Math.max(0, Math.min(1, w.utilization));
    const [cls, word] = level(u, limited);
    const fill = h("span", { class: "m2-fill" });
    fill.style.width = pct(u);
    const tip = `${label}: ${pct(u)} used${w.resets_at ? `, resets ${when(w.resets_at)}` : ""}`;
    return h("span", { class: `m2-meter ${cls}`, title: tip },
      h("span", {}, label),
      h("span", { class: "m2-track", role: "meter", "aria-valuemin": 0, "aria-valuemax": 100,
                  "aria-valuenow": Math.round(u * 100), "aria-label": `${label} Claude usage` }, fill),
      h("span", { class: "m2-val" }, word ? `${pct(u)} ${word}` : pct(u)));
  }

  function draw(bar) {
    const meters = bar.querySelector(".m2-meters");
    if (!usage || !usage.known) {
      meters.replaceChildren(h("span", { class: "m2-muted", title: "Shown after the first Claude session" }, "Claude usage –"));
      return;
    }
    const lim = usage.limited;
    const kids = [                                   // replaceChildren() would print a null as "null"
      h("span", { class: "m2-muted" }, "Claude usage"),
      meterEl("5 h", usage.five_hour, lim && lim.type === "five_hour"),
      meterEl("week", usage.seven_day, lim && lim.type === "seven_day")];
    if (lim) kids.push(h("span", { class: "m2-val" }, `⚠ limit until ${when(lim.resets_at)}`));
    meters.replaceChildren(...kids);
  }

  async function poll(bar) {
    try { usage = await api("GET", "/api/usage"); } catch (e) { /* keep the last value */ }
    draw(bar);
  }

  async function confirmNewReview() {
    try { usage = await api("GET", "/api/usage"); } catch (e) { return true; }
    if (!usage || !usage.warn_new_review) return true;
    return window.confirm(`${usage.warn_new_review}\n\nStart the review anyway?`);
  }

  LR.api = async function (method, path, body) {
    if (String(method).toUpperCase() === "POST" && NEW_REVIEW.test(path) && !(await confirmNewReview())) {
      throw new Error("Not started: your weekly Claude usage is high.");
    }
    return api(method, path, body);
  };
  LR.features.usage = { get: () => usage, confirmNewReview };

  function show(r) {
    const dlg = h("dialog", { class: "m2-dialog" },
      h("h3", {}, r.ok ? "Updated" : "The update didn't finish"),
      r.error ? h("p", {}, r.error) : null,
      r.restart ? h("p", {}, h("strong", {}, r.restart)) : null,
      (r.steps || []).map((s) => h("details", { open: s.exit !== 0 },
        h("summary", {}, `${s.exit === 0 ? "✓" : "✗"} ${s.title}`),
        h("pre", {}, s.output || "(no output)"))),
      h("form", { method: "dialog" }, h("button", { class: "m2-btn" }, "Close")));
    document.body.append(dlg);
    dlg.addEventListener("close", () => dlg.remove());
    dlg.showModal();
  }

  async function update(btn) {
    const ok = window.confirm("Update the reviewing-literature plugin and Litrev App now?\n\n" +
      "This runs claude plugin marketplace update, claude plugin update, and reinstalls the app. " +
      "It is refused while a review session, a refresh or a chat message is running.");
    if (!ok) return;
    btn.disabled = true;
    btn.textContent = "Updating…";
    try { show(await api("POST", "/api/update", {})); }
    catch (e) { LR.toast(String((e && e.message) || e)); }
    finally { btn.disabled = false; btn.textContent = "Update"; }
  }

  function mount() {
    style();
    const host = document.querySelector("header");
    const btn = h("button", { class: "m2-btn", title: "Update the plugin and the app" }, "Update");
    btn.addEventListener("click", () => update(btn));
    const bar = h("div", { class: host ? "m2-toolbar" : "m2-toolbar m2-floating" }, h("span", { class: "m2-meters" }), btn);
    (host || document.body).append(bar);
    poll(bar);
    setInterval(() => poll(bar), POLL_MS);
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", mount);
  else mount();
})();
