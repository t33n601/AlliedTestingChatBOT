/* KB Helper web UI: streaming RAG chat with citations, a live knowledge map and document upload. */
(() => {
  "use strict";

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
  const el = (tag, cls, html) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (html != null) n.innerHTML = html;
    return n;
  };
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const fmtMs = (ms) => (ms == null ? "—" : ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(1)} s`);
  const store = {
    get(k, d) { try { const v = localStorage.getItem(k); return v == null ? d : JSON.parse(v); } catch { return d; } },
    set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch { /* private mode */ } },
  };

  const EXAMPLES = [
    "What services do you offer?",
    "How do you handle security testing?",
    "Tell me about your QA consulting",
    "Какие AI-решения есть у компании?",
  ];
  /* the "not in the knowledge base" card speaks the language of the question */
  const isRu = (text) => /[а-яё]/i.test(text || "");
  const NOANSWER_EN = {
    title: "That isn't in the knowledge base",
    body: (pct) => `None of the documents cover this — the closest match was only ${pct}% similar — so I won't guess an answer.`,
    again: "Ask something else", add: "Add a document that covers it",
  };
  const NOANSWER_RU = {
    title: "Этого нет в базе знаний",
    body: (pct) => `Ни один документ не отвечает на этот вопрос — самое близкое совпадение всего ${pct}%, поэтому я не буду угадывать.`,
    again: "Спросить другое", add: "Добавить документ на эту тему",
  };
  const ICONS = {
    up: '<svg viewBox="0 0 24 24"><path d="M7 11v9H3v-9zM7 11l4-8c1.7 0 3 1.3 3 3v3h6l-2 11H7"/></svg>',
    down: '<svg viewBox="0 0 24 24"><path d="M17 13V4h4v9zM17 13l-4 8c-1.7 0-3-1.3-3-3v-3H4L6 4h11"/></svg>',
    copy: '<svg viewBox="0 0 24 24"><path d="M8 8h12v12H8zM4 16V4h12"/></svg>',
    redo: '<svg viewBox="0 0 24 24"><path d="M20 12a8 8 0 1 1-2.3-5.7M20 4v5h-5"/></svg>',
  };

  let stats = null;
  let busy = null;            // AbortController of the running request
  /* chats: [{id, title, updated, history}] — every conversation is kept, New chat starts another */
  const newId = () => Date.now().toString(36) + Math.random().toString(36).slice(2, 6);
  let chats = store.get("kb.chats", null);
  if (!Array.isArray(chats)) {                       // migrate the old single-conversation storage
    const old = store.get("kb.history", []);
    chats = old.length ? [{ id: newId(), title: "", updated: Date.now(), history: old }] : [];
  }
  let currentId = store.get("kb.current", chats[0] ? chats[0].id : newId());
  let history = ((chats.find((c) => c.id === currentId) || {}).history || []).slice();   // [{role, content, ...render data}]
  let lastHit = null;         // {query, point, sources} for the map

  marked.setOptions({ gfm: true, breaks: false });

  const MapData = {
    points: [], byId: new Map(), loaded: false, pending: null,
    async load(force) {
      if (this.loaded && !force) return this.points;
      if (!this.pending) {
        this.pending = api("/kb/map").then((d) => {
          this.points = d.points;
          this.byId = new Map(d.points.map((p) => [p.id, p]));
          this.loaded = true;
          return this.points;
        }).finally(() => { this.pending = null; });
      }
      return this.pending;
    },
    invalidate() { this.loaded = false; },
  };

  /* ------------------------------------------------------------ api */
  function adminHeaders() {
    const t = $("#admin-token").value.trim() || store.get("kb.admin", "");
    return t ? { "X-Admin-Token": t } : {};
  }
  async function api(path, opts = {}) {
    const r = await fetch(path, opts);
    let data = null;
    try { data = await r.json(); } catch { /* empty body */ }
    if (!r.ok) throw new Error((data && data.detail) || `${r.status} ${r.statusText}`);
    return data;
  }
  function toast(text, ms = 3200, action = null) {
    const t = $("#toast");
    t.textContent = text;
    if (action) {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "toast-act";
      b.textContent = action.label;
      b.addEventListener("click", () => { clearTimeout(toast.timer); t.hidden = true; action.run(); });
      t.appendChild(b);
    }
    t.hidden = false;
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => { t.hidden = true; }, ms);
  }

  async function loadStats() {
    const live = $("#live");
    try {
      stats = await api("/kb/stats");
      $$("[data-stat]").forEach((n) => { n.textContent = stats[n.dataset.stat] ?? "—"; });
      live.className = "live ok";
      live.innerHTML = `Assistant online`;
      live.title = `${stats.chat_model} · ${stats.chunks} passages`;
      $("#admin").hidden = !stats.admin_required;
    } catch {
      live.className = "live off";
      live.textContent = "Assistant offline";
    }
  }

  /* ------------------------------------------------------------ routing */
  function show(view) {
    if (!["assistant", "map", "documents"].includes(view)) view = "assistant";
    $$(".view").forEach((v) => v.classList.toggle("on", v.id === `view-${view}`));
    $$("#nav a, #tabbar a").forEach((a) => a.classList.toggle("on", a.dataset.view === view));
    document.body.dataset.view = view;
    Sheet.close();
    if (view === "map") MapView.open();
    if (view === "documents") Docs.load();
  }
  window.addEventListener("hashchange", () => show(location.hash.slice(1)));

  /* ------------------------------------------------------------ pipeline */
  const Pipe = {
    reset() {
      $$("#pipeline li").forEach((li) => { li.className = ""; $("small", li).textContent = "—"; });
    },
    fromMsg(msg) {      // show the timings of a finished answer
      const t = msg.timings || {};
      this.reset();
      if (t.retrieve_ms == null) return;
      this.set("retrieve", "done", fmtMs(t.retrieve_ms));
      this.set("augment", "done", `${(msg.sources || []).length} chunks`);
      if (msg.noAnswer) this.set("generate", "done", "not in KB");
      else if (t.total_ms != null) this.set("generate", "done", `${fmtMs(t.total_ms)} · ${msg.tokens || 0} tok`);
    },
    set(step, state, note) {
      const li = $(`#pipeline li[data-step="${step}"]`);
      li.className = state;
      if (note != null) $("small", li).textContent = note;
    },
  };

  /* ------------------------------------------------------------ chat rendering */
  const messagesEl = $("#messages");

  function scrollDown(force) {
    const nearBottom = messagesEl.scrollHeight - messagesEl.scrollTop - messagesEl.clientHeight < 140;
    if (force || nearBottom) messagesEl.scrollTop = messagesEl.scrollHeight;
  }

  function renderMarkdown(text, sources) {
    const html = DOMPurify.sanitize(marked.parse(text || ""));
    // [1] [2][3] -> citation chips (only numbers that exist among the sources)
    return html.replace(/\[(\d{1,2})\]/g, (m, n) => {
      const i = Number(n);
      if (!sources || i < 1 || i > sources.length) return m;
      const s = sources[i - 1];
      return `<button class="cite" data-n="${i}" title="${esc(s.source)} › ${esc(s.heading)}">${i}</button>`;
    });
  }

  function userBubble(text) {
    const m = el("div", "msg user");
    m.append(el("div", "bubble", esc(text)));
    return m;
  }

  function botShell() {
    const m = el("div", "msg bot");
    m.innerHTML = `<div class="rhomb sm" aria-hidden="true"><span class="shape"><img src="/static/assets/hammer.png" alt=""></span></div>
      <div class="body">
        <p class="rewrite" hidden></p>
        <div class="think" hidden><button type="button" class="think-head">Reasoning</button><div class="think-body"></div></div>
        <div class="answer"><span class="waiting">Searching the knowledge base…</span></div>
        <button type="button" class="src-btn" data-act="evidence" hidden></button>
        <div class="meta" hidden></div>
      </div>`;
    return m;
  }

  function fillSources(node, sources, content) {
    const btn = $(".src-btn", node);
    if (!sources || !sources.length) { btn.hidden = true; return; }
    const cited = new Set([...(content || "").matchAll(/\[(\d{1,2})\]/g)].map((m) => Number(m[1])));
    btn.hidden = false;
    btn.title = cited.size ? `${cited.size} of ${sources.length} sources cited in the answer` : "Sources the assistant read";
    btn.innerHTML = `<span class="bars" aria-hidden="true">${sources.map((s, i) => `<i class="${cited.has(i + 1) ? "cited" : ""}"></i>`).join("")}</span>
      <span><b>${sources.length}</b> sources</span><span class="chev">▸ view</span>`;
  }

  function fillMeta(node, msg) {
    const meta = $(".meta", node);
    const t = msg.timings || {};
    const parts = [];
    if (t.total_ms != null) parts.push(`<b>${fmtMs(t.total_ms)}</b>`);
    if (t.first_token_ms != null) parts.push(`first word ${fmtMs(t.first_token_ms)}`);
    if (msg.tokens) parts.push(`${msg.tokens} tok`);
    if (t.tokens_per_sec) parts.push(`${t.tokens_per_sec} tok/s`);
    if (msg.model) parts.push(`${esc(msg.model)}${msg.tone && msg.tone !== "formal" ? ` · ${esc(msg.tone)}` : ""}`);
    meta.innerHTML = parts.map((p) => `<span>${p}</span>`).join("") +
      `<span class="acts">
        <button type="button" data-act="copy" title="Copy answer" aria-label="Copy answer">${ICONS.copy}</button>
        ${msg.qid ? `<button type="button" data-act="up" title="Good answer" aria-label="Good answer" class="${msg.rating === 1 ? "on" : ""}">${ICONS.up}</button>
        <button type="button" data-act="down" title="Bad answer" aria-label="Bad answer" class="${msg.rating === -1 ? "on" : ""}">${ICONS.down}</button>` : ""}
        <button type="button" data-act="redo" title="Regenerate this answer" aria-label="Regenerate" class="redo">${ICONS.redo}</button>
      </span>`;
    meta.hidden = false;
  }

  function renderBot(node, msg, live) {
    const rw = $(".rewrite", node);
    if (msg.rewrite) { rw.hidden = false; rw.innerHTML = `searched as → <b>${esc(msg.rewrite)}</b>`; }
    const think = $(".think", node);
    if (msg.think) {
      think.hidden = false;
      $(".think-body", think).textContent = msg.think.trim();
      const secs = msg.timings && msg.timings.first_token_ms && msg.timings.first_think_ms
        ? ` · ${((msg.timings.first_token_ms - msg.timings.first_think_ms) / 1000).toFixed(1)} s` : "";
      const thinking = !!(live && !msg.content);
      $(".think-head", think).textContent = (thinking ? "Thinking…" : "Reasoning") + secs;
      if (think.classList.contains("streaming") !== thinking) think.classList.toggle("open", thinking);   // open while thinking, fold after
      think.classList.toggle("streaming", thinking);
      if (thinking) { const b = $(".think-body", think); b.scrollTop = b.scrollHeight; }
    }
    const ans = $(".answer", node);
    if (msg.noAnswer) {
      const pct = Math.round((msg.noAnswer.best || 0) * 100);
      const t = msg.noAnswer.ru ? NOANSWER_RU : NOANSWER_EN;
      ans.innerHTML = `<div class="noanswer">
        <h4>${t.title}</h4>
        <p>${t.body(pct)}</p>
        <div class="noanswer-acts">
          <button type="button" data-act="new">${t.again}</button>
          <a href="#documents">${t.add} →</a>
        </div></div>`;
    } else if (msg.content) {
      ans.innerHTML = renderMarkdown(msg.content, msg.sources) + (live ? '<span class="cursor"></span>' : "");
    } else if (live && msg.sources) {
      ans.innerHTML = msg.think ? "" : `<span class="waiting">writing…</span>`;
    }
  }

  function addBotFromHistory(msg, index) {
    const node = botShell();
    node.dataset.index = index;
    if (msg.error) { node.classList.add("error"); $(".answer", node).textContent = msg.content; return node; }
    renderBot(node, msg, false);
    fillSources(node, msg.sources, msg.content);
    fillMeta(node, msg);
    return node;
  }

  function renderHistory() {
    $$(".msg", messagesEl).forEach((m) => m.remove());
    history.forEach((m, i) => {                      // older saved chats: work out the language from the question
      if (m.noAnswer && typeof m.noAnswer !== "object") m.noAnswer = {};
      if (m.noAnswer && m.noAnswer.ru === undefined) m.noAnswer.ru = isRu(history[i - 1] && history[i - 1].content);
    });
    $("#empty").hidden = history.length > 0;
    history.forEach((m, i) => messagesEl.append(m.role === "user" ? userBubble(m.content) : addBotFromHistory(m, i)));
    scrollDown(true);
    const last = lastAnswerIndex();
    Evidence.show(last);
    updateRedo();
    renderNext();
    if (last >= 0) Pipe.fromMsg(history[last]); else Pipe.reset();
    setStatus();
  }

  function renderNext() {
    $$(".next", messagesEl).forEach((n) => n.remove());
    if (!history.length || busy) return;
    const asked = new Set(history.filter((m) => m.role === "user").map((m) => m.content));
    const qs = EXAMPLES.filter((q) => !asked.has(q)).slice(0, 2);
    if (!qs.length) return;
    const box = el("div", "next", '<span>Ask next</span>');
    box.setAttribute("role", "group");
    box.setAttribute("aria-label", "Suggested questions");
    qs.forEach((q) => { const b = el("button", "", esc(q)); b.type = "button"; b.addEventListener("click", () => ask(q)); box.append(b); });
    messagesEl.append(box);
  }
  function updateRedo() {
    $$(".msg.bot .redo", messagesEl).forEach((b) => { b.hidden = Number(b.closest(".msg").dataset.index) !== history.length - 1; });
  }
  function lastAnswerIndex() {
    for (let i = history.length - 1; i >= 0; i--) if (history[i].role === "assistant" && !history[i].error) return i;
    return -1;
  }
  const runId = (msg, i) => `#${String(msg && msg.qid ? msg.qid : Math.ceil((i + 1) / 2)).padStart(4, "0")}`;
  function setStatus(text) {
    const s = $("#run-status");
    s.classList.toggle("busy", !!busy);
    if (text) { s.textContent = text; return; }
    const i = lastAnswerIndex();
    s.textContent = i < 0 ? "ready" : `run ${runId(history[i], i)} · ${history[i].noAnswer ? "not in KB" : history[i].stopped ? "stopped" : "passed"}`;
  }

  /* ------------------------------------------------------------ runs (left rail) */
  function chatWhen(t) {
    const d = new Date(t), now = new Date();
    if (d.toDateString() === now.toDateString()) return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    return d.toLocaleDateString([], { day: "numeric", month: "short" });
  }
  function persistChats(dropOldest = () => false) {
    for (;;) {                                       // drop the oldest unpinned chats if storage is full
      try { localStorage.setItem("kb.chats", JSON.stringify(chats)); return; }
      catch { if (!dropOldest()) return; }
    }
  }
  const PIN_SVG = '<svg class="pin-ic" viewBox="0 0 24 24" aria-hidden="true"><path d="M9 4h6l-1 6 4 4H6l4-4zM12 14v7"/></svg>';
  function runRow(c) {
    const n = (c.history || []).filter((m) => m.role === "user").length;
    return `<div class="run${c.id === currentId ? " on" : ""}${c.pinned ? " pinned" : ""}" data-id="${esc(c.id)}" role="listitem">
      <button type="button" class="run-main" title="${esc(c.title)}">
        <small>${chatWhen(c.updated)} · ${n} question${n === 1 ? "" : "s"}</small><span>${esc(c.title)}</span>
      </button>
      <button type="button" class="run-more" aria-label="Chat options" aria-haspopup="menu" title="Options">
        <svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="5" cy="12" r="1.6"/><circle cx="12" cy="12" r="1.6"/><circle cx="19" cy="12" r="1.6"/></svg>
      </button></div>`;
  }
  function renderRuns() {
    const box = $("#runs");
    const live = chats.some((c) => c.id === currentId) || !history.length ? chats.slice() : (() => {
      const draft = { id: currentId, title: (history.find((m) => m.role === "user") || {}).content || "New chat", updated: Date.now(), history };
      const at = chats.filter((c) => c.pinned).length;
      return [...chats.slice(0, at), draft, ...chats.slice(at)];
    })();
    if (!live.length) { box.innerHTML = '<p class="runs-empty">No chats yet. Ask a question to start one.</p>'; return; }
    const q = ($("#run-search").value || "").trim().toLowerCase();
    box.classList.toggle("searching", !!q);
    const shown = q ? live.filter((c) => (c.title || "").toLowerCase().includes(q) || (c.history || []).some((m) => m.role === "user" && m.content.toLowerCase().includes(q))) : live;
    if (!shown.length) { box.innerHTML = `<p class="runs-empty">No chats match “${esc(q)}”.</p>`; return; }
    const pinned = shown.filter((c) => c.pinned), rest = shown.filter((c) => !c.pinned);
    box.innerHTML = pinned.length
      ? `<p class="runs-group" data-group="pinned">${PIN_SVG}Pinned</p>${pinned.map(runRow).join("")}
         <p class="runs-group" data-group="recent">Recent</p>${rest.length ? rest.map(runRow).join("") : '<p class="runs-empty runs-drop">Drag a chat here to unpin it</p>'}`
      : rest.map(runRow).join("");
  }
  $("#run-search").addEventListener("input", () => renderRuns());
  $("#runs").addEventListener("click", (e) => {
    if (RunDrag.justDropped) return;
    const more = e.target.closest(".run-more");
    if (more) { RunMenu.toggle(more.closest(".run").dataset.id, more); return; }
    const b = e.target.closest(".run-main");
    if (!b || b.closest(".run").classList.contains("editing")) return;
    const id = b.closest(".run").dataset.id;
    if (id !== currentId) openChat(id);
  });

  /* chat actions: the ⋯ menu (pin, rename, delete) */
  function startNewChat() {
    if (busy) busy.abort();
    currentId = newId();
    history = [];
    store.set("kb.current", currentId);
    Pipe.reset();
    renderHistory();
    renderRuns();
  }
  const RunMenu = {
    el: null, id: null, anchor: null,
    build() {
      const m = document.createElement("div");
      m.className = "model-menu run-menu";
      m.setAttribute("role", "menu");
      m.hidden = true;
      document.body.appendChild(m);
      m.addEventListener("click", (e) => {
        const b = e.target.closest(".mm-item");
        if (!b) return;
        const id = this.id;
        this.close();
        ({ pin: togglePin, rename: renameChat, delete: deleteChat })[b.dataset.act](id);
      });
      m.addEventListener("keydown", (e) => {
        const items = $$(".mm-item", m), i = items.indexOf(document.activeElement);
        if (e.key === "ArrowDown" || e.key === "ArrowUp") {
          e.preventDefault();
          items[(i + (e.key === "ArrowDown" ? 1 : items.length - 1)) % items.length].focus();
        } else if (e.key === "Escape") { const a = this.anchor; this.close(); a && a.focus(); }
        else if (e.key === "Tab") this.close();
      });
      return (this.el = m);
    },
    toggle(id, anchor) {
      if (this.id === id && !this.el.hidden) { this.close(); return; }
      this.open(id, anchor);
    },
    open(id, anchor) {
      const m = this.el || this.build();
      const chat = chats.find((c) => c.id === id);
      if (!chat) { toast("Ask a question first to keep this chat"); return; }
      this.id = id; this.anchor = anchor;
      m.innerHTML = `
        <button type="button" role="menuitem" class="mm-item" data-act="pin"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9 4h6l-1 6 4 4H6l4-4zM12 14v7"/></svg>${chat.pinned ? "Unpin" : "Pin"}</button>
        <button type="button" role="menuitem" class="mm-item" data-act="rename"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 20h4L19 9l-4-4L4 16zM13.5 6.5l4 4"/></svg>Rename</button>
        <button type="button" role="menuitem" class="mm-item danger" data-act="delete"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13M10 11v6M14 11v6"/></svg>Delete</button>`;
      m.hidden = false;
      $$(".run-more.open").forEach((b) => b.classList.remove("open"));
      anchor.classList.add("open");
      anchor.setAttribute("aria-expanded", "true");
      const r = anchor.getBoundingClientRect(), w = m.offsetWidth, h = m.offsetHeight;
      const top = r.bottom + 6 + h > innerHeight - 8 ? r.top - h - 6 : r.bottom + 6;
      m.style.left = `${Math.max(8, Math.min(r.right - w, innerWidth - w - 8))}px`;
      m.style.top = `${Math.max(8, top)}px`;
      $(".mm-item", m).focus({ preventScroll: true });
    },
    close() {
      if (!this.el || this.el.hidden) return;
      this.el.hidden = true;
      $$(".run-more.open").forEach((b) => { b.classList.remove("open"); b.setAttribute("aria-expanded", "false"); });
      this.id = null; this.anchor = null;
    },
  };
  document.addEventListener("pointerdown", (e) => {
    if (RunMenu.el && !RunMenu.el.hidden && !e.target.closest(".run-menu, .run-more")) RunMenu.close();
  });
  window.addEventListener("resize", () => RunMenu.close());
  $(".rail").addEventListener("scroll", () => RunMenu.close(), true);

  function togglePin(id) {
    const chat = chats.find((c) => c.id === id);
    if (!chat) return;
    chats = chats.filter((c) => c !== chat);
    chat.pinned = !chat.pinned;
    const pinnedCount = chats.filter((c) => c.pinned).length;
    chats.splice(chat.pinned ? pinnedCount : pinnedCount, 0, chat);   // last pinned / first recent
    persistChats();
    renderRuns();
    toast(chat.pinned ? "Chat pinned" : "Chat unpinned");
  }
  function renameChat(id) {
    const row = $(`.run[data-id="${CSS.escape(id)}"]`, $("#runs"));
    const chat = chats.find((c) => c.id === id);
    if (!row || !chat) return;
    row.classList.add("editing");
    const span = $(".run-main span", row);
    const field = document.createElement("input");
    field.className = "run-rename";
    field.value = chat.title;
    field.maxLength = 120;
    field.setAttribute("aria-label", "Chat name");
    span.replaceWith(field);
    field.focus();
    field.select();
    let done = false;
    const finish = (keep) => {
      if (done) return;
      done = true;
      const v = field.value.trim();
      if (keep && v && v !== chat.title) { chat.title = v; chat.renamed = true; persistChats(); }
      renderRuns();
    };
    field.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); finish(true); }
      else if (e.key === "Escape") { e.preventDefault(); finish(false); }
      e.stopPropagation();
    });
    field.addEventListener("click", (e) => e.stopPropagation());
    field.addEventListener("blur", () => finish(true));
  }
  function deleteChat(id) {
    const at = chats.findIndex((c) => c.id === id);
    if (at < 0) return;
    const chat = chats[at], wasCurrent = id === currentId;
    chats.splice(at, 1);
    persistChats();
    if (wasCurrent) startNewChat(); else renderRuns();
    toast("Chat deleted", 5000, {
      label: "Undo",
      run() {
        if (chats.some((c) => c.id === chat.id)) return;
        chats.splice(Math.min(at, chats.length), 0, chat);
        persistChats();
        if (wasCurrent && !history.length) openChat(chat.id);
        renderRuns();
      },
    });
  }

  /* drag to reorder: pointer events, so it works with a mouse and on touch screens */
  const RunDrag = {
    justDropped: false,
    init() {
      const box = $("#runs");
      box.addEventListener("pointerdown", (e) => {
        if (e.button !== 0 || e.target.closest(".run-more, .run-rename")) return;
        const row = e.target.closest(".run");
        if (!row || box.classList.contains("searching") || !chats.some((c) => c.id === row.dataset.id)) return;
        this.arm(e, row, box);
      });
    },
    arm(e, row, box) {
      const startY = e.clientY, startX = e.clientX, pid = e.pointerId;
      let dragging = false, target = null, holdTimer = null, minDy = 0, maxDy = 0, layout = null;
      const touch = e.pointerType === "touch";
      const begin = () => {
        dragging = true;
        RunMenu.close();
        layout = this.measure(box, row);
        minDy = layout.boxTop - layout.rowTop;          // the row stays inside the list while dragged
        maxDy = layout.boxBottom - layout.rowBottom;
        row.setPointerCapture(pid);
        row.classList.add("dragging");
        box.classList.add("sorting");
        if (navigator.vibrate && touch) navigator.vibrate(10);
      };
      if (touch) holdTimer = setTimeout(begin, 280);   // on touch: press and hold, so scrolling still works
      const move = (ev) => {
        if (ev.pointerId !== pid) return;
        if (!dragging) {
          const far = Math.abs(ev.clientY - startY) > 6 || Math.abs(ev.clientX - startX) > 6;
          if (touch) { if (far) cleanup(); return; }
          if (!far) return;
          begin();
        }
        ev.preventDefault();
        const dy = Math.max(minDy, Math.min(maxDy, ev.clientY - startY));
        row.style.transform = `translateY(${dy}px)`;
        target = this.dropTarget(layout, row, layout.rowMid + dy);
        this.makeRoom(layout, target);
        const rail = box.closest(".rail") || box;
        const rr = rail.getBoundingClientRect();
        if (ev.clientY < rr.top + 40) rail.scrollTop -= 8;
        else if (ev.clientY > rr.bottom - 40) rail.scrollTop += 8;
      };
      const up = (ev) => {
        if (ev.pointerId !== pid) return;
        const wasDragging = dragging;
        cleanup();
        if (!wasDragging || !target) return;
        this.justDropped = true;
        setTimeout(() => { this.justDropped = false; }, 0);
        this.drop(row.dataset.id, target);
      };
      const cleanup = () => {
        clearTimeout(holdTimer);
        removeEventListener("pointermove", move);
        removeEventListener("pointerup", up);
        removeEventListener("pointercancel", up);
        row.classList.remove("dragging");
        box.classList.remove("sorting");
        $$(":scope > *", box).forEach((el) => { el.style.transform = ""; });
      };
      addEventListener("pointermove", move, { passive: false });
      addEventListener("pointerup", up);
      addEventListener("pointercancel", up);
    },
    // positions at the start of the drag: decisions use these, not the rows that are sliding around
    measure(box, row) {
      const items = $$(":scope > *", box);
      const r = row.getBoundingClientRect(), b = box.getBoundingClientRect();
      const gap = parseFloat(getComputedStyle(box).rowGap) || 0;
      const rows = items.filter((el) => el.classList.contains("run") && el !== row).map((el) => {
        const rr = el.getBoundingClientRect();
        return { el, id: el.dataset.id, mid: rr.top + rr.height / 2, pinned: el.classList.contains("pinned") };
      });
      const recentHead = $('.runs-group[data-group="recent"]', box);
      return {
        items, rows, recentHead, from: items.indexOf(row), step: r.height + gap,
        recentTop: recentHead ? recentHead.getBoundingClientRect().top : Infinity,
        rowTop: r.top, rowBottom: r.bottom, rowMid: r.top + r.height / 2, boxTop: b.top, boxBottom: b.bottom,
      };
    },
    // where the dragged row would land: before which chat, and in which group
    dropTarget(L, row, y) {
      const group = L.recentHead ? (y < L.recentTop ? "pinned" : "recent") : (row.classList.contains("pinned") ? "pinned" : "recent");
      const inGroup = L.rows.filter((r) => r.pinned === (group === "pinned"));
      const next = inGroup.find((r) => y < r.mid);
      // the element the row is inserted in front of (end of "Pinned" = in front of the "Recent" header)
      const anchor = next ? next.el : group === "pinned" ? L.recentHead : null;
      return { before: next ? next.id : null, group, anchor };
    },
    // slide the other rows (and group headers) by one row height to open a gap where the row will land
    makeRoom(L, target) {
      const to = target.anchor ? L.items.indexOf(target.anchor) : L.items.length;
      L.items.forEach((el, i) => {
        if (i === L.from) return;
        const wasAfter = i > L.from, nowAfter = i >= to;
        const shift = (nowAfter ? 1 : 0) - (wasAfter ? 1 : 0);
        el.style.transform = shift ? `translateY(${shift * L.step}px)` : "";
      });
    },
    drop(id, { before, group }) {
      const chat = chats.find((c) => c.id === id);
      if (!chat) return;
      const pinChanged = chat.pinned !== (group === "pinned");
      chats = chats.filter((c) => c !== chat);
      chat.pinned = group === "pinned";
      let at = before ? chats.findIndex((c) => c.id === before) : -1;
      if (at < 0) at = group === "pinned" ? chats.filter((c) => c.pinned).length : chats.length;
      chats.splice(at, 0, chat);
      persistChats();
      renderRuns();
      if (pinChanged) toast(chat.pinned ? "Chat pinned" : "Chat unpinned");
    },
  };
  RunDrag.init();

  /* ------------------------------------------------------------ evidence (right panel) */
  const Evidence = {
    index: -1,
    msg: null,
    show(i) {
      this.index = i;
      const msg = history[i];
      this.msg = msg || null;
      $$(".msg.bot", messagesEl).forEach((n) => n.classList.toggle("on", Number(n.dataset.index) === i));
      const list = $("#ev-list"), map = $("#ev-minimap");
      const sources = (msg && msg.sources) || [];
      const cited = new Set([...((msg && msg.content) || "").matchAll(/\[(\d{1,2})\]/g)].map((m) => Number(m[1])));
      const row = (s, n, unused) => {
        const pct = Math.round(s.score * 100);
        return `<li class="src${unused ? " unused" : ""}" data-n="${n}"><button type="button" aria-expanded="false"><span class="n">${n}</span>
          <span class="name"><small>${esc(s.source)}</small><span>${esc(s.heading)}</span></span>
          <span class="score"><i style="--w:${pct}%"></i>${pct}%</span></button>
          <p class="body">${esc(s.body)}</p></li>`;
      };
      const numbered = sources.map((s, k) => ({ s, n: k + 1 }));
      const used = cited.size ? numbered.filter((x) => cited.has(x.n)) : numbered;
      const rest = cited.size ? numbered.filter((x) => !cited.has(x.n)) : [];
      $("#ev-title").textContent = !sources.length ? "Sources" : cited.size ? `Sources · ${used.length} cited` : `Sources · ${sources.length}`;
      $("#ev-map").hidden = !sources.length;
      $("#ev-empty").hidden = !!sources.length;
      $("#ev-empty").textContent = msg && msg.noAnswer
        ? "No sources were close enough to this question, so nothing was used for the answer."
        : "Sources appear here with each answer: the passages the assistant read, how closely each one matches your question, and where they sit on the knowledge map.";
      list.innerHTML = used.map((x) => row(x.s, x.n, false)).join("") +
        (rest.length ? `<li class="ev-sep" aria-hidden="true">Also retrieved · not used</li>` + rest.map((x) => row(x.s, x.n, true)).join("") : "");
      map.innerHTML = "";
      if (sources.length && msg.point) this.minimap(msg);
      renderRuns();
    },
    async minimap(msg) {
      try { await MapData.load(); } catch { return; }
      if (this.msg !== msg) return;
      const pos = (p) => ({ x: 3 + Math.max(0, Math.min(1, p.x)) * 94, y: 4 + (1 - Math.max(0, Math.min(1, p.y))) * 91 });
      const q = pos(msg.point);
      let html = MapData.points.map((p) => { const c = pos(p); return `<span class="d" style="left:${c.x}%;top:${c.y}%"></span>`; }).join("");
      let rays = "";
      msg.sources.forEach((s, k) => {
        const p = MapData.byId.get(s.id);
        if (!p) return;
        const c = pos(p);
        rays += `<line x1="${q.x}" y1="${q.y}" x2="${c.x}" y2="${c.y}" vector-effect="non-scaling-stroke"/>`;
        html += `<span class="h" style="left:${c.x}%;top:${c.y}%"></span><span class="hn" style="left:${c.x}%;top:${c.y}%">${k + 1}</span>`;
      });
      html += `<svg viewBox="0 0 100 100" preserveAspectRatio="none">${rays}</svg>
        <span class="qr" style="left:${q.x}%;top:${q.y}%"></span><span class="q" style="left:${q.x}%;top:${q.y}%"></span>
        <span class="cap">◆ your question</span>`;
      $("#ev-minimap").innerHTML = html;
    },
    open(n) {
      const row = $(`#ev-list .src[data-n="${n}"]`);
      if (!row) return;
      $$("#ev-list .src.open").forEach((r) => { if (r !== row) { r.classList.remove("open"); $("button", r).setAttribute("aria-expanded", "false"); } });
      row.classList.add("open", "flash");
      $("button", row).setAttribute("aria-expanded", "true");
      row.scrollIntoView({ behavior: "smooth", block: "nearest" });
      setTimeout(() => row.classList.remove("flash"), 1200);
    },
  };
  $("#ev-list").addEventListener("click", (e) => {
    const b = e.target.closest(".src > button");
    if (!b) return;
    const open = b.parentElement.classList.toggle("open");
    b.setAttribute("aria-expanded", String(open));
  });
  $("#ev-map").addEventListener("click", () => {
    const i = Evidence.index, msg = Evidence.msg;
    if (!msg) return;
    const q = history[i - 1];
    lastHit = { query: msg.rewrite || (q && q.content) || "question", point: msg.point, sources: msg.sources };
    location.hash = "map";
  });

  function save(id = currentId, hist = history) {
    let chat = chats.find((c) => c.id === id);
    if (!hist.length) {                           // an empty chat is not worth keeping
      if (chat) chats = chats.filter((c) => c !== chat);
    } else {
      if (!chat) {                                   // a new chat goes to the top of "Recent", under the pinned ones
        chat = { id };
        chats.splice(chats.filter((c) => c.pinned).length, 0, chat);
      }
      const first = hist.find((m) => m.role === "user");
      if (!chat.renamed) chat.title = first ? first.content : "New chat";
      chat.updated = Date.now();
      chat.history = hist.slice(-40);
    }
    // the order is the user's: no re-sorting by activity. Over 30 chats, the oldest unpinned ones go.
    const dropOldest = () => {
      const loose = chats.filter((c) => !c.pinned && c.id !== currentId);
      if (!loose.length) return false;
      const oldest = loose.reduce((a, b) => (a.updated <= b.updated ? a : b));
      chats = chats.filter((c) => c !== oldest);
      return true;
    };
    while (chats.filter((c) => !c.pinned).length > 30 && dropOldest());
    persistChats(dropOldest);
    store.set("kb.current", currentId);
    try { localStorage.removeItem("kb.history"); } catch { /* ignore */ }
    renderRuns();
  }
  function openChat(id) {
    if (busy) busy.abort();
    currentId = id;
    const chat = chats.find((c) => c.id === id);
    history = chat ? chat.history.slice() : [];
    store.set("kb.current", currentId);
    renderHistory();
  }

  /* ------------------------------------------------------------ streaming */
  async function* sseEvents(response) {
    const reader = response.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let i;
      while ((i = buf.indexOf("\n\n")) >= 0) {
        const raw = buf.slice(0, i);
        buf = buf.slice(i + 2);
        let event = "message", data = "";
        raw.split("\n").forEach((line) => {
          if (line.startsWith("event:")) event = line.slice(6).trim();
          else if (line.startsWith("data:")) data += line.slice(5).trim();
        });
        if (data) yield { event, data: JSON.parse(data) };
      }
    }
  }

  async function ask(text) {
    text = text.trim();
    if (!text || busy) return;
    $("#empty").hidden = true;
    $$(".next", messagesEl).forEach((n) => n.remove());
    history.push({ role: "user", content: text });
    const chatId = currentId, chatHist = history;     // the chat this question belongs to
    messagesEl.append(userBubble(text));
    const msg = { role: "assistant", content: "", think: "", sources: null, timings: {} };
    const node = botShell();
    messagesEl.append(node);
    scrollDown(true);

    setBusy(true);
    renderRuns();
    setStatus("running…");
    Pipe.reset();
    Pipe.set("retrieve", "active", "embedding…");
    const payload = {
      // skip failed turns and "not in the knowledge base" turns (and the questions that led to them)
      messages: history.filter((m, i) => !m.error && !m.noAnswer && !(history[i + 1] && (history[i + 1].noAnswer || history[i + 1].error)))
        .map((m) => ({ role: m.role, content: m.content })),
      think: $("#think-toggle").checked,
      tone: settings.tone,
      top_k: settings.top_k,
      model: settings.model || undefined,
    };

    let raf = 0;
    const paint = () => { raf = 0; renderBot(node, msg, true); scrollDown(); };
    const schedule = () => { if (!raf) raf = requestAnimationFrame(paint); };

    try {
      const r = await fetch("/kb/chat/stream", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload), signal: busy.signal,
      });
      if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
      for await (const { event, data } of sseEvents(r)) {
        if (event === "rewrite") {
          msg.rewrite = data.query;
          schedule();
        } else if (event === "sources") {
          msg.sources = data.sources;
          msg.point = data.point;
          msg.timings.retrieve_ms = data.retrieve_ms;
          Pipe.set("retrieve", "done", fmtMs(data.retrieve_ms));
          Pipe.set("augment", "done", `${data.sources.length} chunks`);
          Pipe.set("generate", "active", "streaming…");
          fillSources(node, msg.sources);
          lastHit = { query: msg.rewrite || text, point: data.point, sources: data.sources };
          history.push(msg);                 // provisional, so the evidence panel can show it now
          node.dataset.index = history.length - 1;
          Evidence.show(history.length - 1);
          history.pop();
          schedule();
        } else if (event === "think") {
          msg.think += data.t;
          if (!msg.timings.first_think_ms) msg.timings.first_think_ms = 1;
          schedule();
        } else if (event === "no_answer") {
          msg.noAnswer = { best: data.best_score, min: data.min_score, ru: isRu(text) };
          Pipe.set("augment", "done", "0 chunks");
          Pipe.set("generate", "done", "not in KB");
          schedule();
        } else if (event === "token") {
          msg.content += data.t;
          schedule();
        } else if (event === "done") {
          Object.assign(msg.timings, data.timings);
          msg.qid = data.query_id;
          msg.model = data.model;
          msg.tokens = data.tokens;
          msg.tone = settings.tone;
          if (!msg.noAnswer) Pipe.set("generate", "done", `${fmtMs(data.timings.total_ms)} · ${data.tokens} tok`);
        } else if (event === "error") {
          throw new Error(data.detail);
        }
      }
      if (!msg.content && !msg.noAnswer) msg.content = "_The model returned an empty answer._";
    } catch (e) {
      if (e.name === "AbortError") {
        msg.content = (msg.content || "") + "\n\n_— stopped —_";
        msg.stopped = true;
        Pipe.set("generate", "fail", "stopped");
      } else {
        msg.error = true;
        msg.content = `Something went wrong: ${e.message}`;
        node.classList.add("error");
        $$("#pipeline li.active").forEach((li) => { li.className = "fail"; $("small", li).textContent = "error"; });
      }
    } finally {
      if (raf) cancelAnimationFrame(raf);
      if (msg.error) {
        $(".answer", node).textContent = msg.content;
      } else {
        renderBot(node, msg, false);
        fillSources(node, msg.sources, msg.content);
        fillMeta(node, msg);
      }
      if (chatId !== currentId) {                  // user switched chats while this was running
        chatHist.push(msg);
        save(chatId, chatHist);
        setBusy(false);
        return;
      }
      history.push(msg);
      node.dataset.index = history.length - 1;
      save();
      setBusy(false);
      if (!msg.error) Evidence.show(history.length - 1); else renderRuns();
      setStatus(msg.error ? "run failed" : undefined);
      updateRedo();
      renderNext();
      scrollDown();
      loadStats();
    }
  }

  function setBusy(on) {
    const btn = $("#ask");
    busy = on ? new AbortController() : null;
    $("span", btn).textContent = on ? "Stop" : "Ask";
    btn.classList.toggle("stop", on);
    $("#motif").classList.toggle("busy", on);
  }

  /* ------------------------------------------------------------ chat events */
  const input = $("#input");
  const autosize = () => { input.style.height = "auto"; input.style.height = `${Math.min(input.scrollHeight, 160)}px`; };
  input.addEventListener("input", autosize);
  /* iOS scrolls the whole page up to show the keyboard and leaves it there — put it back */
  const unshift = () => { if (matchMedia("(max-width: 700px)").matches && $("#view-assistant").classList.contains("on")) window.scrollTo(0, 0); };
  input.addEventListener("blur", () => setTimeout(unshift, 50));
  window.visualViewport && visualViewport.addEventListener("resize", () => { if (document.activeElement !== input) unshift(); });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); $("#composer").requestSubmit(); }
  });
  $("#composer").addEventListener("submit", (e) => {
    e.preventDefault();
    if (busy) { busy.abort(); return; }
    const text = input.value;
    input.value = "";
    autosize();
    ask(text);
  });
  $("#m-new").addEventListener("click", () => { location.hash = "assistant"; $("#new-chat").click(); });
  $("#new-chat").addEventListener("click", () => {
    if (busy) busy.abort();
    save();                                          // keep the current chat in the list
    currentId = newId();
    history = [];
    store.set("kb.current", currentId);
    Pipe.reset();
    renderHistory();
    renderRuns();
    input.focus();
  });
  /* settings: tone, number of sources, model */
  const settings = Object.assign({ tone: "formal", top_k: 5, model: "" }, store.get("kb.settings", {}));
  const saveSettings = () => store.set("kb.settings", settings);
  function paintSettings() {
    $$("#tone button").forEach((b) => { const on = b.dataset.v === settings.tone; b.classList.toggle("on", on); b.setAttribute("aria-checked", String(on)); });
    $("#topk-dec").disabled = settings.top_k <= 1;
    $("#topk-inc").disabled = settings.top_k >= 10;
    $("#topk").value = settings.top_k;
    $("#topk-val").textContent = settings.top_k;
    const tone = settings.tone.charAt(0).toUpperCase() + settings.tone.slice(1);
    $("#style-summary").textContent = `${tone} · ${settings.top_k}`;
    $("#style-btn").title = `Answer style: ${tone}, up to ${settings.top_k} source${settings.top_k === 1 ? "" : "s"}`;
  }
  /* answer style: a second chip in the composer, same pop-up as the model menu */
  $("#style-btn").addEventListener("click", (e) => { e.stopPropagation(); setMenu("style", $("#style-menu").hidden); });
  $("#style-menu").addEventListener("click", (e) => e.stopPropagation());
  $("#style-menu").addEventListener("keydown", (e) => { if (e.key === "Escape") { setMenu("style", false); $("#style-btn").focus(); } });
  $("#tone").addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (b) { settings.tone = b.dataset.v; saveSettings(); paintSettings(); }
  });
  const stepTopk = (d) => { settings.top_k = Math.min(10, Math.max(1, settings.top_k + d)); saveSettings(); paintSettings(); };
  $("#topk-dec").addEventListener("click", () => stepTopk(-1));
  $("#topk-inc").addEventListener("click", () => stepTopk(1));
  $("#topk").addEventListener("input", (e) => { settings.top_k = Number(e.target.value); saveSettings(); paintSettings(); });
  /* model picker: a chip inside the composer with a pop-up menu, like ChatGPT / Gemini */
  const Models = { list: [], def: "" };
  function modelHint(m) {
    const size = parseFloat((m.match(/(\d+(?:\.\d+)?)b\b/i) || [])[1]);
    if (!size) return "Local model";
    if (size <= 2) return "Fastest · good for quick answers";
    if (size <= 5) return "Balanced · smarter, a bit slower";
    return "Most capable · slowest";
  }
  const currentModel = () => settings.model || Models.def;
  function paintModels() {
    $("#model-name").textContent = currentModel() || "model";
    const menu = $("#model-menu");
    menu.innerHTML = `<p class="mm-head">Model</p>` + Models.list.map((m) => `
      <button type="button" role="option" class="mm-item${m === currentModel() ? " on" : ""}" aria-selected="${m === currentModel()}" data-m="${esc(m)}">
        <span class="mm-text"><b>${esc(m)}${m === Models.def ? '<i class="mm-tag">default</i>' : ""}</b><small>${modelHint(m)}</small></span>
        <svg class="mm-check" viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12.5l4.5 4.5L19 7.5"/></svg>
      </button>`).join("") +
      (Models.list.length < 2 ? `<p class="mm-foot">Pull more models into Ollama on the server and they appear here.</p>` : "");
  }
  function setMenu(name, open) {                    // name: "model" | "style"; only one menu open at a time
    ["model", "style"].forEach((n) => {
      const on = open && n === name;
      $(`#${n}-menu`).hidden = !on;
      $(`#${n}-btn`).setAttribute("aria-expanded", String(on));
      $(`#${n}-btn`).parentElement.classList.toggle("open", on);
    });
    if (open && name === "model") { const on = $("#model-menu .mm-item.on") || $("#model-menu .mm-item"); on && on.focus(); }
  }
  const setModelMenu = (open) => setMenu("model", open);
  $("#model-btn").addEventListener("click", (e) => { e.stopPropagation(); setModelMenu($("#model-menu").hidden); });
  $("#model-menu").addEventListener("click", (e) => {
    e.stopPropagation();
    const b = e.target.closest(".mm-item");
    if (!b) return;
    settings.model = b.dataset.m === Models.def ? "" : b.dataset.m;
    saveSettings(); paintModels(); setModelMenu(false); input.focus();
  });
  $("#model-menu").addEventListener("keydown", (e) => {
    const items = $$("#model-menu .mm-item"), i = items.indexOf(document.activeElement);
    if (e.key === "ArrowDown" || e.key === "ArrowUp") { e.preventDefault(); const n = items[(i + (e.key === "ArrowDown" ? 1 : -1) + items.length) % items.length]; n && n.focus(); }
    if (e.key === "Escape") { setModelMenu(false); $("#model-btn").focus(); }
  });
  document.addEventListener("click", () => { if (!$("#model-menu").hidden || !$("#style-menu").hidden) setMenu("model", false); });
  async function loadModels() {
    try {
      const r = await api("/kb/models");
      Models.list = r.models; Models.def = r.default;
      if (settings.model && !r.models.includes(settings.model)) { settings.model = ""; saveSettings(); }
      paintModels();
    } catch (e) { toast(`Models: ${e.message}`); }
  }
  paintSettings();
  loadModels();

  $("#think-toggle").checked = store.get("kb.think", true);
  $("#think-toggle").addEventListener("change", (e) => store.set("kb.think", e.target.checked));

  EXAMPLES.forEach((q) => {
    const b = el("button", "", esc(q));
    b.type = "button";
    b.addEventListener("click", () => ask(q));
    $("#examples").append(b);
  });

  messagesEl.addEventListener("click", async (e) => {
    const node = e.target.closest(".msg.bot");
    if (!node) return;
    const msg = history[Number(node.dataset.index)];
    const index = Number(node.dataset.index);
    const cite = e.target.closest(".cite");
    if (cite) {
      if (Evidence.index !== index) Evidence.show(index);
      Evidence.open(cite.dataset.n);
      Sheet.open();
      return;
    }
    const th = e.target.closest(".think-head");
    if (th) { th.parentElement.classList.toggle("open"); return; }
    const act = e.target.closest("[data-act]");
    if (act && act.dataset.act === "new") { input.value = ""; autosize(); input.focus(); return; }
    if (!act || !msg) return;
    if (act.dataset.act === "evidence") {
      Evidence.show(index);
      Pipe.fromMsg(msg);
      Sheet.open();
    } else if (act.dataset.act === "redo") {
      if (busy || index !== history.length - 1) return;
      const q = history[index - 1];
      history.splice(index - 1, 2);
      save();
      renderHistory();
      if (q) ask(q.content);
    } else if (act.dataset.act === "copy") {
      try { await navigator.clipboard.writeText(msg.noAnswer ? (msg.noAnswer.ru ? NOANSWER_RU : NOANSWER_EN).title + "." : msg.content); toast("Answer copied"); } catch { toast("Copy is not available here"); }
    } else if (act.dataset.act === "up" || act.dataset.act === "down") {
      const rating = act.dataset.act === "up" ? 1 : -1;
      try {
        await api(`/kb/feedback/${msg.qid}`, {
          method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ rating }),
        });
        msg.rating = rating;
        save();
        fillMeta(node, msg);
        toast(rating === 1 ? "Thanks! Marked as helpful" : "Thanks! Marked as not helpful");
      } catch (err) { toast(err.message); }
    }
  });

  const Sheet = {
    open() { if (matchMedia("(max-width: 700px)").matches) { $("#evidence").classList.add("sheet-open"); document.body.classList.add("sheet-lock"); } },
    close() { $("#evidence").classList.remove("sheet-open"); document.body.classList.remove("sheet-lock"); },
  };
  $("#ev-close").addEventListener("click", () => Sheet.close());

  /* ------------------------------------------------------------ knowledge map */
  const MapView = (() => {
    const W = 1000, H = 720, PAD = 48;
    const svg = $("#map");
    const stage = svg.parentElement;
    const tip = $("#map-tip");
    const NS = "http://www.w3.org/2000/svg";
    let points = [];
    let byId = new Map();
    let loaded = false;
    let selectedDoc = null;
    let shownHit = null;
    let vb = { x: 0, y: 0, w: W, h: H };
    const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
    const narrow = () => { const w = svg.clientWidth || W; return clamp(W / w * 0.55, 1, 2.2); };
    const k = () => (vb.w / W) * narrow();          // marker scale: follows zoom, larger on phones

    const sx = (x) => PAD + clamp(x, -0.05, 1.05) * (W - 2 * PAD);
    const sy = (y) => H - PAD - clamp(y, -0.05, 1.05) * (H - 2 * PAD);
    const node = (tag, attrs, parent) => {
      const n = document.createElementNS(NS, tag);
      Object.entries(attrs).forEach(([a, v]) => n.setAttribute(a, v));
      if (parent) parent.append(n);
      return n;
    };

    async function load(force) {
      if (loaded && !force && MapData.loaded) return;
      await MapData.load(force);
      points = MapData.points;
      byId = MapData.byId;
      loaded = true;
      draw();
      legend();
    }

    /* ---- view: zoom + pan through the viewBox ---- */
    function applyView() {
      svg.setAttribute("viewBox", `${vb.x} ${vb.y} ${vb.w} ${vb.h}`);
      svg.style.setProperty("--k", k().toFixed(4));
      clusters();
      overlay(false);
    }
    function zoom(f, cx = vb.x + vb.w / 2, cy = vb.y + vb.h / 2) {
      const nw = clamp(vb.w * f, W / 8, W);   // zoom range
      const r = nw / vb.w;
      vb = { x: cx - (cx - vb.x) * r, y: cy - (cy - vb.y) * r, w: nw, h: nw * H / W };
      pan(0, 0);
    }
    function pan(dx, dy) {
      vb.x = clamp(vb.x + dx, -W * 0.05, W * 1.05 - vb.w);
      vb.y = clamp(vb.y + dy, -H * 0.05, H * 1.05 - vb.h);
      applyView();
    }
    function toSvg(e) {
      const m = svg.getScreenCTM();
      return m ? new DOMPoint(e.clientX, e.clientY).matrixTransform(m.inverse()) : { x: W / 2, y: H / 2 };
    }
    stage.addEventListener("wheel", (e) => {
      if (!loaded) return;
      e.preventDefault();
      const p = toSvg(e);
      zoom(e.deltaY > 0 ? 1.15 : 1 / 1.15, p.x, p.y);
    }, { passive: false });
    let drag = null;
    stage.addEventListener("pointerdown", (e) => {
      if (e.button !== 0) return;
      drag = { x: e.clientX, y: e.clientY, moved: false, id: e.pointerId };
    });
    stage.addEventListener("pointermove", (e) => {
      if (!drag) return;
      const dx = e.clientX - drag.x, dy = e.clientY - drag.y;
      if (!drag.moved && Math.hypot(dx, dy) < 4) return;
      if (!drag.moved) { drag.moved = true; stage.setPointerCapture(drag.id); stage.classList.add("dragging"); tip.hidden = true; }
      const m = svg.getScreenCTM();
      const unit = m ? 1 / m.a : 1;
      drag.x = e.clientX; drag.y = e.clientY;
      pan(-dx * unit, -dy * unit);
    });
    const endDrag = () => {
      if (drag && drag.moved) { stage.classList.remove("dragging"); setTimeout(() => { drag = null; }, 0); } else drag = null;
    };
    stage.addEventListener("pointerup", endDrag);
    stage.addEventListener("pointercancel", endDrag);
    $("#zoom-in").addEventListener("click", () => zoom(1 / 1.4));
    $("#zoom-out").addEventListener("click", () => zoom(1.4));
    $("#zoom-reset").addEventListener("click", () => { vb = { x: 0, y: 0, w: W, h: H }; highlightDoc(null); applyView(); });

    /* ---- drawing ---- */
    function draw() {
      svg.innerHTML = "";
      ["cross", "labels", "rays", "dots", "hits", "query"].forEach((id) => node("g", { id: `m-${id}` }, svg));
      const dots = $("#m-dots", svg);
      points.forEach((p) => {
        const c = node("circle", { class: "dot", cx: sx(p.x), cy: sy(p.y) }, dots);
        c.dataset.id = p.id;
        c.dataset.source = p.source;
      });
      applyView();
      highlightDoc(selectedDoc);
      count();
    }

    function docGroups() {
      const g = {};
      points.forEach((p) => { (g[p.source] = g[p.source] || []).push(p); });
      return g;
    }

    function clusters() {
      const box = $("#m-labels", svg);
      if (!box) return;
      box.innerHTML = "";
      const med = (a) => { const s = a.slice().sort((x, y) => x - y); return s[Math.floor(s.length / 2)]; };
      Object.entries(docGroups()).filter(([src, ps]) => ps.length >= 7 || src === selectedDoc).forEach(([src, ps]) => {
        const t = node("text", { class: `cluster${src === selectedDoc ? " on" : ""}`, x: sx(med(ps.map((p) => p.x))),
          y: sy(Math.max(...ps.map((p) => p.y))) - 16 * k(), "text-anchor": "middle" }, box);
        t.dataset.source = src;
        t.textContent = src;
      });
    }

    function count() {
      const docs = Object.keys(docGroups()).length;
      $("#map-count").textContent = `${points.length} chunks · ${docs} documents · ${selectedDoc ? `isolating ${selectedDoc}` : "all documents"}`;
    }

    function legend() {
      const ul = $("#legend");
      ul.innerHTML = "";
      Object.entries(docGroups()).sort((a, b) => b[1].length - a[1].length).forEach(([src, ps]) => {
        const li = el("li", src === selectedDoc ? "on" : "", `<span>${esc(src)}</span><small>${ps.length}</small>`);
        li.dataset.source = src;
        li.setAttribute("role", "button");
        li.setAttribute("aria-pressed", String(src === selectedDoc));
        ul.append(li);
      });
    }

    function highlightDoc(src) {
      selectedDoc = src;
      svg.classList.toggle("focus", !!src);
      $$(".dot", svg).forEach((d) => d.classList.toggle("hl", !!src && d.dataset.source === src));
      clusters();
      $$("#legend li").forEach((li) => { li.classList.toggle("on", li.dataset.source === src); li.setAttribute("aria-pressed", String(li.dataset.source === src)); });
      count();
    }

    // question marker, crosshair, rays and numbered hits; sizes follow the zoom
    function overlay(animate) {
      const cross = $("#m-cross", svg), rays = $("#m-rays", svg), hits = $("#m-hits", svg), q = $("#m-query", svg);
      if (!q) return;
      [cross, rays, hits, q].forEach((g) => { g.innerHTML = ""; });
      $$(".dot.hit", svg).forEach((d) => d.classList.remove("hit"));
      const h = shownHit;
      if (!h || !h.point) return;
      const s = k();
      const qx = sx(h.point.x), qy = sy(h.point.y);
      node("line", { class: "cross", x1: -W, x2: 2 * W, y1: qy, y2: qy }, cross);
      node("line", { class: "cross", x1: qx, x2: qx, y1: -H, y2: 2 * H }, cross);
      (h.sources || []).forEach((src, i) => {
        const p = byId.get(src.id);
        if (!p) return;
        const x = sx(p.x), y = sy(p.y);
        const line = node("line", { class: "ray", x1: qx, y1: qy, x2: x, y2: y }, rays);
        if (animate) line.animate([{ opacity: 0 }, { opacity: 1 }], { duration: 400, delay: i * 90, fill: "both" });
        node("circle", { class: "hit-glow", cx: x, cy: y }, hits);
        node("circle", { class: "hit-dot", cx: x, cy: y }, hits);
        const tag = node("g", { class: "hit-tag" }, hits);
        node("rect", { x: x + 8 * s, y: y - 26 * s, width: 17 * s, height: 16 * s }, tag);
        const t = node("text", { x: x + 16.5 * s, y: y - 14 * s, "text-anchor": "middle" }, tag);
        t.textContent = i + 1;
        const dot = $(`.dot[data-id="${src.id}"]`, svg);
        if (dot) dot.classList.add("hit");
      });
      const d = 10 * s;
      node("rect", { class: "q-ring", x: qx - 11 * s, y: qy - 11 * s, width: 22 * s, height: 22 * s }, q);
      node("rect", { class: "q", x: qx - d, y: qy - d, width: 2 * d, height: 2 * d, transform: `rotate(45 ${qx} ${qy})` }, q);
      const label = `“${h.query.length > 48 ? `${h.query.slice(0, 46)}…` : h.query}”`;
      const pill = node("g", { class: "q-pill" }, q);
      const tw = label.length * 7.9 * s, ph = 30 * s, pw = tw + 20 * s;
      let px = clamp(qx - pw / 2, vb.x + 8 * s, vb.x + vb.w - pw - 8 * s);
      const py = qy + 22 * s + ph > vb.y + vb.h ? qy - 22 * s - ph : qy + 22 * s;
      node("rect", { class: "shadow", x: px + 4 * s, y: py + 4 * s, width: pw, height: ph }, pill);
      node("rect", { x: px, y: py, width: pw, height: ph }, pill);
      const t = node("text", { x: px + pw / 2, y: py + ph / 2 + 4.5 * s, "text-anchor": "middle" }, pill);
      t.textContent = label;
    }

    function hit(h, animate = true) {
      shownHit = h;
      overlay(animate);
      showDetail(h);
    }

    function srcRows(sources) {
      return `<ol class="src-list ev-list">${(sources || []).map((s, i) => {
        const pct = Math.round(s.score * 100);
        return `<li class="src" data-id="${s.id}"><button type="button" aria-expanded="false"><span class="n">${i + 1}</span>
          <span class="name"><small>${esc(s.source)}</small><span>${esc(s.heading)}</span></span>
          <span class="score"><i style="--w:${pct}%"></i>${pct}%</span></button>
          <p class="body">${esc(s.body)}</p></li>`;
      }).join("")}</ol>`;
    }

    function showDetail(h) {
      const box = $("#map-detail");
      if (!h) { box.hidden = true; return; }
      box.hidden = false;
      box.innerHTML = `<h4>Nearest chunks</h4><h5>“${esc(h.query)}”</h5>${srcRows(h.sources)}`;
    }

    async function showChunk(id) {
      const box = $("#map-detail");
      try {
        const c = await api(`/kb/${id}`);
        box.hidden = false;
        const back = shownHit ? '<button type="button" class="link" id="map-back">◂ nearest chunks</button>' : "";
        box.innerHTML = `<div class="side-title"><h4>${esc(c.source)} · #${c.id}</h4>${back}</div><h5>${esc(c.heading)}</h5><p>${esc(c.body)}</p>`;
        $$(".dot.sel", svg).forEach((d) => d.classList.remove("sel"));
        const dot = $(`.dot[data-id="${id}"]`, svg);
        if (dot) dot.classList.add("sel");
      } catch (e) { toast(e.message); }
    }

    svg.addEventListener("mousemove", (e) => {
      const d = e.target.closest(".dot");
      if (!d || (drag && drag.moved)) { tip.hidden = true; return; }
      const p = byId.get(Number(d.dataset.id));
      const hitIdx = shownHit ? (shownHit.sources || []).findIndex((s) => s.id === p.id) : -1;
      const extra = hitIdx >= 0 ? ` · #${hitIdx + 1} · ${Math.round(shownHit.sources[hitIdx].score * 100)}%` : ` · #${p.id}`;
      const box = stage.getBoundingClientRect();
      tip.innerHTML = `<small>${esc(p.source)}${extra}</small>${esc(p.heading)}`;
      tip.hidden = false;
      const x = e.clientX - box.left + 14, y = e.clientY - box.top + 14;
      tip.style.left = `${Math.min(x, box.width - 270)}px`;
      tip.style.top = `${Math.min(y, box.height - 70)}px`;
    });
    svg.addEventListener("mouseleave", () => { tip.hidden = true; });
    svg.addEventListener("click", (e) => {
      if (drag && drag.moved) return;
      const d = e.target.closest(".dot");
      if (d) showChunk(Number(d.dataset.id));
    });
    $("#legend").addEventListener("click", (e) => {
      const li = e.target.closest("li");
      if (li) highlightDoc(selectedDoc === li.dataset.source ? null : li.dataset.source);
    });
    $("#map-detail").addEventListener("click", (e) => {
      if (e.target.closest("#map-back")) { showDetail(shownHit); $$(".dot.sel", svg).forEach((d) => d.classList.remove("sel")); return; }
      const b = e.target.closest(".src > button");
      if (b) { const open = b.parentElement.classList.toggle("open"); b.setAttribute("aria-expanded", String(open)); }
    });
    $("#map-search").addEventListener("submit", async (e) => {
      e.preventDefault();
      const q = $("#map-query").value.trim();
      if (!q) return;
      try {
        await load();
        const r = await api(`/kb/retrieve?query=${encodeURIComponent(q)}&limit=5`);
        highlightDoc(null);
        hit({ query: q, point: r.point, sources: r.sources });
        $("#map-hint").textContent = `located in ${fmtMs(r.ms)} · embedding + cosine search · drag to pan · scroll to zoom`;
      } catch (err) { toast(err.message); }
    });

    let pendingIsolate = null;
    return {
      async open() {
        try {
          await load();
          if (pendingIsolate) {
            const src = pendingIsolate;
            pendingIsolate = null;
            shownHit = null; overlay(false); showDetail(null);
            vb = { x: 0, y: 0, w: W, h: H }; applyView();
            highlightDoc(src);
            return;
          }
          if (lastHit && lastHit !== shownHit) { highlightDoc(null); hit(lastHit); }
        } catch (e) { toast(`Map: ${e.message}`); }
      },
      isolate(src) { pendingIsolate = src; },
      refresh() { if (loaded) applyView(); },
      invalidate() { loaded = false; MapData.invalidate(); },
    };
  })();

  /* ------------------------------------------------------------ documents */
  const Docs = (() => {
    let docs = [];
    let current = null;
    let sort = store.get("kb.docsort", "chunks");
    const fresh = new Set();
    const fmtK = (n) => (n >= 1000 ? `${(n / 1000).toFixed(n >= 100000 ? 0 : 1)}K` : String(n));

    async function load() {
      try {
        docs = await api("/kb/documents");
        const chunks = docs.reduce((s, d) => s + d.chunks, 0);
        const chars = docs.reduce((s, d) => s + d.chars, 0);
        $("#ds-files").textContent = docs.length;
        $("#ds-chunks").textContent = chunks;
        $("#ds-chars").textContent = fmtK(chars);
        if (!current || !docs.some((d) => d.source === current)) current = docs.length ? largest().source : null;
        render();
        inspect(current);
      } catch (e) { toast(e.message); }
    }
    const largest = () => docs.slice().sort((a, b) => b.chunks - a.chunks)[0];

    function render() {
      const q = $("#doc-filter").value.trim().toLowerCase();
      const total = docs.reduce((s, d) => s + d.chunks, 0) || 1;
      const max = Math.max(1, ...docs.map((d) => d.chunks));
      const rows = docs.filter((d) => d.source.toLowerCase().includes(q)).sort((a, b) =>
        sort === "name" ? a.source.localeCompare(b.source)
        : sort === "size" ? b.chars - a.chars
        : b.chunks - a.chunks || a.source.localeCompare(b.source));
      $$("#doc-sort button").forEach((b) => { const on = b.dataset.v === sort; b.classList.toggle("on", on); b.setAttribute("aria-pressed", String(on)); });
      $("#docs").innerHTML = rows.length ? rows.map((d, i) => `
        <tr data-source="${esc(d.source)}" class="${d.source === current ? "on" : ""}${fresh.has(d.source) ? " new" : ""}">
          <td class="idx">${String(i + 1).padStart(2, "0")}</td>
          <td><button type="button">${esc(d.source)}</button></td>
          <td><div class="bar-cell"><i style="--w:${(d.chunks / max * 100).toFixed(1)}%"></i><b>${d.chunks}</b></div></td>
          <td class="num">${fmtK(d.chars)}</td>
          <td class="num dim">${(d.chunks / total * 100).toFixed(1)}%</td>
        </tr>`).join("") : `<tr class="none"><td colspan="5">No documents match “${esc(q)}”</td></tr>`;
    }

    async function inspect(source) {
      current = source;
      $$("#docs tr").forEach((tr) => tr.classList.toggle("on", tr.dataset.source === source));
      const d = docs.find((x) => x.source === source);
      $("#insp-acts").hidden = !d;
      if (!d) {
        $("#insp-title").textContent = "Select a document";
        $("#insp-meta").textContent = "click a row in the table";
        $("#insp-body").innerHTML = "";
        return;
      }
      const total = docs.reduce((s, x) => s + x.chunks, 0) || 1;
      $("#insp-title").textContent = d.source;
      $("#insp-meta").textContent = `${d.chunks} chunks · ${fmtK(d.chars)} chars · ${(d.chunks / total * 100).toFixed(1)}% of KB`;
      try {
        const r = await api(`/kb/documents/${encodeURIComponent(source)}`);
        if (current !== source) return;
        $("#insp-body").innerHTML = r.chunks.map((c, i) =>
          `<li><div class="ch-head"><span>#${i + 1} · id ${c.id}</span><h4>${esc(c.heading)}</h4></div><p>${esc(c.body)}</p></li>`).join("");
      } catch (e) { toast(e.message); }
    }

    function job(state, name, meta, steps) {
      const box = $("#job");
      box.className = `job ${state}`;
      $("#job-state").textContent = { idle: "ready", running: "ingesting", done: "ingested", failed: "failed" }[state];
      $("#job-name").textContent = name;
      $("#job-meta").textContent = meta;
      const order = ["parse", "split", "embed", "index"];
      $$("#job-steps li").forEach((li) => {
        const st = steps[li.dataset.s] || "todo";
        li.className = st;
        li.textContent = `${st === "ok" ? "✓" : st === "now" ? "▸" : "○"} ${li.dataset.s}`;
      });
      return order;
    }

    async function upload(file) {
      if (!file) return;
      const drop = $("#drop");
      drop.classList.add("busy");
      $("#upload-progress").hidden = false;
      $("b", drop).textContent = `Embedding ${file.name}…`;
      job("running", file.name, `${(file.size / 1024).toFixed(1)} KB`, { parse: "now", split: "now", embed: "now" });
      const fd = new FormData();
      fd.append("file", file);
      try {
        const r = await api("/kb/upload", { method: "POST", body: fd, headers: adminHeaders() });
        fresh.add(r.source);
        current = r.source;
        job("done", file.name, `${r.chunks} chunks in ${fmtMs(r.ms)}`, { parse: "ok", split: "ok", embed: "ok", index: "ok" });
        toast(`“${r.source}” added: ${r.chunks} chunks in ${fmtMs(r.ms)}`, 4500);
        MapView.invalidate();
        await Promise.all([load(), loadStats()]);
      } catch (e) {
        job("failed", file.name, e.message, {});
        toast(`Upload failed: ${e.message}`, 5000);
      } finally {
        drop.classList.remove("busy", "over");
        $("#upload-progress").hidden = true;
        $("b", drop).textContent = "Drop a file here";
        $("#file").value = "";
      }
    }

    $("#file").addEventListener("change", (e) => upload(e.target.files[0]));
    const drop = $("#drop");
    ["dragenter", "dragover"].forEach((t) => drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.add("over"); }));
    ["dragleave", "drop"].forEach((t) => drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.remove("over"); }));
    drop.addEventListener("drop", (e) => upload(e.dataTransfer.files[0]));
    $("#admin-token").value = store.get("kb.admin", "");
    $("#admin-token").addEventListener("change", (e) => store.set("kb.admin", e.target.value.trim()));
    $("#doc-filter").addEventListener("input", render);
    $("#doc-sort").addEventListener("click", (e) => {
      const b = e.target.closest("button");
      if (!b) return;
      sort = b.dataset.v;
      store.set("kb.docsort", sort);
      render();
    });
    $("#docs").addEventListener("click", (e) => {
      const tr = e.target.closest("tr[data-source]");
      if (tr) inspect(tr.dataset.source);
    });
    $("#insp-map").addEventListener("click", () => { if (current) { MapView.isolate(current); location.hash = "map"; } });
    $("#insp-reembed").addEventListener("click", async (e) => {
      if (!current) return;
      const btn = e.currentTarget;
      btn.disabled = true;
      btn.textContent = "Embedding…";
      try {
        const r = await api(`/kb/documents/${encodeURIComponent(current)}/reembed`, { method: "POST", headers: adminHeaders() });
        MapView.invalidate();
        toast(`Re-embedded “${r.source}”: ${r.chunks} chunks in ${fmtMs(r.ms)}`);
      } catch (err) { toast(err.message); } finally { btn.disabled = false; btn.textContent = "Re-embed"; }
    });
    $("#insp-delete").addEventListener("click", async () => {
      if (!current || !confirm(`Delete “${current}” and all its chunks from the knowledge base?`)) return;
      try {
        const r = await api(`/kb/documents/${encodeURIComponent(current)}`, { method: "DELETE", headers: adminHeaders() });
        toast(`Deleted “${r.source}” (${r.chunks} chunks)`);
        fresh.delete(r.source);
        current = null;
        MapView.invalidate();
        await Promise.all([load(), loadStats()]);
      } catch (e) { toast(e.message); }
    });

    return { load };
  })();

  const phone = matchMedia("(max-width: 700px)");
  const setPlaceholder = () => { input.placeholder = phone.matches ? "Ask a question…" : "Ask about services, products, expertise…"; };
  phone.addEventListener("change", setPlaceholder);
  setPlaceholder();
  window.addEventListener("resize", () => { if (document.body.dataset.view === "map") MapView.refresh(); });

  /* ------------------------------------------------------------ boot */
  fetch("/motif.svg").then((r) => r.text()).then((svgText) => {
    $("#motif").innerHTML = svgText;
    const svg = $("#motif svg");
    if (svg) svg.setAttribute("preserveAspectRatio", "xMidYMid slice");
  }).catch(() => {});
  loadStats();
  renderHistory();
  show(location.hash.slice(1) || "assistant");
  setInterval(loadStats, 30000);
})();
