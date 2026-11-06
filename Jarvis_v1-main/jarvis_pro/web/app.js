/* JARVIS PRO dashboard */
(() => {
  "use strict";

  const LK = window.LivekitClient;
  const { Room, RoomEvent, Track, ConnectionState } = LK;

  const EVENTS_TOPIC = "jarvis.events";
  const APPROVE_RPC = "jarvis.approve";
  const $ = (id) => document.getElementById(id);

  const STATES = {
    standby:      { label: "Standby",    sub: "Connect to wake JARVIS",   color: [120, 140, 170] },
    connecting:   { label: "Connecting", sub: "Establishing secure link", color: [255, 181, 71] },
    initializing: { label: "Booting",    sub: "JARVIS is coming online",  color: [255, 181, 71] },
    listening:    { label: "Listening",  sub: "Go ahead, I'm listening",  color: [56, 214, 255] },
    thinking:     { label: "Thinking",   sub: "Working on it",            color: [124, 107, 255] },
    speaking:     { label: "Speaking",   sub: "",                         color: [56, 214, 255] },
    approval:     { label: "Awaiting",   sub: "Your approval is needed",  color: [255, 181, 71] },
  };

  let room = null;
  let agentState = "standby";
  let agentAnalyser = null;
  let micAnalyser = null;
  let audioCtx = null;
  let unseenActivity = 0;
  const transcriptEls = new Map();
  const activityEls = new Map();

  // ------------------------------------------------------------ helpers --
  function safeStorage(fn, fallback = null) {
    try { return fn(); } catch { return fallback; }
  }

  function apiKey() {
    return safeStorage(() => sessionStorage.getItem("jarvis-key"), "") || "";
  }

  async function api(path, opts = {}) {
    const res = await fetch(path, { ...opts, headers: { "x-jarvis-key": apiKey(), ...(opts.headers || {}) } });
    if (res.status === 429) throw new Error((await res.json().catch(() => ({}))).detail || "Locked out");
    if (res.status === 401) {
      const key = window.prompt(apiKey() ? "Wrong password. Dashboard password:" : "Dashboard password:");
      if (key == null) throw new Error("Password required");
      safeStorage(() => sessionStorage.setItem("jarvis-key", key));
      // cookie lets report links (opened in a new tab) authenticate too
      document.cookie = `jarvis_key=${encodeURIComponent(key)}; path=/; SameSite=Strict` + (location.protocol === "https:" ? "; Secure" : "");
      return api(path, opts);
    }
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
    return res.json();
  }

  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }

  function timeOf(ts) {
    const d = ts ? new Date(ts) : new Date();
    return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  }

  function clearEmpty(container) {
    const empty = container.querySelector(".empty");
    if (empty) empty.remove();
  }

  function scrollToEnd(container) {
    const nearBottom = container.scrollHeight - container.scrollTop - container.clientHeight < 120;
    if (nearBottom) container.scrollTop = container.scrollHeight;
  }

  // ------------------------------------------------------------- state --
  function setState(state, sub) {
    agentState = state;
    const s = STATES[state] || STATES.standby;
    $("agent-state").textContent = s.label;
    $("agent-sub").textContent = sub ?? s.sub;
  }

  function setConnection(kind, text) {
    const c = $("conn");
    c.className = "conn " + kind;
    $("conn-text").textContent = text;
  }

  function setControls(connected) {
    ["btn-mic", "btn-screen", "btn-cam", "chat-input", "chat-send"].forEach((id) => ($(id).disabled = !connected));
    const b = $("btn-connect");
    b.textContent = connected ? "Disconnect" : "Connect";
    b.classList.toggle("live", connected);
    b.disabled = false;
    if (!connected) {
      ["btn-mic", "btn-screen", "btn-cam"].forEach((id) => $(id).classList.remove("on", "muted-mic"));
      $("preview").hidden = true;
    }
  }

  // ------------------------------------------------------------ access --
  async function loadAccess() {
    try {
      const data = await api("/api/access");
      const list = $("access-list");
      list.replaceChildren();
      for (const cap of data.capabilities) {
        const card = el("div", "cap" + (cap.enabled ? "" : " off"));
        const top = el("div", "cap-top");
        top.append(el("span", "cap-name", cap.id), el("span", "risk " + (cap.risk || "medium"), cap.risk || "medium"));
        card.append(top, el("div", "cap-desc", cap.description || ""));
        const acts = el("div", "cap-actions");
        acts.append(el("span", "switch", cap.enabled ? "● enabled" : "○ disabled"));
        for (const [name, level] of Object.entries(cap.actions)) acts.append(el("span", "chip " + level, name));
        card.append(acts);
        list.append(card);
      }
    } catch (e) {
      console.warn("access load failed", e);
    }
  }

  // ---------------------------------------------------------- activity --
  function renderActivity(ev, live = true) {
    const box = $("tab-activity");
    clearEmpty(box);
    const key = ev.id || `${ev.ts}-${ev.capability}-${ev.status}`;
    let card = activityEls.get(key);
    if (!card) {
      card = el("div", "act");
      const top = el("div", "act-top");
      top.append(el("span", "act-cap"), el("span", "act-status"));
      card.append(top, el("div", "act-sum"), el("div", "act-time"));
      activityEls.set(key, card);
      if (live) box.prepend(card); else box.append(card);
    }
    card.className = "act s-" + (ev.status || "info");
    card.querySelector(".act-cap").textContent = ev.capability || "event";
    card.querySelector(".act-status").textContent = (ev.status || "").replace(/_/g, " ");
    card.querySelector(".act-sum").textContent = ev.summary || "";
    card.querySelector(".act-time").textContent = timeOf(ev.ts) + (ev.approved_by && ev.approved_by !== "policy" ? `  ·  approved via ${ev.approved_by}` : "");
    const errText = ev.error || (ev.status === "denied" || ev.status === "blocked" ? ev.reason : "");
    let err = card.querySelector(".act-err");
    if (errText) {
      if (!err) { err = el("div", "act-err"); card.append(err); }
      err.textContent = errText;
    } else if (err) err.remove();

    if (live && $("tab-activity").hidden) {
      unseenActivity += 1;
      const badge = $("activity-badge");
      badge.textContent = String(unseenActivity);
      badge.hidden = false;
    }
  }

  async function loadAudit() {
    try {
      const rows = await api("/api/audit?limit=60");
      for (const row of rows) renderActivity(row, false);
    } catch (e) {
      console.warn("audit load failed", e);
    }
  }

  // -------------------------------------------------------- transcript --
  function upsertLine(id, who, text, final) {
    const box = $("tab-conversation");
    clearEmpty(box);
    let node = transcriptEls.get(id);
    if (!node) {
      node = el("div", "msg " + who);
      node.append(el("span", "who", who === "agent" ? "JARVIS" : "You"), el("span", "txt"));
      transcriptEls.set(id, node);
      box.append(node);
    }
    node.querySelector(".txt").textContent = text;
    node.classList.toggle("interim", !final);
    scrollToEnd(box);
  }

  // ------------------------------------------------- open link on device --
  function openOnThisDevice(url) {
    let safe;
    try { safe = new URL(url); } catch { return; }
    if (!/^https?:$/.test(safe.protocol)) return;   // never javascript:, file:, etc.
    const win = window.open(safe.href, "_blank", "noopener");
    // Phones usually block pop-ups that weren't started by a tap, so always offer a tappable link too.
    const toast = $("toast");
    const link = $("toast-link");
    link.href = safe.href;
    link.textContent = safe.hostname;
    $("toast-text").textContent = win ? "Opened in a new tab:" : "JARVIS wants to open:";
    toast.hidden = false;
    clearTimeout(openOnThisDevice.timer);
    openOnThisDevice.timer = setTimeout(() => (toast.hidden = true), 20000);
  }
  $("toast-close").addEventListener("click", () => ($("toast").hidden = true));
  $("toast-link").addEventListener("click", () => setTimeout(() => ($("toast").hidden = true), 300));

  // ---------------------------------------------------------- approval --
  let approvalResolve = null;
  let approvalTimer = null;

  function askApproval(ev, timeoutMs) {
    return new Promise((resolve) => {
      const dlg = $("approval");
      if (approvalResolve) approvalResolve(false); // only one at a time
      approvalResolve = resolve;
      $("approval-cap").textContent = ev.capability;
      $("approval-summary").textContent = ev.summary;
      const details = ev.details && Object.keys(ev.details).length ? JSON.stringify(ev.details, null, 2) : "";
      $("approval-details").textContent = details;
      $("approval-details").hidden = !details;

      const bar = $("approval-timer");
      const total = Math.max(5000, (timeoutMs || 60000) - 2000);
      const start = performance.now();
      clearInterval(approvalTimer);
      approvalTimer = setInterval(() => {
        const left = 1 - (performance.now() - start) / total;
        bar.style.transform = `scaleX(${Math.max(0, left)})`;
        if (left <= 0) finishApproval(false);
      }, 100);

      setState("approval");
      if (!dlg.open) dlg.showModal();
      if (document.hidden && "Notification" in window && Notification.permission === "granted") {
        new Notification("JARVIS needs approval", { body: ev.summary });
      }
    });
  }

  function finishApproval(approved) {
    clearInterval(approvalTimer);
    const dlg = $("approval");
    if (dlg.open) dlg.close();
    if (agentState === "approval") setState("thinking");
    if (approvalResolve) { approvalResolve(approved); approvalResolve = null; }
  }

  $("approval-approve").addEventListener("click", () => finishApproval(true));
  $("approval-deny").addEventListener("click", () => finishApproval(false));
  $("approval").addEventListener("cancel", (e) => { e.preventDefault(); finishApproval(false); });

  // ------------------------------------------------------------- audio --
  function analyserFor(mediaStreamTrack) {
    audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
    const src = audioCtx.createMediaStreamSource(new MediaStream([mediaStreamTrack]));
    const an = audioCtx.createAnalyser();
    an.fftSize = 256;
    an.smoothingTimeConstant = 0.75;
    src.connect(an);
    return { an, buf: new Uint8Array(an.frequencyBinCount) };
  }

  function level(a) {
    if (!a) return 0;
    a.an.getByteFrequencyData(a.buf);
    let sum = 0;
    for (let i = 2; i < 48; i++) sum += a.buf[i];
    return Math.min(1, sum / (46 * 160));
  }

  // --------------------------------------------------------------- orb --
  const canvas = $("orb");
  const ctx2d = canvas.getContext("2d");
  let col = [120, 140, 170];
  let amp = 0;
  let t = 0;

  function resize() {
    const r = canvas.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    canvas.width = r.width * dpr;
    canvas.height = r.height * dpr;
    ctx2d.setTransform(dpr, 0, 0, dpr, 0, 0);
  }
  window.addEventListener("resize", resize);
  resize();

  function draw() {
    const w = canvas.clientWidth, h = canvas.clientHeight;
    const cx = w / 2, cy = h / 2, R = Math.min(w, h) * 0.3;
    const target = STATES[agentState]?.color || STATES.standby.color;
    col = col.map((c, i) => c + (target[i] - c) * 0.06);
    const rgb = (a) => `rgba(${col[0] | 0},${col[1] | 0},${col[2] | 0},${a})`;

    let want = 0;
    if (agentState === "speaking") want = level(agentAnalyser);
    else if (agentState === "listening") want = level(micAnalyser) * 0.6;
    amp += (want - amp) * 0.25;
    t += agentState === "thinking" ? 0.045 : 0.012;

    ctx2d.clearRect(0, 0, w, h);

    // glow
    const g = ctx2d.createRadialGradient(cx, cy, R * 0.2, cx, cy, R * 1.9);
    g.addColorStop(0, rgb(0.28 + amp * 0.35));
    g.addColorStop(0.5, rgb(0.06));
    g.addColorStop(1, rgb(0));
    ctx2d.fillStyle = g;
    ctx2d.fillRect(0, 0, w, h);

    // organic core blob
    ctx2d.beginPath();
    const pts = 96;
    for (let i = 0; i <= pts; i++) {
      const a = (i / pts) * Math.PI * 2;
      const n = Math.sin(a * 3 + t * 2) * 0.03 + Math.sin(a * 5 - t * 3) * 0.02 + Math.sin(a * 7 + t) * amp * 0.12;
      const r = R * (0.78 + amp * 0.22 + n);
      const x = cx + Math.cos(a) * r, y = cy + Math.sin(a) * r;
      i ? ctx2d.lineTo(x, y) : ctx2d.moveTo(x, y);
    }
    const core = ctx2d.createRadialGradient(cx, cy - R * 0.3, R * 0.1, cx, cy, R);
    core.addColorStop(0, rgb(0.35));
    core.addColorStop(1, rgb(0.06));
    ctx2d.fillStyle = core;
    ctx2d.fill();
    ctx2d.lineWidth = 1.5;
    ctx2d.strokeStyle = rgb(0.85);
    ctx2d.shadowColor = rgb(0.9);
    ctx2d.shadowBlur = 18;
    ctx2d.stroke();
    ctx2d.shadowBlur = 0;

    // HUD rings
    const rings = [
      { r: 1.12, w: 1, a: 0.35, seg: 1, speed: 0 },
      { r: 1.25, w: 2, a: 0.7, seg: 3, speed: 1 },
      { r: 1.38, w: 1, a: 0.4, seg: 24, speed: -0.5 },
    ];
    for (const ring of rings) {
      ctx2d.strokeStyle = rgb(ring.a);
      ctx2d.lineWidth = ring.w;
      const rr = R * ring.r;
      if (ring.seg === 1) {
        ctx2d.beginPath(); ctx2d.arc(cx, cy, rr, 0, Math.PI * 2); ctx2d.stroke();
        continue;
      }
      const span = (Math.PI * 2) / ring.seg;
      for (let i = 0; i < ring.seg; i++) {
        const s = i * span + t * ring.speed;
        ctx2d.beginPath();
        ctx2d.arc(cx, cy, rr, s, s + span * (ring.seg > 6 ? 0.35 : 0.6));
        ctx2d.stroke();
      }
    }

    // ticks
    ctx2d.strokeStyle = rgb(0.25);
    ctx2d.lineWidth = 1;
    for (let i = 0; i < 72; i++) {
      const a = (i / 72) * Math.PI * 2;
      const r1 = R * 1.5, r2 = R * (i % 6 === 0 ? 1.58 : 1.54);
      ctx2d.beginPath();
      ctx2d.moveTo(cx + Math.cos(a) * r1, cy + Math.sin(a) * r1);
      ctx2d.lineTo(cx + Math.cos(a) * r2, cy + Math.sin(a) * r2);
      ctx2d.stroke();
    }
    requestAnimationFrame(draw);
  }
  requestAnimationFrame(draw);

  // ---------------------------------------------------------- livekit --
  async function connect() {
    $("btn-connect").disabled = true;
    setConnection("connecting", "Connecting…");
    setState("connecting");
    try {
      const { url, token } = await api("/api/token", { method: "POST" });
      room = new Room({
        adaptiveStream: true,
        dynacast: true,
        audioCaptureDefaults: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      });
      wireRoom(room);
      await room.connect(url, token);
      await room.startAudio();
      await room.localParticipant.setMicrophoneEnabled(true);
      const micPub = room.localParticipant.getTrackPublication(Track.Source.Microphone);
      if (micPub?.track) micAnalyser = analyserFor(micPub.track.mediaStreamTrack);
      $("btn-mic").classList.add("on");
      setConnection("connected", "Connected");
      setControls(true);
      setState("initializing");
      if ("Notification" in window && Notification.permission === "default") Notification.requestPermission();
    } catch (e) {
      console.error(e);
      setConnection("", "Connection failed");
      setState("standby", String(e.message || e));
      setControls(false);
      if (room) { await room.disconnect(); room = null; }
    }
  }

  function wireRoom(r) {
    r.registerTextStreamHandler("lk.transcription", async (reader, participantInfo) => {
      const who = participantInfo.identity === r.localParticipant.identity ? "user" : "agent";
      const id = reader.info.attributes?.["lk.segment_id"] || reader.info.id;
      let text = "";
      for await (const chunk of reader) {
        text += chunk;
        upsertLine(id, who, text, false);
      }
      upsertLine(id, who, text, true);
    });

    r.localParticipant.registerRpcMethod(APPROVE_RPC, async (data) => {
      const ev = JSON.parse(data.payload);
      const approved = await askApproval(ev, data.responseTimeout);
      return JSON.stringify({ approved });
    });

    r.on(RoomEvent.DataReceived, (payload, _p, _kind, topic) => {
      if (topic !== EVENTS_TOPIC) return;
      try {
        const ev = JSON.parse(new TextDecoder().decode(payload));
        if (ev.type === "action") renderActivity(ev, true);
        if (ev.type === "open_url") openOnThisDevice(ev.url);
      } catch (e) { console.warn("bad event", e); }
    });

    r.on(RoomEvent.ParticipantAttributesChanged, (_changed, p) => {
      const s = p.attributes?.["lk.agent.state"];
      if (s && p !== r.localParticipant && agentState !== "approval") setState(s);
    });

    r.on(RoomEvent.TrackSubscribed, (track, _pub, participant) => {
      if (track.kind !== Track.Kind.Audio) return;
      $("audio-sink").append(track.attach());
      agentAnalyser = analyserFor(track.mediaStreamTrack);
      const s = participant.attributes?.["lk.agent.state"];
      if (s) setState(s);
    });

    r.on(RoomEvent.TrackUnsubscribed, (track) => track.detach().forEach((n) => n.remove()));

    r.on(RoomEvent.LocalTrackPublished, (pub) => {
      if (pub.source === Track.Source.ScreenShare || pub.source === Track.Source.Camera) showPreview(pub);
    });
    r.on(RoomEvent.LocalTrackUnpublished, (pub) => {
      if (pub.source === Track.Source.ScreenShare) $("btn-screen").classList.remove("on");
      if (pub.source === Track.Source.Camera) $("btn-cam").classList.remove("on");
      refreshPreview();
    });

    r.on(RoomEvent.ParticipantDisconnected, (p) => {
      if (p.isAgent) setState("standby", "JARVIS left the room");
    });

    r.on(RoomEvent.ConnectionStateChanged, (s) => {
      if (s === ConnectionState.Reconnecting) setConnection("connecting", "Reconnecting…");
      if (s === ConnectionState.Connected) setConnection("connected", "Connected");
    });

    r.on(RoomEvent.Disconnected, () => {
      setConnection("", "Offline");
      setState("standby");
      setControls(false);
      finishApproval(false);
      agentAnalyser = micAnalyser = null;
      room = null;
    });
  }

  function showPreview(pub) {
    const v = $("preview-video");
    pub.track.attach(v);
    $("preview-label").textContent = pub.source === Track.Source.ScreenShare ? "JARVIS can see your screen" : "JARVIS can see your camera";
    $("preview").hidden = false;
  }

  function refreshPreview() {
    if (!room) return;
    const pub = room.localParticipant.getTrackPublication(Track.Source.ScreenShare)
      || room.localParticipant.getTrackPublication(Track.Source.Camera);
    if (pub?.track) showPreview(pub); else $("preview").hidden = true;
  }

  // --------------------------------------------------------- controls --
  $("btn-connect").addEventListener("click", async () => {
    if (room) { $("btn-connect").disabled = true; await room.disconnect(); }
    else await connect();
  });

  $("btn-mic").addEventListener("click", async () => {
    if (!room) return;
    const on = !room.localParticipant.isMicrophoneEnabled;
    await room.localParticipant.setMicrophoneEnabled(on);
    $("btn-mic").classList.toggle("on", on);
    $("btn-mic").classList.toggle("muted-mic", !on);
  });

  $("btn-screen").addEventListener("click", async () => {
    if (!room) return;
    const on = !room.localParticipant.isScreenShareEnabled;
    try {
      await room.localParticipant.setScreenShareEnabled(on, { audio: false });
      $("btn-screen").classList.toggle("on", on);
      if (!on) refreshPreview();
    } catch (e) { console.warn("screen share cancelled", e); }
  });

  $("btn-cam").addEventListener("click", async () => {
    if (!room) return;
    const on = !room.localParticipant.isCameraEnabled;
    try {
      await room.localParticipant.setCameraEnabled(on);
      $("btn-cam").classList.toggle("on", on);
      if (!on) refreshPreview();
    } catch (e) { console.warn("camera failed", e); }
  });

  $("chat-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const input = $("chat-input");
    const text = input.value.trim();
    if (!text || !room) return;
    input.value = "";
    upsertLine("chat-" + Date.now(), "user", text, true);
    await room.localParticipant.sendText(text, { topic: "lk.chat" });
  });

  document.querySelectorAll(".tab").forEach((tab) =>
    tab.addEventListener("click", () => {
      document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t === tab));
      const name = tab.dataset.tab;
      $("tab-conversation").hidden = name !== "conversation";
      $("tab-activity").hidden = name !== "activity";
      $("tab-websites").hidden = name !== "websites";
      if (name === "activity") { unseenActivity = 0; $("activity-badge").hidden = true; }
      if (name === "websites") loadWebsites();
    })
  );

  // ----------------------------------------------------------- websites --
  async function loadWebsites() {
    const list = $("site-list"), reports = $("report-list");
    try {
      const data = await api("/api/websites");
      list.replaceChildren();
      reports.replaceChildren();
      if (!data.allowed) { list.append(el("div", "muted small", "websites.report is denied in access.yaml.")); return; }
      if (!data.sites.length) list.append(el("div", "muted small", "No websites configured yet."));
      for (const s of data.sites) {
        const card = el("div", "site");
        const top = el("div", "site-top");
        const state = s.up == null ? "unknown" : (s.up ? "up" : "down");
        top.append(el("span", "site-name", s.name), el("span", "site-state " + state, "● " + state.toUpperCase()));
        card.append(top, el("div", "site-url", s.url));
        const meta = [];
        if (s.response_ms != null) meta.push(`${s.response_ms} ms`);
        if (s.ssl_days_left != null) meta.push(`SSL ${s.ssl_days_left} days`);
        if (s.ts) meta.push(`checked ${timeOf(s.ts)}`);
        if (meta.length) card.append(el("div", "site-meta", meta.join(" · ")));
        if (s.issues?.length) {
          const ul = el("ul", "site-issues");
          s.issues.forEach((i) => ul.append(el("li", null, i)));
          card.append(ul);
        }
        list.append(card);
      }
      if (!data.reports.length) reports.append(el("div", "muted small", "No reports yet."));
      for (const day of data.reports) {
        const a = el("a", "report-link", day);
        a.href = `/reports/${day}`;
        a.target = "_blank";
        a.rel = "noopener";
        reports.append(a);
      }
    } catch (e) {
      list.replaceChildren(el("div", "muted small", "Could not load websites: " + e.message));
    }
  }

  $("btn-report").addEventListener("click", async () => {
    const b = $("btn-report");
    b.disabled = true;
    b.textContent = "Checking sites…";
    try {
      await api("/api/websites/report", { method: "POST" });
      await loadWebsites();
    } catch (e) {
      alert("Report failed: " + e.message);
    } finally {
      b.disabled = false;
      b.textContent = "Generate report";
    }
  });

  // -------------------------------------------------------------- boot --
  setInterval(() => { $("clock").textContent = new Date().toLocaleString([], { weekday: "short", hour: "2-digit", minute: "2-digit", second: "2-digit" }); }, 1000);
  setState("standby");
  setControls(false);
  loadAccess();
  loadAudit();
  setInterval(loadAccess, 10000);
})();
