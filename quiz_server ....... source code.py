#!/usr/bin/env python3
"""
Let's Quiz - Server

Run:   pip install flask
       python quiz_server.py
Open:  http://localhost:5000

Flow for students:
  1. Home > Join Quiz: enter a unique ID and the quiz code, then get a link.
  2. Open the link and enter the password the teacher gave.
  3. Loading screen runs to 100%, then the test starts.

- Questions and correct answers stay on the server.
- Every score is saved in quiz.db (SQLite).
- Results are shown to everyone when the teacher closes the quiz.
"""

import json
import secrets
import sqlite3
import string
import threading
import time

from flask import Flask, abort, g, jsonify, render_template_string, request

DB = "quiz.db"
ACTIVE_TTL = 120  # seconds a joined ID stays reserved without activity
app = Flask(__name__)

ACTIVE = {}  # (quiz_code, student_name) -> last activity timestamp
LOCK = threading.Lock()


# ---------- database ----------

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    with sqlite3.connect(DB) as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS quizzes (
                code TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                questions TEXT NOT NULL,
                password TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'open',
                created TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                quiz_code TEXT NOT NULL,
                student TEXT NOT NULL,
                score INTEGER NOT NULL,
                total INTEGER NOT NULL,
                submitted TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (quiz_code, student)
            );
        """)
        # Upgrade older databases that were created before passwords existed.
        cols = [r[1] for r in conn.execute("PRAGMA table_info(quizzes)")]
        if "password" not in cols:
            conn.execute("ALTER TABLE quizzes ADD COLUMN password TEXT NOT NULL DEFAULT ''")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS responses (
                quiz_code TEXT NOT NULL,
                student TEXT NOT NULL,
                question INTEGER NOT NULL,
                choice INTEGER NOT NULL,
                correct INTEGER NOT NULL,
                PRIMARY KEY (quiz_code, student, question)
            )
        """)


# ---------- helpers ----------

def validate_questions(data):
    errors = []
    if not isinstance(data, list) or not data:
        return ["Questions must be a non-empty list."]
    for i, q in enumerate(data, start=1):
        if not isinstance(q, dict):
            errors.append(f"Question {i}: must be an object.")
            continue
        if not isinstance(q.get("question"), str) or not q["question"].strip():
            errors.append(f"Question {i}: 'question' is missing or empty.")
        opts = q.get("options")
        if not isinstance(opts, list) or len(opts) < 2:
            errors.append(f"Question {i}: 'options' needs at least 2 items.")
        elif not all(isinstance(o, str) and o.strip() for o in opts):
            errors.append(f"Question {i}: every option must be non-empty text.")
        ans = q.get("answer")
        if not isinstance(ans, str) or not isinstance(opts, list) or ans not in opts:
            errors.append(f"Question {i}: 'answer' must exactly match one of the options.")
    return errors


def new_code(conn):
    alphabet = string.ascii_uppercase + string.digits
    while True:
        code = "".join(secrets.choice(alphabet) for _ in range(6))
        if not conn.execute("SELECT 1 FROM quizzes WHERE code = ?", (code,)).fetchone():
            return code


def new_password():
    return "".join(secrets.choice(string.digits) for _ in range(4))


def get_quiz(code):
    row = get_db().execute("SELECT * FROM quizzes WHERE code = ?", (code.upper(),)).fetchone()
    if row is None:
        abort(404)
    return row


def clean_name(value):
    return (value or "").strip()[:30]


def is_active(code, name):
    """True if this ID is reserved (joined or verified) and has not expired."""
    with LOCK:
        last = ACTIVE.get((code, name))
        if last is None or time.time() - last > ACTIVE_TTL:
            ACTIVE.pop((code, name), None)
            return False
        return True


def touch(code, name):
    with LOCK:
        ACTIVE[(code, name)] = time.time()


def release(code, name):
    with LOCK:
        ACTIVE.pop((code, name), None)


def already_submitted(code, name):
    row = get_db().execute(
        "SELECT 1 FROM results WHERE quiz_code = ? AND student = ?", (code, name)).fetchone()
    return row is not None


# ---------- pages ----------

