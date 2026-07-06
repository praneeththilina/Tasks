"""
Life Planner Telegram Bot — single-file Flask app (Render + Supabase edition).
================================================================================

V2 IMPROVEMENTS OVER THE ORIGINAL
1. FREE AI, MORE RELIABLY — three free providers (OpenCode Zen, Gemini,
   Groq) chained as automatic fallback. If your active provider is down,
   rate-limited, or the key is bad, the bot silently tries the next
   configured one before giving up. Costs nothing extra either way — all
   three have permanently free tiers.
2. BUTTON-DRIVEN UI — /menu is now a real control panel (inline keyboard):
   status, tasks (with per-task ✅ Done / 🗑 Delete buttons), pause/resume
   toggle, and an admin panel. Menus edit IN PLACE instead of spamming new
   messages.
3. ADMIN SESSION — enter your password once via the Admin button and
   you're unlocked for 5 minutes; every admin action inside that window
   (change provider/model/key/password, restart) needs no further typing
   of the password. Old-style `/setpassword <old> <new>` etc. still work
   too, for scripting / muscle memory.
4. AUTO-DELETING SECRETS — any Telegram message that ever contains a
   plaintext password or API key (your slash-command form, or a reply typed
   into a password prompt) is deleted from the chat immediately after the
   bot reads it. Telegram bots are allowed to delete incoming messages in
   private chats, so your password never sits in the chat history.
5. Nicer formatting (emoji, HTML bold) and a typing indicator while the AI
   thinks, so mid-day chats feel less like a bare API echo.

WHAT'S THE SAME AS BEFORE
- Two secret routes only: POST /webhook/<WEBHOOK_SECRET>, GET /cron/<CRON_SECRET>.
  Everything else 404s. No web login, no HTML dashboard.
- Only ONE Telegram chat id may ever use this bot (first /claim wins).
- Postgres (Supabase) storage — nothing depends on Render's local disk.

FIRST-TIME SETUP — see README.md.
"""

import datetime
import html
import json
import os
import re
import threading
import time
from contextlib import contextmanager

import psycopg2
import psycopg2.extras
import psycopg2.pool
import requests
from flask import Flask, request, jsonify
from werkzeug.security import generate_password_hash, check_password_hash

# ============================== CONFIG (ENV VARS) ==========================


def require_env(name):
    val = os.environ.get(name)
    if not val:
        raise RuntimeError(
            f"Missing required environment variable: {name}. "
            "Set it in the Render dashboard under Environment."
        )
    return val


TELEGRAM_BOT_TOKEN = require_env("TELEGRAM_BOT_TOKEN")
WEBHOOK_SECRET = require_env("WEBHOOK_SECRET")
CRON_SECRET = require_env("CRON_SECRET")
SETUP_PASSWORD = require_env("SETUP_PASSWORD")
DATABASE_URL = require_env("DATABASE_URL")  # Supabase connection string

# Fallback AI credentials used only if nothing has been set via the bot yet.
# All three of these have free tiers — set as many as you like. The bot
# tries them in this order: current provider first, then the others as
# automatic fallback if a call fails.
DEFAULT_AI_PROVIDER = os.environ.get("DEFAULT_AI_PROVIDER", "OpenCode Zen")  # opencode|gemini|groq

DEFAULT_OPENCODE_MODEL = os.environ.get("DEFAULT_OPENCODE_MODEL", "big-pickle")
DEFAULT_OPENCODE_API_KEY = os.environ.get("DEFAULT_OPENCODE_API_KEY", "")

DEFAULT_GEMINI_MODEL = os.environ.get("DEFAULT_GEMINI_MODEL", "gemini-2.5-flash")
DEFAULT_GEMINI_API_KEY = os.environ.get("DEFAULT_GEMINI_API_KEY", "")

# Groq: free, fast Llama/Kimi/Gpt-oss inference. Get a free key at
# https://console.groq.com/keys
DEFAULT_GROQ_MODEL = os.environ.get("DEFAULT_GROQ_MODEL", "llama-3.3-70b-versatile")
DEFAULT_GROQ_API_KEY = os.environ.get("DEFAULT_GROQ_API_KEY", "")

OPENCODE_API_BASE = "https://opencode.ai/zen/v1"
GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta"
GROQ_API_BASE = "https://api.groq.com/openai/v1"

TELEGRAM_API_BASE = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

PROVIDERS = ("opencode", "gemini", "groq")
ADMIN_SESSION_SECONDS = 5 * 60

# ============================== DATABASE (POSTGRES) =========================
#
# IMPORTANT: gunicorn is run with --threads 4 (see Procfile/render.yaml), so
# several requests can be inside this module at the same instant, all sharing
# one pool object. psycopg2.pool.SimpleConnectionPool is explicitly documented
# as NOT thread-safe — only ThreadedConnectionPool is. Under concurrent
# button-driven traffic (the v2 menu UI fires far more overlapping requests
# than the old command-only bot did) SimpleConnectionPool's internal
# bookkeeping can get corrupted, which shows up as a request hanging forever
# waiting on getconn() — exactly the "tap Admin, spinner never stops" symptom.
# connect_timeout also makes sure a genuinely unreachable DB fails in seconds
# instead of hanging indefinitely.
db_pool = psycopg2.pool.ThreadedConnectionPool(
    1, 10, dsn=DATABASE_URL, sslmode="require", connect_timeout=10
)


@contextmanager
def db_cursor(commit=False):
    conn = db_pool.getconn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        try:
            yield cur
            if commit:
                conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.close()
    finally:
        db_pool.putconn(conn)


