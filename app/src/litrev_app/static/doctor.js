/* Litrev App feature: Doctor. Checks this Mac (GET /api/doctor) and starts the fixes the app can do
   (POST /api/doctor/<id>/fix). Shown as its own page (#/doctor), at the top of Settings, and over any page at
   startup when a check fails. Text goes in through textContent only; styles come from /static/doctor.css (the app's
   CSP allows no inline styles). */
(function () {
  "use strict";
  const LR = window.LR;
  if (!LR) return;

  function h(tag, attrs, ...kids) {
    const el = document.createElement(tag);
    Object.entries(attrs || {}).forEach(([k, v]) => {
      if (v === null || v === undefined || v === false) return;
      if (k === "class") el.className = v;
      else if (k.slice(0, 2) === "on") el.addEventListener(k.slice(2), v);
      else el.setAttribute(k, v === true ? "" : String(v));
    });
    for (const kid of kids.flat()) {
      if (kid === null || kid === undefined || kid === false) continue;
      el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
    }
    return el;
  }

  function addStyle() {
    if (document.getElementById("lr-doc-css")) return;
    document.head.append(h("link", { id: "lr-doc-css", rel: "stylesheet", href: "/static/doctor.css" }));
  }

  const BADGE = { ok: "OK", warn: "Warning", fail: "Problem" };
  const errText = (e) => (e && e.message) || String(e);
  const load = (fresh) => LR.api("GET", "/api/doctor" + (fresh ? "?fresh=1" : ""));

  function row(c, refresh) {
    const fixButton = c.action && c.level !== "ok" ? h("button", {
      type: "button",
      onclick: async (ev) => {
        ev.target.disabled = true;
        try {
          const r = await LR.api("POST", "/api/doctor/" + encodeURIComponent(c.id) + "/fix", {});
          LR.toast(r.message || "Started");
        } catch (e) {
          LR.toast(errText(e));
        }
        setTimeout(() => refresh(true), 1500);
      }
    }, c.action) : null;
    return h("div", { class: "lr-doc-row", "data-check": c.id },
      h("span", { class: "lr-doc-badge lr-doc-" + c.level }, BADGE[c.level] || c.level),
      h("div", {},
        h("div", { class: "lr-doc-title" }, c.title),
        h("div", { class: "lr-doc-msg" }, c.message),
        c.fix && c.level !== "ok" ? h("div", { class: "lr-doc-fix" }, c.fix) : null),
      fixButton);
  }

  /* Render the checks into root. opts: title, intro, extra (a node for the button bar), onReady(result). */
  function render(root, opts) {
    opts = opts || {};
    addStyle();
    const list = h("div", { "aria-live": "polite" }, h("p", {}, "Checking this Mac…"));
    let timer = null;
    async function refresh(fresh) {
      clearTimeout(timer);
      let r;
      try {
        r = await load(fresh);
      } catch (e) {
        list.replaceChildren(h("p", { class: "lr-doc-msg" }, "Could not run the checks: " + errText(e)));
        return null;
      }
      list.replaceChildren(...r.checks.map((c) => row(c, refresh)));
      if (r.ready && opts.onReady) opts.onReady(r);
      if (r.checks.some((c) => c.detail && c.detail.building)) {
        timer = setTimeout(() => { if (list.isConnected) refresh(false); }, 3000);
      }
      return r;
    }
    root.append(h("section", { class: "lr-doc" },
      h("h2", {}, opts.title || "Doctor"),
      h("p", {}, opts.intro || "What the app needs on this Mac. Warnings don't stop a review; problems do."),
      list,
      h("div", { class: "lr-doc-bar" },
        h("button", { type: "button", onclick: () => refresh(true) }, "Check again"),
        opts.extra || null)));
    return refresh(false);
  }

  function overlay() {
    if (document.querySelector(".lr-doc-overlay")) return;
    addStyle();
    const card = h("div", { class: "lr-doc-card", role: "dialog", "aria-modal": "true",
                            "aria-label": "Before you start" });
    const ov = h("div", { class: "lr-doc-overlay" }, card);
    const close = () => ov.remove();
    document.body.append(ov);
    render(card, {
      title: "Before you start",
      intro: "Something on this Mac needs fixing before the app can run a review.",
      extra: h("button", { type: "button", onclick: close }, "Close"),
      onReady: () => { close(); LR.toast("All set: the app can run reviews."); }
    });
  }

  LR.doctor = { render: render, overlay: overlay };
  LR.pages.doctor = (root) => render(root);

  // The Doctor sits at the top of Settings (M1b's page), added after that page has drawn itself.
  const settingsPage = LR.pages.settings;
  LR.pages.settings = function (root, params) {
    const out = settingsPage ? settingsPage.call(this, root, params) : undefined;
    Promise.resolve(out).then(() => {
      const box = h("div", {});
      root.prepend(box);
      render(box, { title: "Doctor" });
    });
    return out;
  };

  // First run: when a check fails, show the checks over whatever page is open.
  load(false).then((r) => { if (r && !r.ready) overlay(); }).catch(() => {});
})();
