#!/usr/bin/env python3
"""Spanishh — minimal Anki-style flashcard server. Run: python3 app.py --serve"""
import argparse
import json
import os
import sys
import time
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_PATH = os.path.join(ROOT, "data.json")
SEED_PATH = os.path.join(ROOT, "seed.json")
WORDBANK_PATH = os.path.join(ROOT, "wordbank.txt")
DAY = 86400
LOCK = threading.Lock()
LEVEL_ORDER = {"A1": 1, "A2": 2, "B1": 3, "B2": 4, "C1": 5, "C2": 6}
WORDBANK: list[str] = []
DEFAULT_NEW_PER_DAY = 15
RELEARN_GAP = 3
# Ephemeral in-process relearn queue: list of {"id": int, "grades_left": int}.
# Lost on restart; due_ts on each card is the persistent fallback.
RELEARN_QUEUE: list[dict] = []


def load_wordbank() -> list[str]:
    if not os.path.exists(WORDBANK_PATH):
        return []
    with open(WORDBANK_PATH, "r", encoding="utf-8") as f:
        return [w.strip() for w in f if w.strip()]


def suggest(q: str, limit: int = 10) -> list[str]:
    q = q.strip().lower()
    if not q or not WORDBANK:
        return []
    prefix = [w for w in WORDBANK if w.lower().startswith(q)]
    if len(prefix) >= limit:
        return prefix[:limit]
    contains = [w for w in WORDBANK if q in w.lower() and not w.lower().startswith(q)]
    return (prefix + contains)[:limit]


def level_rank(card: dict) -> tuple[int, int]:
    lvl = card.get("level") or "A1"
    sub = card.get("sublevel") or 1
    return (LEVEL_ORDER.get(lvl, 99), int(sub))


def now() -> int:
    return int(time.time())


def today_key() -> str:
    return time.strftime("%Y-%m-%d", time.localtime())


def yesterday_key() -> str:
    return time.strftime("%Y-%m-%d", time.localtime(time.time() - DAY))


def ensure_state_defaults(state: dict) -> None:
    """Backfill new fields on existing data.json without disturbing card progress."""
    if "intro_log" not in state:
        state["intro_log"] = {}
    if "config" not in state:
        state["config"] = {}
    if not isinstance(state["config"].get("new_per_day"), int):
        state["config"]["new_per_day"] = DEFAULT_NEW_PER_DAY
    if "daily_log" not in state:
        state["daily_log"] = {}
    if "streak" not in state:
        state["streak"] = {"last_date": None, "days": 0}


def today_log(state: dict) -> dict:
    k = today_key()
    if k not in state["daily_log"]:
        state["daily_log"][k] = {"reviews": 0, "mastered": 0}
    return state["daily_log"][k]


def new_intros_today(state: dict) -> int:
    return state.get("intro_log", {}).get(today_key(), 0)


def new_per_day(state: dict) -> int:
    return state.get("config", {}).get("new_per_day", DEFAULT_NEW_PER_DAY)


def bump_streak(state: dict) -> bool:
    """Update day-streak; returns True if today is the first review of a new day."""
    today = today_key()
    s = state["streak"]
    if s["last_date"] == today:
        return False
    if s["last_date"] == yesterday_key():
        s["days"] += 1
    else:
        s["days"] = 1
    s["last_date"] = today
    return True


def load_state() -> dict:
    seed = []
    if os.path.exists(SEED_PATH):
        with open(SEED_PATH, "r", encoding="utf-8") as f:
            seed = json.load(f)

    if os.path.exists(DATA_PATH):
        with open(DATA_PATH, "r", encoding="utf-8") as f:
            state = json.load(f)
        ensure_state_defaults(state)
        # Merge: pull in any seed word not yet in data.json (preserve progress);
        # also fill in english/level for existing drafts when seed now has them.
        by_sp = {c["spanish"].lower(): c for c in state["cards"]}
        for w in seed:
            sp = w["spanish"]
            existing = by_sp.get(sp.lower())
            if existing is None:
                cid = state["next_id"]
                state["next_id"] = cid + 1
                state["cards"].append(new_card(
                    cid, sp, w.get("english") or "",
                    w.get("level"), w.get("sublevel"),
                ))
                continue
            if not existing.get("english") and w.get("english"):
                existing["english"] = w["english"]
            if not existing.get("level") and w.get("level"):
                existing["level"] = w["level"]
                existing["sublevel"] = w.get("sublevel")
        return state

    cards = []
    for i, w in enumerate(seed):
        cards.append(new_card(
            i + 1, w["spanish"], w.get("english") or "",
            w.get("level"), w.get("sublevel"),
        ))
    state = {"next_id": len(cards) + 1, "cards": cards}
    ensure_state_defaults(state)
    return state