def init_db():
    with db_cursor(commit=True) as c:
        c.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS profile (chat_id TEXT PRIMARY KEY, data TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS daily_log (chat_id TEXT PRIMARY KEY, data TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS conversation (chat_id TEXT PRIMARY KEY, data TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS chat_mode (chat_id TEXT PRIMARY KEY, value TEXT)")
        c.execute("""CREATE TABLE IF NOT EXISTS scheduled_tasks (
                        id SERIAL PRIMARY KEY,
                        chat_id TEXT,
                        description TEXT,
                        due_date TEXT,
                        done INTEGER DEFAULT 0,
                        created_at TEXT
                     )""")


def get_setting(key, default=None):
    with db_cursor() as c:
        c.execute("SELECT value FROM settings WHERE key = %s", (key,))
        row = c.fetchone()
    return row["value"] if row else default


def set_setting(key, value):
    with db_cursor(commit=True) as c:
        c.execute(
            "INSERT INTO settings (key, value) VALUES (%s, %s) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            (key, value),
        )


def clear_setting(key):
    with db_cursor(commit=True) as c:
        c.execute("DELETE FROM settings WHERE key = %s", (key,))


# --- owner / auth ---

def get_owner_chat_id():
    return get_setting("owner_chat_id")


def is_owner(chat_id):
    owner = get_owner_chat_id()
    return owner is not None and str(owner) == str(chat_id)


def claim_owner(chat_id, password):
    if get_owner_chat_id() is not None:
        return False, "This bot is already claimed."
    if password != SETUP_PASSWORD:
        return False, "Wrong setup password."
    set_setting("owner_chat_id", str(chat_id))
    set_setting("admin_password_hash", generate_password_hash(SETUP_PASSWORD))
    set_setting("ai_provider", DEFAULT_AI_PROVIDER)
    set_setting("ai_opencode_model", DEFAULT_OPENCODE_MODEL)
    set_setting("ai_opencode_key", DEFAULT_OPENCODE_API_KEY)
    set_setting("ai_gemini_model", DEFAULT_GEMINI_MODEL)
    set_setting("ai_gemini_key", DEFAULT_GEMINI_API_KEY)
    set_setting("ai_groq_model", DEFAULT_GROQ_MODEL)
    set_setting("ai_groq_key", DEFAULT_GROQ_API_KEY)
    set_setting("paused", "0")
    return True, "✅ Claimed! Please set a new private password now: /setpassword <setup_password> <new_password>"


def check_password(password):
    stored_hash = get_setting("admin_password_hash")
    if not stored_hash:
        return False
    return check_password_hash(stored_hash, password)


def set_password(new_password):
    set_setting("admin_password_hash", generate_password_hash(new_password))


# --- admin session (so you don't retype your password for every action) ---

def start_admin_session():
    set_setting("admin_session_expires", str(int(time.time()) + ADMIN_SESSION_SECONDS))


def admin_session_active():
    expires = get_setting("admin_session_expires")
    return bool(expires) and int(expires) > int(time.time())


def end_admin_session():
    clear_setting("admin_session_expires")


# --- pending action (what a plain-text reply should be interpreted as) ---

def get_pending_action():
    raw = get_setting("pending_action")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def set_pending_action(action, **extra):
    payload = {"action": action, **extra}
    set_setting("pending_action", json.dumps(payload))


def clear_pending_action():
    clear_setting("pending_action")


# --- profile / log / conversation / mode ---

def _get_json(table, chat_id, default):
    with db_cursor() as c:
        c.execute(f"SELECT data FROM {table} WHERE chat_id = %s", (str(chat_id),))
        row = c.fetchone()
    if not row:
        return default
    try:
        return json.loads(row["data"])
    except (json.JSONDecodeError, TypeError):
        return default


def _set_json(table, chat_id, data):
    with db_cursor(commit=True) as c:
        c.execute(
            f"INSERT INTO {table} (chat_id, data) VALUES (%s, %s) "
            f"ON CONFLICT (chat_id) DO UPDATE SET data = EXCLUDED.data",
            (str(chat_id), json.dumps(data, ensure_ascii=False)),
        )


def get_profile(chat_id):
    return _get_json("profile", chat_id, None)


def set_profile(chat_id, profile):
    _set_json("profile", chat_id, profile)


def get_daily_log(chat_id):
    return _get_json("daily_log", chat_id, {"entries": [], "current_streaks": {}, "last_weekly_summary_date": None})


def set_daily_log(chat_id, log):
    _set_json("daily_log", chat_id, log)


def get_conversation(chat_id):
    return _get_json("conversation", chat_id, [])


def append_conversation(chat_id, role, content):
    convo = get_conversation(chat_id)
    convo.append({"role": role, "content": content})
    convo = convo[-40:]
    _set_json("conversation", chat_id, convo)
    return convo


def clear_conversation(chat_id):
    _set_json("conversation", chat_id, [])


def get_mode(chat_id):
    with db_cursor() as c:
        c.execute("SELECT value FROM chat_mode WHERE chat_id = %s", (str(chat_id),))
        row = c.fetchone()
    return row["value"] if row else "onboarding"


def set_mode(chat_id, mode):
    with db_cursor(commit=True) as c:
        c.execute(
            "INSERT INTO chat_mode (chat_id, value) VALUES (%s, %s) "
            "ON CONFLICT (chat_id) DO UPDATE SET value = EXCLUDED.value",
            (str(chat_id), mode),
        )


def is_paused():
    return get_setting("paused", "0") == "1"


# --- scheduled tasks ---

def add_task(chat_id, description, due_date):
    with db_cursor(commit=True) as c:
        c.execute(
            "INSERT INTO scheduled_tasks (chat_id, description, due_date, done, created_at) "
            "VALUES (%s, %s, %s, 0, %s)",
            (str(chat_id), description, due_date, datetime.datetime.utcnow().isoformat()),
        )


def list_tasks(chat_id, include_done=False):
    with db_cursor() as c:
        if include_done:
            c.execute("SELECT * FROM scheduled_tasks WHERE chat_id = %s ORDER BY due_date", (str(chat_id),))
        else:
            c.execute(
                "SELECT * FROM scheduled_tasks WHERE chat_id = %s AND done = 0 ORDER BY due_date",
                (str(chat_id),),
            )
        rows = c.fetchall()
    return [dict(r) for r in rows]


def tasks_due_within(chat_id, days_ahead=3):
    today = datetime.date.today()
    horizon = today + datetime.timedelta(days=days_ahead)
    all_tasks = list_tasks(chat_id)
    due = []
    for t in all_tasks:
        try:
            d = datetime.date.fromisoformat(t["due_date"])
        except (ValueError, TypeError):
            continue
        if d <= horizon:
            due.append(t)
    return due


def mark_task_done(chat_id, task_id):
    with db_cursor(commit=True) as c:
        c.execute("UPDATE scheduled_tasks SET done = 1 WHERE id = %s AND chat_id = %s", (task_id, str(chat_id)))


def delete_task(chat_id, task_id):
    with db_cursor(commit=True) as c:
        c.execute("DELETE FROM scheduled_tasks WHERE id = %s AND chat_id = %s", (task_id, str(chat_id)))


# ============================== AI PROVIDERS ===============================
# Each call_* raises RuntimeError on failure. call_llm() tries the active
# provider first, then falls back through the other two (only ones that
# have a key configured) so a single dead/rate-limited free key doesn't
# take the whole bot down.


def call_opencode(messages, max_tokens):
    api_key = get_setting("ai_opencode_key", DEFAULT_OPENCODE_API_KEY)
    model = get_setting("ai_opencode_model", DEFAULT_OPENCODE_MODEL)
    if not api_key:
        raise RuntimeError("no opencode key configured")
    url = f"{OPENCODE_API_BASE}/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {"model": model, "messages": messages, "max_tokens": max_tokens, "temperature": 0.6}
    resp = requests.post(url, headers=headers, json=payload, timeout=60)
    if resp.status_code != 200:
        raise RuntimeError(f"OpenCode Zen returned {resp.status_code}: {resp.text[:400]}")
    data = resp.json()
    return data["choices"][0]["message"]["content"]


def call_gemini(messages, max_tokens):
    api_key = get_setting("ai_gemini_key", DEFAULT_GEMINI_API_KEY)
    model = get_setting("ai_gemini_model", DEFAULT_GEMINI_MODEL)
    if not api_key:
        raise RuntimeError("no gemini key configured")
    url = f"{GEMINI_API_BASE}/models/{model}:generateContent"
    headers = {"x-goog-api-key": api_key, "Content-Type": "application/json"}

    system_parts = [m["content"] for m in messages if m["role"] == "system"]
    contents = []
    for m in messages:
        if m["role"] == "system":
            continue
        role = "model" if m["role"] == "assistant" else "user"
        contents.append({"role": role, "parts": [{"text": m["content"]}]})

    payload = {
        "contents": contents,
        "generationConfig": {
            "maxOutputTokens": max_tokens,
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }
    if system_parts:
        payload["system_instruction"] = {"parts": [{"text": "\n\n".join(system_parts)}]}

    resp = requests.post(url, headers=headers, json=payload, timeout=60)
    if resp.status_code != 200:
        raise RuntimeError(f"Gemini returned {resp.status_code}: {resp.text[:400]}")
    data = resp.json()
    try:
        candidate = data["candidates"][0]
        finish_reason = candidate.get("finishReason")
        text = candidate["content"]["parts"][0]["text"]
    except (KeyError, IndexError):
        raise RuntimeError(f"Unexpected Gemini response shape: {data}")

    if finish_reason == "MAX_TOKENS":
        text = text.rstrip() + "\n\n[cut off - reply exceeded the token limit]"
    return text


def call_groq(messages, max_tokens):
    api_key = get_setting("ai_groq_key", DEFAULT_GROQ_API_KEY)
    model = get_setting("ai_groq_model", DEFAULT_GROQ_MODEL)
    if not api_key:
        raise RuntimeError("no groq key configured")
    url = f"{GROQ_API_BASE}/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {"model": model, "messages": messages, "max_tokens": max_tokens, "temperature": 0.6}
    resp = requests.post(url, headers=headers, json=payload, timeout=60)
    if resp.status_code != 200:
        raise RuntimeError(f"Groq returned {resp.status_code}: {resp.text[:400]}")
    data = resp.json()
    return data["choices"][0]["message"]["content"]


CALLERS = {"opencode": call_opencode, "gemini": call_gemini, "groq": call_groq}


def call_llm(messages, max_tokens=1000):
    provider = get_setting("ai_provider", DEFAULT_AI_PROVIDER)
    order = [provider] + [p for p in PROVIDERS if p != provider]
    errors = []
    for p in order:
        caller = CALLERS.get(p)
        if not caller:
            continue
        try:
            return caller(messages, max_tokens)
        except requests.exceptions.RequestException as e:
            errors.append(f"{p}: network error ({e})")
        except RuntimeError as e:
            errors.append(f"{p}: {e}")
    raise RuntimeError("All configured AI providers failed — " + " | ".join(errors))


# ============================== TELEGRAM ===================================


def _tg_post(method, payload):
    try:
        resp = requests.post(f"{TELEGRAM_API_BASE}/{method}", json=payload, timeout=20)
        return resp.json()
    except requests.exceptions.RequestException as e:
        print(f"[telegram] {method} failed: {e}")
        return {}


def send_message(chat_id, text, reply_markup=None, parse_mode=None):
    """Sends text, chunked to Telegram's limit. Returns the LAST chunk's message_id (or None)."""
    chunks = [text[i:i + 3500] for i in range(0, len(text), 3500)] or [""]
    message_id = None
    for i, chunk in enumerate(chunks):
        payload = {"chat_id": chat_id, "text": chunk}
        if parse_mode is not None:
            payload["parse_mode"] = parse_mode
        if reply_markup is not None and i == len(chunks) - 1:
            payload["reply_markup"] = reply_markup
        data = _tg_post("sendMessage", payload)
        if data.get("ok"):
            message_id = data["result"]["message_id"]
    return message_id


def edit_message(chat_id, message_id, text, reply_markup=None, parse_mode=None):
    payload = {"chat_id": chat_id, "message_id": message_id, "text": text}
    if parse_mode is not None:
        payload["parse_mode"] = parse_mode
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    data = _tg_post("editMessageText", payload)
    if not data.get("ok"):
        # message may be identical / too old to edit - fall back to a fresh send
        return send_message(chat_id, text, reply_markup, parse_mode)
    return message_id


def delete_message(chat_id, message_id):
    if not message_id:
        return
    _tg_post("deleteMessage", {"chat_id": chat_id, "message_id": message_id})


def schedule_delete(chat_id, message_id, delay_seconds=8):
    """Deletes a message after a short delay, without blocking the request thread."""
    if not message_id:
        return

    def _job():
        time.sleep(delay_seconds)
        delete_message(chat_id, message_id)

    threading.Thread(target=_job, daemon=True).start()


def send_typing(chat_id):
    _tg_post("sendChatAction", {"chat_id": chat_id, "action": "typing"})


def answer_callback(callback_query_id, text=None, alert=False):
    payload = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = text
        payload["show_alert"] = alert
    _tg_post("answerCallbackQuery", payload)


def esc(s):
    return html.escape(str(s), quote=False)


def kb(rows):
    """rows: list of lists of (label, callback_data) tuples -> Telegram inline_keyboard markup."""
    return {"inline_keyboard": [[{"text": label, "callback_data": data} for label, data in row] for row in rows]}


# ============================== MENUS =======================================


def main_menu_text_and_kb(chat_id):
    paused = is_paused()
    pause_label = "▶️ Resume check-ins" if paused else "⏸ Pause check-ins"
    open_tasks = len(list_tasks(chat_id))
    text = (
        "🧭 <b>Life Planner — Control Panel</b>\n\n"
        f"Daily check-ins: {'⏸ paused' if paused else '✅ active'}\n"
        f"Open tasks: {open_tasks}\n\n"
        "Pick something below, or just message me normally any time."
    )
    markup = kb([
        [("📊 Status", "menu:status"), ("🗒 Tasks", "menu:tasks")],
        [(pause_label, "menu:pause")],
        [("⚙️ Admin", "menu:admin")],
    ])
    return text, markup


def send_main_menu(chat_id, message_id=None):
    text, markup = main_menu_text_and_kb(chat_id)
    if message_id:
        edit_message(chat_id, message_id, text, markup, parse_mode="HTML")
    else:
        send_message(chat_id, text, markup, parse_mode="HTML")


def status_text(chat_id):
    profile = get_profile(chat_id)
    if not profile:
        return "Setup isn't finished yet — just keep chatting with me to complete onboarding."
    log = get_daily_log(chat_id)
    streaks = log.get("current_streaks", {})
    streak_text = ", ".join(f"{esc(g)}: {d}d 🔥" for g, d in streaks.items()) or "none yet"
    name = esc(profile.get("identity", {}).get("name", "set"))
    return (
        f"📊 <b>Status</b>\n\n"
        f"Name: {name}\n"
        f"Streaks: {streak_text}\n"
        f"Paused: {'yes' if is_paused() else 'no'}\n"
        f"Open tasks: {len(list_tasks(chat_id))}"
    )


def tasks_text_and_kb(chat_id):
    tasks = list_tasks(chat_id)
    if not tasks:
        text = "🗒 No open tasks.\n\nAdd one with:\n<code>/addtask YYYY-MM-DD description</code>"
        return text, kb([[("⬅️ Back", "menu:main")]])
    lines = ["🗒 <b>Open tasks</b>"]
    rows = []
    for t in tasks:
        lines.append(f"#{t['id']} · {esc(t['due_date'])} · {esc(t['description'])}")
        rows.append([("✅ Done #" + str(t["id"]), f"task:done:{t['id']}"),
                     ("🗑 Del #" + str(t["id"]), f"task:del:{t['id']}")])
    rows.append([("⬅️ Back", "menu:main")])
    return "\n".join(lines), kb(rows)


def admin_menu_kb():
    provider = get_setting("ai_provider", DEFAULT_AI_PROVIDER)
    return kb([
        [("🤖 Provider (" + provider + ")", "admin:provider")],
        [("🧠 Model", "admin:model"), ("🗝 API Key", "admin:key")],
        [("👁 View Settings", "admin:view"), ("🔑 Change Password", "admin:setpassword")],
        [("♻️ Restart Profile", "admin:restart")],
        [("⬅️ Back", "menu:main")],
    ])


def provider_pick_kb():
    provider = get_setting("ai_provider", DEFAULT_AI_PROVIDER)
    rows = []
    for p in PROVIDERS:
        mark = "✅ " if p == provider else ""
        rows.append([(f"{mark}{p}", f"admin:setprovider:{p}")])
    rows.append([("⬅️ Back", "menu:admin")])
    return kb(rows)


# ============================== PROMPTS ====================================

ONBOARDING_SYSTEM_PROMPT = """You are a life-and-career planning intake agent, talking to one person over
Telegram. Build ONE structured JSON profile by asking questions
conversationally - never a bare form dump. A separate daily-planning
process will use this profile every day, so capture anything it will need.

RULES
- Ask 2-4 short questions at a time. Use their own numbers/words - don't
  assume currency, family structure, or career stage.
- If they give you a lot unprompted, extract instead of re-asking.
- Separate HARD CONSTRAINTS (income, dependents, health limits) from SOFT
  PREFERENCES (reminder time, tone). Don't moralize - collect, don't judge.
- Keep messages short - this is a phone chat.

COLLECT: identity (name, age, location, timezone, family status), career
(role, years experience, in-progress qualification + target date, income,
stability), finances (monthly savings capacity, assets, liabilities,
emergency fund), a RANKED list of goals with horizon (near/mid/long) and
why each matters, reminder preferences (frequency, time, tone:
direct/gentle/balanced, definition of a good day, topics never to raise),
and optionally known failure patterns (what has derailed them before).

When you have enough, read the profile back in plain language and ask
"does this look right?". Only once they explicitly confirm, reply with a
short confirmation sentence followed by EXACTLY one fenced ```json block
in this shape (no other text inside the fence):

{
  "profile_version": 1,
  "identity": {"name": "", "age": null, "location": "", "timezone": "", "family_status": "", "dependents": ""},
  "career": {"role": "", "years_experience": null, "field": "", "qualification_in_progress": {"name": "", "stage": "", "target_date": ""}, "income_monthly": null, "income_currency": "", "income_stability": "", "income_ceiling_note": ""},
  "finance": {"monthly_savings_capacity": null, "assets": [], "liabilities": [], "emergency_fund_status": ""},
  "goals": [{"goal": "", "rank": 1, "horizon": "near|mid|long", "why_it_matters": ""}],
  "preferences": {"checkin_frequency": "", "checkin_time": "", "tone": "direct|gentle|balanced", "definition_of_good_day": "", "do_not_nag_about": []},
  "failure_patterns": [],
  "notes_freeform": ""
}

Never emit this block before explicit confirmation - keep asking questions
until then.
"""

DAILY_SYSTEM_PROMPT = """You are a long-term life-and-career planning companion for one person,
running automatically once a day over Telegram. You're given their
`profile`, a rolling `daily_log`, and any `scheduled_tasks_due` (their own
manually-added reminders, separate from your own suggestions).

YOUR JOB
1. profile.goals is ranked - goal #1 gets priority unless the log shows an
   explicit stated reason it was deprioritized.
2. Read daily_log for streaks, misses, mood/energy. Never push the same
   intensity after a bad day that you would after a good one.
3. Mention any scheduled_tasks_due plainly and briefly - these are things
   the person explicitly asked to be reminded about, so don't bury them.
4. Send ONE message: an honest one-line read on progress toward goal #1
   (truthful and kind, never generic positivity or a lecture), then 1-3
   concrete actions sized to their realistic capacity today, plus a
   relevant metric if you have one (streak days, % of a target reached).
5. You are optimizing for MONTHS of consistency, not one good day. Notice
   and name real long-term progress ("that's 6 weeks now") - this matters
   more than any single day's output, and is what keeps someone going.

HARD RULES - HUMANS ARE NOT ROBOTS
- Never guilt-trip a missed day. Acknowledge once, plainly, move forward.
- Never send more than 3 action items.
- After 2+ consecutive missed check-ins: shorten the message, lower the
  ask - never escalate.
- Never mention topics in profile.preferences.do_not_nag_about.
- Tone "gentle": avoid "failure"/"behind"/"wasting time" - reframe as
  "still open"/"next step". Tone "direct": blunt about numbers/timelines,
  but respectful, never cold.
- A zero-progress day tied to illness or a stated real event is legitimate
  rest, not avoidance - don't assume the worst.
- Every 7th run: give a short weekly-shape summary instead of another
  nudge - what moved, what didn't, whether the plan needs adjusting.
- If a goal shows zero movement for 3+ weekly summaries, say so honestly
  and ask whether the goal, its rank, or its timeline should change.
- Never fabricate progress or numbers you don't have.

OUTPUT FORMAT
First, the message to send - plain conversational text, 4-8 sentences, no
headers. Then, on a new line, EXACTLY one fenced ```json block containing
ONLY the new daily_log entry to append:

{
  "date": "YYYY-MM-DD",
  "checked_in": true,
  "goal_progress": {"goal": "", "action_taken": "", "streak_days": null},
  "mood_or_energy": "",
  "notes": ""
}

If daily_log.entries is empty (first-ever run), say so plainly and just
propose today's 1-3 actions from the profile alone.
"""


def extract_json_block(text):
    match = re.search(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL)
    if not match:
        return None, text
    try:
        parsed = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None, text
    remaining = (text[:match.start()] + text[match.end():]).strip()
    return parsed, remaining


def build_daily_user_message(profile, daily_log, scheduled_tasks_due):
    payload = {"profile": profile, "daily_log": daily_log, "scheduled_tasks_due": scheduled_tasks_due}
    return ("Here is the current state as JSON. Produce today's check-in "
            "message plus the new daily_log entry, following your system "
            "instructions exactly.\n\n"
            f"```json\n{json.dumps(payload, indent=2, ensure_ascii=False)}\n```")


# ============================== FLASK APP ==================================

app = Flask(__name__)
init_db()


@app.route("/")
def health():
    return jsonify({"ok": True, "service": "lifeplanner-bot"})


PASSWORD_BEARING_COMMANDS = {
    "/claim", "/restart", "/setpassword", "/setprovider", "/setmodel", "/setkey", "/settings",
}


@app.route(f"/webhook/{WEBHOOK_SECRET}", methods=["POST"])
def webhook():
    update = request.get_json(silent=True) or {}

    if "callback_query" in update:
        handle_callback(update["callback_query"])
        return jsonify({"ok": True})

    message = update.get("message") or update.get("edited_message")
    if not message:
        return jsonify({"ok": True})

    chat_id = message["chat"]["id"]
    incoming_message_id = message.get("message_id")
    text = (message.get("text") or "").strip()
    if not text:
        return jsonify({"ok": True})

    owner = get_owner_chat_id()

    # --- Bot is unclaimed: only /claim <password> does anything ---
    if owner is None:
        if text.lower().startswith("/claim"):
            parts = text.split(maxsplit=1)
            password = parts[1] if len(parts) > 1 else ""
            ok, msg = claim_owner(chat_id, password)
            send_message(chat_id, msg)
            delete_message(chat_id, incoming_message_id)  # plaintext password - scrub it
        return jsonify({"ok": True})

    # --- Bot is claimed: reject anyone who isn't the owner, no exceptions ---
    if not is_owner(chat_id):
        send_message(chat_id, "This bot is private.")
        return jsonify({"ok": True})

    # --- Pending interactive action (e.g. a password/model/key typed after a button prompt) ---
    pending = get_pending_action()
    if pending and not text.startswith("/"):
        delete_message(chat_id, incoming_message_id)  # always scrub - may contain a secret
        handle_pending_action(chat_id, pending, text)
        return jsonify({"ok": True})

    # --- From here on, chat_id is guaranteed to be the owner ---
    if text.startswith("/"):
        if pending:
            clear_pending_action()  # they bailed out of the password/model/key prompt via a command
        cmd = text.split()[0].lower()
        handle_command(chat_id, text)
        if cmd in PASSWORD_BEARING_COMMANDS:
            delete_message(chat_id, incoming_message_id)  # scrub plaintext password/key
    else:
        mode = get_mode(chat_id)
        send_typing(chat_id)
        if mode == "onboarding":
            handle_onboarding_turn(chat_id, text)
        else:
            handle_active_chat(chat_id, text)

    return jsonify({"ok": True})


@app.route(f"/cron/{CRON_SECRET}", methods=["GET", "POST"])
def cron():
    owner = get_owner_chat_id()
    if owner is None:
        return jsonify({"ok": False, "reason": "not claimed yet"})
    if is_paused():
        return jsonify({"ok": True, "skipped": "paused"})
    run_daily_checkin(owner)
    return jsonify({"ok": True})


# ============================== CALLBACK (BUTTON) HANDLING =================


def handle_callback(cq):
    chat_id = cq["message"]["chat"]["id"]
    message_id = cq["message"]["message_id"]
    data = cq.get("data", "")
    cq_id = cq["id"]

    try:
        if not is_owner(chat_id):
            answer_callback(cq_id, "This bot is private.", alert=True)
            return

        answer_callback(cq_id)  # stop the button's loading spinner immediately
        _dispatch_callback(chat_id, message_id, data)
    except Exception as e:
        # Whatever went wrong, make sure Telegram's button spinner still
        # clears instead of spinning forever, and log it so it shows up
        # in the Render logs.
        print(f"[handle_callback] error handling '{data}': {e}")
        answer_callback(cq_id, "⚠️ Something went wrong handling that — check the logs / try again.", alert=True)


def _dispatch_callback(chat_id, message_id, data):
    if data == "menu:main":
        send_main_menu(chat_id, message_id)

    elif data == "menu:status":
        edit_message(chat_id, message_id, status_text(chat_id), kb([[("⬅️ Back", "menu:main")]]), parse_mode="HTML")

    elif data == "menu:tasks":
        text, markup = tasks_text_and_kb(chat_id)
        edit_message(chat_id, message_id, text, markup, parse_mode="HTML")

    elif data == "menu:pause":
        set_setting("paused", "0" if is_paused() else "1")
        send_main_menu(chat_id, message_id)

    elif data.startswith("task:done:"):
        mark_task_done(chat_id, int(data.split(":")[2]))
        text, markup = tasks_text_and_kb(chat_id)
        edit_message(chat_id, message_id, text, markup, parse_mode="HTML")

    elif data.startswith("task:del:"):
        delete_task(chat_id, int(data.split(":")[2]))
        text, markup = tasks_text_and_kb(chat_id)
        edit_message(chat_id, message_id, text, markup, parse_mode="HTML")

    elif data == "menu:admin":
        if admin_session_active():
            edit_message(chat_id, message_id, "⚙️ <b>Admin panel</b> (unlocked)", admin_menu_kb(), parse_mode="HTML")
        else:
            edit_message(chat_id, message_id, "🔒 Admin needs your password. Reply with it now — "
                                               "your message will auto-delete.")
            set_pending_action("admin_unlock", menu_message_id=message_id)

    elif data == "admin:provider":
        _require_admin(chat_id, message_id, lambda: edit_message(
            chat_id, message_id, "🤖 <b>Choose AI provider</b>", provider_pick_kb(), parse_mode="HTML"))

    elif data.startswith("admin:setprovider:"):
        provider = data.split(":")[2]
        _require_admin(chat_id, message_id, lambda: (
            set_setting("ai_provider", provider),
            edit_message(chat_id, message_id, f"✅ Provider switched to <b>{esc(provider)}</b>.",
                         admin_menu_kb(), parse_mode="HTML"),
        ))

    elif data == "admin:model":
        _require_admin(chat_id, message_id, lambda: (
            edit_message(chat_id, message_id, "🧠 Send the new model id as a message (it will auto-delete)."),
            set_pending_action("setmodel"),
        ))

    elif data == "admin:key":
        _require_admin(chat_id, message_id, lambda: (
            edit_message(chat_id, message_id, "🗝 Send the new API key as a message — "
                                               "it is scrubbed from the chat the instant I read it."),
            set_pending_action("setkey"),
        ))

    elif data == "admin:setpassword":
        _require_admin(chat_id, message_id, lambda: (
            edit_message(chat_id, message_id, "🔑 Send the new password as a message (auto-deletes)."),
            set_pending_action("setpassword_new"),
        ))

    elif data == "admin:view":
        _require_admin(chat_id, message_id, lambda: edit_message(chat_id, message_id, admin_settings_text(),
                                                                   kb([[("⬅️ Back", "menu:admin")]]),
                                                                   parse_mode="HTML"))

    elif data == "admin:restart":
        _require_admin(chat_id, message_id, lambda: edit_message(
            chat_id, message_id,
            "♻️ This wipes your profile and redoes onboarding. Are you sure?",
            kb([[("✅ Yes, wipe it", "admin:restart_yes"), ("❌ Cancel", "menu:admin")]]),
        ))

    elif data == "admin:restart_yes":
        clear_conversation(chat_id)
        set_profile(chat_id, None)
        set_mode(chat_id, "onboarding")
        edit_message(chat_id, message_id, "✅ Profile cleared.")
        send_message(chat_id, "Let's redo setup — tell me a bit about yourself.")


def _require_admin(chat_id, message_id, fn):
    if not admin_session_active():
        edit_message(chat_id, message_id, "🔒 Session expired. Reply with your password — it will auto-delete.")
        set_pending_action("admin_unlock", menu_message_id=message_id)
        return
    fn()


def admin_settings_text():
    provider = get_setting("ai_provider", DEFAULT_AI_PROVIDER)
    model = get_setting(f"ai_{provider}_model", "")
    key = get_setting(f"ai_{provider}_key", "")
    masked = (key[:4] + "..." + key[-4:]) if len(key) > 10 else "(not set)"
    return (f"👁 <b>Current settings</b>\n\nProvider: {esc(provider)}\nModel: {esc(model)}\nKey: {esc(masked)}")


def handle_pending_action(chat_id, pending, text):
    action = pending.get("action")
    clear_pending_action()

    if action == "admin_unlock":
        if check_password(text):
            start_admin_session()
            send_message(chat_id, "✅ Unlocked for 5 minutes.")
            send_message(chat_id, "⚙️ <b>Admin panel</b>", admin_menu_kb(), parse_mode="HTML")
        else:
            send_message(chat_id, "❌ Wrong password.")
        return

    if not admin_session_active():
        send_message(chat_id, "🔒 Session expired — open ⚙️ Admin again.")
        return

    if action == "setmodel":
        provider = get_setting("ai_provider", DEFAULT_AI_PROVIDER)
        set_setting(f"ai_{provider}_model", text.strip())
        send_message(chat_id, f"✅ {esc(provider)} model set to <code>{esc(text.strip())}</code>.",
                     admin_menu_kb(), parse_mode="HTML")

    elif action == "setkey":
        provider = get_setting("ai_provider", DEFAULT_AI_PROVIDER)
        set_setting(f"ai_{provider}_key", text.strip())
        send_message(chat_id, f"✅ {esc(provider)} API key updated.", admin_menu_kb(), parse_mode="HTML")

    elif action == "setpassword_new":
        set_password(text.strip())
        send_message(chat_id, "✅ Password changed.", admin_menu_kb())


# ============================== COMMANDS ===================================


def handle_command(chat_id, text):
    parts = text.split()
    cmd = parts[0].lower()

    if cmd in ("/menu", "/start"):
        send_main_menu(chat_id)

    elif cmd == "/help":
        send_message(chat_id, MENU_TEXT)

    elif cmd == "/status":
        send_message(chat_id, status_text(chat_id), parse_mode="HTML")

    elif cmd == "/pause":
        set_setting("paused", "1")
        send_message(chat_id, "⏸ Paused. Daily check-ins won't send until you /resume.")

    elif cmd == "/resume":
        set_setting("paused", "0")
        send_message(chat_id, "▶️ Resumed - you'll get your next daily check-in as scheduled.")

    elif cmd == "/restart":
        if len(parts) < 2 or not check_password(parts[1]):
            send_message(chat_id, "Usage: /restart <password>  (wipes your profile and redoes onboarding)")
            return
        clear_conversation(chat_id)
        set_profile(chat_id, None)
        set_mode(chat_id, "onboarding")
        send_message(chat_id, "✅ Profile cleared. Let's redo setup - tell me a bit about yourself.")

    elif cmd == "/addtask":
        if len(parts) < 3:
            send_message(chat_id, "Usage: /addtask YYYY-MM-DD description")
            return
        due_date = parts[1]
        description = " ".join(parts[2:])
        try:
            datetime.date.fromisoformat(due_date)
        except ValueError:
            send_message(chat_id, "That date doesn't look like YYYY-MM-DD - try again.")
            return
        add_task(chat_id, description, due_date)
        send_message(chat_id, f"➕ Added: {description} (due {due_date})")

    elif cmd == "/tasks":
        text_, markup = tasks_text_and_kb(chat_id)
        send_message(chat_id, text_, markup, parse_mode="HTML")

    elif cmd == "/done":
        if len(parts) < 2 or not parts[1].isdigit():
            send_message(chat_id, "Usage: /done <task_id>  (see /tasks for ids)")
            return
        mark_task_done(chat_id, int(parts[1]))
        send_message(chat_id, "✅ Marked done.")

    elif cmd == "/deltask":
        if len(parts) < 2 or not parts[1].isdigit():
            send_message(chat_id, "Usage: /deltask <task_id>  (see /tasks for ids)")
            return
        delete_task(chat_id, int(parts[1]))
        send_message(chat_id, "🗑 Deleted.")

    elif cmd == "/setpassword":
        if len(parts) < 3 or not check_password(parts[1]):
            send_message(chat_id, "Usage: /setpassword <old_password> <new_password>")
            return
        set_password(parts[2])
        send_message(chat_id, "✅ Password changed.")

    elif cmd == "/setprovider":
        if len(parts) < 3 or not check_password(parts[1]):
            send_message(chat_id, "Usage: /setprovider <password> <opencode|gemini|groq>")
            return
        provider = parts[2].lower()
        if provider not in PROVIDERS:
            send_message(chat_id, "Provider must be one of: " + ", ".join(PROVIDERS))
            return
        set_setting("ai_provider", provider)
        send_message(chat_id, f"✅ AI provider switched to {provider}.")

    elif cmd == "/setmodel":
        if len(parts) < 3 or not check_password(parts[1]):
            send_message(chat_id, "Usage: /setmodel <password> <model_id>  (applies to the current provider)")
            return
        provider = get_setting("ai_provider", DEFAULT_AI_PROVIDER)
        set_setting(f"ai_{provider}_model", parts[2])
        send_message(chat_id, f"✅ {provider} model set to {parts[2]}.")

    elif cmd == "/setkey":
        if len(parts) < 3 or not check_password(parts[1]):
            send_message(chat_id, "Usage: /setkey <password> <api_key>  (applies to the current provider)")
            return
        provider = get_setting("ai_provider", DEFAULT_AI_PROVIDER)
        set_setting(f"ai_{provider}_key", parts[2])
        send_message(chat_id, f"✅ {provider} API key updated.")

    elif cmd == "/settings":
        if len(parts) < 2 or not check_password(parts[1]):
            send_message(chat_id, "Usage: /settings <password>")
            return
        send_message(chat_id, admin_settings_text(), parse_mode="HTML")

    else:
        send_message(chat_id, "Unknown command. Send /menu to see what's available.")


MENU_TEXT = """Available commands (or just use /menu for buttons):
/status - your profile summary and streaks
/pause /resume - pause or resume daily check-ins
/restart <password> - wipe profile and redo onboarding
/addtask YYYY-MM-DD description - add a reminder
/tasks - list open reminders (with buttons)
/done <id> - mark a reminder done
/deltask <id> - delete a reminder

Admin (password required, or unlock once via ⚙️ Admin for 5 min):
/setpassword <old> <new>
/setprovider <password> opencode|gemini|groq
/setmodel <password> <model_id>
/setkey <password> <api_key>
/settings <password> - view current AI provider/model
"""


# ============================== CHAT FLOWS ==================================


def handle_onboarding_turn(chat_id, text):
    append_conversation(chat_id, "user", text)
    history = get_conversation(chat_id)
    messages = [{"role": "system", "content": ONBOARDING_SYSTEM_PROMPT}] + history

    try:
        reply = call_llm(messages, max_tokens=1500)
    except RuntimeError as e:
        send_message(chat_id, f"⚠️ Couldn't reach any AI provider ({e}). Try again shortly.")
        return

    profile, visible_reply = extract_json_block(reply)
    append_conversation(chat_id, "assistant", reply)

    if profile is not None:
        set_profile(chat_id, profile)
        set_mode(chat_id, "active")
        send_message(chat_id, (visible_reply or "Profile saved.") +
                     "\n\n✅ Setup's done - you'll get a daily check-in automatically. "
                     "Message me anytime, or send /menu to see controls.")
    else:
        send_message(chat_id, visible_reply or reply)


def handle_active_chat(chat_id, text):
    profile = get_profile(chat_id)
    log = get_daily_log(chat_id)
    context_note = ("The person is messaging mid-day, outside their scheduled check-in. "
                     "Reply briefly and helpfully given their profile and recent log. "
                     "Stay encouraging about their longer-term goals. Do not output a "
                     "JSON block unless they explicitly ask to update their profile.")
    messages = [
        {"role": "system", "content": context_note},
        {"role": "user", "content": f"Profile: {profile}\nRecent log: {log}\n\nMessage: {text}"},
    ]
    try:
        reply = call_llm(messages, max_tokens=500)
    except RuntimeError as e:
        send_message(chat_id, f"⚠️ Couldn't reach any AI provider ({e}).")
        return
    send_message(chat_id, reply)


def run_daily_checkin(chat_id):
    profile = get_profile(chat_id)
    if profile is None:
        return
    log = get_daily_log(chat_id)
    due_tasks = tasks_due_within(chat_id, days_ahead=3)

    user_message = build_daily_user_message(profile, log, due_tasks)
    messages = [
        {"role": "system", "content": DAILY_SYSTEM_PROMPT},
        {"role": "user", "content": user_message},
    ]
    try:
        reply = call_llm(messages, max_tokens=1000)
    except RuntimeError as e:
        print(f"[cron] LLM call failed on all providers: {e}")
        return

    new_entry, visible_message = extract_json_block(reply)
    send_message(chat_id, "☀️ " + (visible_message or reply))

    if new_entry:
        new_entry.setdefault("date", datetime.date.today().isoformat())
        log.setdefault("entries", [])
        log["entries"].append(new_entry)
        log["entries"] = log["entries"][-120:]
        goal_name = (new_entry.get("goal_progress") or {}).get("goal")
        streak_days = (new_entry.get("goal_progress") or {}).get("streak_days")
        if goal_name and streak_days is not None:
            log.setdefault("current_streaks", {})[goal_name] = streak_days
        set_daily_log(chat_id, log)


if __name__ == "__main__":
    app.run(debug=bool(os.environ.get("FLASK_DEBUG")), port=int(os.environ.get("PORT", 5000)))
