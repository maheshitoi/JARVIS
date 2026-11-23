/* Wake word: say "Hey JARVIS" (or "JARVIS, …") to connect hands-free; "Goodbye JARVIS" to sleep.
 *
 * Uses the browser's built-in speech recognition (Chrome / Edge). It runs only while you are NOT
 * connected; once connected, LiveKit has the mic and the sleep phrase is caught from JARVIS's own
 * transcript. Whatever you say after the wake word ("Hey JARVIS, what's on my calendar?") is sent
 * with the connection, so JARVIS answers it straight away instead of greeting you.
 * Auto-sleep: after AUTO_SLEEP_MIN minutes with nothing said, JARVIS disconnects and goes back to
 * listening for the wake word (a live voice session costs API time).
 *
 * Privacy: Chrome/Edge may send the audio heard while listening to their speech service (Google /
 * Microsoft) unless on-device recognition is available; the button tooltip says which is in use.
 */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const btn = $("btn-wake");
  const label = $("wake-label");
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  const STORE = "jarvis-wake";
  const AUTO_SLEEP_MIN = 3;
  // "hey jarvis", "ok jarvis", "jarvis," + optional command ("jervis" is a common mishearing).
  const NAME = "(?:jarvis|jervis)";
  const WAKE = new RegExp(`\\b${NAME}\\b[\\s,.!?]*(.*)$`, "i");
  const STRICT_WAKE = new RegExp(`\\b(?:hey|hi|hello|ok|okay)[\\s,]+${NAME}\\b|^\\s*${NAME}\\b`, "i");
  // Whole utterance is a goodbye ("Goodbye JARVIS", "bye", "that's all"), or "JARVIS, go to sleep".
  const SLEEP = new RegExp(
    `^\\s*(?:ok(?:ay)?[\\s,]*)?(?:thanks?[\\s,]*)?(?:good ?bye|bye|that'?s all|go to sleep|sleep|stand ?by)(?:[\\s,]+${NAME})?[\\s.!]*$`
    + `|\\b${NAME}[\\s,]+(?:go to sleep|sleep|stand ?by|good ?bye)\\b`, "i");

  let enabled = false;
  let rec = null;
  let running = false;
  let blocked = false;
  let woke = false;          // wake word heard; waiting for the rest of the sentence
  let wakeTimer = null;
  let lastActivity = Date.now();
  let onDevice = false;

  const store = (v) => { try { localStorage.setItem(STORE, v ? "1" : "0"); } catch { /* private mode */ } };
  const stored = () => { try { return localStorage.getItem(STORE) === "1"; } catch { return false; } };

  function render(text, mode) {
    btn.dataset.mode = mode;               // off | listening | heard | awake | blocked
    btn.setAttribute("aria-pressed", String(enabled));
    label.textContent = text;
    btn.title = !SR ? "The wake word needs Chrome or Edge."
      : mode === "blocked" ? "Microphone access was blocked. Click to try again (allow the mic for this site)."
      : enabled ? `Listening for “Hey JARVIS” (${onDevice ? "on-device recognition" : "browser speech service"}). Click to turn off.`
      : "Turn on hands-free: say “Hey JARVIS” to connect, “Goodbye JARVIS” to sleep.";
  }

  function chime() {
    try {
      const ctx = new AudioContext();
      const o = ctx.createOscillator(), g = ctx.createGain();
      o.type = "sine";
      o.frequency.setValueAtTime(880, ctx.currentTime);
      o.frequency.exponentialRampToValueAtTime(1320, ctx.currentTime + 0.12);
      g.gain.setValueAtTime(0.0001, ctx.currentTime);
      g.gain.exponentialRampToValueAtTime(0.15, ctx.currentTime + 0.02);
      g.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + 0.25);
      o.connect(g).connect(ctx.destination);
      o.start(); o.stop(ctx.currentTime + 0.26);
      setTimeout(() => ctx.close(), 400);
    } catch { /* no audio */ }
  }

  async function preferOnDevice(r, lang) {
    // Chrome's on-device speech recognition, where available: audio stays on this computer.
    try {
      if (typeof SR.available === "function" && "processLocally" in r) {
        const st = await SR.available({ langs: [lang], processLocally: true });
        if (st === "available") { r.processLocally = true; return true; }
      }
    } catch { /* not supported */ }
    return false;
  }

  async function listen() {
    if (!SR || !enabled || running || blocked || window.JarvisApp?.connected()) return;
    const lang = navigator.language || "en-US";
    rec = new SR();
    rec.lang = lang;
    rec.continuous = true;
    rec.interimResults = true;
    rec.maxAlternatives = 1;
    onDevice = await preferOnDevice(rec, lang);
    rec.onresult = (e) => {
      for (let i = e.resultIndex; i < e.results.length; i++) {
        const text = e.results[i][0].transcript;
        const final = e.results[i].isFinal;
        const m = text.match(WAKE);
        if (!m) continue;
        // Interim results are noisy: only act early on a clear "hey jarvis"; otherwise wait for final.
        if (!woke && (final || STRICT_WAKE.test(text))) {
          woke = true;
          chime();
          render("Heard you…", "heard");
          // If no more words follow, connect anyway after a short pause.
          wakeTimer = setTimeout(() => wake(""), 1800);
        }
        if (woke && final) { clearTimeout(wakeTimer); wake(m[1].trim()); }
      }
    };
    rec.onerror = (e) => {
      if (e.error === "not-allowed" || e.error === "service-not-allowed") {
        blocked = true;
        render("Mic blocked", "blocked");
      }
      // "no-speech", "network", "aborted": onend restarts it
    };
    rec.onend = () => {
      running = false;
      if (enabled && !blocked && !woke && !window.JarvisApp?.connected()) setTimeout(listen, 400);
    };
    try {
      rec.start();
      running = true;
      render("Hey JARVIS", "listening");
    } catch { running = false; setTimeout(listen, 1500); }
  }

  function stopListening() {
    if (rec) { rec.onend = null; try { rec.abort(); } catch { /* */ } }
    rec = null;
    running = false;
  }

  async function wake(command) {
    if (!woke) return;
    stopListening();
    lastActivity = Date.now();
    render("Connecting…", "awake");
    try { await window.JarvisApp.connect(command); }
    finally { woke = false; }
    if (!window.JarvisApp.connected()) { render("Hey JARVIS", "listening"); listen(); }
    else render("Awake", "awake");
  }

  function setEnabled(on) {
    enabled = on && !!SR;
    blocked = false;
    store(enabled);
    if (!enabled) { stopListening(); woke = false; clearTimeout(wakeTimer); render("Wake word", "off"); return; }
    if (window.JarvisApp?.connected()) render("Awake", "awake"); else listen();
  }

  btn.addEventListener("click", () => {
    if (blocked) { blocked = false; listen(); return; }
    setEnabled(!enabled);
  });

  // ---- hooks called by app.js ----
  window.JarvisWake = {
    agentState(state) {
      if (state === "speaking" || state === "thinking") lastActivity = Date.now();
    },
    userSpeech(text) {
      lastActivity = Date.now();
      if (enabled && SLEEP.test(text)) {
        render("Going to sleep…", "awake");
        setTimeout(() => window.JarvisApp?.disconnect(), 2500); // let JARVIS say goodbye
      }
    },
    disconnected() {
      if (enabled) { render("Hey JARVIS", "listening"); setTimeout(listen, 600); }
    },
  };

  // Auto-sleep after a few quiet minutes (only in wake-word mode).
  setInterval(() => {
    if (!enabled || !window.JarvisApp?.connected()) return;
    const st = window.JarvisApp.state();
    if (st === "speaking" || st === "thinking" || st === "approval") { lastActivity = Date.now(); return; }
    if (Date.now() - lastActivity > AUTO_SLEEP_MIN * 60_000) {
      lastActivity = Date.now();
      window.JarvisApp.disconnect();
    }
  }, 10_000);

  if (!SR) { btn.disabled = true; render("Wake word", "off"); return; }
  render("Wake word", "off");
  if (stored()) setEnabled(true); // browsers that need a click first report "not-allowed" -> Mic blocked
})();
