"""Telegram bot that bridges chats to a local kimchi agent.

Every text message from an allowlisted user is run as a non-interactive
kimchi turn (with write access) in that chat's own workspace; replies come
back into the chat. Conversation context persists per chat via kimchi
sessions.

Required environment (or a .env file next to this script):
  TELEGRAM_BOT_TOKEN   token from @BotFather
  ALLOWED_TELEGRAM_IDS comma-separated Telegram user IDs permitted to use
                       this bot — this is the ONLY access boundary, since
                       the agent runs with real write access on this PC.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sys
import time
from logging.handlers import RotatingFileHandler
from dataclasses import dataclass
from pathlib import Path

from telegram import Update
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from envutil import load_dotenv
from kimchi_bridge import (
    DEFAULT_ALLOW_TOOLS,
    DEFAULT_MODEL,
    KimchiBridge,
)

log = logging.getLogger("kimchi_telegram")

BASE_DIR = Path(__file__).resolve().parent
MAX_MSG = 4000          # Telegram hard limit is 4096 — leave headroom
DOC_THRESHOLD = 20000  # above this, send the reply as a file

HELP_TEXT = """🤖 Kimchi Telegram bridge

Send me any message — it runs as a prompt on your local kimchi agent, with file write access in this chat's workspace.

Commands:
/new — start a fresh session (clears conversation context)
/status — show model, session and workspace details
/files — list files the agent created in this chat
/cancel — stop the current run
/help — this message

