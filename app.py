"""
Life Planner Telegram Bot — single-file Flask app (Render + Supabase edition).
================================================================================

WHAT CHANGED FROM THE PYTHONANYWHERE VERSION
- All secrets/config now come from environment variables (Render dashboard),
  not hardcoded constants. Nothing sensitive lives in this file.
- Storage moved from a local SQLite file to Supabase Postgres, because
  Render's free web services have an EPHEMERAL FILESYSTEM: any local file
  (like lifeplanner.db) is wiped on every restart/redeploy, and Render can
  restart a free instance at any time — not just after inactivity. A local
  SQLite file is not safe to rely on there. Postgres on Supabase persists
  independently of the web service.
- Runs under gunicorn (see Procfile / render.yaml), not `flask run`.

WHAT'S THE SAME
- Two routes, period:
    POST /webhook/<WEBHOOK_SECRET>   <- Telegram sends messages here
    GET  /cron/<CRON_SECRET>         <- your daily pinger (cron-job.org, etc.)
  Everything else 404s. No web login, no HTML form, no dashboard.
- Only ONE Telegram chat id may ever use this bot (first /claim wins).
- AI provider (OpenCode Zen / Gemini) is swappable at runtime via Telegram,
  password-gated, stored in the settings table.

FIRST-TIME SETUP — see README.md for the full Render + Supabase walkthrough.
"""

import datetime
import json
import os
import re
from contextlib import contextmanager

import psycopg2
import psycopg2.extras
import psycopg2.pool
import requests
from flask import Flask, request, jsonify
from werkzeug.security import generate_password_hash, check_password_hash

# ============================== CONFIG (ENV VARS) ==========================
# Set these in the Render dashboard -> your service -> Environment.
# Nothing in this file needs editing; everything sensitive lives outside it.


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
DEFAULT_AI_PROVIDER = os.environ.get("DEFAULT_AI_PROVIDER", "opencode")  # "opencode" or "gemini"
DEFAULT_OPENCODE_MODEL = os.environ.get("DEFAULT_OPENCODE_MODEL", "big-pickle")
DEFAULT_GEMINI_MODEL = os.environ.get("DEFAULT_GEMINI_MODEL", "gemini-2.5-flash")
DEFAULT_OPENCODE_API_KEY = os.environ.get("DEFAULT_OPENCODE_API_KEY", "")
DEFAULT_GEMINI_API_KEY = os.environ.get("DEFAULT_GEMINI_API_KEY", "")

OPENCODE_API_BASE = "https://opencode.ai/zen/v1"
GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta"

TELEGRAM_API_BASE = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

# ============================== DATABASE (POSTGRES) =========================
# Small connection pool instead of psycopg2.connect() per call, since every
# connection to Supabase goes over the internet (unlike the old same-machine
# SQLite file) and repeated TCP+TLS handshakes add real latency.

db_pool = psycopg2.pool.SimpleConnectionPool(1, 5, dsn=DATABASE_URL, sslmode="require")


@contextmanager
def db_cursor(commit=False):
    conn = db_pool.getconn()
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
    # bootstrap the real, changeable password to match the setup password
    set_setting("admin_password_hash", generate_password_hash(SETUP_PASSWORD))
    set_setting("ai_provider", DEFAULT_AI_PROVIDER)
    set_setting("ai_opencode_model", DEFAULT_OPENCODE_MODEL)
    set_setting("ai_opencode_key", DEFAULT_OPENCODE_API_KEY)
    set_setting("ai_gemini_model", DEFAULT_GEMINI_MODEL)
    set_setting("ai_gemini_key", DEFAULT_GEMINI_API_KEY)
    set_setting("paused", "0")
    return True, "Claimed. Please run /setpassword to change your admin password now."


def check_password(password):
    stored_hash = get_setting("admin_password_hash")
    if not stored_hash:
        return False
    return check_password_hash(stored_hash, password)


def set_password(new_password):
    set_setting("admin_password_hash", generate_password_hash(new_password))


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


# --- scheduled tasks (the user's own reminders, separate from the AI's daily nudge) ---

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


def call_opencode(messages, max_tokens):
    api_key = get_setting("ai_opencode_key", DEFAULT_OPENCODE_API_KEY)
    model = get_setting("ai_opencode_model", DEFAULT_OPENCODE_MODEL)
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
            # Gemini 2.5 models spend part of maxOutputTokens on internal
            # "thinking" tokens before writing the visible reply - for this
            # bot's short structured replies that just eats the budget and
            # truncates the actual message. Turn it off.
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
        # Still ran out of room - better to say so plainly than silently
        # hand back a sentence that stops mid-word.
        text = text.rstrip() + "\n\n[cut off - reply exceeded the token limit, try increasing max_tokens]"
    return text