HOME_PAGE = """
<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Let's Quiz</title>
<style>
  :root { --bg:#f4f6fb; --card:#ffffff; --text:#1f2937; --muted:#6b7280; --primary:#4f46e5; --primary-dark:#4338ca; --border:#d1d5db; }
  body.dark { --bg:#0f172a; --card:#1e293b; --text:#e5e7eb; --muted:#94a3b8; --border:#334155; }
  * { box-sizing:border-box; }
  body { margin:0; min-height:100vh; font-family:system-ui,-apple-system,"Segoe UI",sans-serif; background:var(--bg); color:var(--text); }

  #loader { position:fixed; inset:0; z-index:10; display:flex; flex-direction:column; align-items:center; justify-content:center;
            background:linear-gradient(135deg,#4f46e5,#7c3aed); color:#fff; transition:opacity .6s ease; }
  #loader.done { opacity:0; pointer-events:none; }
  .logo { font-size:2.6rem; font-weight:800; letter-spacing:1px; }
  .tag { opacity:.85; margin:6px 0 28px; }
  .bar-wrap { width:280px; height:10px; background:rgba(255,255,255,.25); border-radius:999px; overflow:hidden; }
  .bar { height:100%; width:0%; background:#fff; border-radius:999px; }
  .pct { margin-top:12px; font-size:1.1rem; font-variant-numeric:tabular-nums; }

  #home { display:none; max-width:900px; margin:0 auto; padding:48px 20px; }
  #home.show { display:block; animation:fadeUp .6s ease; }
  @keyframes fadeUp { from { opacity:0; transform:translateY(12px); } to { opacity:1; transform:none; } }
  header { margin-bottom:36px; }
  header h1 { margin:0; font-size:1.8rem; }
  .greet { color:var(--muted); margin-top:4px; }

  .menu { display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); gap:20px; }
  .tile { background:var(--card); border:1px solid var(--border); border-radius:18px; padding:28px 22px; cursor:pointer;
          box-shadow:0 4px 18px rgba(0,0,0,.06); transition:transform .15s, box-shadow .15s; }
  .tile:hover { transform:translateY(-4px); box-shadow:0 10px 26px rgba(0,0,0,.12); }
  .icon { font-size:2.2rem; margin-bottom:12px; }
  .tile h2 { margin:0 0 6px; font-size:1.2rem; }
  .tile p { margin:0; color:var(--muted); font-size:.92rem; line-height:1.4; }

  .panel { display:none; margin-top:28px; background:var(--card); border:1px solid var(--border); border-radius:18px; padding:24px; }
  .panel.show { display:block; animation:fadeUp .4s ease; }
  .panel h3 { margin:0 0 12px; }
  input[type=text] { width:100%; padding:12px; border:1px solid var(--border); border-radius:10px; font-size:1rem;
                     background:var(--bg); color:var(--text); margin-bottom:12px; }
  .btn { display:inline-block; background:var(--primary); color:#fff; border:none; padding:11px 20px; border-radius:10px;
         font-size:.98rem; cursor:pointer; text-decoration:none; }
  .btn:hover { background:var(--primary-dark); }
  .btn.ghost { background:transparent; color:var(--muted); border:1px solid var(--border); }
  .row { display:flex; gap:12px; align-items:center; flex-wrap:wrap; }
  .switch-row { display:flex; justify-content:space-between; align-items:center; padding:12px 0; border-top:1px solid var(--border); }
  .msg { margin-top:10px; color:#dc2626; font-size:.92rem; }
  .muted { color:var(--muted); font-size:.92rem; }
  .hidden { display:none !important; }

  /* ---- animations ---- */
  @keyframes fadeUp { from { opacity:0; transform:translateY(14px); } to { opacity:1; transform:none; } }
  @keyframes popIn { 0% { opacity:0; transform:scale(.6); } 70% { transform:scale(1.06); } 100% { opacity:1; transform:scale(1); } }
  @keyframes glow { 0%,100% { text-shadow:0 0 0 rgba(255,255,255,0); } 50% { text-shadow:0 0 22px rgba(255,255,255,.75); } }
  @keyframes float { 0%,100% { transform:translateY(0); } 50% { transform:translateY(-6px); } }
  @keyframes shimmer { from { background-position:200% 0; } to { background-position:-200% 0; } }
  .logo { animation: popIn .7s cubic-bezier(.2,.8,.2,1) backwards, glow 2.4s ease-in-out .7s infinite; }
  .tag { animation: fadeUp .8s ease .3s backwards; }
  .bar-wrap { animation: fadeUp .6s ease .2s backwards; }
  .bar { background: linear-gradient(90deg,#fff 0%,#c7d2fe 50%,#fff 100%); background-size:200% 100%; animation: shimmer 1.4s linear infinite; }
  #home.show header h1 { animation: fadeUp .6s ease backwards; }
  #home.show .greet { animation: fadeUp .6s ease .15s backwards; }
  #home.show .tile { animation: fadeUp .6s ease backwards; }
  #home.show .tile:nth-child(1) { animation-delay: .15s; }
  #home.show .tile:nth-child(2) { animation-delay: .3s; }
  #home.show .tile:nth-child(3) { animation-delay: .45s; }
  .icon { display:inline-block; animation: float 3s ease-in-out infinite; }
  .tile:nth-child(2) .icon { animation-delay: .5s; }
  .tile:nth-child(3) .icon { animation-delay: 1s; }
  .tile:active { transform: scale(.97); transition: transform .1s; }
  .btn { transition: transform .15s ease, box-shadow .2s ease, background .2s ease; }
  .btn:hover { transform: translateY(-2px); box-shadow: 0 6px 16px rgba(79,70,229,.35); }
  .btn:active { transform: scale(.96); }
  .btn.ghost:hover { box-shadow: none; }
  input[type=text]:focus { outline:none; border-color: var(--primary); box-shadow: 0 0 0 4px rgba(79,70,229,.15); transition: box-shadow .2s, border-color .2s; }
  #linkBox { animation: popIn .5s ease backwards; }
  .msg { animation: fadeUp .3s ease backwards; }
</style></head>
<body>

<div id="loader">
  <div class="logo">Let's Quiz</div>
  <div class="tag">Learn. Test. Score.</div>
  <div class="bar-wrap"><div class="bar" id="bar"></div></div>
  <div class="pct" id="pct">0%</div>
</div>

<div id="home">
  <header>
    <h1>Let's Quiz</h1>
    <div class="greet" id="greet"></div>
  </header>

  <div class="menu">
    <div class="tile" onclick="openPanel('create')">
      <div class="icon">✏️</div>
      <h2>Create Quiz</h2>
      <p>Paste your questions, set a password and get a quiz code for your students.</p>
    </div>
    <div class="tile" onclick="openPanel('join')">
      <div class="icon">🎯</div>
      <h2>Join Quiz</h2>
      <p>Enter a unique ID and the quiz code to get your link.</p>
    </div>
    <div class="tile" onclick="openPanel('settings')">
      <div class="icon">⚙️</div>
      <h2>Settings</h2>
      <p>Set your name and choose light or dark mode.</p>
    </div>
  </div>

  <div class="panel" id="panel-create">
    <h3>Create Quiz</h3>
    <p class="muted">Open the quiz builder, paste your questions, set a password and get a code to share.</p>
    <a class="btn" href="/create">Go to Quiz Builder</a>
  </div>

  <div class="panel" id="panel-join">
    <h3>Join Quiz</h3>
    <div id="joinStep1">
      <p class="muted">Choose an ID for yourself. Only one student can use the same ID at a time.</p>
      <input type="text" id="joinName" placeholder="Your unique ID" maxlength="30">
      <input type="text" id="joinCode" placeholder="Quiz code, e.g. ICD4U4" style="text-transform:uppercase">
      <div class="row"><button class="btn" onclick="getLink()">Get Link</button></div>
      <div class="msg" id="joinMsg"></div>
    </div>
    <div id="joinStep2" class="hidden">
      <p class="muted">Your link is ready. Open it and enter the password your teacher gave you.</p>
      <input type="text" id="linkBox" readonly>
      <div class="row">
        <button class="btn" onclick="openQuiz()">Open Quiz</button>
        <button class="btn ghost" onclick="resetJoin()">Back</button>
      </div>
    </div>
  </div>

  <div class="panel" id="panel-settings">
    <h3>Settings</h3>
    <input type="text" id="teacherName" placeholder="Your name">
    <div class="switch-row">
      <span>Dark mode</span>
      <input type="checkbox" id="darkToggle">
    </div>
    <div class="row" style="margin-top:14px">
      <button class="btn" onclick="saveSettings()">Save</button>
      <span class="muted" id="saveMsg"></span>
    </div>
  </div>
</div>

<script>
const $ = id => document.getElementById(id);
const val = id => $(id).value.trim();

// ----- loading screen: 0% to 100% -----
let progress = 0;
const loadTimer = setInterval(() => {
  progress += Math.random() * 7 + 2;
  if (progress >= 100) {
    progress = 100;
    clearInterval(loadTimer);
    setTimeout(showHome, 450);
  }
  $("bar").style.width = progress + "%";
  $("pct").textContent = Math.floor(progress) + "%";
}, 90);

function showHome() {
  $("loader").classList.add("done");
  $("home").classList.add("show");
}

// ----- panels -----
function openPanel(name) {
  ["create", "join", "settings"].forEach(p => $("panel-" + p).classList.toggle("show", p === name));
  if (name === "join") $("joinName").focus();
}

// ----- join: ID + code -> link -----
let joinData = null;

async function getLink() {
  const name = val("joinName");
  const code = val("joinCode").toUpperCase();
  $("joinMsg").textContent = "";
  if (!name) { $("joinMsg").textContent = "Please enter a unique ID."; return; }
  if (!code) { $("joinMsg").textContent = "Please enter the quiz code."; return; }

  const res = await fetch("/api/quizzes/" + encodeURIComponent(code) + "/join", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name })
  });
  const data = await res.json();
  if (!res.ok) { $("joinMsg").textContent = data.error; return; }

  joinData = { name, code };
  $("linkBox").value = location.origin + "/quiz/" + code;
  $("joinStep1").classList.add("hidden");
  $("joinStep2").classList.remove("hidden");
}

function openQuiz() {
  sessionStorage.setItem("lq_name_" + joinData.code, joinData.name);
  window.location.href = "/quiz/" + joinData.code;
}

function resetJoin() {
  $("joinStep2").classList.add("hidden");
  $("joinStep1").classList.remove("hidden");
}

// ----- settings -----
function applySettings(name, dark) {
  document.body.classList.toggle("dark", dark);
  $("greet").textContent = name ? "Welcome, " + name + "!" : "Welcome to Let's Quiz";
}

function saveSettings() {
  const name = val("teacherName");
  const dark = $("darkToggle").checked;
  try {
    localStorage.setItem("lq_name", name);
    localStorage.setItem("lq_dark", dark ? "1" : "0");
  } catch (e) { /* storage blocked: settings still apply for this visit */ }
  applySettings(name, dark);
  $("saveMsg").textContent = "Saved.";
  setTimeout(() => $("saveMsg").textContent = "", 1500);
}

(function loadSettings() {
  let name = "", dark = false;
  try {
    name = localStorage.getItem("lq_name") || "";
    dark = localStorage.getItem("lq_dark") === "1";
  } catch (e) {}
  $("teacherName").value = name;
  $("darkToggle").checked = dark;
  applySettings(name, dark);
})();
</script>
</body></html>
"""

