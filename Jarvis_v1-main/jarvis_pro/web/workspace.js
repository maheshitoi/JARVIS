/* JARVIS workspace: every tool JARVIS runs becomes a mini screen in the dashboard.
 *
 * One action fills the whole stage; several at once split it into a grid (2 side by side, 4 as 2x2, ...).
 * Each screen shows a "working" animation while the tool runs, then its result: tables for data
 * (database / MCP results), lists, text, or a YouTube/Spotify player.
 *
 *   JarvisWS.tool(ev)    — {type:"tool", state:"start"|"progress"|"result"|"cancelled", call_id, name, args, output, error}
 *   JarvisWS.action(ev)  — guard "action" events (summary, approval / done / failed) for the same call_id
 *   JarvisWS.player(ev)  — {type:"player", state:"on"|"off", provider, url, link, title, tall, call_id}
 *   JarvisWS.clear()     — remove every screen (on disconnect)
 */
(() => {
  "use strict";

  const MAX_TILES = 9; // past this many, the oldest finished one makes room
  const AUTO_CLOSE_MS = 4000; // a finished screen closes this long after JARVIS stops explaining it
  const MAX_ROWS = 500;
  const MAX_COLS = 12;

  const ws = document.getElementById("ws");
  const grid = document.getElementById("ws-grid");
  const countEl = document.getElementById("ws-count");
  const tiles = new Map(); // call_id -> tile
  let lastActivity = 0;
  let speaking = false; // JARVIS is talking right now (lk.agent.state === "speaking")

  // ------------------------------------------------------------- icons --
  const ICONS = {
    bolt: '<path d="M13 2 4 14h7l-1 8 9-12h-7z"/>',
    chat: '<path d="M4 5h16v11H8l-4 4z"/><path d="M8 9.5h8M8 12.5h5"/>',
    mail: '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="m3.5 6.5 8.5 7 8.5-7"/>',
    music: '<path d="M9 18V5l11-2v13"/><circle cx="6.5" cy="18" r="2.5"/><circle cx="17.5" cy="16" r="2.5"/>',
    app: '<rect x="3" y="3" width="7.5" height="7.5" rx="1.5"/><rect x="13.5" y="3" width="7.5" height="7.5" rx="1.5"/><rect x="3" y="13.5" width="7.5" height="7.5" rx="1.5"/><rect x="13.5" y="13.5" width="7.5" height="7.5" rx="1.5"/>',
    globe: '<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3c2.5 2.6 3.8 5.6 3.8 9s-1.3 6.4-3.8 9c-2.5-2.6-3.8-5.6-3.8-9S9.5 5.6 12 3z"/>',
    screen: '<rect x="3" y="4" width="18" height="12" rx="2"/><path d="M8 20h8M12 16v4"/>',
    data: '<ellipse cx="12" cy="5.5" rx="8" ry="3"/><path d="M4 5.5v13c0 1.7 3.6 3 8 3s8-1.3 8-3v-13M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3"/>',
    brain: '<path d="M9 4a3 3 0 0 0-3 3 3 3 0 0 0-2 5 3 3 0 0 0 2 5 3 3 0 0 0 6 1V5a2 2 0 0 0-3-1zM15 4a3 3 0 0 1 3 3 3 3 0 0 1 2 5 3 3 0 0 1-2 5 3 3 0 0 1-6 1"/>',
    phone: '<path d="M5 3h4l2 5-2.5 1.5a11 11 0 0 0 6 6L16 13l5 2v4a2 2 0 0 1-2 2A16 16 0 0 1 3 5a2 2 0 0 1 2-2z"/>',
    device: '<rect x="6" y="2.5" width="12" height="19" rx="2.5"/><path d="M11 18.5h2"/>',
    social: '<circle cx="18" cy="5" r="2.5"/><circle cx="6" cy="12" r="2.5"/><circle cx="18" cy="19" r="2.5"/><path d="m8.2 10.8 7.6-4.5M8.2 13.2l7.6 4.5"/>',
    expand: '<path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5"/>',
    shrink: '<path d="M9 4v5H4M15 4v5h5M9 20v-5H4M15 20v-5h5"/>',
    close: '<path d="M6 6l12 12M18 6 6 18"/>',
    out: '<path d="M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5"/>',
  };
  const svg = (name) => `<svg viewBox="0 0 24 24" aria-hidden="true">${ICONS[name] || ICONS.bolt}</svg>`;

  const KINDS = [
    [/whatsapp/, "chat", "#25d366"],
    [/mail|gmail|inbox/, "mail", "#ff7a59"],
    [/youtube|spotify|music|media|play/, "music", "#ff3d6e"],
    [/screen|look|see|click|type_text|press/, "screen", "#38d6ff"],
    [/open_app|application|launch/, "app", "#7c6bff"],
    [/url|browser|web|search|site|tab/, "globe", "#4da3ff"],
    [/memory|remember|recall|note/, "brain", "#c084fc"],
    [/call|phone|sms/, "phone", "#2fd598"],
    [/device/, "device", "#38d6ff"],
    [/post|social|instagram|twitter|linkedin|facebook/, "social", "#ff9f43"],
    [/db|database|sql|query|report|stat|list|get|doctor|consult|user|booking|earning|queue|customer/, "data", "#ffb547"],
  ];
  function kindOf(name) {
    const n = (name || "").toLowerCase();
    for (const [re, icon, color] of KINDS) if (re.test(n)) return { icon, color };
    return { icon: "bolt", color: "#38d6ff" };
  }

  // ----------------------------------------------------------- helpers --
  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }
  function pretty(key) {
    return String(key)
      .replace(/^mcp_\w+?__/, "")
      .replace(/([a-z])([A-Z])/g, "$1 $2")
      .replace(/[_-]+/g, " ")
      .trim()
      .replace(/^./, (c) => c.toUpperCase());
  }
  const ISO = /^\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?)?$/;
  function fmtScalar(v) {
    if (v == null || v === "") return "—";
    if (typeof v === "boolean") return v ? "Yes" : "No";
    if (typeof v === "number") return Number.isInteger(v) ? v.toLocaleString() : v.toLocaleString(undefined, { maximumFractionDigits: 2 });
    const s = String(v);
    if (ISO.test(s)) {
      const d = new Date(s);
      if (!isNaN(d)) return s.length <= 10 ? d.toLocaleDateString() : d.toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
    }
    return s;
  }
  const isObj = (v) => v && typeof v === "object" && !Array.isArray(v);
  const isScalar = (v) => v == null || typeof v !== "object";

  /** Short text for a nested value inside a table cell. */
  function cellText(v) {
    if (isScalar(v)) return fmtScalar(v);
    if (Array.isArray(v)) return v.every(isScalar) ? v.map(fmtScalar).join(", ") : `${v.length} item${v.length === 1 ? "" : "s"}`;
    for (const k of ["name", "fullName", "title", "label", "email", "id"]) if (v[k] != null && isScalar(v[k])) return fmtScalar(v[k]);
    const parts = Object.entries(v).filter(([, x]) => isScalar(x)).slice(0, 3).map(([k, x]) => `${pretty(k)}: ${fmtScalar(x)}`);
    return parts.join(" · ") || "…";
  }

  /** Text with https links made clickable (never innerHTML). */
  function linkified(text, cls) {
    const box = el("div", cls);
    const re = /https?:\/\/[^\s<>"')\]]+/g;
    let last = 0, m;
    while ((m = re.exec(text))) {
      box.append(document.createTextNode(text.slice(last, m.index)));
      const a = el("a", "ws-link", m[0]);
      a.href = m[0]; a.target = "_blank"; a.rel = "noopener noreferrer";
      box.append(a);
      last = m.index + m[0].length;
    }
    box.append(document.createTextNode(text.slice(last)));
    return box;
  }

  // ------------------------------------------------------- renderers --
  /** Tool output (string) -> the value inside it: JSON, MCP {"type":"text"} wrappers, or plain text. */
  function decode(output) {
    let v = output;
    for (let i = 0; i < 4 && typeof v === "string"; i++) {
      const s = v.trim();
      if (!/^[[{]/.test(s)) break;
      try { v = JSON.parse(s); } catch { break; }
      if (isObj(v) && v.type === "text" && typeof v.text === "string") v = v.text;
      else if (Array.isArray(v) && v.length && v.every((x) => isObj(x) && x.type === "text")) v = v.map((x) => x.text).join("\n");
    }
    return v;
  }

  function table(rows) {
    const cols = [];
    for (const r of rows.slice(0, 100)) for (const k of Object.keys(r)) if (!cols.includes(k) && cols.length < MAX_COLS) cols.push(k);
    const wrap = el("div", "ws-table-wrap");
    const t = el("table", "ws-table");
    const head = t.createTHead().insertRow();
    for (const c of cols) head.append(el("th", null, pretty(c)));
    const body = t.createTBody();
    for (const r of rows.slice(0, MAX_ROWS)) {
      const tr = body.insertRow();
      for (const c of cols) {
        const td = tr.insertCell();
        td.textContent = cellText(r[c]);
        if (typeof r[c] === "number") td.className = "num";
        td.title = td.textContent;
      }
    }
    wrap.append(t);
    const frag = document.createDocumentFragment();
    frag.append(el("div", "ws-caption", `${rows.length.toLocaleString()} row${rows.length === 1 ? "" : "s"}` +
      (rows.length > MAX_ROWS ? ` · showing first ${MAX_ROWS}` : "")), wrap);
    return frag;
  }

  function stats(entries) {
    const box = el("div", "ws-stats");
    for (const [k, v] of entries) {
      const s = el("div", "ws-stat");
      s.append(el("div", "ws-stat-v", fmtScalar(v)), el("div", "ws-stat-k", pretty(k)));
      if (typeof v === "string" && v.length > 24) s.classList.add("wide");
      box.append(s);
    }
    return box;
  }

  function section(title, content) {
    const s = el("div", "ws-section");
    s.append(el("div", "ws-h", title), content);
    return s;
  }

  function renderValue(v, depth = 0) {
    if (typeof v === "string") return renderText(v);
    if (isScalar(v)) return stats([["value", v]]);
    if (Array.isArray(v)) {
      if (!v.length) return el("div", "ws-empty", "No results.");
      if (v.every(isObj)) return table(v);
      if (v.every(isScalar)) {
        const ul = el("ul", "ws-list");
        for (const x of v.slice(0, MAX_ROWS)) ul.append(el("li", null, fmtScalar(x)));
        return ul;
      }
      const box = el("div", "ws-stack");
      v.slice(0, 50).forEach((x, i) => box.append(section(`#${i + 1}`, renderValue(x, depth + 1))));
      return box;
    }
    // object: scalars as stat cards, arrays as tables, nested objects as sections
    const box = el("div", "ws-stack");
    const entries = Object.entries(v);
    const scalars = entries.filter(([, x]) => isScalar(x));
    if (scalars.length) box.append(stats(scalars));
    for (const [k, x] of entries) {
      if (isScalar(x)) continue;
      if (depth > 3) { box.append(section(pretty(k), el("pre", "ws-pre", JSON.stringify(x, null, 2)))); continue; }
      box.append(section(pretty(k), renderValue(x, depth + 1)));
    }
    return box;
  }

  /** Plain text: markdown tables -> table, bullet lines -> list, otherwise readable paragraphs. */
  function renderText(text) {
    const s = text.trim();
    if (!s) return el("div", "ws-empty", "Done.");
    const lines = s.split(/\r?\n/);
    const tl = lines.filter((l) => /^\s*\|.*\|\s*$/.test(l));
    if (tl.length >= 3) {
      const cells = (l) => l.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim());
      const head = cells(tl[0]);
      const rows = tl.slice(1).filter((l) => !/^\s*\|[\s:|-]+\|\s*$/.test(l))
        .map((l) => Object.fromEntries(cells(l).map((c, i) => [head[i] || `col${i + 1}`, c])));
      const box = el("div", "ws-stack");
      const before = lines.slice(0, lines.indexOf(tl[0])).join("\n").trim();
      if (before) box.append(linkified(before, "ws-text"));
      box.append(table(rows));
      return box;
    }
    const bullets = lines.filter((l) => /^\s*([-*•]|\d+[.)])\s+/.test(l));
    if (bullets.length >= 2 && bullets.length >= lines.filter((l) => l.trim()).length - 2) {
      const box = el("div", "ws-stack");
      const ul = el("ul", "ws-list");
      const item = (text, cls) => { const li = el("li", cls); li.append(linkified(text, "")); return li; };
      for (const l of lines) {
        if (!l.trim()) continue;
        if (/^\s*([-*•]|\d+[.)])\s+/.test(l)) ul.append(item(l.replace(/^\s*([-*•]|\d+[.)])\s+/, "")));
        else if (!ul.childElementCount) box.append(linkified(l, "ws-text"));
        else ul.append(item(l.trim(), "sub"));
      }
      box.append(ul);
      return box;
    }
    return linkified(s, s.length < 220 ? "ws-text big" : "ws-text");
  }

  // ------------------------------------------------------------ tiles --
  function makeTile(id, name) {
    const k = kindOf(name);
    const t = { id, name: name || "action", state: "running", player: false, done: 0 };
    const n = el("article", "tile");
    n.style.setProperty("--kind", k.color);
    n.dataset.st = "running";

    const head = el("header", "tile-head");
    const icon = el("span", "tile-icon"); icon.innerHTML = svg(k.icon);
    const titles = el("div", "tile-titles");
    t.titleEl = el("div", "tile-title", pretty(name));
    t.subEl = el("div", "tile-sub", name || "");
    titles.append(t.titleEl, t.subEl);
    t.chip = el("span", "tile-chip", "Working");
    const btns = el("div", "tile-btns");
    t.linkBtn = el("a", "tile-btn"); t.linkBtn.innerHTML = svg("out"); t.linkBtn.title = "Open on the site";
    t.linkBtn.target = "_blank"; t.linkBtn.rel = "noopener noreferrer"; t.linkBtn.hidden = true;
    const focus = el("button", "tile-btn"); focus.type = "button"; focus.title = "Full screen"; focus.innerHTML = svg("expand");
    focus.addEventListener("click", () => toggleFocus(t, focus));
    const close = el("button", "tile-btn"); close.type = "button"; close.title = "Close"; close.innerHTML = svg("close");
    close.addEventListener("click", () => removeTile(t));
    btns.append(t.linkBtn, focus, close);
    // Reading it? Hovering a screen keeps it open; leaving restarts the 4 s countdown.
    n.addEventListener("mouseenter", () => { t.hover = true; cancelClose(t); });
    n.addEventListener("mouseleave", () => {
      t.hover = false;
      if (t.phase === "explained" || (t.phase === "subject" && !speaking)) scheduleClose(t);
    });
    head.append(icon, titles, t.chip, btns);

    t.body = el("div", "tile-body");
    t.body.append(workingScene(k.icon, {}));
    n.append(head, t.body, el("div", "tile-scan"));
    t.node = n;
    return t;
  }

  function workingScene(icon, args) {
    const s = el("div", "scene-working");
    const orb = el("div", "sw-orb");
    orb.innerHTML = svg(icon);
    orb.append(el("span", "sw-ring"), el("span", "sw-ring r2"));
    const list = el("div", "sw-args");
    let i = 0;
    for (const [k, v] of Object.entries(args || {})) {
      if (i++ >= 5) break;
      const row = el("div", "sw-arg");
      row.style.animationDelay = `${0.12 * i}s`;
      row.append(el("span", "sw-k", pretty(k)), el("span", "sw-v", isScalar(v) ? fmtScalar(v) : cellText(v)));
      list.append(row);
    }
    const bar = el("div", "sw-bar");
    bar.append(el("span"));
    s.append(orb, list, bar, el("div", "sw-msg", "Working on it…"));
    return s;
  }

  function getTile(id, name) {
    let t = tiles.get(id);
    if (t) return t;
    t = makeTile(id, name);
    tiles.set(id, t);
    grid.append(t.node); // first command = top-left screen
    enforceLimit();
    layout();
    return t;
  }

  function setState(t, state, label) {
    t.state = state;
    t.node.dataset.st = state;
    t.chip.textContent = label;
    if (state === "done" || state === "failed") {
      t.done = Date.now();
      if (!t.player && !t.phase) queueForExplanation(t);
    } else if (t.phase) {
      forget(t); // running again / waiting for approval: not finished any more
    }
    lastActivity = Date.now();
  }

  // --------------------------------------------------- auto close --
  // Screens close ONE BY ONE, each 4 s after JARVIS finishes explaining *that* screen.
  //   phase "pending"   result is in, JARVIS hasn't started on it yet (results queue up in order)
  //   phase "subject"   the screen JARVIS is explaining right now
  //   phase "explained" JARVIS moved on to the next screen -> its 4 s countdown is running
  // Results explained one after another: screen 1's countdown starts the moment JARVIS starts on
  // screen 2, even though JARVIS is still talking. Several results covered in one answer close
  // one after another, 4 s apart. A short pause (< RESUME_MS) means JARVIS is still on the same screen.
  const RESUME_MS = 1500;
  let subject = null;   // tile being explained
  const pending = [];   // tiles waiting to be explained, oldest first
  let speechStart = 0;  // when the current / last speech started
  let lastStop = -Infinity;

  function closable(t) {
    return (t.state === "done" || t.state === "failed") && !t.player && !t.hover && ws.dataset.focus !== t.id;
  }

  function scheduleClose(t, delay = AUTO_CLOSE_MS) {
    cancelClose(t);
    if (!closable(t)) return;
    t.node.classList.add("closing");
    t.node.style.setProperty("--close-delay", `${delay - AUTO_CLOSE_MS}ms`); // bar waits its turn
    t.closeTimer = setTimeout(() => { if (closable(t)) removeTile(t); }, delay);
  }

  function cancelClose(t) {
    clearTimeout(t.closeTimer);
    t.closeTimer = null;
    t.node.classList.remove("closing");
  }

  function queueForExplanation(t) {
    if (speaking && !subject) { // JARVIS is already talking as the result lands: it's about this one
      t.phase = "subject";
      subject = t;
    } else {
      t.phase = "pending";
      pending.push(t);
    }
  }

  function forget(t) {
    cancelClose(t);
    const i = pending.indexOf(t);
    if (i >= 0) pending.splice(i, 1);
    if (subject === t) subject = null;
    t.phase = null;
  }

  function finishSubject() { // JARVIS moved on: the current screen's countdown starts now
    if (!subject) return;
    subject.phase = "explained";
    scheduleClose(subject);
    subject = null;
  }

  function agentState(state) {
    const was = speaking;
    speaking = state === "speaking";
    const now = Date.now();
    if (speaking && !was) {
      speechStart = now;
      for (const t of pending) cancelClose(t); // queued screens wait for their own turn
      const shortPause = now - lastStop < RESUME_MS;
      const newResult = pending.some((t) => t.done >= lastStop); // a result landed during the gap
      if (subject && !newResult && (shortPause || !pending.length)) {
        cancelClose(subject); // still explaining the same screen
      } else if (pending.length) {
        finishSubject(); // previous screen done -> its 4 s timer runs while JARVIS talks on
        subject = pending.shift();
        subject.phase = "subject";
        cancelClose(subject);
      }
    } else if (!speaking && was) {
      lastStop = now;
      let delay = AUTO_CLOSE_MS;
      if (subject) { scheduleClose(subject, delay); delay += AUTO_CLOSE_MS; }
      // Results that were already on screen during this answer were covered by it too:
      // close them one after another, 4 s apart (oldest first).
      for (const t of pending) {
        if (t.done < speechStart) { scheduleClose(t, delay); delay += AUTO_CLOSE_MS; }
      }
    }
  }

  // ------------------------------------- which screen is JARVIS talking about? --
  // Each screen keeps the words in its data (names, numbers, column names, dates, title). JARVIS's
  // live transcript is matched against them: a spoken word found on exactly ONE screen is a vote for
  // it. Two votes for another screen within a few words = JARVIS moved on, so the screen it was
  // explaining starts its 4 s countdown right then, even in the middle of one long answer.
  const SWITCH_VOTES = 2;  // unique-word hits needed to decide JARVIS moved to another screen
  const VOTE_WINDOW = 15;  // ... within this many spoken words
  const STOP = new Set(("the and for with that this from have has are was were you your total rows row " +
    "table count number value values there here which what about into than then they them their its " +
    "also just like more less over under very some each other only been being will would could should " +
    "not all any can one two now out our see shows showing data report list result results item items " +
    "okay boss sure let check got done").split(" "));
  const MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august",
    "september", "october", "november", "december"];

  function words(text) {
    const out = [];
    for (const m of String(text).matchAll(/\p{L}[\p{L}'’-]*|\d[\d,]*(?:\.\d+)?/gu)) {
      let w = m[0];
      if (/^\d/.test(w)) {
        w = w.replace(/,/g, "");
        if (w.includes(".")) {
          const n = Number(w);
          out.push(String(Math.round(n)), String(Math.trunc(n)));
          w = String(n);
        }
        if (w.length >= 2) out.push(w);
      } else {
        w = w.toLowerCase().normalize("NFKD").replace(/[̀-ͯ'’-]/g, "");
        if (w.length >= 3 && !STOP.has(w)) out.push(w);
      }
    }
    return out;
  }

  function collectKeys(v, set, depth = 0) {
    if (v == null || set.size > 5000 || depth > 6) return;
    if (Array.isArray(v)) { for (const x of v.slice(0, MAX_ROWS)) collectKeys(x, set, depth + 1); return; }
    if (typeof v === "object") {
      for (const [k, x] of Object.entries(v)) { words(pretty(k)).forEach((w) => set.add(w)); collectKeys(x, set, depth + 1); }
      return;
    }
    const s = String(v);
    words(s).forEach((w) => set.add(w));
    if (ISO.test(s)) { // spoken as "18 November 2026"
      const d = new Date(s);
      if (!isNaN(d)) { set.add(MONTHS[d.getMonth()]); set.add(String(d.getFullYear())); }
    }
  }

  function keysFor(t, value) {
    const set = new Set();
    collectKeys(value, set);
    words(t.titleEl.textContent).forEach((w) => set.add(w));
    words(pretty(t.name)).forEach((w) => set.add(w));
    return set;
  }

  const heard = new Map(); // transcript segment id -> characters already matched
  let recent = [];         // recent votes: {t, i}
  let wordNo = 0;

  function speech(id, text, final) {
    const from = heard.get(id) || 0;
    let to = text.length;
    if (!final) { // only whole words: the last one may still be growing
      const tail = text.search(/\S+$/);
      if (tail >= 0) to = tail;
    }
    if (to <= from) return;
    heard.set(id, to);
    if (heard.size > 200) heard.delete(heard.keys().next().value);
    for (const w of words(text.slice(from, to))) hear(w);
  }

  function hear(w) {
    wordNo++;
    let match = null;
    for (const t of tiles.values()) {
      if (!t.phase || !t.keys || !t.keys.has(w)) continue;
      if (match) return; // on several screens: says nothing about which one
      match = t;
    }
    if (!match) return;
    match.hits = (match.hits || 0) + 1;
    recent = recent.filter((r) => wordNo - r.i < VOTE_WINDOW && tiles.has(r.t.id));
    recent.push({ t: match, i: wordNo });
    if (match === subject) { cancelClose(match); return; }
    if (recent.filter((r) => r.t === match).length >= SWITCH_VOTES) switchTo(match);
  }

  function switchTo(t) {
    if (subject) {
      if (subject.hits) finishSubject(); // it was explained -> 4 s countdown starts now
      else { subject.phase = "pending"; pending.push(subject); subject = null; } // never got to it
    }
    const i = pending.indexOf(t);
    if (i >= 0) pending.splice(i, 1);
    pending.sort((a, b) => a.done - b.done);
    t.phase = "subject";
    subject = t;
    cancelClose(t); // JARVIS came back to it: keep it open
    recent = [];
  }

  function showResult(t, node) {
    t.body.replaceChildren();
    const r = el("div", "tile-result");
    r.append(node);
    t.body.append(r);
  }

  function removeTile(t) {
    if (!tiles.has(t.id)) return;
    forget(t);
    tiles.delete(t.id);
    if (ws.dataset.focus === t.id) delete ws.dataset.focus;
    t.node.classList.add("leaving");
    setTimeout(() => { t.node.remove(); layout(); }, 220);
    layout(tiles.size);
  }

  function enforceLimit() {
    const finished = [...tiles.values()].filter((t) => t.state === "done" || t.state === "failed")
      .sort((a, b) => (a.player - b.player) || (a.done - b.done));
    while (tiles.size > MAX_TILES && finished.length) removeTile(finished.shift());
  }

  function toggleFocus(t, btn) {
    const on = ws.dataset.focus !== t.id;
    for (const x of tiles.values()) x.node.classList.toggle("focus", on && x === t);
    if (on) ws.dataset.focus = t.id; else delete ws.dataset.focus;
    if (on) cancelClose(t); // a full-screen view stays until you shrink it
    else if (t.phase === "explained" || (t.phase === "subject" && !speaking)) scheduleClose(t);
    btn.innerHTML = svg(on ? "shrink" : "expand");
    btn.title = on ? "Back to all screens" : "Full screen";
  }

  /** Split the workspace equally: 1 = full, 2 = halves, 4 = 2x2, 6 = 3x2 ... A shorter last row
   *  (e.g. 5 = 3 + 2) gets wider screens so there is never an empty gap. */
  function layout(n = tiles.size) {
    grid.dataset.n = String(Math.min(n, MAX_TILES));
    const cols = n <= 3 ? Math.max(n, 1) : n === 4 ? 2 : 3;
    const rows = Math.max(1, Math.ceil(n / cols));
    const last = n - (rows - 1) * cols; // screens in the last row
    const gcd = (a, b) => (b ? gcd(b, a % b) : a);
    const units = (cols * last) / gcd(cols, last); // grid columns that both row sizes divide evenly
    grid.style.gridTemplateColumns = `repeat(${units}, minmax(0, 1fr))`;
    grid.style.gridTemplateRows = `repeat(${rows}, minmax(0, 1fr))`;
    [...tiles.values()].forEach((t, i) => {
      t.node.style.gridColumn = `span ${i >= (rows - 1) * cols ? units / last : units / cols}`;
    });
    countEl.textContent = n ? `${n} screen${n === 1 ? "" : "s"}` : "";
    const on = n > 0;
    if (document.body.classList.contains("ws-on") !== on) {
      document.body.classList.toggle("ws-on", on);
      ws.hidden = !on;
      requestAnimationFrame(() => window.dispatchEvent(new Event("resize"))); // orb canvas re-measures
    }
  }

  // ------------------------------------------------------- public API --
  function tool(ev) {
    if (!ev.call_id) return;
    if (ev.state === "start") {
      const t = getTile(ev.call_id, ev.name);
      if (ev.args && Object.keys(ev.args).length && !t.player) {
        t.body.replaceChildren(workingScene(kindOf(ev.name).icon, ev.args));
      }
      lastActivity = Date.now();
    } else if (ev.state === "progress") {
      const t = tiles.get(ev.call_id);
      const msg = t && t.body.querySelector(".sw-msg");
      if (msg) msg.textContent = ev.message || "";
    } else if (ev.state === "cancelled") {
      const t = tiles.get(ev.call_id);
      if (t) { setState(t, "failed", "Cancelled"); if (!t.player) showResult(t, el("div", "ws-empty", "Cancelled.")); }
    } else if (ev.state === "result") {
      const t = getTile(ev.call_id, ev.name);
      if (t.state !== "failed" || ev.error) setState(t, ev.error ? "failed" : "done", ev.error ? "Failed" : "Done");
      if (t.player) { // keep the player; the result is just a caption under it
        if (!ev.error && typeof ev.output === "string" && ev.output.length < 400) t.caption.textContent = ev.output;
        return;
      }
      let node;
      const value = decode(ev.output);
      try { node = ev.error ? linkified(String(value || "Failed."), "ws-text err") : renderValue(value); }
      catch (e) { console.warn("workspace render", e); node = el("pre", "ws-pre", String(ev.output)); }
      showResult(t, node);
      try { t.keys = keysFor(t, value); } catch { t.keys = null; } // for matching what JARVIS says
      t.hits = 0;
    }
  }

  function action(ev) {
    if (!ev.call_id) return;
    const t = getTile(ev.call_id, ev.capability);
    if (ev.summary) t.titleEl.textContent = ev.summary;
    if (ev.capability) t.subEl.textContent = ev.capability;
    switch (ev.status) {
      case "awaiting_approval": setState(t, "approval", "Needs approval"); break;
      case "running": if (t.state === "approval" || t.state === "running") setState(t, "running", "Working"); break;
      case "denied": case "blocked": case "needs_confirmation":
        setState(t, "failed", ev.status === "needs_confirmation" ? "Needs confirm" : pretty(ev.status));
        if (!t.player) showResult(t, linkified(ev.reason || "Not allowed.", "ws-text err"));
        break;
      case "failed":
        setState(t, "failed", "Failed");
        if (!t.player) showResult(t, linkified(ev.error || "Failed.", "ws-text err"));
        break;
      default: break; // "done": the tool's own result (JarvisWS.tool) fills the screen
    }
  }

  const PLAYER_HOSTS = {
    youtube: ["www.youtube-nocookie.com", "www.youtube.com"],
    spotify: ["open.spotify.com"],
  };

  function player(ev) {
    if (ev.state === "off") {
      for (const t of [...tiles.values()]) if (t.player) removeTile(t);
      return;
    }
    let url;
    try { url = new URL(ev.url); } catch { return; }
    const hosts = PLAYER_HOSTS[ev.provider];
    if (!hosts || url.protocol !== "https:" || !hosts.includes(url.host)) return;
    for (const t of [...tiles.values()]) if (t.player && t.id !== ev.call_id) removeTile(t); // one player at a time

    const t = getTile(ev.call_id || `player-${Date.now()}`, ev.provider);
    t.player = true;
    t.node.classList.add("is-player", ev.provider);
    t.node.style.setProperty("--kind", ev.provider === "youtube" ? "#ff0033" : "#1db954");
    t.titleEl.textContent = ev.title || "Now playing";
    t.subEl.textContent = ev.provider === "youtube" ? "YouTube" : "Spotify";
    setState(t, "done", "Playing");
    const safe = /^https:\/\//.test(ev.link || "") ? ev.link : "";
    t.linkBtn.hidden = !safe;
    if (safe) t.linkBtn.href = safe;

    const box = el("div", "tile-player " + (ev.provider === "youtube" ? "video" : "spotify" + (ev.tall ? " tall" : "")));
    const f = document.createElement("iframe");
    f.src = url.href;
    f.title = ev.title || "Player";
    f.allow = "autoplay; encrypted-media; picture-in-picture; fullscreen; clipboard-write";
    f.allowFullscreen = true;
    f.referrerPolicy = "strict-origin-when-cross-origin"; // the dashboard sends no referrer; YouTube needs one
    box.append(f);
    t.caption = el("div", "ws-caption");
    t.body.replaceChildren(box, t.caption);
  }

  function clear() {
    for (const t of [...tiles.values()]) removeTile(t);
  }

  document.getElementById("ws-clear").addEventListener("click", clear);
  document.getElementById("ws-panels").addEventListener("click", () => {
    const on = document.body.classList.toggle("ws-panels");
    document.getElementById("ws-panels").classList.toggle("on", on);
    requestAnimationFrame(() => window.dispatchEvent(new Event("resize")));
  });

  window.JarvisWS = { tool, action, player, clear, agentState, speech, _decode: decode };
})();
