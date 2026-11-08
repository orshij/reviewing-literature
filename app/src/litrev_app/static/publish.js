/* Litrev App feature: Publish. POST /api/reviews/<slug>/publish opens a Terminal window where Claude Code publishes
   the hub once the person approves; the panel says "Waiting for your approval in the Terminal window" and polls GET
   every 2 s until the link comes back. LR.features.publish(slug) (the review page's Publish button, M2's refresh
   view) opens the dialog and starts the publish; the Publish review tab shows the link and the last result. Text goes
   in through textContent only; styles come from /static/publish.css (the app's CSP allows no inline styles). */
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
    if (document.getElementById("lr-pub-css")) return;
    document.head.append(h("link", { id: "lr-pub-css", rel: "stylesheet", href: "/static/publish.css" }));
  }

  const MB = (n) => (n / 1048576).toFixed(1) + " MB";
  const errText = (e) => (e && e.message) || String(e);

  // Only claude.ai links become clickable: meta.json's hub_url is data, not trusted markup.
  function link(url) {
    return /^https:\/\/claude\.ai\//.test(url || "")
      ? h("a", { href: url, target: "_blank", rel: "noopener noreferrer" }, url)
      : document.createTextNode(String(url || ""));
  }

  function copyButton(url) {
    return h("button", { type: "button", onclick: () => {
      navigator.clipboard.writeText(url).then(() => LR.toast("Link copied"), () => LR.toast(url));
    } }, "Copy link");
  }

  /* The publish panel for one review. opts.start: press Publish as soon as the current state is known. */
  function panel(root, slug, opts) {
    opts = opts || {};
    addStyle();
    const box = h("section", { class: "lr-pub", "aria-live": "polite" }, h("p", {}, "Loading…"));
    root.append(box);
    const path = "/api/reviews/" + encodeURIComponent(slug) + "/publish";
    let timer = null;
    let autostart = !!opts.start;

    function show(s) {
      clearTimeout(timer);
      const kids = [h("h2", {}, "Publish the hub")];
      kids.push(s.hub_url
        ? h("p", {}, "Current link: ", link(s.hub_url))
        : h("p", {}, "Not published yet. Publishing makes a private page on claude.ai; you choose whom to share " +
                     "it with there."));
      if (s.hub_bytes) {
        kids.push(h("p", { class: "lr-pub-muted" },
                    "hub.html is " + MB(s.hub_bytes) + " (the limit is " + MB(s.max_bytes) + ")."));
      }
      if (s.state === "waiting_approval") {
        kids.push(h("div", { class: "lr-pub-wait" },
          h("span", { class: "lr-pub-spin", "aria-hidden": "true" }),
          h("div", {},
            h("strong", {}, "Waiting for your approval in the Terminal window."),
            h("p", {}, "A Terminal window opened with Claude Code. Approve the publish there (one click). The " +
                       "first time, Claude Code also asks whether you trust the reviews folder: answer yes. The " +
                       "link shows up here a few seconds after you approve."),
            s.message && s.message.indexOf("Double-click") >= 0 ? h("p", { class: "lr-pub-err" }, s.message) : null)));
        kids.push(h("button", { type: "button", onclick: cancel }, "Cancel"));
        timer = setTimeout(poll, 2000);
      } else {
        if (s.state === "published" && s.url) {
          kids.push(h("p", { class: "lr-pub-ok" }, (s.message || "Published.") + " ", link(s.url), " ",
                      copyButton(s.url)));
        }
        if (s.state === "failed" && s.message) kids.push(h("p", { class: "lr-pub-err" }, s.message));
        if (s.error) kids.push(h("p", { class: "lr-pub-err" }, s.error));
        if (s.busy) kids.push(h("p", { class: "lr-pub-muted" }, s.busy));
        kids.push(h("button", { type: "button", class: "lr-pub-go", disabled: !!s.busy, onclick: start },
                    s.hub_url ? "Republish" : "Publish"));
      }
      box.replaceChildren(...kids);
      if (autostart && s.state !== "waiting_approval" && !s.busy) {
        autostart = false;
        start();
      }
      autostart = false;
    }

    async function poll() {
      if (!box.isConnected) return;
      try {
        show(await LR.api("GET", path));
      } catch (e) {
        timer = setTimeout(poll, 5000);
      }
    }

    async function start(ev) {
      if (ev && ev.target) ev.target.disabled = true;
      try {
        show(await LR.api("POST", path, {}));
      } catch (e) {
        LR.toast(errText(e));
        poll();
      }
    }

    async function cancel() {
      try {
        show(await LR.api("POST", path, { action: "cancel" }));
      } catch (e) {
        LR.toast(errText(e));
      }
    }

    poll();
    return box;
  }

  function open(slug, opts) {
    addStyle();
    const card = h("div", { class: "lr-pub-card", role: "dialog", "aria-modal": "true",
                            "aria-label": "Publish the hub" });
    const ov = h("div", { class: "lr-pub-overlay" }, card);
    const onKey = (e) => { if (e.key === "Escape") close(); };
    const close = () => { ov.remove(); document.removeEventListener("keydown", onKey); };
    ov.addEventListener("click", (e) => { if (e.target === ov) close(); });
    document.addEventListener("keydown", onKey);
    card.append(h("button", { type: "button", class: "lr-pub-x", "aria-label": "Close", onclick: close }, "×"));
    document.body.append(ov);
    panel(card, slug, opts);
  }

  LR.publish = { open: open, panel: panel };
  // Truthy, so the review page shows its Publish button; callable, so that button and M2's refresh view start a publish.
  LR.features.publish = (slug) => open(slug, { start: true });
  LR.pages.publish = (root, params) => panel(root, (params && params.slug) || "");
  LR.reviewTabs.push({ id: "publish", title: "Publish", render: (root, review) => panel(root, review.slug) });
})();
