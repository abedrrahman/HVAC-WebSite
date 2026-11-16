document.documentElement.classList.add("js");

function store(fn) { try { return fn(); } catch (e) { return null; } }

// Sidebar
const body = document.body;
if (store(() => localStorage.getItem("sidebar")) === "collapsed") body.classList.add("collapsed");
document.querySelectorAll("[data-toggle-sidebar]").forEach(b => b.addEventListener("click", () => {
  if (matchMedia("(max-width: 860px)").matches) { body.classList.toggle("nav-open"); return; }
  body.classList.toggle("collapsed");
  store(() => localStorage.setItem("sidebar", body.classList.contains("collapsed") ? "collapsed" : "open"));
}));
body.addEventListener("click", e => { if (e.target === body) body.classList.remove("nav-open"); });

// Headline numbers count up on arrival.
if (!matchMedia("(prefers-reduced-motion: reduce)").matches) document.querySelectorAll(".stat .n, [data-count]").forEach(el => {
  const end = parseInt(el.textContent, 10), start = performance.now();
  if (!end) return;
  el.textContent = "0";
  (function tick(now) {
    const p = Math.min((now - start) / 600, 1);
    el.textContent = Math.round(end * (1 - (1 - p) ** 3));
    if (p < 1) requestAnimationFrame(tick);
  })(start);
});

// Site search on a country page filters as you type.
document.querySelectorAll("[data-site-search]").forEach(input => input.addEventListener("input", () => {
  const q = input.value.trim().toLowerCase(), cards = [...document.querySelectorAll(".site-card")];
  cards.forEach(c => { c.hidden = !c.dataset.names.toLowerCase().includes(q); });
  document.querySelector("[data-site-empty]").hidden = cards.some(c => !c.hidden);
}));