CREATE_PAGE = """
<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Let's Quiz - Create</title>
<style>
  body { font-family: system-ui, sans-serif; background:#f4f6fb; margin:0; padding:24px; color:#1f2937; }
  .card { background:#fff; max-width:760px; margin:0 auto 20px; padding:24px; border-radius:14px; box-shadow:0 4px 20px rgba(0,0,0,.08); }
  h1 { margin-top:0; text-align:center; }
  input[type=text] { width:100%; padding:10px; border:1px solid #d1d5db; border-radius:8px; font-size:1rem; margin-bottom:12px; box-sizing:border-box; }
  textarea { width:100%; min-height:220px; font-family:monospace; padding:12px; border:1px solid #d1d5db; border-radius:8px; box-sizing:border-box; }
  .btn { background:#4f46e5; color:#fff; border:none; padding:10px 18px; border-radius:8px; cursor:pointer; font-size:.95rem; margin-top:12px; }
  .btn:hover { background:#4338ca; }
  .msg { margin-top:12px; font-size:.95rem; white-space:pre-wrap; }
  .ok { color:#16a34a; } .bad { color:#dc2626; }
  a { color:#4f46e5; }
  table { width:100%; border-collapse:collapse; margin-top:10px; }
  th, td { text-align:left; padding:8px; border-bottom:1px solid #e5e7eb; }
  .muted { color:#6b7280; font-size:.9rem; }

  @keyframes fadeUp { from { opacity:0; transform:translateY(14px); } to { opacity:1; transform:none; } }
  .card { animation: fadeUp .6s ease backwards; }
  .card + .card { animation-delay: .15s; }
  .btn { transition: transform .15s ease, background .2s ease, box-shadow .2s ease; }
  .btn:hover { transform: translateY(-2px); box-shadow: 0 6px 16px rgba(79,70,229,.35); }
  .btn:active { transform: scale(.96); }
  tbody tr { animation: fadeUp .4s ease backwards; }
</style></head>
<body>
<div class="card">
  <p><a href="/">&larr; Back to home</a></p>
  <h1>Create Quiz</h1>
  <p class="muted">Paste questions in JSON. Each question needs "question", "options" (at least 2) and "answer" (exactly one of the options).</p>
  <input type="text" id="title" placeholder="Quiz title, e.g. Pakistan Studies Test 1">
  <input type="text" id="password" placeholder="Password for students (leave empty for a random 4-digit one)">
  <textarea id="questions" placeholder='[{"question": "What is 2 + 2?", "options": ["3","4","5"], "answer": "4"}]'></textarea>
  <button class="btn" onclick="createQuiz()">Create Quiz</button>
  <div class="msg" id="createMsg"></div>
</div>

<div class="card">
  <h2>Your quizzes</h2>
  <table><thead><tr><th>Title</th><th>Code</th><th>Status</th><th>Links</th></tr></thead>
  <tbody id="quizList"></tbody></table>
</div>

<script>
async function createQuiz() {
  const msg = document.getElementById("createMsg");
  let questions;
  try { questions = JSON.parse(document.getElementById("questions").value); }
  catch (e) { msg.className = "msg bad"; msg.textContent = "Invalid JSON: " + e.message; return; }
  const res = await fetch("/api/quizzes", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({
      title: document.getElementById("title").value,
      password: document.getElementById("password").value,
      questions
    })
  });
  const data = await res.json();
  if (!res.ok) { msg.className = "msg bad"; msg.textContent = data.error; return; }
  msg.className = "msg ok";
  msg.textContent = "Quiz created!\\nCode: " + data.code + "\\nPassword: " + data.password +
                    "\\nStudent link: " + location.origin + "/quiz/" + data.code +
                    "\\n(Give students the code and the password. They join from the home page.)";
  loadList();
}

async function loadList() {
  const res = await fetch("/api/quizzes");
  const items = await res.json();
  const tbody = document.getElementById("quizList");
  tbody.innerHTML = "";
  items.forEach(q => {
    const tr = document.createElement("tr");
    [q.title, q.code, q.status].forEach(t => {
      const td = document.createElement("td"); td.textContent = t; tr.appendChild(td);
    });
    const td = document.createElement("td");
    td.innerHTML = '<a href="/quiz/' + q.code + '" target="_blank">Student link</a> | <a href="/results/' + q.code + '">Results</a>';
    tr.appendChild(td);
    tbody.appendChild(tr);
  });
}
loadList();
</script>
</body></html>
"""

