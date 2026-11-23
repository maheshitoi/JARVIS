/* JARVIS Control Center: insights, conversation history & memory, background tasks, settings,
 * plus the proactive alert pop-ups. Uses window.JarvisAPI (app.js) for authenticated requests.
 *
 * Charts are plain SVG. Colours (validated for the dark panel, incl. colour-blind separation):
 *   done = #3987e5 (blue), failed = #d03b3b (status: critical) — always with a legend + labels.
 */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const api = (path, opts) => window.JarvisAPI(path, opts);
  const SVGNS = "http://www.w3.org/2000/svg";
  const C = { done: "#3987e5", failed: "#d03b3b", bar: "#3987e5" };

  const dlg = $("center");
  const tip = $("viz-tip");
  let view = "insights";
  let days = 14;

  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }
  function svgEl(tag, attrs = {}) {
    const n = document.createElementNS(SVGNS, tag);
    for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
    return n;
  }
  const fmt = (n) => (n == null ? "—" : Number(n).toLocaleString());
  const dayLabel = (iso) => new Date(iso + "T00:00:00").toLocaleDateString([], { day: "numeric", month: "short" });
  const when = (iso) => new Date(iso).toLocaleString([], { dateStyle: "medium", timeStyle: "short" });

  // ------------------------------------------------------------ dialog --
  $("btn-center").addEventListener("click", () => { dlg.showModal(); show(view); });
  $("center-close").addEventListener("click", () => dlg.close());
  dlg.addEventListener("click", (e) => { if (e.target === dlg) dlg.close(); });
  document.querySelectorAll(".ctab").forEach((b) => b.addEventListener("click", () => show(b.dataset.view)));

  function show(name) {
    view = name;
    document.querySelectorAll(".ctab").forEach((b) => b.classList.toggle("active", b.dataset.view === name));
    for (const v of ["insights", "history", "tasks", "automations", "settings"]) $("view-" + v).hidden = v !== name;
    ({ insights: loadInsights, history: loadHistory, tasks: loadTasks, automations: loadAutomations, settings: loadSettings })[name]();
  }

  function fail(box, e) {
    box.replaceChildren(el("div", "c-empty", "Could not load: " + (e.message || e)));
  }

  // ------------------------------------------------------------ tooltip --
  function hover(target, html) {
    target.addEventListener("mousemove", (e) => {
      tip.replaceChildren(...html());
      tip.hidden = false;
      const r = dlg.getBoundingClientRect();
      let x = e.clientX - r.left + 14, y = e.clientY - r.top + 14;
      if (x + tip.offsetWidth > r.width - 8) x = e.clientX - r.left - tip.offsetWidth - 14;
      tip.style.left = x + "px";
      tip.style.top = y + "px";
    });
    target.addEventListener("mouseleave", () => { tip.hidden = true; });
  }
  const tipRow = (color, label, value) => {
    const row = el("div", "tip-row");
    const sw = el("span", "tip-sw"); sw.style.background = color;
    row.append(sw, el("span", "tip-k", label), el("span", "tip-v", value));
    return row;
  };

  // -------------------------------------------------------------- charts --
  /** Bar path with 4px rounded top corners, square at the baseline. */
  function barPath(x, y, w, h, r = 4) {
    if (h <= 0) return "";
    r = Math.min(r, w / 2, h);
    return `M${x},${y + h}V${y + r}Q${x},${y} ${x + r},${y}H${x + w - r}Q${x + w},${y} ${x + w},${y + r}V${y + h}Z`;
  }

  function niceMax(v) {
    if (v <= 4) return 4;
    const p = Math.pow(10, Math.floor(Math.log10(v)));
    return [1, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10].map((m) => m * p).find((m) => m >= v);
  }

  /** Vertical bars per day; series = [{key,label,color}] stacked bottom-up. */
  function dayBars(rows, series, { height = 200 } = {}) {
    const W = 640, H = height, L = 34, B = 24, T = 10, R = 6;
    const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, class: "viz", role: "img" });
    const total = (r) => series.reduce((s, x) => s + (r[x.key] || 0), 0);
    const max = niceMax(Math.max(1, ...rows.map(total)));
    const y = (v) => T + (H - T - B) * (1 - v / max);
    for (const t of [0, max / 2, max]) { // recessive grid
      svg.append(svgEl("line", { x1: L, x2: W - R, y1: y(t), y2: y(t), class: "grid" }));
      const lab = svgEl("text", { x: L - 6, y: y(t) + 4, class: "axis", "text-anchor": "end" });
      lab.textContent = fmt(t); svg.append(lab);
    }
    const slot = (W - L - R) / rows.length;
    const bw = Math.max(4, Math.min(22, slot * 0.6));
    const every = Math.ceil(rows.length / 8);
    const lastLabeled = Math.floor((rows.length - 1) / every) * every;
    rows.forEach((r, i) => {
      const x = L + i * slot + (slot - bw) / 2;
      let base = 0;
      const top = series.filter((s) => r[s.key] > 0).at(-1);
      series.forEach((s) => {
        const v = r[s.key] || 0;
        if (!v) return;
        const y1 = y(base + v), y0 = y(base);
        const gap = base > 0 ? 2 : 0; // 2px surface gap between stacked segments
        const h = y0 - y1 - gap;
        const d = s === top ? barPath(x, y1, bw, h) : `M${x},${y1}h${bw}v${h}h${-bw}Z`;
        if (d) svg.append(svgEl("path", { d, fill: s.color }));
        base += v;
      });
      // every Nth day, plus the last day only when it won't collide with the previous label
      if (i % every === 0 || (i === rows.length - 1 && i - lastLabeled >= Math.max(2, every * 0.75))) {
        const lab = svgEl("text", { x: x + bw / 2, y: H - 6, class: "axis", "text-anchor": "middle" });
        lab.textContent = dayLabel(r.day); svg.append(lab);
      }
      const hit = svgEl("rect", { x: L + i * slot, y: T, width: slot, height: H - T - B, class: "hit" });
      hover(hit, () => [el("div", "tip-h", dayLabel(r.day)), ...series.map((s) => tipRow(s.color, s.label, fmt(r[s.key])))]);
      svg.append(hit);
    });
    return svg;
  }

  /** Horizontal ranking bars (one series), value at the bar end. */
  function rankBars(items, { label, value, color = C.bar, extra }) {
    const box = el("div", "rank");
    const max = Math.max(1, ...items.map(value));
    for (const it of items) {
      const row = el("div", "rank-row");
      const name = el("span", "rank-name mono", label(it));
      name.title = label(it);
      const track = el("div", "rank-track");
      const bar = el("div", "rank-bar");
      bar.style.width = `${Math.max(1.5, (100 * value(it)) / max)}%`;
      bar.style.background = color;
      track.append(bar);
      row.append(name, track, el("span", "rank-v", fmt(value(it))));
      hover(row, () => [el("div", "tip-h", label(it)), tipRow(color, "Actions", fmt(value(it))),
        ...(extra ? [tipRow(C.failed, "Failed", fmt(extra(it)))] : [])]);
      box.append(row);
    }
    return box;
  }

  function legend(series) {
    const lg = el("div", "legend");
    for (const s of series) {
      const it = el("span", "legend-item");
      const sw = el("span", "legend-sw"); sw.style.background = s.color;
      it.append(sw, document.createTextNode(s.label));
      lg.append(it);
    }
    return lg;
  }

  function tableView(cols, rows) {
    const d = el("details", "c-table");
    d.append(el("summary", null, "Show as table"));
    const t = el("table");
    const hr = t.createTHead().insertRow();
    cols.forEach((c) => hr.append(el("th", null, c.label)));
    const body = t.createTBody();
    rows.forEach((r) => { const tr = body.insertRow(); cols.forEach((c) => { tr.insertCell().textContent = c.get(r); }); });
    d.append(t);
    return d;
  }

  function card(title, sub, ...content) {
    const c = el("div", "c-card");
    const h = el("div", "c-card-head");
    h.append(el("h3", null, title));
    if (sub) h.append(el("span", "c-sub", sub));
    c.append(h, ...content);
    return c;
  }

  function tile(value, label, note) {
    const t = el("div", "c-tile");
    t.append(el("div", "c-tile-v", value), el("div", "c-tile-k", label));
    if (note) t.append(el("div", "c-tile-n", note));
    return t;
  }

  // ------------------------------------------------------------ insights --
  async function loadInsights() {
    const box = $("view-insights");
    box.replaceChildren(el("div", "c-empty", "Loading…"));
    let d;
    try { d = await api(`/api/insights?days=${days}`); } catch (e) { return fail(box, e); }
    if (!d.available) return box.replaceChildren(el("div", "c-empty", d.reason));

    const bar = el("div", "c-filters");
    const sel = el("select", "c-select");
    sel.setAttribute("aria-label", "Time range");
    for (const n of [7, 14, 30, 90]) {
      const o = el("option", null, `Last ${n} days`); o.value = n; o.selected = n === days; sel.append(o);
    }
    sel.addEventListener("change", () => { days = Number(sel.value); loadInsights(); });
    bar.append(sel);

    const t = d.totals || {};
    const tiles = el("div", "c-tiles");
    tiles.append(
      tile(fmt(t.actions), "Actions", `${fmt(t.done)} done · ${fmt(t.failed)} failed`),
      tile(t.success_rate == null ? "—" : t.success_rate + "%", "Success rate", "done ÷ (done + failed)"),
      tile(t.median_s == null ? "—" : t.median_s + " s", "Median action time"),
      tile(fmt(t.sessions ?? 0), "Conversations"),
      tile(fmt(t.memories ?? 0), "Memories"),
      tile(fmt(t.tasks ?? 0), "Background tasks", t.tasks ? `${fmt(t.tasks_done)} done` : null),
    );

    const series = [{ key: "done", label: "Done", color: C.done }, { key: "failed", label: "Failed", color: C.failed }];
    const perDay = card("Actions per day", "done vs failed", legend(series), dayBars(d.per_day, series),
      tableView([{ label: "Day", get: (r) => dayLabel(r.day) }, { label: "Done", get: (r) => fmt(r.done) },
        { label: "Failed", get: (r) => fmt(r.failed) }], d.per_day));

    const top = card("Most used", `top ${d.top.length}`, d.top.length
      ? rankBars(d.top, { label: (r) => r.capability, value: (r) => r.count, extra: (r) => r.failed })
      : el("div", "c-empty", "No actions in this period."));

    const anyConv = (d.conversations_per_day || []).some((r) => r.sessions > 0);
    const conv = d.conversations_per_day && !anyConv
      ? card("Conversations per day", null, el("div", "c-empty", "No conversations recorded yet. They're saved from the next session on."))
      : d.conversations_per_day
      ? card("Conversations per day", "sessions", dayBars(d.conversations_per_day,
          [{ key: "sessions", label: "Sessions", color: C.bar }], { height: 150 }),
        tableView([{ label: "Day", get: (r) => dayLabel(r.day) }, { label: "Sessions", get: (r) => fmt(r.sessions) },
          { label: "Lines", get: (r) => fmt(r.lines) }], d.conversations_per_day))
      : null;

    const fails = card("Recent failures", null, d.recent_failures.length ? (() => {
      const ul = el("ul", "c-list");
      for (const f of d.recent_failures) {
        const li = el("li");
        const head = el("div", "c-li-head");
        head.append(el("span", "status-chip critical", "✕ Failed"), el("span", "mono", f.capability),
          el("span", "c-sub", f.ts ? when(f.ts) : ""));
        li.append(head, el("div", "c-li-text", f.summary || ""), el("div", "c-li-err", f.error || ""));
        ul.append(li);
      }
      return ul;
    })() : el("div", "c-empty", "No failures."));

    const grid = el("div", "c-grid");
    grid.append(perDay, top, ...(conv ? [conv] : []), fails);
    box.replaceChildren(bar, tiles, grid);
  }

  // ------------------------------------------------------ history & memory --
  async function loadHistory() {
    const box = $("view-history");
    const cols = el("div", "c-two");
    const left = el("div", "c-col"), right = el("div", "c-col");
    cols.append(left, right);
    box.replaceChildren(cols);

    // conversations
    const search = el("input", "c-input");
    search.placeholder = "Search everything you and JARVIS said…";
    search.type = "search";
    const list = el("div", "c-scroll");
    left.append(el("h3", "c-h", "Conversations"), search, list);
    const loadConvos = async () => {
      list.replaceChildren(el("div", "c-empty", "Loading…"));
      try {
        const d = await api(`/api/history?q=${encodeURIComponent(search.value)}`);
        if (!d.items.length) return list.replaceChildren(el("div", "c-empty", d.search ? "No matches." : "No conversations saved yet. They're recorded from the next session on."));
        list.replaceChildren(...d.items.map((it) => d.search ? lineRow(it) : sessionRow(it)));
      } catch (e) { fail(list, e); }
    };
    let timer;
    search.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(loadConvos, 300); });
    const sessionRow = (s) => {
      const b = el("button", "c-session");
      b.append(el("span", "c-session-t", s.first || "(no words from you)"),
        el("span", "c-sub", `${when(s.started)} · ${s.lines} lines`));
      b.addEventListener("click", () => openSession(s.session_id, b));
      return b;
    };
    const lineRow = (l) => {
      const b = el("button", "c-session");
      b.append(el("span", "c-session-t", (l.role === "user" ? "You: " : "JARVIS: ") + l.text), el("span", "c-sub", when(l.ts)));
      b.addEventListener("click", () => openSession(l.session_id, b));
      return b;
    };
    async function openSession(id, anchor) {
      const lines = await api(`/api/history/${id}`).catch(() => []);
      const t = el("div", "c-thread");
      for (const l of lines) {
        const m = el("div", "c-msg " + l.role);
        m.append(el("span", "c-msg-who", l.role === "user" ? "You" : "JARVIS"), el("span", null, l.text));
        t.append(m);
      }
      list.querySelectorAll(".c-thread").forEach((x) => x.remove());
      anchor.after(t);
    }

    // memories
    const msearch = el("input", "c-input");
    msearch.placeholder = "Search memory by meaning, e.g. “family” or “coffee”";
    msearch.type = "search";
    const mlist = el("div", "c-scroll");
    right.append(el("h3", "c-h", "What JARVIS remembers"), msearch, mlist);
    const loadMem = async () => {
      mlist.replaceChildren(el("div", "c-empty", "Loading…"));
      try {
        const items = await api(`/api/memories?q=${encodeURIComponent(msearch.value)}`);
        if (!items.length) return mlist.replaceChildren(el("div", "c-empty", msearch.value ? "Nothing related." : "No memories yet."));
        mlist.replaceChildren(...items.map((m) => {
          const row = el("div", "c-mem");
          const txt = el("div", "c-mem-t");
          txt.append(el("span", "c-kind", m.kind || "fact"), document.createTextNode(m.fact));
          const meta = el("div", "c-sub", [m.source === "auto" ? "learned from a conversation" : m.source,
            m.updated ? when(m.updated) : "", m.score != null ? `match ${Math.round(m.score * 100)}%` : ""].filter(Boolean).join(" · "));
          const del = el("button", "c-del", "Forget");
          del.addEventListener("click", async () => {
            if (!confirm(`Forget: “${m.fact}”?`)) return;
            try { await api(`/api/memories/${m.id}`, { method: "DELETE" }); row.remove(); }
            catch (e) { alert("Could not delete: " + e.message); }
          });
          row.append(txt, meta, del);
          return row;
        }));
      } catch (e) { fail(mlist, e); }
    };
    let mtimer;
    msearch.addEventListener("input", () => { clearTimeout(mtimer); mtimer = setTimeout(loadMem, 350); });
    loadConvos();
    loadMem();
  }

  // ------------------------------------------------------------- tasks --
  async function loadTasks() {
    const box = $("view-tasks");
    box.replaceChildren(el("div", "c-empty", "Loading…"));
    let items;
    try { items = await api("/api/tasks"); } catch (e) { return fail(box, e); }
    if (!items.length) {
      return box.replaceChildren(el("div", "c-empty", "No background tasks yet. Ask JARVIS: “prepare this week's report in the background”."));
    }
    const STATUS = { running: ["serious", "◷ Running"], done: ["good", "✓ Done"], failed: ["critical", "✕ Failed"], cancelled: ["muted", "– Cancelled"] };
    box.replaceChildren(...items.map((t) => {
      const c = el("details", "c-task");
      const s = el("summary");
      const [cls, label] = STATUS[t.status] || ["muted", t.status];
      s.append(el("span", "status-chip " + cls, label), el("span", "c-task-t", t.title),
        el("span", "c-sub", when(t.created) + (t.tool_calls ? ` · ${t.tool_calls} tool calls` : "")));
      c.append(s, el("pre", "c-task-r", t.result || t.error || "(no result yet)"));
      return c;
    }));
  }

  // -------------------------------------------------------- automations --
  const EXAMPLES = [
    ["Morning check", "weekdays 09:00", "only_if_issue", "Check that all my websites are up, list any failed JARVIS actions from the last 24 hours, and tell me only what needs attention."],
    ["Weekly payments", "monday 09:30", "auto", "Compare last week's successful and failed payments with the week before, per day, and point out anything unusual."],
    ["Evening wrap-up", "daily 19:00", "auto", "Summarise today: website status, phone messages, important unread emails and tomorrow's meetings."],
    ["Site watch", "every 2 hours", "only_if_issue", "Check all my websites and SSL certificates; say 'All good' if nothing is wrong."],
  ];
  const NOTIFY_LABEL = {
    auto: "Speak if I'm connected, else push to phone", push: "Push to phone", speak: "Speak when I'm connected",
    both: "Push and speak", only_if_issue: "Only when something needs attention",
  };
  const short = (iso) => (iso ? new Date(iso).toLocaleString([], { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }) : "—");

  async function loadAutomations() {
    const box = $("view-automations");
    box.replaceChildren(el("div", "c-empty", "Loading…"));
    let d;
    try { d = await api("/api/automations"); } catch (e) { return fail(box, e); }

    // connections
    const chip = (ok, yes, no) => el("span", "status-chip " + (ok ? "good" : "serious"), (ok ? "✓ " : "! ") + (ok ? yes : no));
    const conn = el("div", "c-row");
    const testBtn = el("button", "btn small-btn", "Send test notification"); testBtn.type = "button"; testBtn.disabled = !d.push_ready;
    const testOut = el("span", "c-help");
    testBtn.addEventListener("click", async () => {
      testOut.textContent = "Sending…";
      try { const r = await api("/api/notify/test", { method: "POST" }); testOut.textContent = r.ok ? "✓ Sent — check your phone" : "✕ The ntfy server refused it"; }
      catch (e) { testOut.textContent = "✕ " + e.message; }
    });
    conn.append(chip(d.push_ready, "Phone push ready", "Phone push off (NTFY_TOPIC)"),
      chip(d.calendar_ready, "Calendar connected", "No calendar (CALENDAR_ICS_URLS)"),
      chip(d.email_ready, "Email connected", "No email (EMAIL_ADDRESS)"), testBtn, testOut);

    // new automation
    const form = el("form", "c-set c-auto-new");
    form.autocomplete = "off";
    const input = (name, ph) => { const i = el("input", "c-input"); i.name = name; i.placeholder = ph; i.required = true; return i; };
    const title = input("title", "Morning check");
    const when = input("when", "weekdays 09:00  ·  monday 9am  ·  every 2 hours");
    const job = el("textarea", "c-input"); job.name = "job"; job.required = true;
    job.placeholder = "What should JARVIS do? Write it like you'd say it, with all details.";
    const notifySel = el("select", "c-select");
    for (const m of d.notify_modes) { const o = el("option", null, NOTIFY_LABEL[m] || m); o.value = m; notifySel.append(o); }
    const field = (label, ctl, wide) => { const f = el("label", "c-field" + (wide ? " c-wide" : "")); f.append(el("span", "c-label", label), ctl); return f; };
    const examples = el("div", "c-examples c-wide");
    examples.append(el("span", "c-label", "Examples:"));
    for (const [t, w, n, j] of EXAMPLES) {
      const b = el("button", "btn", t); b.type = "button";
      b.addEventListener("click", () => { title.value = t; when.value = w; notifySel.value = n; job.value = j; });
      examples.append(b);
    }
    const create = el("button", "btn primary", "Create automation");
    const out = el("span", "c-help");
    const row = el("div", "c-row c-wide"); row.append(create, out);
    form.append(el("legend", null, "New automation"), examples, field("Name", title), field("Schedule", when),
      field("Job", job, true), field("Tell me", notifySel), row);
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      out.textContent = "Creating…";
      try {
        const a = await api("/api/automations", { method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ title: title.value, when: when.value, job: job.value, notify: notifySel.value }) });
        out.textContent = `✓ ${a.schedule} — first run ${short(a.next_run)}`;
        form.reset();
        renderList();
      } catch (err) { out.textContent = "✕ " + err.message; }
    });

    const list = el("div");
    async function renderList() {
      let items;
      try { items = (await api("/api/automations")).items; } catch (e) { return fail(list, e); }
      if (!items.length) {
        return list.replaceChildren(el("div", "c-empty", "No automations yet. Create one above, or tell JARVIS: “every Monday at 9, check last week's payments”."));
      }
      list.replaceChildren(...items.map((a) => {
        const card = el("div", "c-auto");
        const head = el("div", "c-auto-head");
        const state = a.enabled ? ["good", "✓ On"] : ["muted", "‖ Paused"];
        head.append(el("span", "status-chip " + state[0], state[1]), el("span", "c-task-t", a.title),
          el("span", "c-sub", a.schedule + (a.enabled && a.next_run ? ` · next ${short(a.next_run)}` : "")));
        if (a.last_status === "failed") head.append(el("span", "status-chip critical", "✕ Last run failed"));
        const act = async (path, opts) => { try { await api(path, opts); } catch (e) { window.alert(e.message); } renderList(); };
        const btn = (text, cls, fn) => { const b = el("button", "btn small-btn" + (cls ? " " + cls : ""), text); b.type = "button"; b.addEventListener("click", fn); return b; };
        head.append(
          btn("Run now", "", async (e) => { e.target.disabled = true; e.target.textContent = "Queued…"; await act(`/api/automations/${a.id}/run`, { method: "POST" }); }),
          btn(a.enabled ? "Pause" : "Resume", "", () => act(`/api/automations/${a.id}/enabled`, {
            method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ enabled: !a.enabled }) })),
          btn("Delete", "danger", () => { if (window.confirm(`Delete “${a.title}”?`)) act(`/api/automations/${a.id}`, { method: "DELETE" }); }));
        card.append(head, el("p", "c-auto-job", a.job + " — " + (NOTIFY_LABEL[a.notify] || a.notify)));
        if (a.last_run) {
          const det = el("details");
          det.append(el("summary", null, `Last result · ${short(a.last_run)}${a.last_engine && a.last_engine !== "-" ? " · " + a.last_engine : ""}`),
            el("pre", "c-task-r", a.last_result || "(empty)"));
          card.append(det);
        }
        return card;
      }));
    }
    box.replaceChildren(conn, form, list);
    renderList();
  }

  // ----------------------------------------------------------- settings --
  async function loadSettings() {
    const box = $("view-settings");
    box.replaceChildren(el("div", "c-empty", "Loading…"));
    let s;
    try { s = await api("/api/settings"); } catch (e) { return fail(box, e); }
    const form = el("form", "c-form");
    form.autocomplete = "off";
    const ro = !s.editable;
    if (ro) form.append(el("div", "c-note", "Read-only here: keys and models can only be changed on the PC running JARVIS."));

    const status = el("div", "c-status");
    const chip = (ok, yes, no) => el("span", "status-chip " + (ok ? "good" : "serious"), (ok ? "✓ " : "! ") + (ok ? yes : no));
    status.append(chip(s.claude_ready, "Claude connected", "Claude not set up"), chip(s.database, "Database connected", "No database"));
    form.append(status);

    const field = (label, input, help) => {
      const f = el("label", "c-field");
      f.append(el("span", "c-label", label), input);
      if (help) f.append(el("span", "c-help", help));
      return f;
    };
    const text = (name, value, placeholder, type = "text") => {
      const i = el("input", "c-input"); i.name = name; i.type = type; i.value = value || ""; i.placeholder = placeholder || ""; i.disabled = ro;
      return i;
    };

    const claude = el("fieldset", "c-set");
    claude.append(el("legend", null, "Claude — deep-thinking brain"));
    const key = text("ANTHROPIC_API_KEY", "", s.env.ANTHROPIC_API_KEY.set ? `Saved (${s.env.ANTHROPIC_API_KEY.value}) — type to replace` : "sk-ant-…", "password");
    const effort = el("select", "c-select"); effort.name = "CLAUDE_EFFORT"; effort.disabled = ro;
    for (const e of s.efforts) { const o = el("option", null, e); o.value = e; o.selected = (s.env.CLAUDE_EFFORT.value || s.defaults.CLAUDE_EFFORT) === e; effort.append(o); }
    const testBtn = el("button", "btn small-btn", "Test connection"); testBtn.type = "button"; testBtn.disabled = ro;
    const testOut = el("span", "c-help");
    testBtn.addEventListener("click", async () => {
      testOut.textContent = "Testing…";
      try { const r = await api("/api/settings/test-claude", { method: "POST" }); testOut.textContent = r.ok ? `✓ ${r.model}: “${r.reply}”` : `✕ ${r.error}`; }
      catch (e) { testOut.textContent = "✕ " + e.message; }
    });
    const testRow = el("div", "c-row"); testRow.append(testBtn, testOut);
    claude.append(
      field("API key", key, "From console.anthropic.com → API keys. Stored in jarvis_pro/.env."),
      field("Model", text("CLAUDE_MODEL", s.env.CLAUDE_MODEL.value, s.defaults.CLAUDE_MODEL)),
      field("Effort", effort, "Higher = more thorough and slower. “high” suits reports."),
      testRow);

    const voice = el("fieldset", "c-set");
    voice.append(el("legend", null, "Voice — Gemini Live"),
      field("Model", text("JARVIS_MODEL", s.env.JARVIS_MODEL.value, s.defaults.JARVIS_MODEL)),
      field("Voice", text("JARVIS_VOICE", s.env.JARVIS_VOICE.value, s.defaults.JARVIS_VOICE)));

    const pro = el("fieldset", "c-set");
    const p = s.proactive;
    const time = text("brief_time", p.brief_time || "09:00", "09:00", "time");
    const check = (name, on) => { const i = el("input"); i.type = "checkbox"; i.name = name; i.checked = !!on; i.disabled = ro; return i; };
    const withClaude = check("brief_with_claude", p.brief_with_claude);
    const speakInfo = check("speak_info_alerts", p.speak_info_alerts);
    const cbField = (input, label) => { const l = el("label", "c-check"); l.append(input, document.createTextNode(label)); return l; };
    pro.append(el("legend", null, "Proactive"), field("Daily briefing after", time),
      cbField(withClaude, "Claude writes the briefing (adds business numbers; uses the API)"),
      cbField(speakInfo, "Speak info alerts too (phone messages, site back up)"));

    const save = el("button", "btn primary", "Save");
    save.disabled = ro;
    const saveOut = el("span", "c-help");
    const saveRow = el("div", "c-row"); saveRow.append(save, saveOut);
    form.append(claude, voice, pro, saveRow);
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const env = {};
      for (const n of ["CLAUDE_MODEL", "JARVIS_MODEL", "JARVIS_VOICE"]) env[n] = form.elements[n].value.trim();
      env.CLAUDE_EFFORT = effort.value;
      if (key.value.trim()) env.ANTHROPIC_API_KEY = key.value.trim();
      const proactive = { brief_time: time.value, brief_with_claude: withClaude.checked, speak_info_alerts: speakInfo.checked };
      saveOut.textContent = "Saving…";
      try {
        const r = await api("/api/settings", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ env, proactive }) });
        saveOut.textContent = r.changed.length ? `✓ Saved${r.restart_agent ? " — restart the agent (python agent.py dev) to apply" : ""}` : "Nothing changed.";
        key.value = "";
      } catch (err) { saveOut.textContent = "✕ " + err.message; }
    });
    box.replaceChildren(form);
  }

  // ------------------------------------------------------------- alerts --
  const ICON = { critical: "✕", warning: "!", info: "i" };
  function alert(ev) {
    const host = $("alerts");
    // A popover sits in the top layer, so alerts show above the open Control Center too.
    try { if (host.matches(":popover-open")) host.hidePopover(); host.showPopover(); } catch { /* old browser */ }
    const a = el("div", "alert " + (ev.level || "info"));
    a.setAttribute("role", ev.level === "critical" ? "alert" : "status");
    const icon = el("span", "alert-icon", ICON[ev.level] || "i");
    const body = el("div", "alert-body");
    body.append(el("div", "alert-title", ev.title || "Alert"), el("div", "alert-text", ev.text || ""));
    const x = el("button", "alert-x", "×"); x.setAttribute("aria-label", "Dismiss");
    const drop = () => { a.remove(); if (!host.childElementCount) try { host.hidePopover(); } catch { /* */ } };
    x.addEventListener("click", drop);
    a.append(icon, body, x);
    host.prepend(a);
    while (host.childElementCount > 4) host.lastElementChild.remove();
    if (ev.level !== "critical") setTimeout(drop, 12000);
  }

  window.JarvisCenter = { alert, show: (v) => { dlg.showModal(); show(v); } };
})();