// Alarm Lookup chat. Every reply is built with text nodes, so manual text can never run as HTML.
document.querySelectorAll("[data-chat]").forEach(chat => {
  const log = chat.querySelector("[data-chat-log]"), form = chat.querySelector("[data-chat-form]");
  const el = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text != null) n.textContent = text; return n; };
  const say = (who, build) => {
    const msg = el("div", `msg ${who}`), bubble = el("div", "bubble");
    build(bubble);
    msg.append(bubble);
    log.append(msg);
    msg.scrollIntoView({ block: "nearest" });
    return msg;
  };
  const source = (s, label) => {
    const text = `${s.name} — ${label} ${s.page}`;
    if (!s.url) return el("div", "src", text);
    const a = el("a", "src", text);
    a.href = s.url; a.target = "_blank"; a.rel = "noopener";
    return a;
  };
  const marked = text => {
    const p = el("p", "snippet");
    p.dir = "auto";
    text.split(/(\[\[.*?\]\])/).forEach(part => p.append(part.startsWith("[[") ? el("mark", "", part.slice(2, -2)) : part));
    return p;
  };
  const heading = (a, L) => {
    const box = el("div"), head = el("div", "answer-head");
    head.append(el("b", "", a.code), el("span", "", a.name));
    head.children[0].dir = "ltr";
    box.append(head, el("div", "meta", `${L.controller}: ${a.controllers.join("، ")}`));
    return box;
  };
  const button = (text, cls, fn) => { const b = el("button", cls, text); b.type = "button"; b.addEventListener("click", fn); return b; };
  // Walks through one alarm's steps; only the current step is on screen, finished ones stay ticked above it.
  const guide = (host, a, L) => {
    const card = el("div", "guide"), done = el("ol", "steps-done"), now = el("div", "step-now");
    card.append(heading(a, L));
    if (a.effect) card.append(el("div", "meta", `${L.effect}: ${a.effect}`));
    if (a.causes) card.append(el("div", "meta", `${L.causes}: ${a.causes}`));
    card.append(done, now);
    const footer = () => {
      const src = source(a.source, L.page);
      return a.note ? [src, el("div", "meta", a.note)] : [src];
    };
    const finish = text => { now.replaceChildren(el("p", "step-result", text), ...footer()); };
    const ask = () => now.replaceChildren(el("p", "step-text", L.ask), el("div", "step-actions"));
    const show = i => {
      done.replaceChildren(...a.steps.slice(0, i).map(s => el("li", "", s.text)));
      if (i >= a.steps.length) {
        ask();
        now.querySelector(".step-actions").append(button(L.yes, "btn primary", () => finish(L.solved)), button(L.no, "btn", () => finish(L.stuck)));
        now.querySelector("button").focus({ preventScroll: true });
        return;
      }
      const s = a.steps[i], bar = el("div", "step-bar"), fill = el("span");
      fill.style.width = `${(i + 1) / a.steps.length * 100}%`;
      bar.append(fill);
      const text = el("p", "step-text", s.text);
      text.dir = "auto";
      const acts = el("div", "step-actions");
      acts.append(button(L.next, "btn primary", () => show(i + 1)), button(L.fixed, "btn", () => { done.replaceChildren(...a.steps.slice(0, i + 1).map(x => el("li", "", x.text))); finish(L.solved); }));
      if (i) acts.append(button(L.back, "btn", () => show(i - 1)));
      now.replaceChildren(el("div", "step-count", `${L.step} ${i + 1} ${L.of} ${a.steps.length}`), bar, el("div", "step-kind", s.label), text, acts);
      acts.firstChild.focus({ preventScroll: true });
      card.scrollIntoView({ block: "nearest" });
    };
    host.append(card);
    if (a.steps.length) show(0); else finish(L.none);
  };
  const reply = (b, data) => {
    b.append(el("p", "", data.text));
    if (data.picks) {
      const chips = el("div", "chips");
      data.picks.forEach(name => {
        const c = el("button", "chip", name);
        c.type = "button"; c.dir = "ltr";
        c.addEventListener("click", () => { form.elements.model.value = name; ask(chat.lastQuestion); });
        chips.append(c);
      });
      b.append(chips);
    }
    if (data.tip) b.append(el("p", "tip", data.tip));
    if (data.alarms?.length) guide(b, data.alarms[0], data.labels);
    if (data.alarms?.length > 1) b.append(el("h3", "", data.labels.alarms));
    data.alarms?.slice(1).forEach(a => {
      const card = el("div", "answer");
      card.append(heading(a, data.labels));
      const go = el("button", "chip", data.labels.guide);
      go.type = "button";
      go.addEventListener("click", () => say("bot", n => guide(n, a, data.labels)));
      card.append(go);
      b.append(card);
    });
    if (data.passages?.length) b.append(el("h3", "", data.labels.passages));
    data.passages?.forEach(p => {
      const card = el("div", "answer");
      card.append(marked(p.snippet), source(p.source, data.labels.page));
      b.append(card);
    });
  };
  async function ask(question) {
    chat.lastQuestion = question;
    say("me", b => { const p = el("p", "", question); p.dir = "auto"; b.append(p); });
    const waiting = say("bot", b => b.append(el("div", "typing", "")));
    waiting.querySelector(".typing").append(el("i"), el("i"), el("i"));
    const params = new URLSearchParams({ q: question, model: form.elements.model.value.trim() });
    let data;
    try {
      const res = await fetch(`${chat.dataset.url}?${params}`);
      if (!res.ok || !res.headers.get("content-type")?.includes("json")) throw new Error(res.status);
      data = await res.json();
    } catch (e) { data = { text: chat.dataset.error }; }
    waiting.remove();
    say("bot", b => reply(b, data));
  }
  form.addEventListener("submit", e => {
    e.preventDefault();
    const q = form.elements.q.value.trim();
    if (q) { form.elements.q.value = ""; ask(q); }
  });
  chat.querySelectorAll("[data-ask]").forEach(c => c.addEventListener("click", () => ask(c.textContent)));
});