STUDENT_PAGE = """
<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{{ title }}</title>
<style>
  body { font-family: system-ui, sans-serif; background:#f4f6fb; margin:0; padding:24px; color:#1f2937; }
  .card { background:#fff; max-width:640px; margin:0 auto; padding:28px; border-radius:16px; box-shadow:0 4px 20px rgba(0,0,0,.08); text-align:center; }
  h1 { margin-top:0; }
  .hidden { display:none !important; }
  input[type=password] { width:100%; padding:12px; border:1px solid #d1d5db; border-radius:8px; font-size:1rem; margin:12px 0; box-sizing:border-box; }
  .btn { display:inline-block; background:#4f46e5; color:#fff; border:none; padding:12px 24px; border-radius:8px; font-size:1rem; cursor:pointer; margin-top:10px; text-decoration:none; }
  .btn:hover { background:#4338ca; }
  .btn:disabled { background:#9ca3af; cursor:not-allowed; }
  .options { display:flex; flex-direction:column; gap:10px; margin:16px 0; text-align:left; }
  .option { background:#fff; border:2px solid #d1d5db; border-radius:10px; padding:12px 14px; font-size:1rem; cursor:pointer; text-align:left; }
  .option.selected { border-color:#4f46e5; background:#eef2ff; }
  .meta { display:flex; justify-content:space-between; color:#6b7280; font-size:.9rem; }
  .progress { height:8px; background:#e5e7eb; border-radius:999px; overflow:hidden; margin:12px 0 18px; }
  .progress-bar { height:100%; background:#4f46e5; width:0%; transition:width .3s; }
  .nav { display:flex; justify-content:space-between; gap:10px; }
  table { width:100%; border-collapse:collapse; margin-top:12px; }
  th, td { padding:8px; border-bottom:1px solid #e5e7eb; text-align:left; }
  .me { background:#eef2ff; font-weight:600; }
  .muted { color:#6b7280; }
  .msg { color:#dc2626; margin-top:10px; }

  /* ---- animations ---- */
  @keyframes fadeUp { from { opacity:0; transform:translateY(14px); } to { opacity:1; transform:none; } }
  @keyframes slideIn { from { opacity:0; transform:translateX(40px); } to { opacity:1; transform:none; } }
  @keyframes popIn { 0% { opacity:0; transform:scale(.5); } 70% { transform:scale(1.08); } 100% { opacity:1; transform:scale(1); } }
  @keyframes shimmer { from { background-position:200% 0; } to { background-position:-200% 0; } }
  @keyframes pulseText { 0%,100% { opacity:.5; } 50% { opacity:1; } }
  .card { animation: fadeUp .6s ease backwards; }
  .slide { animation: slideIn .45s cubic-bezier(.2,.8,.2,1) both; }
  .option { transition: transform .15s ease, border-color .2s, background .2s, box-shadow .2s; }
  .option:hover:not(:disabled) { transform: translateX(4px); border-color:#818cf8; }
  .option:active:not(:disabled) { transform: scale(.98); }
  .option.selected { animation: popIn .3s ease backwards; box-shadow: 0 0 0 3px rgba(79,70,229,.15); }
  .progress-bar { background: linear-gradient(90deg,#4f46e5,#a78bfa,#4f46e5); background-size:200% 100%; animation: shimmer 2s linear infinite; }
  #loadScreen p { animation: pulseText 1.2s ease-in-out infinite; }
  .check { width:76px; height:76px; margin:0 auto 12px; border-radius:50%; background:#16a34a; color:#fff;
           font-size:2.4rem; line-height:76px; animation: popIn .5s ease backwards; }
  .btn { transition: transform .15s ease, box-shadow .2s ease, background .2s ease; }
  .btn:hover:not(:disabled) { transform: translateY(-2px); box-shadow: 0 6px 16px rgba(79,70,229,.35); }
  .btn:active:not(:disabled) { transform: scale(.96); }
  input[type=password]:focus { outline:none; border-color:#4f46e5; box-shadow: 0 0 0 4px rgba(79,70,229,.15); transition: box-shadow .2s, border-color .2s; }
  .timer-track { height:10px; background:#e5e7eb; border-radius:999px; overflow:hidden; margin:0 0 18px; }
  .timer-bar { height:100%; width:100%; background:#16a34a; border-radius:999px; transition: background .3s; }
  .timer-bar.warn { background:#f59e0b; }
  .timer-bar.danger { background:#dc2626; animation: pulseText .5s ease-in-out infinite; }
  .feedback { margin-top:14px; font-weight:600; color:#dc2626; min-height:1.4em; }
  .feedback.good { color:#16a34a; }
  .feedback.bad { color:#dc2626; }
  .option.correct { border-color:#16a34a; background:#dcfce7; }
  .option.wrong { border-color:#dc2626; background:#fee2e2; }
  .option:disabled { cursor:default; }
</style></head>
<body>
<div class="card">
  <h1>{{ title }}</h1>

  <div id="noNameScreen" class="hidden">
    <p class="msg">You have not joined this quiz yet.</p>
    <a class="btn" href="/">Go to home and join</a>
  </div>

  <div id="passScreen" class="hidden">
    <p class="muted">Enter the password your teacher gave you.</p>
    <input type="password" id="password" placeholder="Password">
    <button class="btn" id="passBtn">Enter</button>
    <div class="msg" id="passMsg"></div>
  </div>

  <div id="loadScreen" class="hidden">
    <p class="muted">Preparing your test...</p>
    <div class="progress"><div class="progress-bar" id="loadBar"></div></div>
    <div class="muted" id="loadPct">0%</div>
  </div>

  <div id="quizScreen" class="hidden">
    <div class="meta"><span id="counter"></span><span>Time left: <b id="timeLeft">30</b>s</span></div>
    <div class="progress"><div class="progress-bar" id="progressBar"></div></div>
    <div class="timer-track"><div class="timer-bar" id="timerBar"></div></div>
    <h2 id="questionText"></h2>
    <div class="options" id="optionsBox"></div>
    <div class="feedback" id="timeMsg"></div>
  </div>

  <div id="doneScreen" class="hidden">
    <div class="check">✓</div>
    <h2>Thanks, your test is submitted!</h2>
    <p id="submitStatus" class="muted"></p>
    <div id="resultsBox"></div>
  </div>

  <div id="lbBox" class="hidden" style="margin-top:24px; text-align:left">
    <h3 style="text-align:center; margin:0 0 6px">Live Leaderboard</h3>
    <table>
      <thead><tr><th>#</th><th>Name</th><th>Correct</th><th>Answered</th></tr></thead>
      <tbody id="lbBody"></tbody>
    </table>
  </div>
</div>

<script>
const CODE = "{{ code }}";
const NAME = sessionStorage.getItem("lq_name_" + CODE) || "";
let questions = [], index = 0, answers = [], heartbeatTimer = null;

const $ = id => document.getElementById(id);
const SCREENS = ["noNameScreen", "passScreen", "loadScreen", "quizScreen", "doneScreen"];
const show = id => SCREENS.forEach(s => $(s).classList.toggle("hidden", s !== id));

show(NAME ? "passScreen" : "noNameScreen");

// ----- password -----
$("passBtn").addEventListener("click", verifyPassword);
$("password").addEventListener("keydown", e => { if (e.key === "Enter") verifyPassword(); });

async function verifyPassword() {
  const pw = $("password").value;
  $("passMsg").textContent = "";
  if (!pw) { $("passMsg").textContent = "Please enter the password."; return; }
  const res = await fetch("/api/quizzes/" + CODE + "/verify", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name: NAME, password: pw })
  });
  const data = await res.json();
  if (!res.ok) { $("passMsg").textContent = data.error; return; }
  startLoading();
}

// ----- loading: 0% to 100% then start -----
function startLoading() {
  show("loadScreen");
  let p = 0;
  const t = setInterval(() => {
    p += Math.random() * 8 + 3;
    if (p >= 100) {
      p = 100;
      clearInterval(t);
      $("loadBar").style.width = "100%";
      $("loadPct").textContent = "100%";
      setTimeout(loadQuiz, 400);
    } else {
      $("loadBar").style.width = p + "%";
      $("loadPct").textContent = Math.floor(p) + "%";
    }
  }, 90);
}

async function loadQuiz() {
  const res = await fetch("/api/quizzes/" + CODE + "/questions");
  const data = await res.json();
  if (data.status !== "open") { alert("This quiz is closed."); return; }
  questions = data.questions;
  answers = new Array(questions.length).fill(null);
  index = 0;
  show("quizScreen");
  render();
  heartbeatTimer = setInterval(sendHeartbeat, 20000);
  $("lbBox").classList.remove("hidden");
  refreshLeaderboard();
  setInterval(refreshLeaderboard, 3000);
}

async function refreshLeaderboard() {
  try {
    const res = await fetch("/api/quizzes/" + CODE + "/leaderboard");
    const data = await res.json();
    const body = $("lbBody");
    body.innerHTML = "";
    if (!data.leaderboard.length) {
      body.innerHTML = '<tr><td colspan="4" class="muted">No answers yet. Be the first!</td></tr>';
      return;
    }
    data.leaderboard.forEach(r => {
      const tr = document.createElement("tr");
      if (r.student === NAME) tr.className = "me";
      [r.rank, r.student, r.correct, r.answered].forEach(t => {
        const td = document.createElement("td");
        td.textContent = t;
        tr.appendChild(td);
      });
      body.appendChild(tr);
    });
  } catch (e) { /* try again on the next tick */ }
}

function sendHeartbeat() {
  fetch("/api/quizzes/" + CODE + "/heartbeat", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name: NAME })
  });
}

// ----- quiz -----
const TIME_PER_QUESTION = 30; // seconds
let timerInterval = null, deadline = 0, locked = false;

function render() {
  const q = questions[index];
  $("counter").textContent = "Question " + (index + 1) + " of " + questions.length;
  $("progressBar").style.width = (index / questions.length * 100) + "%";
  $("questionText").textContent = q.question;
  $("timeMsg").textContent = "";
  $("timeMsg").className = "feedback";

  const box = $("optionsBox");
  box.innerHTML = "";
  q.options.forEach((opt, i) => {
    const b = document.createElement("button");
    b.className = "option";
    b.textContent = opt;
    b.addEventListener("click", () => choose(i));
    box.appendChild(b);
  });
  startTimer();
}

function startTimer() {
  clearInterval(timerInterval);
  locked = false;
  deadline = Date.now() + TIME_PER_QUESTION * 1000;
  updateTimer();
  timerInterval = setInterval(updateTimer, 200);
}

function updateTimer() {
  const remainingMs = Math.max(0, deadline - Date.now());
  const left = Math.ceil(remainingMs / 1000);
  $("timeLeft").textContent = left;

  const bar = $("timerBar");
  bar.style.width = (remainingMs / (TIME_PER_QUESTION * 1000) * 100) + "%";
  bar.classList.toggle("warn", left <= 10);
  bar.classList.toggle("danger", left <= 5);

  if (remainingMs <= 0 && !locked) {
    locked = true;
    clearInterval(timerInterval);
    lockOptions(null);
    $("timeMsg").textContent = "Time's up!";
    setTimeout(nextQuestion, 900);
  }
}

async function choose(i) {
  if (locked) return;
  locked = true;
  clearInterval(timerInterval);
  answers[index] = i;
  lockOptions(i);

  try {
    const res = await fetch("/api/quizzes/" + CODE + "/answer", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: NAME, question: index, choice: i })
    });
    if (res.ok) {
      const data = await res.json();
      showFeedback(i, data.correct_index);
    }
  } catch (e) { /* network problem: continue without colors */ }

  setTimeout(nextQuestion, 1500);
}

function showFeedback(chosen, correctIndex) {
  const buttons = document.querySelectorAll(".option");
  buttons[correctIndex].classList.add("correct");
  const isRight = chosen === correctIndex;
  if (!isRight) buttons[chosen].classList.add("wrong");
  $("timeMsg").textContent = isRight ? "Correct!" : "Wrong!";
  $("timeMsg").className = "feedback " + (isRight ? "good" : "bad");
}

function lockOptions(chosen) {
  document.querySelectorAll(".option").forEach((b, i) => {
    b.disabled = true;
    if (i === chosen) b.classList.add("selected");
  });
}

function nextQuestion() {
  if (index < questions.length - 1) {
    index++;
    render();
    slideIn();
  } else {
    submitTest();
  }
}

function slideIn() {
  ["questionText", "optionsBox"].forEach(id => {
    const el = $(id);
    el.classList.remove("slide");
    void el.offsetWidth; // restart the animation
    el.classList.add("slide");
  });
}


async function submitTest() {
  clearInterval(timerInterval);
  locked = true;
  const res = await fetch("/api/quizzes/" + CODE + "/submit", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name: NAME, answers })
  });
  const data = await res.json();
  if (!res.ok) { alert(data.error); return; }
  clearInterval(heartbeatTimer);
  show("doneScreen");
  $("submitStatus").textContent = "Your answers are saved. Results will appear here when your teacher closes the quiz.";
  pollResults();
}

async function pollResults() {
  const res = await fetch("/api/quizzes/" + CODE + "/results");
  const data = await res.json();
  if (data.status === "closed") {
    const box = $("resultsBox");
    box.innerHTML = "";
    $("submitStatus").textContent = "The quiz is closed. Here are the results:";
    const table = document.createElement("table");
    table.innerHTML = "<thead><tr><th>#</th><th>Name</th><th>Score</th></tr></thead>";
    const tbody = document.createElement("tbody");
    data.results.forEach((r, i) => {
      const tr = document.createElement("tr");
      if (r.student === NAME) tr.className = "me";
      [i + 1, r.student, r.score + " / " + r.total].forEach(t => {
        const td = document.createElement("td"); td.textContent = t; tr.appendChild(td);
      });
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    box.appendChild(table);
    return;
  }
  setTimeout(pollResults, 4000);
}
</script>
</body></html>
"""

