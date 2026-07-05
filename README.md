# Life Planner Telegram Bot — Render + Supabase Version (v2)

Everything lives in **`app.py`**. One Flask process (run by gunicorn), two
real routes, storage in a free Supabase Postgres database. No separate cron
script — your daily pinger calls the same app over HTTP.

## What's new in this version

**1. Three free AI providers, chained as automatic fallback.**
The bot now supports **OpenCode Zen**, **Gemini**, and **Groq** — all free.
Whichever one you set as `DEFAULT_AI_PROVIDER` is tried first; if that call
fails (bad key, rate limit, provider outage, network block), the bot
automatically tries the other two configured providers before giving up.
You don't have to do anything for this to kick in — just fill in as many
of the three API keys as you want. More keys filled in = more reliability,
still zero cost.

- OpenCode Zen key: `https://opencode.ai/auth` (model `big-pickle`, free)
- Gemini key: `https://aistudio.google.com/apikey` (free tier)
- Groq key: `https://console.groq.com/keys` (free tier, fast Llama models)

**2. Real button-driven UI.** `/menu` (or `/start`) now opens an inline
keyboard "control panel" — Status, Tasks (with per-task ✅ Done / 🗑 Delete
buttons), a Pause/Resume toggle, and an Admin panel. Menus edit themselves
in place instead of spamming new messages every tap.

**3. Admin session instead of retyping your password constantly.** Tap
⚙️ Admin and enter your password once — you're unlocked for 5 minutes.
Every admin action in that window (change provider/model/key/password,
restart) needs no further password entry. All the old
`/setpassword <old> <new>`-style commands still work exactly as before too.

**4. Auto-deleting secrets.** Any message that ever contains a plaintext
password or API key — whether typed as `/setkey <password> <key>` or typed
into an interactive prompt after tapping a button — is deleted from the
Telegram chat the instant the bot reads it. Telegram bots are allowed to
delete incoming messages in private chats, so your passwords and keys
never sit around in your chat history.

**5. Small UX polish.** A typing indicator shows while the AI is thinking;
confirmations use a bit of emoji/bold formatting; destructive actions
(profile restart) now get a Yes/No confirm button instead of firing on the
first tap.

Nothing about the underlying storage, security model, or route surface
changed — see below, it's the same as before.

## Why Postgres instead of a local SQLite file?

Render's **free** web services have an ephemeral filesystem: any local file
is wiped on every redeploy *and* Render can restart a free instance at any
time (not only after inactivity). Supabase's free Postgres persists
independently of the web service, so everything (settings, profile, daily
log, conversation scratch space, tasks) is stored there instead.

## Routes (this is the entire attack surface)
- `GET  /` — plain health check, returns `{"ok": true}`. Point your uptime
  pinger here — it reveals nothing sensitive.
- `POST /webhook/<WEBHOOK_SECRET>` — Telegram delivers messages and button
  taps here.
- `GET  /cron/<CRON_SECRET>` — your daily pinger hits this once a day.
- Everything else 404s automatically. There is no web login, no HTML page,
  no dashboard. All control happens through Telegram.

## Security model
- Only one Telegram chat id can ever use this bot. The **first** person to
  send `/claim <password>` (matching `SETUP_PASSWORD`) permanently becomes
  the owner — recorded in the database.
- Every message after that is checked against the owner's chat id before
  anything else happens. Anyone else gets a flat "This bot is private." and
  **no AI call is ever made on their behalf**.
- Destructive/sensitive actions (restart, change password/provider/model/
  key, view settings) require the current password — checked as a proper
  salted hash (`werkzeug.security`), not a plaintext comparison — or an
  active 5-minute admin session unlocked by that same password.
- Any plaintext password or API key sent in the chat (via slash command or
  an interactive prompt) is deleted from Telegram immediately after use.
- All secrets (bot token, webhook/cron secrets, setup password, DB
  connection string, API keys) live in environment variables on Render, not
  in the code. Nothing sensitive is committed to your repo.

## 1. Get your credentials
- **Telegram bot token**: `@BotFather` → `/newbot` → copy the token.
- **OpenCode Zen API key**: `https://opencode.ai/auth` (model id `big-pickle`, free).
- **Gemini API key**: `https://aistudio.google.com/apikey` (free tier).
- **Groq API key**: `https://console.groq.com/keys` (free tier).
  You don't need all three — one is enough to run — but each additional one
  you fill in becomes an automatic free fallback if another goes down.
- Generate random secrets with:
  ```bash
  python3 -c "import secrets; print(secrets.token_urlsafe(32))"
  ```
  Run it twice — once for `WEBHOOK_SECRET`, once for `CRON_SECRET`.