Notes:
• Conversation context persists across messages, per chat.
• The agent can create and edit files (docs, code, anything).
• Runs can take a while — one task at a time per chat.
• Everything runs on the PC hosting this bot, under your kimchi account."""


# ----------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------

class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Config:
    token: str
    allowed_ids: frozenset[int]
    model: str
    allow_tools: tuple[str, ...]
    timeout_s: float
    kimchi_js: str | None

    @classmethod
    def from_env(cls) -> "Config":
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        if not token:
            raise ConfigError(
                "TELEGRAM_BOT_TOKEN is not set. Create a bot with @BotFather "
                "on Telegram, then put its token in the .env file "
                "(see .env.example) or the environment."
            )

        raw_ids = os.environ.get("ALLOWED_TELEGRAM_IDS", "").strip()
        if not raw_ids:
            raise ConfigError(
                "ALLOWED_TELEGRAM_IDS is not set. This bot runs kimchi with "
                "real write access on this PC, so you MUST allowlist your own "
                "Telegram user ID(s). Find yours by messaging @userinfobot."
            )
        try:
            ids = frozenset(int(x.strip()) for x in raw_ids.split(",") if x.strip())
        except ValueError:
            raise ConfigError(
                f"ALLOWED_TELEGRAM_IDS must be comma-separated numbers, got: {raw_ids!r}"
            ) from None
        if not ids:
            raise ConfigError("ALLOWED_TELEGRAM_IDS resolved to an empty set.")

        tools_raw = os.environ.get("KIMCHI_ALLOW_TOOLS", ",".join(DEFAULT_ALLOW_TOOLS))
        allow_tools = tuple(t.strip() for t in tools_raw.split(",") if t.strip())

        return cls(
            token=token,
            allowed_ids=ids,
            model=os.environ.get("KIMCHI_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL,
            allow_tools=allow_tools or DEFAULT_ALLOW_TOOLS,
            timeout_s=float(os.environ.get("KIMCHI_TIMEOUT", "600")),
            kimchi_js=(os.environ.get("KIMCHI_JS", "").strip() or None),
        )


# Populated in main() before the application starts polling.
CFG: Config | None = None
BRIDGE: KimchiBridge | None = None


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------

async def _authorized(update: Update) -> bool:
    """Gate: only allowlisted Telegram users may drive the agent."""
    user = update.effective_user
    if user is not None and CFG is not None and user.id in CFG.allowed_ids:
        return True
    if user is not None:
        log.warning(
            "unauthorized access attempt: id=%s name=%r username=%r",
            user.id, user.first_name, user.username,
        )
    else:
        log.warning("unauthorized access attempt: no user object")
    if update.effective_message is not None:
        with contextlib.suppress(TelegramError):
            await update.effective_message.reply_text(
                "⛔ This bot is private. You are not authorized to use it."
            )
    return False


def _split(text: str, size: int) -> list[str]:
    return [text[i:i + size] for i in range(0, len(text), size)]


async def _edit_or_send(
    context: ContextTypes.DEFAULT_TYPE,
    placeholder,
    chat_id: int,
    text: str,
) -> None:
    """Replace the placeholder message, falling back to a fresh message."""
    if placeholder is not None:
        try:
            await placeholder.edit_text(text)
            return
        except (BadRequest, TelegramError):
            pass
    await context.bot.send_message(chat_id=chat_id, text=text)


async def _tick(placeholder, started: float, done: asyncio.Event) -> None:
    """Keep the placeholder alive with an elapsed-seconds counter."""
    while not done.is_set():
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(done.wait(), timeout=60.0)
        if done.is_set():
            return
        elapsed = int(time.monotonic() - started)
        try:
            await placeholder.edit_text(f"⏳ Kimchi is working… {elapsed}s")
        except (BadRequest, TelegramError):
            pass


async def _deliver(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    placeholder,
    text: str,
) -> None:
    if len(text) <= MAX_MSG:
        await _edit_or_send(context, placeholder, chat_id, text)
        return
    if len(text) > DOC_THRESHOLD:
        await _edit_or_send(
            context, placeholder, chat_id,
            f"✅ Done — full reply ({len(text):,} chars) attached as a file.",
        )
        await context.bot.send_document(
            chat_id=chat_id,
            document=text.encode("utf-8"),
            filename="kimchi-reply.md",
        )
        return
    chunks = _split(text, MAX_MSG)
    total = len(chunks)
    for i, chunk in enumerate(chunks):
        body = chunk if total == 1 else f"{chunk}\n\n[{i + 1}/{total}]"
        if i == 0:
            await _edit_or_send(context, placeholder, chat_id, body)
        else:
            await context.bot.send_message(chat_id=chat_id, text=body)


# ----------------------------------------------------------------------
# Handlers
# ----------------------------------------------------------------------

async def on_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _authorized(update):
        return
    await update.message.reply_text(
        "👋 Send me a task — I'll run it on your local kimchi agent.\n/help for details."
    )


async def on_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _authorized(update):
        return
    await update.message.reply_text(HELP_TEXT)


async def on_new(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _authorized(update):
        return
    BRIDGE.new_session(update.effective_chat.id)
    await update.message.reply_text(
        "🆕 Fresh session — your next message starts a new conversation.\n"
        "Files already created stay in the workspace (see /files)."
    )


async def on_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _authorized(update):
        return
    d = BRIDGE.describe(update.effective_chat.id)
    session = str(d["session"])
    if not session.startswith("("):
        session = Path(session).name
    lines = [
        "🤖 Kimchi Telegram bridge",
        f"Model: {d['model']}",
        f"Tools: {d['allowed_tools']}",
        f"Workspace: {d['workspace']}",
        f"Session: {session}",
        f"Status: {'🟢 busy' if d['busy'] else '⚪ idle'}",
        f"Timeout: {d['timeout_s']:.0f}s",
    ]
    await update.message.reply_text("\n".join(lines))


async def on_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _authorized(update):
        return
    killed = await BRIDGE.cancel(update.effective_chat.id)
    if killed:
        await update.message.reply_text("🛑 Cancelling current run…")
    else:
        await update.message.reply_text("Nothing is running right now.")


async def on_files(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _authorized(update):
        return
    files = BRIDGE.list_workspace_files(update.effective_chat.id)
    if not files:
        await update.message.reply_text(
            "📂 Workspace is empty — ask me to create a doc!"
        )
        return
    lines = [f"📂 {len(files)} file(s) in this chat's workspace:", ""]
    for name, size in files[:50]:
        lines.append(f"• {name} ({size:,} B)")
    if len(files) > 50:
        lines.append(f"… and {len(files) - 50} more")
    await update.message.reply_text("\n".join(lines))


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _authorized(update):
        return
    text = (update.message.text or "").strip()
    if not text:
        return
    chat_id = update.effective_chat.id

    if BRIDGE.is_busy(chat_id):
        await update.message.reply_text(
            "⏳ Still working on your previous message — /cancel to stop it."
        )
        return

    placeholder = await update.message.reply_text("⏳ Kimchi is working…")
    started = time.monotonic()
    done = asyncio.Event()
    ticker = asyncio.create_task(_tick(placeholder, started, done))

    try:
        result = await BRIDGE.run_prompt(chat_id, text)
    except Exception:
        log.exception("chat %s: bridge crashed", chat_id)
        await _edit_or_send(
            context, placeholder, chat_id,
            "💥 Unexpected bridge error — see the bot console for details.",
        )
        return
    finally:
        done.set()
        with contextlib.suppress(asyncio.CancelledError):
            await ticker

    if result.ok:
        await _deliver(context, chat_id, placeholder, result.output)
    else:
        msg = f"⚠️ kimchi exited with code {result.exit_code}."
        if result.stderr_tail:
            msg += f"\n\nDiagnostics:\n{result.stderr_tail}"
        await _edit_or_send(context, placeholder, chat_id, msg)


async def on_non_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _authorized(update):
        return
    await update.message.reply_text(
        "I currently only handle text messages — send your task as text."
    )


# ----------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------

async def _post_init(app) -> None:
    """Register the bot's command menu in Telegram's UI."""
    from telegram import BotCommand
    await app.bot.set_my_commands([
        BotCommand("new", "Start a fresh session"),
        BotCommand("status", "Show model, session, workspace"),
        BotCommand("files", "List files created in this chat"),
        BotCommand("cancel", "Stop the current run"),
        BotCommand("help", "How this bot works"),
    ])


def main() -> None:
    global CFG, BRIDGE
    handlers: list[logging.Handler] = [
        RotatingFileHandler(BASE_DIR / "bot.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    ]
    if sys.stderr is not None:  # pythonw (windowless) has no stderr
        handlers.append(logging.StreamHandler())
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=handlers,
    )
    load_dotenv(BASE_DIR / ".env")

    try:
        CFG = Config.from_env()
    except ConfigError as e:
        print(f"Configuration error:\n  {e}", file=sys.stderr)
        sys.exit(2)

    try:
        BRIDGE = KimchiBridge(
            kimchi_js=CFG.kimchi_js,
            model=CFG.model,
            allowed_tools=CFG.allow_tools,
            timeout_s=CFG.timeout_s,
        )
    except FileNotFoundError as e:
        print(f"Setup error:\n  {e}", file=sys.stderr)
        sys.exit(2)

    app = (
        ApplicationBuilder()
        .token(CFG.token)
        # Let updates interleave so /cancel works while a run is active.
        .concurrent_updates(True)
        .post_init(_post_init)
        .build()
    )
    app.add_handler(CommandHandler("start", on_start))
    app.add_handler(CommandHandler("help", on_help))
    app.add_handler(CommandHandler("new", on_new))
    app.add_handler(CommandHandler("status", on_status))
    app.add_handler(CommandHandler("cancel", on_cancel))
    app.add_handler(CommandHandler("files", on_files))
    app.add_handler(
        MessageHandler(filters.UpdateType.MESSAGE & filters.TEXT & ~filters.COMMAND, on_message)
    )
    app.add_handler(
        MessageHandler(filters.UpdateType.MESSAGE & ~filters.TEXT & ~filters.COMMAND, on_non_text)
    )

    log.info("bot starting — authorized users: %s", sorted(CFG.allowed_ids))
    app.run_polling()


if __name__ == "__main__":
    main()