RESULTS_PAGE = """
<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Results - {{ title }}</title>
<style>
  body { font-family: system-ui, sans-serif; background:#f4f6fb; margin:0; padding:24px; color:#1f2937; }
  .card { background:#fff; max-width:700px; margin:0 auto; padding:26px; border-radius:14px; box-shadow:0 4px 20px rgba(0,0,0,.08); }
  h1 { margin-top:0; }
  .muted { color:#6b7280; }
  .btn { background:#dc2626; color:#fff; border:none; padding:10px 18px; border-radius:8px; cursor:pointer; font-size:.95rem; }
  .btn:disabled { background:#9ca3af; }
  table { width:100%; border-collapse:collapse; margin-top:14px; }
  th, td { padding:10px; border-bottom:1px solid #e5e7eb; text-align:left; }
  .big { font-size:1.6rem; font-weight:700; }
  a { color:#4f46e5; }

  /* ---- animations ---- */
  @keyframes fadeUp { from { opacity:0; transform:translateY(14px); } to { opacity:1; transform:none; } }
  @keyframes popIn { 0% { opacity:0; transform:scale(.6); } 70% { transform:scale(1.08); } 100% { opacity:1; transform:scale(1); } }
  @keyframes rowIn { from { opacity:0; transform:translateX(-16px); } to { opacity:1; transform:none; } }
  .card { animation: fadeUp .6s ease backwards; }
  #count { display:inline-block; animation: popIn .5s ease .2s backwards; }
  tbody tr { animation: rowIn .4s ease backwards; animation-delay: calc(var(--i, 0) * 70ms); }
  .btn { transition: transform .15s ease, background .2s ease, box-shadow .2s ease; }
  .btn:hover:not(:disabled) { transform: translateY(-2px); box-shadow: 0 6px 16px rgba(220,38,38,.35); }
  .btn:active:not(:disabled) { transform: scale(.96); }
</style></head>
<body>
<div class="card">
  <p><a href="/">&larr; Back to home</a></p>
  <h1>{{ title }}</h1>
  <p class="muted">Code: <b>{{ code }}</b> | Status: <b id="status"></b></p>
  <p>Students submitted: <span class="big" id="count">0</span></p>
  <button class="btn" id="closeBtn">Close Quiz &amp; Show Results</button>

  <h3 style="margin-top:24px">Live Leaderboard</h3>
  <div id="liveBoard"></div>

  <h3 style="margin-top:24px">Question Performance</h3>
  <div id="qStats" class="muted"></div>

  <div id="resultsBox"></div>
</div>

<script>
const CODE = "{{ code }}";

async function refresh() {
  const res = await fetch("/api/quizzes/" + CODE + "/results");
  const data = await res.json();
  document.getElementById("status").textContent = data.status;
  document.getElementById("count").textContent = data.submitted;
  const btn = document.getElementById("closeBtn");
  btn.disabled = data.status === "closed";
  btn.textContent = data.status === "closed" ? "Results are shown to students" : "Close Quiz & Show Results";

  if (data.status === "closed") {
    const box = document.getElementById("resultsBox");
    box.innerHTML = "";
    const table = document.createElement("table");
    table.innerHTML = "<thead><tr><th>#</th><th>Name</th><th>Score</th><th>Percent</th></tr></thead>";
    const tbody = document.createElement("tbody");
    data.results.forEach((r, i) => {
      const tr = document.createElement("tr");
      tr.style.setProperty("--i", i);
      const pct = r.total ? Math.round(r.score / r.total * 100) : 0;
      [i + 1, r.student, r.score + " / " + r.total, pct + "%"].forEach(t => {
        const td = document.createElement("td"); td.textContent = t; tr.appendChild(td);
      });
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    box.appendChild(table);
  }
}

document.getElementById("closeBtn").addEventListener("click", async () => {
  if (!confirm("Close the quiz? No more students can submit after this.")) return;
  await fetch("/api/quizzes/" + CODE + "/close", { method: "POST" });
  refresh();
});

async function loadBoard() {
  try {
    const res = await fetch("/api/quizzes/" + CODE + "/leaderboard");
    const data = await res.json();

    const board = document.getElementById("liveBoard");
    board.innerHTML = "";
    const table = document.createElement("table");
    table.innerHTML = "<thead><tr><th>#</th><th>Name</th><th>Correct</th><th>Answered</th><th>Accuracy</th></tr></thead>";
    const tbody = document.createElement("tbody");
    if (!data.leaderboard.length) {
      tbody.innerHTML = '<tr><td colspan="5" class="muted">No answers yet.</td></tr>';
    }
    data.leaderboard.forEach(r => {
      const tr = document.createElement("tr");
      const acc = r.answered ? Math.round(r.correct / r.answered * 100) : 0;
      [r.rank, r.student, r.correct, r.answered, acc + "%"].forEach(t => {
        const td = document.createElement("td"); td.textContent = t; tr.appendChild(td);
      });
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    board.appendChild(table);

    const qs = document.getElementById("qStats");
    qs.innerHTML = "";
    data.questions.forEach(q => {
      const line = document.createElement("div");
      const acc = q.answered ? Math.round(q.correct / q.answered * 100) : 0;
      line.textContent = "Q" + (q.index + 1) + ": " + q.correct + " of " + q.answered +
                         " answered correctly (" + acc + "%)";
      qs.appendChild(line);
    });
  } catch (e) { /* try again on the next tick */ }
}

refresh();
loadBoard();
setInterval(() => { refresh(); loadBoard(); }, 4000);
</script>
</body></html>
"""