## 2. Create a free Supabase Postgres database
1. Go to `https://supabase.com`, sign up, **New project**.
2. Pick a database password and save it — you'll need it in the connection
   string.
3. Once the project is ready: **Project Settings → Database → Connection
   string**. Choose the **Transaction pooler** string (port `6543`), not the
   direct `5432` connection.
4. It looks like:
   `postgresql://postgres.xxxxxxxx:[YOUR-PASSWORD]@aws-0-<region>.pooler.supabase.com:6543/postgres`
   Fill in your actual password — this is your `DATABASE_URL`.
5. Tables are created automatically by `app.py` on first run.
6. Supabase free projects pause after **one week with no API activity**.
   Your daily cron ping keeps it well within that.

## 3. Push this project to GitHub
Create a new repo and push `app.py`, `requirements.txt`, `Procfile`,
`render.yaml`, and `.gitignore` (do **not** commit a real `.env` — only
`.env.example` if you keep it).

## 4. Deploy to Render (free tier)
**Option A — Blueprint (recommended):**
1. On Render, **New → Blueprint**, connect your GitHub repo. Render reads
   `render.yaml` and creates the web service for you, already set to the
   free plan with the right build/start commands.
2. Fill in the env vars marked `sync: false`:
   - `TELEGRAM_BOT_TOKEN`, `WEBHOOK_SECRET`, `CRON_SECRET`, `SETUP_PASSWORD`
   - `DATABASE_URL` (from step 2)
   - `DEFAULT_OPENCODE_API_KEY`, `DEFAULT_GEMINI_API_KEY`,
     `DEFAULT_GROQ_API_KEY` (fill in whichever you have — leave others blank)
3. Click **Apply**.

**Option B — Manual web service:**
1. **New → Web Service**, connect your repo, runtime **Python 3**.
2. Build command: `pip install -r requirements.txt`
3. Start command: `gunicorn app:app --workers 1 --threads 4 --timeout 120`
4. Instance type: **Free**.
5. Add the same environment variables listed above under **Environment**.

Your app's public URL will be something like
`https://lifeplanner-bot.onrender.com`.

## 5. Point Telegram at the webhook (one-time)
```bash
curl -F "url=https://lifeplanner-bot.onrender.com/webhook/<WEBHOOK_SECRET>" \
  https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/setWebhook
```

## 6. Set up UptimeRobot (keeps it out of the 15-minute sleep + gives you daily cron)
1. **Keep-alive monitor**: New Monitor → HTTP(s) →
   `https://lifeplanner-bot.onrender.com/` → interval **5 minutes**.
2. **Daily check-in monitor**: New Monitor → HTTP(s) →
   `https://lifeplanner-bot.onrender.com/cron/<CRON_SECRET>` → interval
   **1 day**. For an exact clock time, use `https://cron-job.org` (also
   free) instead for this one monitor, and keep UptimeRobot for keep-alive.

Sri Lanka is UTC+5:30, so a 7:00 AM Colombo check-in = **01:30 UTC**.

**Cold-start risk on the free tier:** if Render restarts the instance right
as a message or cron ping arrives, that one request can be slow or
occasionally dropped/retried. It's not silent data loss — Postgres already
has your data — worst case is a delayed or missed single reply/check-in.

## 7. First run
Message your bot on Telegram: `/claim <SETUP_PASSWORD>`. This message
auto-deletes itself right after. Then tap **⚙️ Admin** from `/menu` (or run
`/setpassword <SETUP_PASSWORD> <your new private password>`) to set a real
password. Then just chat — the bot walks you through onboarding.

## Bot commands
```
/menu or /start                        button control panel
/help                                   plain-text command list
/status                                 profile summary + streaks
/pause /resume                         pause or resume daily check-ins
/restart <password>                     wipe profile, redo onboarding
/addtask YYYY-MM-DD description         add a personal reminder
/tasks                                  list open reminders (with buttons)
/done <id>                              mark a reminder done
/deltask <id>                           delete a reminder

/setpassword <old> <new>                change the admin password
/setprovider <password> opencode|gemini|groq   switch AI provider live
/setmodel <password> <model_id>         change model for current provider
/setkey <password> <api_key>            change API key for current provider
/settings <password>                    view current provider/model (key masked)
```
All of the password-gated commands above can also be done through the
⚙️ Admin button without retyping your password each time (5-minute session).

## Notes
- All state lives in Supabase Postgres — nothing depends on the Render
  instance's local disk.
- Switching providers is live and immediate — no redeploy needed. If a call
  to every configured provider fails, you get a clear error message instead
  of a silent failure.
- Upgrading the same service to a paid Render plan later needs no code
  changes.