// On phones, filters start folded unless one is in use, so results show without scrolling.
if (matchMedia("(max-width: 860px)").matches) document.querySelectorAll("[data-filter-box]").forEach(box => {
  const used = [...new FormData(box.querySelector("form"))].some(([name, value]) => value && !["view", "date"].includes(name));
  if (!used) box.open = false;
});

// Tabs follow the URL hash so a link or browser back lands on the right section.
const tabs = [...document.querySelectorAll(".tabs a[href^='#']")];
function showTab() {
  if (!tabs.length) return;
  const wanted = tabs.find(a => a.getAttribute("href") === location.hash) || tabs[0];
  tabs.forEach(a => {
    const on = a === wanted;
    a.classList.toggle("active", on);
    a.setAttribute("aria-selected", on);
    document.querySelector(a.getAttribute("href"))?.classList.toggle("active", on);
  });
}
addEventListener("hashchange", showTab);
// pushState keeps the Back button working without the page jumping to the panel.
tabs.forEach(a => a.addEventListener("click", e => { e.preventDefault(); history.pushState(null, "", a.getAttribute("href")); showTab(); }));
showTab();

// Confirmation for irreversible actions
document.addEventListener("submit", e => {
  const msg = e.target.dataset.confirm;
  if (msg && !confirm(msg)) e.preventDefault();
});

// A saved form shows it is working and cannot be sent twice.
document.addEventListener("submit", e => {
  if (e.defaultPrevented || e.target.method !== "post") return;
  const btn = e.submitter;
  if (!btn || !btn.classList.contains("btn")) return;
  btn.classList.add("busy");
  setTimeout(() => { btn.disabled = true; }, 0);  // after the browser has read the button's own value
  setTimeout(() => { btn.disabled = false; btn.classList.remove("busy"); }, 15000);  // a response that opens no new page must not leave it stuck
});
addEventListener("pageshow", e => { if (e.persisted) document.querySelectorAll(".btn.busy").forEach(b => { b.disabled = false; b.classList.remove("busy"); }); });

// Success messages leave by themselves; errors stay until read.
document.querySelectorAll(".content > .alert.success").forEach(a => setTimeout(() => a.remove(), 4500));

// Dark mode, remembered per browser.
document.querySelectorAll("[data-theme-toggle]").forEach(b => b.addEventListener("click", () => {
  const dark = document.documentElement.dataset.theme !== "dark";
  if (dark) document.documentElement.dataset.theme = "dark"; else delete document.documentElement.dataset.theme;
  store(() => localStorage.setItem("theme", dark ? "dark" : "light"));
}));

// Dependent selects: refilled from the server when a parent select changes
async function refill(select) {
  const params = new URLSearchParams();
  const parents = select.dataset.parent.split(",").map(id => document.getElementById(id)).filter(Boolean);
  parents.forEach(p => params.set(p.name, p.value));
  const wanted = select.dataset.pending || select.value;
  delete select.dataset.pending;
  // List filters show every option until a parent is chosen, and pass a cleared value down the chain.
  const isFilter = select.dataset.source.startsWith("filter_");
  if (!isFilter && !parents.some(p => p.value)) { select.length = 1; select.selectedIndex = 0; return; }
  const token = select.refillToken = (select.refillToken || 0) + 1;
  const res = await fetch(`/options/${select.dataset.source}/?${params}`);
  if (!res.ok) return;
  const items = await res.json();
  if (isFilter && token !== select.refillToken) return;
  const before = select.value;
  select.length = 1;
  items.forEach(item => select.add(new Option(item.label, item.id)));
  select.value = items.some(i => String(i.id) === wanted) ? wanted : "";
  if (isFilter && select.value !== before) select.dispatchEvent(new Event("change"));
}
const dependents = [...document.querySelectorAll("select[data-parent]")];
dependents.forEach(sel => sel.dataset.parent.split(",").forEach(id => {
  document.getElementById(id)?.addEventListener("change", () => refill(sel));
}));