# ---------- routes ----------

@app.route("/")
def home():
    return render_template_string(HOME_PAGE)


@app.route("/create")
def create_page():
    return render_template_string(CREATE_PAGE)


@app.route("/quiz/<code>")
def student_page(code):
    quiz = get_quiz(code)
    return render_template_string(STUDENT_PAGE, code=quiz["code"], title=quiz["title"])


@app.route("/results/<code>")
def results_page(code):
    quiz = get_quiz(code)
    return render_template_string(RESULTS_PAGE, code=quiz["code"], title=quiz["title"])


@app.errorhandler(404)
def not_found(_e):
    if request.path.startswith("/api/"):
        return jsonify({"error": "Quiz not found. Check the code and try again."}), 404
    return "Page not found", 404


# ---------- API ----------

@app.route("/api/quizzes", methods=["GET"])
def list_quizzes():
    rows = get_db().execute(
        "SELECT code, title, status FROM quizzes ORDER BY created DESC").fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/quizzes", methods=["POST"])
def create_quiz():
    body = request.get_json(silent=True) or {}
    title = (body.get("title") or "Untitled Quiz").strip()
    password = (body.get("password") or "").strip() or new_password()
    questions = body.get("questions")
    errors = validate_questions(questions)
    if errors:
        return jsonify({"error": "\n".join(errors)}), 400

    db = get_db()
    code = new_code(db)
    db.execute("INSERT INTO quizzes (code, title, questions, password) VALUES (?, ?, ?, ?)",
               (code, title, json.dumps(questions), password))
    db.commit()
    return jsonify({"code": code, "password": password}), 201


