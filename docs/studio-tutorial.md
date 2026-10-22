# Getting Kimchi-bot running with Kimchi Studio

This tutorial shows you how to deploy this Telegram bot using
[Kimchi Studio](https://kimchi.dev/studio) — the visual workspace for Kimchi
agents. Instead of typing terminal commands yourself, you point a Studio
agent at the repo and let it do the work. (That's honestly how this project
was built, tested, and published in the first place.)

**Time needed:** ~15 minutes, most of it waiting for the agent.

---

## Prerequisites

- **Kimchi Studio** installed ([download](https://kimchi.dev/studio)) and
  signed in — on Windows, macOS, or Linux. The bot itself was developed and
  tested on Windows 11; the agent will verify your platform as it goes.
- **A Telegram account** (on your phone).
- Nothing else — the agent checks for Python, git, and the kimchi CLI, and
  tells you what's missing.

## Step 1 — Get your bot token and user ID (the only manual steps)

The agent can't message Telegram for you, so do these two things on your phone
first:

1. Message [@BotFather](https://t.me/BotFather) → send `/newbot` → follow the
   prompts → **copy the token** it gives you (looks like `1234567890:AAAA...`).
2. Message [@userinfobot](https://t.me/userinfobot) → it replies with your
   **numeric user ID**. Copy it.

Keep both handy — you'll paste them when the agent asks.

## Step 2 — Open Studio and hand the job to an agent

Open Kimchi Studio, start a **new chat**, and paste this prompt:

```text
Set up the Kimchi-bot Telegram bridge on this machine:

1. Clone https://github.com/johndemian/Kimchi-bot and work inside that folder.
2. Ask me for the TELEGRAM_BOT_TOKEN and my Telegram user ID, then create a
   .env file (based on .env.example) with those values.
3. Create a Python virtual environment (.venv) and install requirements.txt.
4. Run: py -3 test_bridge.py — it runs two real agent turns and verifies
   file writing, sessions, and context. Fix anything that fails.
5. Start the bot in the background and confirm from bot.log that it says
   "Application started".
6. Report the bot's @username and tell me it's ready to test.
```

The agent will run commands in its terminal panel — cloning, venv creation,
dependency installs, the test suite. Studio shows you everything it does and
**asks for your approval** when a step needs it (you'll get a "Needs you"
notification if you switch away — approvals, diffs, and terminal output are
all visible in the session). Let it work through the checklist.

When it asks for your token and user ID (Step 2 of its checklist), paste the
values from Step 1 above.

## Step 3 — Test it

Open the bot's @username in Telegram and send it a task:

> Create a file called ideas.md with 5 blog post ideas about home espresso

Expect a reply in ~10–60 seconds (a real agent turn is running on your
machine). Then try `/files` to list what it created, and `/status` to see the
session details.

## Step 4 — Make it survive reboots

The bot only works while it's running. Tell the agent:

```text
Set up auto-start for the bot: register the KimchiTelegramBot scheduled task
exactly as documented in the README's "Auto-start on Windows" section, start
it, and confirm it's running.
```

(On Windows this creates a logon-triggered Scheduled Task that runs the bot
hidden, with no time limit. macOS/Linux users: ask the agent to set up an
equivalent — a launchd agent or systemd user unit.)

## Day-2 operations (all from Studio)

Just ask the agent in the project's session — it has the context:

| You want | Say |
|---|---|
| Add a teammate to the bot | "Add user ID 123456789 to ALLOWED_TELEGRAM_IDS in .env and restart the bot" |
| Bot stopped replying | "The bot isn't responding — check bot.log, diagnose, and fix it" |
| Update to the latest version | "Pull the latest changes from the repo, run test_bridge.py, and restart the bot" |
| Change the model or timeout | "Set KIMCHI_MODEL to kimi-k3 in .env and restart" |
| Stop the bot | "Stop the KimchiTelegramBot scheduled task" |

## Good to know

- **Teams:** this project uses **individual bots** — each teammate runs their
  own instance (own token, own allowlist entry, own machine and kimchi
  account). Nothing is shared. See the README's "Using it with a team" section.
- **Security:** the allowlist is the *only* access boundary — the agent behind
  the bot has real write access on the host machine. Only allowlist people
  you trust with that. (Details and tested caveats in the README.)
- **The host machine must be on** for the bot to answer. If you need it to
  run even when your laptop is closed, ask the agent about Kimchi's cloud
  execution options.