// Show only the fields relevant to a choice
function fieldOf(id) { return document.getElementById(id)?.closest(".field"); }
function setVisible(el, on) { if (el) el.style.display = on ? "" : "none"; }

function applyDocumentLevel(form) {
  const level = form.querySelector("input[name=level]:checked")?.value;
  form.querySelectorAll("[data-levels]").forEach(el => {
    setVisible(el.closest(".field"), !!level && el.dataset.levels.split(",").includes(level));
  });
}
document.querySelectorAll("form").forEach(form => {
  if (!form.querySelector("input[name=level]")) return;
  form.addEventListener("change", e => { if (e.target.name === "level") applyDocumentLevel(form); });
  applyDocumentLevel(form);
});

function applySchedule() {
  const freq = document.getElementById("id_frequency"), scope = document.getElementById("id_scope");
  if (!freq || !scope) return;
  setVisible(fieldOf("id_custom_value"), freq.value === "custom");
  setVisible(fieldOf("id_custom_unit"), freq.value === "custom");
  setVisible(fieldOf("id_dates"), freq.value === "manual");
  setVisible(fieldOf("id_start_date"), freq.value !== "manual");
  setVisible(fieldOf("id_unit"), scope.value === "unit");
  setVisible(fieldOf("id_include_new_units"), scope.value === "general");
}
["id_frequency", "id_scope"].forEach(id => document.getElementById(id)?.addEventListener("change", applySchedule));
applySchedule();

// Select-all for contract equipment
document.querySelectorAll("[data-check-all]").forEach(btn => btn.addEventListener("click", () => {
  const on = btn.dataset.checkAll === "on";
  document.querySelectorAll("input[name=units]:not(:disabled)").forEach(c => { c.checked = on; });
}));

// Language switch: keeps the page and anything typed so far (files cannot be restored by browsers).
const DRAFT = "lang-switch-draft";
function snapshotForms() {
  const forms = [...document.querySelectorAll("form[method=post], form[method=POST]")].map(form =>
    [...form.elements].filter(el => el.name && el.name !== "csrfmiddlewaretoken" && !["password", "file", "submit", "button", "hidden"].includes(el.type))
      .map(el => (el.type === "checkbox" || el.type === "radio") ? [el.name, el.value, el.checked] : [el.name, el.value]));
  store(() => sessionStorage.setItem(DRAFT, JSON.stringify({ path: location.pathname, forms })));
}
async function restoreForms() {
  const saved = store(() => JSON.parse(sessionStorage.getItem(DRAFT) || "null"));
  store(() => sessionStorage.removeItem(DRAFT));
  if (saved && saved.path === location.pathname) {
    const forms = [...document.querySelectorAll("form[method=post], form[method=POST]")];
    saved.forms.forEach((fields, i) => {
      const form = forms[i];
      if (!form) return;
      fields.forEach(([name, value, checked]) => {
        form.querySelectorAll(`[name="${CSS.escape(name)}"]`).forEach(el => {
          if (el.type === "checkbox" || el.type === "radio") { if (el.value === value) el.checked = checked; }
          else if (el.dataset.parent) el.dataset.pending = value;
          else el.value = value;
        });
      });
      if (form.querySelector("input[name=level]")) applyDocumentLevel(form);
    });
    applySchedule();
  }
  // Parents hold values after a bound re-render or restore; bring their children in line.
  for (const sel of dependents) {
    if (sel.dataset.pending || sel.length <= 1) await refill(sel);
  }
}
restoreForms();

document.querySelectorAll("[data-lang]").forEach(btn => btn.addEventListener("click", async () => {
  snapshotForms();
  const data = new URLSearchParams({ lang: btn.dataset.lang });
  await fetch("/language/", { method: "POST", body: data, headers: { "X-CSRFToken": document.querySelector("meta[name=csrf]").content } });
  // A changed query guarantees a fresh GET, even on a page reached by submitting a form.
  const url = new URL(location.href);
  url.searchParams.set("l", btn.dataset.lang);
  location.assign(url);
}));