@app.route("/api/quizzes/<code>/questions", methods=["GET"])
def get_questions(code):
    quiz = get_quiz(code)
    questions = json.loads(quiz["questions"])
    # Never send the correct answer to the student's browser.
    public = [{"question": q["question"], "options": q["options"]} for q in questions]
    return jsonify({"title": quiz["title"], "status": quiz["status"], "questions": public})


@app.route("/api/quizzes/<code>/join", methods=["POST"])
def join(code):
    quiz = get_quiz(code)
    if quiz["status"] != "open":
        return jsonify({"error": "This quiz is closed."}), 403

    name = clean_name((request.get_json(silent=True) or {}).get("name"))
    if not name:
        return jsonify({"error": "Please enter a unique ID."}), 400
    if already_submitted(quiz["code"], name):
        return jsonify({"error": "This ID has already submitted this quiz. Choose another ID."}), 409
    if is_active(quiz["code"], name):
        return jsonify({"error": "This ID is in use right now. Please choose a different one."}), 409

    touch(quiz["code"], name)
    return jsonify({"ok": True, "name": name})


@app.route("/api/quizzes/<code>/verify", methods=["POST"])
def verify(code):
    quiz = get_quiz(code)
    body = request.get_json(silent=True) or {}
    name = clean_name(body.get("name"))
    password = (body.get("password") or "")

    if not is_active(quiz["code"], name):
        return jsonify({"error": "Your ID session expired. Please join again from the home page."}), 401
    if not secrets.compare_digest(password.encode(), quiz["password"].encode()):
        return jsonify({"error": "Wrong password."}), 403

    touch(quiz["code"], name)
    return jsonify({"ok": True})


