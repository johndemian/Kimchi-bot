# Kimchi Telegram Bot

Chat with your local [kimchi](https://kimchi.dev) coding agent from Telegram.
Every text message you send is run as a full agent turn — **including file
write access**, so you can ask it to create docs, notes, code, anything — and
the reply comes back in the chat. Conversation context persists per chat, and
files the agent creates land in a per-chat workspace on your machine.

```
Telegram app ⇄ this bot (bot.py) ⇄ kimchi CLI (non-interactive, -p) ⇄ files in workspace/
```

## Features

- 💬 **Full agent turns from Telegram** — prompts run on your local kimchi CLI
- 📝 **Write access** — the agent can create and edit files (docs, code, …)
- 🧠 **Persistent context** — each chat has its own kimchi session, resumed
  automatically (and survives bot restarts via `bridge_state.json`)
- 📂 **Per-chat workspaces** — files land in `workspace/<chat id>/`, listed via `/files`
- 🔒 **User allowlist** — only the Telegram user IDs you configure can use it
- 🛑 **Cancel & timeout** — `/cancel` mid-run; runs are killed after a configurable timeout
- 🧾 **Spend attribution** — requests are tagged `source:telegram-bot` (KIMCHI_TAGS)
- 🧰 **No MCP exposure** — runs against an empty MCP config (`mcp-empty.json`),
  so the bot never touches your personal MCP servers (Slack, mail, calendar, …)

## Requirements

- **Windows** (developed and tested on Windows 11; the kimchi.js path
  auto-detection targets npm-on-Windows — set `KIMCHI_JS` for other layouts)
- **Python 3.10+** (verified on 3.14) with the `py` launcher
- **kimchi CLI** installed and logged in (`kimchi setup`) — verified with 1.16.1
- A **Telegram bot token** (2 minutes, see below)

## Quick start

1. **Create the bot on Telegram**
   Message [@BotFather](https://t.me/BotFather) → `/newbot` → follow the prompts →
   copy the token it gives you (looks like `1234567890:AAAA...`).

2. **Get your Telegram user ID**
   Message [@userinfobot](https://t.me/userinfobot) — it replies with your numeric ID.

3. **Configure**
   ```powershell
   git clone https://github.com/johndemian/Kimchi-bot.git
   cd Kimchi-bot
   Copy-Item .env.example .env
   # edit .env: TELEGRAM_BOT_TOKEN and ALLOWED_TELEGRAM_IDS
   ```

4. **Run**
   Double-click `run.bat` (creates a venv and installs dependencies on first run),
   or manually:
   ```powershell
   py -m venv .venv
   .venv\Scripts\python -m pip install -r requirements.txt
   .venv\Scripts\python bot.py
   ```

5. Open your bot in Telegram and send it a task, e.g.
   *"Create a file called ideas.md with a brainstorm of 10 blog post ideas about home espresso."*

### Auto-start on Windows

A Scheduled Task named `KimchiTelegramBot` can start the bot automatically when
you log in — hidden (no console window, via `pythonw.exe`), with no time
limit, battery-safe, and up to 3 automatic restarts on failure. Logs rotate
in `bot.log`. One-time setup (PowerShell):

```powershell
$dir = "<path to your clone>"
$action   = New-ScheduledTaskAction -Execute "$dir\.venv\Scripts\pythonw.exe" -Argument "bot.py" -WorkingDirectory $dir
$trigger  = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName "KimchiTelegramBot" -Action $action -Trigger $trigger -Settings $settings
```

Manage it: `Start-ScheduledTask KimchiTelegramBot` · `Stop-ScheduledTask KimchiTelegramBot`
· `Unregister-ScheduledTask KimchiTelegramBot` (remove). Don't run `run.bat`
while the task instance is already running — Telegram allows only one poller
per bot token.

## Using it with a team

Two patterns, pick one:

**A) Shared bot (one host machine)**
One person hosts the bot; teammates' Telegram IDs go into
`ALLOWED_TELEGRAM_IDS` (comma-separated). Everyone messages the same bot;
each chat gets its own isolated workspace and session. Note: all agent turns
run on the host's machine under the host's kimchi account (spend shows up
under the `source:telegram-bot` tag).

**B) Everyone runs their own**
Each teammate creates their own bot via @BotFather and runs their own
instance (their machine, their kimchi account). Fully isolated.

## Commands

| Command   | What it does                                          |
|-----------|-------------------------------------------------------|
| `/new`    | Start a fresh session (clears conversation context)  |
| `/status` | Show model, session, workspace, busy state            |
| `/files`  | List files the agent created in this chat            |
| `/cancel` | Stop the current run                                  |
| `/help`   | Usage overview                                        |

## Configuration (env vars / .env)

| Variable              | Default                                   | Notes                                    |
|-----------------------|-------------------------------------------|------------------------------------------|
| `TELEGRAM_BOT_TOKEN`  | — (required)                              | From @BotFather                          |
| `ALLOWED_TELEGRAM_IDS`| — (required)                              | Comma-separated user IDs                 |
| `KIMCHI_MODEL`        | `glm-5.3`                                 | Any model from `kimchi --list-models`    |
| `KIMCHI_ALLOW_TOOLS` | `read,write,edit,ls,find,grep`            | Add `bash` for shell access (riskier)    |
| `KIMCHI_TIMEOUT`     | `600` seconds                             | Runs are killed after this               |
| `KIMCHI_JS`           | npm global path (auto-detected)           | Full path to kimchi.js if non-standard    |

## Security — read this

- **The allowlist is the only access boundary.** This was tested empirically on
  kimchi 1.16.1: a session `--allow-tool write` rule **overrides deny rules**
  (even an explicit `--deny-tool write(.env)` did not block a `.env` write).
  Anyone who can message the bot can run an agent with real write access on
  the hosting machine. Only allowlist people you trust with that.
- The bot refuses to start without `ALLOWED_TELEGRAM_IDS` set.
- Unauthorized users get a rejection reply; every attempt is logged with the
  user's ID/name.
- Treat the bot token like a password: anyone holding it can impersonate the
  bot. Revoke/regenerate via @BotFather if it leaks. Never commit `.env`.
- Each message costs a real agent turn on the hosting kimchi account (same as
  running `kimchi -p` yourself).

## Development

- `kimchi_bridge.py` — spawns `kimchi -p` per message; per-chat workspaces and
  sessions; permission allow rules; timeout and cancel handling.
- `bot.py` — Telegram layer (python-telegram-bot 22.x): allowlist gate, handlers,
  chunked replies, rotating file log.
- `test_bridge.py` — end-to-end test that runs two real kimchi turns (file
  creation, session adoption, cross-run context recall, reset):
  ```powershell
  py -3 test_bridge.py
  ```

## Limitations / roadmap

- Replies arrive when the turn completes (no streaming).
- One task at a time per chat.
- Long outputs > ~20k chars are sent as a file (`kimchi-reply.md`).
- Upgrade path: switch the bridge from `-p` to `kimchi --mode acp`
  (JSON-RPC over stdio) to get live progress and **permission buttons**
  (Allow/Deny) in Telegram, like the IDE experience.

## License

[MIT](LICENSE)