def call_llm(messages, max_tokens=1000):
    provider = get_setting("ai_provider", DEFAULT_AI_PROVIDER)
    try:
        if provider == "gemini":
            return call_gemini(messages, max_tokens)
        return call_opencode(messages, max_tokens)
    except requests.exceptions.RequestException as e:
        raise RuntimeError(f"Network error calling {provider}: {e}")


# ============================== TELEGRAM ===================================


def send_message(chat_id, text):
    url = f"{TELEGRAM_API_BASE}/sendMessage"
    chunks = [text[i:i + 3500] for i in range(0, len(text), 3500)] or [""]
    for chunk in chunks:
        try:
            requests.post(url, json={"chat_id": chat_id, "text": chunk}, timeout=20)
        except requests.exceptions.RequestException as e:
            print(f"[telegram] send failed: {e}")


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
    # Lets UptimeRobot (or any uptime pinger) hit "/" instead of a secret URL,
    # so your webhook/cron paths never show up in a public monitor's logs.
    return jsonify({"ok": True, "service": "lifeplanner-bot"})


@app.route(f"/webhook/{WEBHOOK_SECRET}", methods=["POST"])
def webhook():
    update = request.get_json(silent=True) or {}
    message = update.get("message") or update.get("edited_message")
    if not message:
        return jsonify({"ok": True})

    chat_id = message["chat"]["id"]
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
        # silently ignore everything else pre-claim - no hints given out
        return jsonify({"ok": True})

    # --- Bot is claimed: reject anyone who isn't the owner, no exceptions ---
    if not is_owner(chat_id):
        send_message(chat_id, "This bot is private.")
        return jsonify({"ok": True})

    # --- From here on, chat_id is guaranteed to be the owner ---
    if text.startswith("/"):
        handle_command(chat_id, text)
    else:
        mode = get_mode(chat_id)
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


# No other routes exist. Flask returns 404 for anything else automatically -
# there is no HTML page, no login form, no dashboard to find.


# ============================== COMMANDS ===================================