def save_state(state: dict) -> None:
    tmp = DATA_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, DATA_PATH)


def new_card(cid: int, spanish: str, english: str, level=None, sublevel=None) -> dict:
    return {
        "id": cid,
        "spanish": spanish,
        "english": english,
        "level": level,
        "sublevel": sublevel,
        "ease": 2.5,
        "interval_days": 0.0,
        "due_ts": now(),
        "reps": 0,
        "lapses": 0,
    }


def review(card: dict, rating: int, state: dict) -> dict:
    """SM-2 lite. rating: 0=Again, 1=Hard, 2=Good, 3=Easy.
    Returns gamification events: {mastered, streak_bumped, streak_days, new_today, reviews_today, combo}."""
    was_new = card["reps"] == 0
    was_mature = card["interval_days"] >= 21
    ease = card["ease"]
    interval = card["interval_days"]
    if rating == 0:
        card["lapses"] += 1
        ease = max(1.3, ease - 0.2)
        interval = 0.007  # ~10 minutes; in-session relearn queue handles immediate re-show.
    else:
        if interval < 1:
            interval = 1.0 if rating >= 2 else 0.5
        else:
            mult = {1: 1.2, 2: ease, 3: ease * 1.3}[rating]
            interval = interval * mult
        ease = max(1.3, ease + {1: -0.15, 2: 0.0, 3: 0.15}[rating])
    card["ease"] = round(ease, 3)
    card["interval_days"] = round(interval, 3)
    card["due_ts"] = now() + int(interval * DAY)
    card["reps"] += 1

    if was_new:
        k = today_key()
        state["intro_log"][k] = state["intro_log"].get(k, 0) + 1

    log = today_log(state)
    log["reviews"] = log.get("reviews", 0) + 1
    is_mature = card["interval_days"] >= 21
    if not was_mature and is_mature:
        log["mastered"] = log.get("mastered", 0) + 1

    streak_bumped = bump_streak(state)

    # Tick down every entry in the in-session relearn queue; re-queue this card if it still needs work.
    for r in RELEARN_QUEUE:
        r["grades_left"] -= 1
    if rating <= 1 and card["interval_days"] < 1:
        RELEARN_QUEUE.append({"id": card["id"], "grades_left": RELEARN_GAP})

    return {
        "card": card,
        "mastered": (not was_mature) and is_mature,
        "streak_bumped": streak_bumped,
        "streak_days": state["streak"]["days"],
        "new_today": new_intros_today(state),
        "reviews_today": log["reviews"],
        "was_new": was_new,
    }


def pick_due(state: dict) -> dict | None:
    """Order: in-session relearn queue → review-due → new (capped per day)."""
    # 1. In-session relearn queue: any card whose countdown has elapsed.
    for i, r in enumerate(RELEARN_QUEUE):
        if r["grades_left"] <= 0:
            cid = r["id"]
            del RELEARN_QUEUE[i]
            for c in state["cards"]:
                if c["id"] == cid:
                    return c
            break
    # 2. Standard review-due, skipping cards already pending in the queue.
    in_queue = {r["id"] for r in RELEARN_QUEUE}
    t = now()
    review_due = [c for c in state["cards"]
                  if c["due_ts"] <= t and c["reps"] > 0 and c.get("english") and c["id"] not in in_queue]
    if review_due:
        review_due.sort(key=lambda c: (-c["lapses"], c["due_ts"]))
        return review_due[0]
    # 3. New cards, capped per local day so the backlog can drain.
    if new_intros_today(state) < new_per_day(state):
        new_cards = [c for c in state["cards"] if c["reps"] == 0 and c.get("english")]
        if new_cards:
            new_cards.sort(key=lambda c: (level_rank(c), c["id"]))
            return new_cards[0]
    # 4. Fallback: nothing else to show but a relearn entry is still pending — show it now.
    if RELEARN_QUEUE:
        cid = RELEARN_QUEUE.pop(0)["id"]
        for c in state["cards"]:
            if c["id"] == cid:
                return c
    return None


