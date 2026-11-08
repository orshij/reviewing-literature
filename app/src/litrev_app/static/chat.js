// Litrev App M2: the review tab "Ask Claude". Each message runs one Claude Code session on the review (the app
// resumes the same conversation); the reply streams in over SSE ("chat" events). While a step session works on the
// review the chat is read-only. Replies can quote untrusted paper text, so everything is shown as plain text.
(function () {
  "use strict";
  const LR = window.LR;
  let sub = null;
  let offVisible = null;     // reload when the browser tab is shown again: hidden tabs close their streams

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

  const money = (x) => (typeof x === "number" ? `$${x.toFixed(2)}` : "");
  const errText = (e) => String((e && e.message) || e);

  async function render(root, review) {
    css();
    const slug = review.slug;
    const base = `/api/reviews/${slug}/chat`;
    if (sub && typeof sub.close === "function") sub.close();
    if (offVisible) { offVisible(); offVisible = null; }
    const list = h("div", { class: "m2-chat-list", role: "log", "aria-live": "polite" });
    const badge = h("span", { class: "m2-badge", hidden: true,
      title: "A step session is working on this review: Claude can read it but not change it until the step ends." },
      "Read-only while a step runs");
    const total = h("span", { class: "m2-muted" });
    const input = h("textarea", { rows: 3, placeholder: "Ask about this review: “Why isn't Depth Pro in the review?”, “Add arXiv 2410.02073”…" });
    const send = h("button", { class: "m2-primary" }, "Send");
    const stop = h("button", { hidden: true }, "Stop");
    const fresh = h("button", { title: "Start a new conversation; this one is saved in the review folder (cache/app)." }, "New conversation");
    let live = null;
    root.replaceChildren(h("div", { class: "m2-chat" },
      h("div", { class: "m2-row" }, h("strong", {}, "Ask Claude"), badge, total, h("span", { class: "m2-grow" }), fresh),
      list,
      h("div", { class: "m2-row m2-chat-input" }, input, h("div", { class: "m2-col" }, send, stop)),
      h("p", { class: "m2-muted" }, "Claude follows the skill, makes one change at a time and never publishes. Costs are API-equivalent estimates. ⌘↩ sends.")));

    const setMode = (mode) => { badge.hidden = mode !== "read_only"; };
    const setRunning = (on) => { send.disabled = on; stop.hidden = !on; fresh.disabled = on; };
    const scroll = () => { list.scrollTop = list.scrollHeight; };

    function bubble(m) {
      const meta = [];
      if (m.role === "assistant") {
        if (typeof m.cost_usd === "number") meta.push(`${money(m.cost_usd)} API-equivalent`);
        if (m.profile === "chat_ro") meta.push("read-only");
        if (m.outcome && m.outcome !== "ok") meta.push(m.outcome.replace(/_/g, " "));
      }
      const el = h("div", { class: `m2-msg m2-${m.role}${m.error ? " m2-error" : ""}` },
        h("div", { class: "m2-text" }, m.text || ""),
        h("ul", { class: "m2-acts" }),
        meta.length ? h("div", { class: "m2-meta" }, meta.join(" · ")) : null);
      if (m.run_from) {
        const b = h("button", { class: "m2-primary" }, `Run steps ${m.run_from}–7`);
        b.addEventListener("click", async () => {
          b.disabled = true;
          try {
            await LR.api("POST", `${base}/run-steps`, { step: m.run_from });
            LR.toast(`Steps ${m.run_from}–7 queued; follow them on the review page.`);
          } catch (e) { LR.toast(errText(e)); b.disabled = false; }
        });
        el.append(h("div", { class: "m2-row" }, h("span", { class: "m2-muted" }, "Claude suggests running the later steps for this change."), b));
      }
      return el;
    }

    function startLive(text) {
      live = bubble({ role: "assistant", text: text || "" });
      list.append(live);
      scroll();
    }

    async function load() {
      let s;
      try { s = await LR.api("GET", base); } catch (e) { list.replaceChildren(h("p", {}, errText(e))); return; }
      list.replaceChildren(...s.messages.map(bubble));
      live = null;
      if (s.running) startLive(s.partial);
      setMode(s.mode);
      setRunning(s.running);
      total.textContent = s.cost_usd ? `${money(s.cost_usd)} so far` : "";
      scroll();
    }

    async function doSend() {
      const text = input.value.trim();
      if (!text) return;
      setRunning(true);
      try { await LR.api("POST", base, { text }); input.value = ""; }
      catch (e) { LR.toast(errText(e)); setRunning(false); }
    }

    send.addEventListener("click", doSend);
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); doSend(); }
    });
    stop.addEventListener("click", () => LR.api("POST", `${base}/stop`, {}).catch((e) => LR.toast(errText(e))));
    fresh.addEventListener("click", async () => {
      if (!window.confirm("Start a new conversation? Claude won't remember this one; its messages leave this page and are saved in the review folder (cache/app).")) return;
      try { await LR.api("POST", `${base}/reset`, {}); } catch (e) { LR.toast(errText(e)); }
      load();
    });

    let modeTimer = null;
    sub = LR.sse(slug, (ev) => {
      if (!ev) return;
      if (ev.type !== "chat") {        // a step started or ended: re-check the read-only badge, at most every 3 s
        if (!modeTimer) {
          modeTimer = setTimeout(async () => {
            modeTimer = null;
            try { setMode((await LR.api("GET", base)).mode); } catch (e) { /* the next event retries */ }
          }, 3000);
        }
        return;
      }
      if (ev.event === "start") {
        list.append(bubble(ev.message));
        startLive("");
        setRunning(true);
        setMode(ev.profile === "chat_ro" ? "read_only" : "read_write");
      } else if (ev.event === "delta" && live) {
        live.querySelector(".m2-text").append(ev.text);
        scroll();
      } else if (ev.event === "activity" && live) {
        const a = ev.activity || {};
        live.querySelector(".m2-acts").append(h("li", { class: a.status === "failed" ? "m2-error" : null },
          `${a.title}${a.detail ? `: ${a.detail}` : ""}`));
        scroll();
      } else if (ev.event === "done") {
        load();
      }
    });
    offVisible = LR.on("visible", () => load());
    LR.onLeave(() => { if (offVisible) { offVisible(); offVisible = null; } });
    await load();
  }

  LR.reviewTabs.push({ id: "chat", title: "Ask Claude", render });
})();