def handle_command(chat_id, text):
    parts = text.split()
    cmd = parts[0].lower()

    if cmd in ("/menu", "/help"):
        send_message(chat_id, MENU_TEXT)

    elif cmd == "/status":
        profile = get_profile(chat_id)
        log = get_daily_log(chat_id)
        if not profile:
            send_message(chat_id, "Setup isn't finished yet - just keep chatting with me to complete onboarding.")
            return
        streaks = log.get("current_streaks", {})
        streak_text = ", ".join(f"{g}: {d}d" for g, d in streaks.items()) or "none yet"
        paused_text = "yes" if is_paused() else "no"
        send_message(chat_id, f"Profile: {profile['identity'].get('name', 'set')}\n"
                               f"Streaks: {streak_text}\nPaused: {paused_text}\n"
                               f"Open tasks: {len(list_tasks(chat_id))}")

    elif cmd == "/pause":
        set_setting("paused", "1")
        send_message(chat_id, "Paused. Daily check-ins won't send until you /resume.")

    elif cmd == "/resume":
        set_setting("paused", "0")
        send_message(chat_id, "Resumed - you'll get your next daily check-in as scheduled.")

    elif cmd == "/restart":
        # destructive, so gate it with the password like other sensitive actions
        if len(parts) < 2 or not check_password(parts[1]):
            send_message(chat_id, "Usage: /restart <password>  (wipes your profile and redoes onboarding)")
            return
        clear_conversation(chat_id)
        set_profile(chat_id, None)
        set_mode(chat_id, "onboarding")
        send_message(chat_id, "Profile cleared. Let's redo setup - tell me a bit about yourself.")

    elif cmd == "/addtask":
        # /addtask 2026-08-15 Submit CA exam application
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
        send_message(chat_id, f"Added: {description} (due {due_date})")

    elif cmd == "/tasks":
        tasks = list_tasks(chat_id)
        if not tasks:
            send_message(chat_id, "No open scheduled tasks. Add one with /addtask YYYY-MM-DD description")
            return
        lines = [f"#{t['id']} · {t['due_date']} · {t['description']}" for t in tasks]
        send_message(chat_id, "\n".join(lines))

    elif cmd == "/done":
        if len(parts) < 2 or not parts[1].isdigit():
            send_message(chat_id, "Usage: /done <task_id>  (see /tasks for ids)")
            return
        mark_task_done(chat_id, int(parts[1]))
        send_message(chat_id, "Marked done.")

    elif cmd == "/deltask":
        if len(parts) < 2 or not parts[1].isdigit():
            send_message(chat_id, "Usage: /deltask <task_id>  (see /tasks for ids)")
            return
        delete_task(chat_id, int(parts[1]))
        send_message(chat_id, "Deleted.")

    elif cmd == "/setpassword":
        if len(parts) < 3 or not check_password(parts[1]):
            send_message(chat_id, "Usage: /setpassword <old_password> <new_password>")
            return
        set_password(parts[2])
        send_message(chat_id, "Password changed.")

    elif cmd == "/setprovider":
        if len(parts) < 3 or not check_password(parts[1]):
            send_message(chat_id, "Usage: /setprovider <password> <opencode|gemini>")
            return
        provider = parts[2].lower()
        if provider not in ("opencode", "gemini"):
            send_message(chat_id, "Provider must be 'opencode' or 'gemini'.")
            return
        set_setting("ai_provider", provider)
        send_message(chat_id, f"AI provider switched to {provider}.")

    elif cmd == "/setmodel":
        if len(parts) < 3 or not check_password(parts[1]):
            send_message(chat_id, "Usage: /setmodel <password> <model_id>  (applies to the current provider)")
            return
        provider = get_setting("ai_provider", DEFAULT_AI_PROVIDER)
        key = "ai_opencode_model" if provider == "opencode" else "ai_gemini_model"
        set_setting(key, parts[2])
        send_message(chat_id, f"{provider} model set to {parts[2]}.")

    elif cmd == "/setkey":
        if len(parts) < 3 or not check_password(parts[1]):
            send_message(chat_id, "Usage: /setkey <password> <api_key>  (applies to the current provider)")
            return
        provider = get_setting("ai_provider", DEFAULT_AI_PROVIDER)
        key = "ai_opencode_key" if provider == "opencode" else "ai_gemini_key"
        set_setting(key, parts[2])
        send_message(chat_id, f"{provider} API key updated.")

    elif cmd == "/settings":
        if len(parts) < 2 or not check_password(parts[1]):
            send_message(chat_id, "Usage: /settings <password>")
            return
        provider = get_setting("ai_provider", DEFAULT_AI_PROVIDER)
        model = get_setting("ai_opencode_model" if provider == "opencode" else "ai_gemini_model")
        key = get_setting("ai_opencode_key" if provider == "opencode" else "ai_gemini_key", "")
        masked = (key[:4] + "..." + key[-4:]) if len(key) > 10 else "(not set)"
        send_message(chat_id, f"Provider: {provider}\nModel: {model}\nKey: {masked}")

    else:
        send_message(chat_id, "Unknown command. Send /menu to see what's available.")


MENU_TEXT = """Available commands:
/status - your profile summary and streaks
/pause /resume - pause or resume daily check-ins
/restart <password> - wipe profile and redo onboarding
/addtask YYYY-MM-DD description - add a reminder
/tasks - list open reminders
/done <id> - mark a reminder done
/deltask <id> - delete a reminder

Admin (password required):
/setpassword <old> <new>
/setprovider <password> opencode|gemini
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
        send_message(chat_id, f"Couldn't reach the AI service ({e}). Try again shortly.")
        return

    profile, visible_reply = extract_json_block(reply)
    append_conversation(chat_id, "assistant", reply)

    if profile is not None:
        set_profile(chat_id, profile)
        set_mode(chat_id, "active")
        send_message(chat_id, (visible_reply or "Profile saved.") +
                     "\n\nSetup's done - you'll get a daily check-in automatically. "
                     "Message me anytime, or send /menu to see commands.")
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
        send_message(chat_id, f"Couldn't reach the AI service ({e}).")
        return
    send_message(chat_id, reply)


def run_daily_checkin(chat_id):
    profile = get_profile(chat_id)
    if profile is None:
        return  # onboarding not finished yet - nothing to check in about
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
        print(f"[cron] LLM call failed: {e}")
        return

    new_entry, visible_message = extract_json_block(reply)
    send_message(chat_id, visible_message or reply)

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
    # Local testing only - Render runs this via gunicorn instead (see Procfile).
    app.run(debug=bool(os.environ.get("FLASK_DEBUG")), port=int(os.environ.get("PORT", 5000)))
