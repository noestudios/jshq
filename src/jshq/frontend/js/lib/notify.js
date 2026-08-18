/* Completion sounds, synthesized with WebAudio — no audio assets, no requests.
   Autoplay policy: an AudioContext only produces sound after a user gesture, so
   a one-time pointerdown/keydown listener (registered at import) creates and
   resumes the context; a completion that lands before any gesture is silently
   skipped (the toast/banner still informs). Several pollers can witness the
   same refresh finish (app.js + today.js + jobs.js) — the optional dedupe key,
   derived from the completion report's timestamp, guarantees one ding each.
   macOS pop-ups are the backend's job (app/notify.py via osascript); the
   browser Notification API is deliberately unused — it needs a secure
   context, which plain http only gets on localhost itself. */

const SOUND_KEY = "hq_notify_sound"; // "off" = muted; absent = on (per device)

let ctx = null;
const playedKeys = new Set();

function unlock() {
  try {
    ctx = ctx || new (window.AudioContext || window.webkitAudioContext)();
    if (ctx.state === "suspended") ctx.resume();
  } catch {
    /* no WebAudio — stay silent forever */
  }
}
["pointerdown", "keydown"].forEach((type) =>
  window.addEventListener(type, unlock, { once: true, passive: true })
);

export function soundEnabled() {
  return localStorage.getItem(SOUND_KEY) !== "off";
}

export function setSoundEnabled(on) {
  if (on) localStorage.removeItem(SOUND_KEY);
  else localStorage.setItem(SOUND_KEY, "off");
}

function note(freq, at, wave, peak, decay) {
  const osc = ctx.createOscillator();
  const gain = ctx.createGain();
  osc.type = wave;
  osc.frequency.value = freq;
  gain.gain.setValueAtTime(0.0001, at);
  gain.gain.linearRampToValueAtTime(peak, at + 0.012); // 12ms attack: no click
  gain.gain.exponentialRampToValueAtTime(0.0001, at + decay); // bell-like tail
  osc.connect(gain).connect(ctx.destination);
  osc.start(at);
  osc.stop(at + decay + 0.05);
}

function play(kind, key) {
  if (!soundEnabled()) return;
  if (key) {
    if (playedKeys.has(key)) return;
    playedKeys.add(key);
  }
  if (!ctx || ctx.state !== "running") return; // pre-gesture: skip, don't queue
  const t = ctx.currentTime + 0.02;
  if (kind === "ok") {
    note(659.25, t, "sine", 0.16, 0.35); // E5 …
    note(880.0, t + 0.09, "sine", 0.16, 0.45); // → A5: rising fourth, "ta-ding"
  } else {
    note(293.66, t, "triangle", 0.12, 0.3); // D4 …
    note(196.0, t + 0.12, "triangle", 0.12, 0.45); // → G3: low falling "duh-dum"
  }
}

export function chime(key) {
  play("ok", key);
}

export function buzz(key) {
  play("err", key);
}

/* Settings-toggle preview: runs inside the click gesture, so resume() is
   allowed to succeed right now — also serves as the session's audio unlock. */
export async function preview() {
  try {
    ctx = ctx || new (window.AudioContext || window.webkitAudioContext)();
    await ctx.resume();
  } catch {
    return;
  }
  play("ok");
}