@app.route("/api/quizzes/<code>/heartbeat", methods=["POST"])
def heartbeat(code):
    quiz = get_quiz(code)
    name = clean_name((request.get_json(silent=True) or {}).get("name"))
    if not is_active(quiz["code"], name):
        return jsonify({"error": "Session expired."}), 401
    touch(quiz["code"], name)
    return jsonify({"ok": True})


@app.route("/api/quizzes/<code>/submit", methods=["POST"])
def submit(code):
    quiz = get_quiz(code)
    if quiz["status"] != "open":
        return jsonify({"error": "This quiz is closed."}), 403

    body = request.get_json(silent=True) or {}
    name = clean_name(body.get("name"))
    answers = body.get("answers")
    questions = json.loads(quiz["questions"])

    if not name or not is_active(quiz["code"], name):
        return jsonify({"error": "Your session expired. Please join again from the home page."}), 401
    if not isinstance(answers, list) or len(answers) != len(questions):
        return jsonify({"error": "Answers are incomplete."}), 400

    score = 0
    for q, chosen in zip(questions, answers):
        if isinstance(chosen, int) and 0 <= chosen < len(q["options"]):
            if q["options"][chosen] == q["answer"]:
                score += 1

    db = get_db()
    try:
        db.execute(
            "INSERT INTO results (quiz_code, student, score, total) VALUES (?, ?, ?, ?)",
            (quiz["code"], name, score, len(questions)))
        db.commit()
    except sqlite3.IntegrityError:
        return jsonify({"error": "This ID has already submitted this quiz."}), 409

    for qi, (q, chosen) in enumerate(zip(questions, answers)):
        if isinstance(chosen, int) and 0 <= chosen < len(q["options"]):
            db.execute(
                "INSERT OR IGNORE INTO responses (quiz_code, student, question, choice, correct) "
                "VALUES (?, ?, ?, ?, ?)",
                (quiz["code"], name, qi, chosen, int(q["options"][chosen] == q["answer"])))
    db.commit()

    release(quiz["code"], name)
    return jsonify({"score": score, "total": len(questions)}), 201


@app.route("/api/quizzes/<code>/answer", methods=["POST"])
def check_answer(code):
    """Tell the student right away whether this one choice is correct.
    The student can answer each question only once, so revealing the result
    right after the choice does not help them change an answer."""
    quiz = get_quiz(code)
    if quiz["status"] != "open":
        return jsonify({"error": "This quiz is closed."}), 403

    body = request.get_json(silent=True) or {}
    name = clean_name(body.get("name"))
    qi = body.get("question")
    choice = body.get("choice")
    questions = json.loads(quiz["questions"])

    if not name or not is_active(quiz["code"], name):
        return jsonify({"error": "Your session expired. Please join again from the home page."}), 401
    if not isinstance(qi, int) or not 0 <= qi < len(questions):
        return jsonify({"error": "Invalid question."}), 400

    q = questions[qi]
    if not isinstance(choice, int) or not 0 <= choice < len(q["options"]):
        return jsonify({"error": "Invalid choice."}), 400

    correct_index = q["options"].index(q["answer"])
    is_correct = choice == correct_index

    db = get_db()
    db.execute(
        "INSERT OR IGNORE INTO responses (quiz_code, student, question, choice, correct) "
        "VALUES (?, ?, ?, ?, ?)",
        (quiz["code"], name, qi, choice, int(is_correct)))
    db.commit()

    return jsonify({"correct": is_correct, "correct_index": correct_index})


@app.route("/api/quizzes/<code>/leaderboard", methods=["GET"])
def leaderboard(code):
    quiz = get_quiz(code)
    questions = json.loads(quiz["questions"])
    db = get_db()

    student_rows = db.execute(
        "SELECT student, SUM(correct) AS correct, COUNT(*) AS answered "
        "FROM responses WHERE quiz_code = ? GROUP BY student",
        (quiz["code"],)).fetchall()
    board = [{"student": r["student"], "correct": r["correct"], "answered": r["answered"],
              "total": len(questions)} for r in student_rows]
    board.sort(key=lambda x: (-x["correct"], x["student"].lower()))
    for rank, row in enumerate(board, start=1):
        row["rank"] = rank

    question_rows = db.execute(
        "SELECT question, SUM(correct) AS correct, COUNT(*) AS answered "
        "FROM responses WHERE quiz_code = ? GROUP BY question",
        (quiz["code"],)).fetchall()
    by_q = {r["question"]: (r["correct"], r["answered"]) for r in question_rows}
    stats = []
    for i in range(len(questions)):
        correct, answered = by_q.get(i, (0, 0))
        stats.append({"index": i, "correct": correct, "answered": answered})

    return jsonify({"status": quiz["status"], "total": len(questions),
                    "leaderboard": board, "questions": stats})


@app.route("/api/quizzes/<code>/results", methods=["GET"])
def results(code):
    quiz = get_quiz(code)
    db = get_db()
    count = db.execute("SELECT COUNT(*) FROM results WHERE quiz_code = ?",
                       (quiz["code"],)).fetchone()[0]
    payload = {"status": quiz["status"], "submitted": count}
    if quiz["status"] == "closed":
        rows = db.execute(
            "SELECT student, score, total FROM results WHERE quiz_code = ? "
            "ORDER BY score DESC, student ASC", (quiz["code"],)).fetchall()
        payload["results"] = [dict(r) for r in rows]
    return jsonify(payload)


@app.route("/api/quizzes/<code>/close", methods=["POST"])
def close_quiz(code):
    quiz = get_quiz(code)
    db = get_db()
    db.execute("UPDATE quizzes SET status = 'closed' WHERE code = ?", (quiz["code"],))
    db.commit()
    return jsonify({"status": "closed"})


init_db()  # also runs when a hosting service starts the app with gunicorn

if __name__ == "__main__":
    import os
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False, threaded=True)