def stats(state: dict) -> dict:
    t = now()
    cards = state["cards"]
    active = [c for c in cards if c.get("english")]
    drafts = sum(1 for c in cards if not c.get("english"))
    due = sum(1 for c in active if c["due_ts"] <= t and c["reps"] > 0)
    learning = sum(1 for c in active if c["interval_days"] < 1 and c["reps"] > 0)
    new = sum(1 for c in active if c["reps"] == 0)
    mature = sum(1 for c in active if c["interval_days"] >= 21)
    log = state.get("daily_log", {}).get(today_key(), {})
    return {
        "total": len(active), "due": due, "new": new,
        "learning": learning, "mature": mature, "drafts": drafts,
        "new_today": new_intros_today(state),
        "new_per_day": new_per_day(state),
        "reviews_today": log.get("reviews", 0),
        "mastered_today": log.get("mastered", 0),
        "streak_days": state.get("streak", {}).get("days", 0),
    }


# ---------- HTTP ----------


INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Spanishh — flashcards</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  :root { --bg:#0f1115; --fg:#e8e8ea; --muted:#8b8f99; --accent:#ffb86b; --card:#1a1d24; --border:#262a33; }
  * { box-sizing: border-box; }
  body { margin:0; font-family: -apple-system, BlinkMacSystemFont, "SF Pro", system-ui, sans-serif;
         background:var(--bg); color:var(--fg); display:flex; flex-direction:column; min-height:100vh; }
  header { padding:1rem 1.25rem; border-bottom:1px solid var(--border); display:flex; justify-content:space-between; align-items:center; }
  header h1 { margin:0; font-size:1.1rem; font-weight:600; letter-spacing:.02em; }
  .stats { color:var(--muted); font-size:.85rem; font-variant-numeric: tabular-nums; }
  .stats span { margin-left:1rem; }
  main { flex:1; display:flex; flex-direction:column; align-items:center; justify-content:center; padding:2rem 1rem; }
  .card { background:var(--card); border:1px solid var(--border); border-radius:14px;
          width:min(560px, 100%); padding:3rem 2rem; text-align:center; min-height:280px;
          display:flex; flex-direction:column; justify-content:center; gap:1rem; }
  .word { font-size:2.4rem; font-weight:600; word-break:break-word; }
  .answer { font-size:1.6rem; color:var(--accent); }
  .hint { color:var(--muted); font-size:.85rem; }
  .btns { display:flex; gap:.5rem; flex-wrap:wrap; justify-content:center; margin-top:1.5rem; width:min(560px,100%); }
  button { background:var(--card); color:var(--fg); border:1px solid var(--border); padding:.65rem 1.1rem;
           border-radius:8px; cursor:pointer; font-size:.95rem; font-family:inherit; flex:1; min-width:90px; }
  button:hover { border-color:var(--accent); }
  button.show { background:var(--accent); color:#1a1d24; border-color:var(--accent); flex:1; }
  .r0 { color:#ff6b6b; } .r1 { color:#f8d36b; } .r2 { color:#7bd88f; } .r3 { color:#6bb7ff; }
  .badge { display:inline-block; align-self:center; font-size:.7rem; letter-spacing:.06em;
           color:var(--muted); border:1px solid var(--border); border-radius:999px;
           padding:.15rem .55rem; font-variant-numeric:tabular-nums; }
  .empty { text-align:center; color:var(--muted); }
  .empty h2 { color:var(--fg); }
  footer { padding:1rem 1.25rem; border-top:1px solid var(--border); display:flex; gap:.5rem; }
  footer input, footer select { background:var(--bg); color:var(--fg); border:1px solid var(--border);
                 padding:.5rem .65rem; border-radius:6px; font-family:inherit; font-size:.9rem; }
  footer input { flex:1; }
  footer select, footer #sub { flex:0 0 auto; width:auto; }
  footer #sub { width:4rem; }
  footer button { flex:0; }
  kbd { background:var(--bg); border:1px solid var(--border); border-radius:4px; padding:1px 5px; font-size:.75rem; }
  .toast { position: fixed; top: 1rem; left: 50%; transform: translate(-50%, -16px);
           background: var(--accent); color: #1a1d24; padding: .5rem .9rem; border-radius: 999px;
           font-weight: 600; font-size: .85rem; opacity: 0; pointer-events: none;
           transition: opacity .25s ease, transform .25s ease; z-index: 100;
           box-shadow: 0 4px 16px rgba(0,0,0,.45); max-width: 90vw;
           white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .toast.show { opacity: 1; transform: translate(-50%, 0); }
  .streak { color: #ffb86b; font-weight: 600; }
</style>
</head>
<body>
<header>
  <h1>Spanishh</h1>
  <div class="stats" id="stats">…</div>
</header>
<main id="main"><div class="empty">Loading…</div></main>
<footer>
  <input id="sp" placeholder="español" autocomplete="off" list="sp-suggest">
  <datalist id="sp-suggest"></datalist>
  <input id="en" placeholder="english" autocomplete="off">
  <select id="lvl" title="CEFR level">
    <option>A1</option><option>A2</option><option>B1</option>
    <option>B2</option><option>C1</option><option>C2</option>
  </select>
  <input id="sub" type="number" min="1" max="99" value="1" title="sublevel">
  <button id="add">Add</button>
</footer>
<script>
let current = null;
let revealed = false;

async function api(path, opts) {
  const r = await fetch(path, opts);
  return r.json();
}

let lastStats = null;

async function refreshStats() {
  const s = await api('/api/stats');
  lastStats = s;
  const streakHtml = s.streak_days >= 2 ? `<span class="streak" title="day streak">🔥 ${s.streak_days}d</span>` : '';
  document.getElementById('stats').innerHTML =
    `${streakHtml}<span>due <b>${s.due}</b></span><span>new ${s.new}</span><span>learning ${s.learning}</span><span>mature ${s.mature}</span><span>total ${s.total}</span><span id="today-cap" style="cursor:pointer;text-decoration:underline dotted" title="tap to change daily new-card limit">today ${s.new_today}/${s.new_per_day}</span>`;
  document.getElementById('today-cap').onclick = editDailyCap;
}

async function editDailyCap() {
  const cur = lastStats ? lastStats.new_per_day : 15;
  const v = prompt(`New cards per day (currently ${cur}). Use a high number like 999 for unlimited.`, String(cur));
  if (v === null) return;
  const n = parseInt(v, 10);
  if (!Number.isFinite(n) || n < 0) return;
  await api('/api/config', {method:'POST', headers:{'content-type':'application/json'}, body: JSON.stringify({new_per_day: n})});
  refreshStats();
  if (!current) loadNext();
}

function toast(html, ms=2200) {
  const t = document.createElement('div');
  t.className = 'toast';
  t.innerHTML = html;
  document.body.appendChild(t);
  requestAnimationFrame(() => t.classList.add('show'));
  setTimeout(() => { t.classList.remove('show'); setTimeout(() => t.remove(), 300); }, ms);
}

let sessionCombo = 0;

async function loadNext() {
  revealed = false;
  const r = await api('/api/next');
  current = r.card;
  render();
  refreshStats();
}

function render() {
  const m = document.getElementById('main');
  if (!current) {
    const capped = lastStats && lastStats.new > 0 && lastStats.new_today >= lastStats.new_per_day;
    if (capped) {
      m.innerHTML = `<div class="empty"><h2>Daily new-card limit reached</h2>
        <p>${lastStats.new_today} new cards introduced today (cap ${lastStats.new_per_day}).
        ${lastStats.new} new cards still in the deck. Reviews are caught up.</p>
        <div class="btns" style="margin-top:1rem"><button onclick="loadMore()">+10 more today</button><button onclick="editDailyCap()">Change cap…</button></div></div>`;
    } else {
      m.innerHTML = `<div class="empty"><h2>All caught up 🎉</h2><p>No cards due right now. Add new words below or come back later.</p></div>`;
    }
    return;
  }
  const tag = current.level ? `${current.level}.${current.sublevel || 1}` : '—';
  if (!revealed) {
    m.innerHTML = `
      <div class="card">
        <div class="badge">${tag}</div>
        <div class="word">${escapeHtml(current.spanish)}</div>
      </div>
      <div class="btns"><button class="show" onclick="reveal()">Show answer <kbd>enter</kbd></button></div>`;
  } else {
    m.innerHTML = `
      <div class="card">
        <div class="badge">${tag}</div>
        <div class="word">${escapeHtml(current.spanish)}</div>
        <div class="answer">${escapeHtml(current.english)}</div>
        <div class="hint">reps ${current.reps} · ease ${current.ease} · interval ${current.interval_days}d</div>
      </div>
      <div class="btns">
        <button class="r0" onclick="grade(0)">Again <kbd>1</kbd></button>
        <button class="r1" onclick="grade(1)">Hard <kbd>2</kbd></button>
        <button class="r2" onclick="grade(2)">Good <kbd>3</kbd></button>
        <button class="r3" onclick="grade(3)">Easy <kbd>4</kbd></button>
      </div>`;
  }
}

function reveal() { revealed = true; render(); }

async function loadMore() {
  await api('/api/relax_cap', {method: 'POST', headers: {'content-type':'application/json'}, body: '{}'});
  loadNext();
}

async function grade(rating) {
  if (!current) return;
  const word = current.spanish;
  const r = await api('/api/review', {
    method: 'POST', headers: {'content-type':'application/json'},
    body: JSON.stringify({id: current.id, rating}),
  });
  sessionCombo = rating >= 2 ? sessionCombo + 1 : 0;
  // Toast cascade — only one popup per grade, prioritized.
  if (r.mastered) {
    toast(`🌟 Mastered: ${escapeHtml(word)}`);
  } else if (r.streak_bumped && r.streak_days >= 2) {
    toast(`🔥 Day ${r.streak_days} streak!`);
  } else if (r.was_new && r.new_today > 0 && r.new_today % 5 === 0) {
    toast(`🌱 ${r.new_today} new words today!`);
  } else if ([25, 50, 100, 200].includes(r.reviews_today)) {
    toast(`⚡ ${r.reviews_today} reviews today!`);
  } else if ([5, 10, 20, 50].includes(sessionCombo)) {
    toast(`💫 ${sessionCombo}× combo!`);
  }
  loadNext();
}

async function addWord() {
  const sp = document.getElementById('sp').value.trim();
  const en = document.getElementById('en').value.trim();
  const lvl = document.getElementById('lvl').value;
  const sub = parseInt(document.getElementById('sub').value, 10) || 1;
  if (!sp || !en) return;
  await api('/api/add', {
    method: 'POST', headers: {'content-type':'application/json'},
    body: JSON.stringify({spanish: sp, english: en, level: lvl, sublevel: sub}),
  });
  document.getElementById('sp').value = '';
  document.getElementById('en').value = '';
  document.getElementById('sp').focus();
  refreshStats();
  if (!current) loadNext();
}
document.getElementById('add').onclick = addWord;
for (const id of ['sp','en','sub']) {
  document.getElementById(id).addEventListener('keydown', e => {
    if (e.key === 'Enter') { e.preventDefault(); addWord(); }
  });
}

let suggestTimer = null;
document.getElementById('sp').addEventListener('input', e => {
  const q = e.target.value.trim();
  clearTimeout(suggestTimer);
  if (q.length < 2) { document.getElementById('sp-suggest').innerHTML = ''; return; }
  suggestTimer = setTimeout(async () => {
    const r = await api('/api/suggest?q=' + encodeURIComponent(q));
    document.getElementById('sp-suggest').innerHTML =
      (r.matches || []).map(w => `<option value="${escapeHtml(w)}">`).join('');
  }, 120);
});

document.addEventListener('keydown', e => {
  if (e.target.tagName === 'INPUT') return;
  if (!current) return;
  if (!revealed && (e.key === ' ' || e.key === 'Enter')) { e.preventDefault(); reveal(); return; }
  if (revealed && ['1','2','3','4'].includes(e.key)) { grade(parseInt(e.key)-1); }
});

function escapeHtml(s) { return s.replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }

loadNext();
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        n = int(self.headers.get("Content-Length", "0"))
        if not n:
            return {}
        return json.loads(self.rfile.read(n).decode("utf-8"))

    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            body = INDEX_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/api/next":
            with LOCK:
                state = load_state()
                card = pick_due(state)
            self._send_json({"card": card})
            return
        if self.path == "/api/stats":
            with LOCK:
                state = load_state()
                self._send_json(stats(state))
            return
        if self.path.startswith("/api/suggest"):
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(self.path).query)
            q = (qs.get("q", [""])[0] or "")[:64]
            self._send_json({"matches": suggest(q)})
            return
        self.send_error(404)

    def do_POST(self):
        if self.path == "/api/review":
            data = self._read_json()
            cid = int(data.get("id", 0))
            rating = int(data.get("rating", -1))
            if rating not in (0, 1, 2, 3):
                self._send_json({"error": "bad rating"}, 400)
                return
            with LOCK:
                state = load_state()
                for c in state["cards"]:
                    if c["id"] == cid:
                        events = review(c, rating, state)
                        save_state(state)
                        self._send_json({"ok": True, **events})
                        return
            self._send_json({"error": "not found"}, 404)
            return
        if self.path == "/api/config":
            data = self._read_json()
            n = data.get("new_per_day")
            if not isinstance(n, int) or n < 0:
                self._send_json({"error": "bad new_per_day"}, 400)
                return
            with LOCK:
                state = load_state()
                state["config"]["new_per_day"] = n
                save_state(state)
            self._send_json({"ok": True, "new_per_day": n})
            return
        if self.path == "/api/relax_cap":
            # Lets the user "load 10 more today" without permanently raising the daily cap.
            with LOCK:
                state = load_state()
                k = today_key()
                state["intro_log"][k] = max(0, state["intro_log"].get(k, 0) - 10)
                save_state(state)
            self._send_json({"ok": True, "new_today": new_intros_today(state)})
            return
        if self.path == "/api/add":
            data = self._read_json()
            sp = (data.get("spanish") or "").strip()
            en = (data.get("english") or "").strip()
            lvl = (data.get("level") or "A1").strip().upper()
            sub = int(data.get("sublevel") or 1)
            if not sp or not en:
                self._send_json({"error": "missing fields"}, 400)
                return
            if lvl not in LEVEL_ORDER:
                lvl = "A1"
            with LOCK:
                state = load_state()
                cid = state["next_id"]
                state["next_id"] = cid + 1
                state["cards"].append(new_card(cid, sp, en, lvl, sub))
                save_state(state)
            self._send_json({"ok": True, "id": cid})
            return
        self.send_error(404)


def serve(host: str, port: int) -> None:
    global WORDBANK
    # Persist merged state on startup so new seed words are committed to data.json.
    with LOCK:
        state = load_state()
        save_state(state)
    WORDBANK = load_wordbank()
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"Spanishh serving on http://{host}:{port}  ({len(state['cards'])} cards, {len(WORDBANK)} wordbank entries)")
    print(f"Data persisted to {DATA_PATH}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


def main() -> None:
    p = argparse.ArgumentParser(description="Spanishh flashcard server")
    p.add_argument("--serve", action="store_true", help="start the web server")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    args = p.parse_args()
    if args.serve:
        serve(args.host, args.port)
    else:
        p.print_help()


if __name__ == "__main__":
    main()
