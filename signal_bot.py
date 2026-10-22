"""Signal bot that bridges conversations to a local kimchi agent.

Runs on top of signalbot (2.x) + signal-cli-rest-api (Docker). Every text
message from an allowlisted sender (private chat) is run as a non-interactive
kimchi turn with write access; replies come back into the chat, with a typing
indicator while the agent works. Conversation context persists per chat, and
files land in a per-chat workspace — same as the Telegram frontend.

Required environment (or a .env file next to this script — see
docs/signal-setup.md for the full walkthrough):
  SIGNAL_PHONE_NUMBER     the Signal number of the account linked to
                          signal-cli-rest-api (E.164, e.g. +15551234567)
  ALLOWED_SIGNAL_NUMBERS  comma-separated sender numbers permitted to use
                          this bot — the ONLY access boundary, since the
                          agent runs with real write access on this PC
  SIGNAL_SERVICE          signal-cli-rest-api host:port (default
                          localhost:8080)

The shared KIMCHI_* variables (model, tools, timeout, kimchi.js path) work
exactly like the Telegram frontend. NOTE: implemented against the signalbot
2.1.3 documented API; not yet exercised against a live signal-cli-rest-api
instance.
"""

from __future__ import annotations

import logging
import os
import re
import sys
import tempfile
from logging.handlers import RotatingFileHandler
from pathlib import Path

from signalbot import (
    Config,
    DataMessageContext,
    DataMessageHandler,
    SendMessage,
    SignalBot,
)

from envutil import load_dotenv
from kimchi_bridge import (
    DEFAULT_ALLOW_TOOLS,
    DEFAULT_MODEL,
    KimchiBridge,
)

log = logging.getLogger("kimchi_signal")

BASE_DIR = Path(__file__).resolve().parent
MAX_MSG = 2000           # chunk replies for readability
DOC_THRESHOLD = 20000   # above this, send the reply as an attachment

HELP_TEXT = """🤖 Kimchi Signal bridge

Send me any message — it runs as a prompt on your local kimchi agent, with file write access in this chat's workspace.

Commands (plain text, with or without "/"):
new — start a fresh session (clears conversation context)
status — show model, session and workspace details
files — list files the agent created in this chat
cancel — stop the current run
help — this message

Notes:
• Conversation context persists across messages, per chat.
• The agent can create and edit files (docs, code, anything).
• Runs can take a while — one task at a time per chat.
• Everything runs on the machine hosting this bot, under its kimchi account."""


class SignalConfigError(RuntimeError):
    pass


def _normalize_number(raw: str) -> str:
    digits = re.sub(r"\D", "", raw)
    return f"+{digits}" if digits else ""


def _chat_key(context: DataMessageContext) -> str:
    """Stable, filesystem-safe workspace key for a conversation."""
    raw = context.message.source_or_group_id() or "unknown"
    key = re.sub(r"[^A-Za-z0-9_-]", "_", str(raw))
    return key[:64] or "unknown"


def _split(text: str, size: int) -> list[str]:
    return [text[i:i + size] for i in range(0, len(text), size)]


class KimchiHandler(DataMessageHandler):
    """Runs every incoming text message as a kimchi agent turn.

    Registered with ``contacts=<allowlist>, groups=False``, so only private
    chats from allowlisted senders ever reach this handler. Messages from
    anyone else are simply ignored (they still arrive on your phone like
    normal Signal messages).
    """

    def __init__(self, bridge: KimchiBridge) -> None:
        self.bridge = bridge

    async def handle_data_message(self, context: DataMessageContext) -> None:
        text = context.message.text
        if text is None:
            await context.send(SendMessage(
                text="I currently only handle text messages — send your task as text."
            ))
            return
        text = text.strip()
        if not text:
            return

        cmd = text.lstrip("/").lower()
        if cmd in ("help", "start"):
            await context.send(SendMessage(text=HELP_TEXT))
            return
        if cmd == "new":
            self.bridge.new_session(_chat_key(context))
            await context.send(SendMessage(
                text="🆕 Fresh session — your next message starts a new conversation.\n"
                     "Files already created stay in the workspace (see files)."
            ))
            return
        if cmd == "status":
            await context.send(SendMessage(text=self._status(context)))
            return
        if cmd == "files":
            await context.send(SendMessage(text=self._files(context)))
            return
        if cmd == "cancel":
            killed = await self.bridge.cancel(_chat_key(context))
            msg = "🛑 Cancelling current run…" if killed else "Nothing is running right now."
            await context.send(SendMessage(text=msg))
            return

        chat = _chat_key(context)
        if self.bridge.is_busy(chat):
            await context.send(SendMessage(
                text="⏳ Still working on your previous message — cancel to stop it."
            ))
            return

        await context.start_typing()
        try:
            result = await self.bridge.run_prompt(chat, text)
        except Exception:
            log.exception("chat %s: bridge crashed", chat)
            await context.send(SendMessage(
                text="💥 Unexpected bridge error — see the bot log for details."
            ))
            return
        finally:
            await context.stop_typing()

        if result.ok:
            await self._deliver(context, result.output)
        else:
            msg = f"⚠️ kimchi exited with code {result.exit_code}."
            if result.stderr_tail:
                msg += f"\n\nDiagnostics:\n{result.stderr_tail}"
            await self._deliver(context, msg)

    # -- helpers ---------------------------------------------------------

    def _status(self, context: DataMessageContext) -> str:
        d = self.bridge.describe(_chat_key(context))
        session = str(d["session"])
        if not session.startswith("("):
            session = Path(session).name
        return "\n".join([
            "🤖 Kimchi Signal bridge",
            f"Model: {d['model']}",
            f"Tools: {d['allowed_tools']}",
            f"Workspace: {d['workspace']}",
            f"Session: {session}",
            f"Status: {'🟢 busy' if d['busy'] else '⚪ idle'}",
            f"Timeout: {d['timeout_s']:.0f}s",
        ])

    def _files(self, context: DataMessageContext) -> str:
        files = self.bridge.list_workspace_files(_chat_key(context))
        if not files:
            return "📂 Workspace is empty — ask me to create a doc!"
        lines = [f"📂 {len(files)} file(s) in this chat's workspace:", ""]
        for name, size in files[:50]:
            lines.append(f"• {name} ({size:,} B)")
        if len(files) > 50:
            lines.append(f"… and {len(files) - 50} more")
        return "\n".join(lines)

    async def _deliver(self, context: DataMessageContext, text: str) -> None:
        if len(text) <= MAX_MSG:
            await context.send(SendMessage(text=text))
            return
        if len(text) > DOC_THRESHOLD:
            tmp = tempfile.NamedTemporaryFile(
                mode="w", suffix=".md", prefix="kimchi-reply-",
                delete=False, encoding="utf-8",
            )
            try:
                tmp.write(text)
                tmp.close()
                await context.send(SendMessage(
                    text=f"✅ Done — full reply ({len(text):,} chars) attached.",
                    attachments=[Path(tmp.name)],
                ))
            finally:
                Path(tmp.name).unlink(missing_ok=True)
            return
        chunks = _split(text, MAX_MSG)
        total = len(chunks)
        for i, chunk in enumerate(chunks):
            body = chunk if total == 1 else f"{chunk}\n\n[{i + 1}/{total}]"
            await context.send(SendMessage(text=body))


def _config_from_env() -> tuple[str, str, list[str], KimchiBridge]:
    phone = os.environ.get("SIGNAL_PHONE_NUMBER", "").strip()
    if not phone:
        raise SignalConfigError(
            "SIGNAL_PHONE_NUMBER is not set. It is the Signal number of the account "
            "you linked to signal-cli-rest-api (E.164, e.g. +15551234567). "
            "Put it in the .env file (see .env.example and docs/signal-setup.md)."
        )

    raw_allowed = os.environ.get("ALLOWED_SIGNAL_NUMBERS", "").strip()
    if not raw_allowed:
        raise SignalConfigError(
            "ALLOWED_SIGNAL_NUMBERS is not set. This bot runs kimchi with real "
            "write access on this PC, so you MUST allowlist sender numbers "
            "(E.164, comma-separated)."
        )
    allowed = [n for n in (_normalize_number(x) for x in raw_allowed.split(",")) if n]
    if not allowed:
        raise SignalConfigError("ALLOWED_SIGNAL_NUMBERS resolved to an empty set.")

    tools_raw = os.environ.get("KIMCHI_ALLOW_TOOLS", ",".join(DEFAULT_ALLOW_TOOLS))
    bridge = KimchiBridge(
        kimchi_js=(os.environ.get("KIMCHI_JS", "").strip() or None),
        model=os.environ.get("KIMCHI_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL,
        allowed_tools=[t.strip() for t in tools_raw.split(",") if t.strip()],
        timeout_s=float(os.environ.get("KIMCHI_TIMEOUT", "600")),
    )

    service = os.environ.get("SIGNAL_SERVICE", "localhost:8080").strip() or "localhost:8080"
    return phone, service, allowed, bridge


def main() -> None:
    handlers: list[logging.Handler] = [
        RotatingFileHandler(
            BASE_DIR / "signal-bot.log",
            maxBytes=1_000_000, backupCount=3, encoding="utf-8",
        )
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
        phone, service, allowed, bridge = _config_from_env()
    except (SignalConfigError, FileNotFoundError, ValueError) as e:
        print(f"Configuration error:\n  {e}", file=sys.stderr)
        sys.exit(2)

    bot = SignalBot(Config(
        phone_number=phone,
        signal_service=service,
        logging_level=logging.INFO,
    ))
    # Allowlist enforced at registration: only private chats from these
    # senders reach the handler. Groups are disabled entirely.
    bot.register(KimchiHandler(bridge), contacts=allowed, groups=False)
    log.info("signal bot starting — authorized senders: %s", allowed)
    bot.start()


if __name__ == "__main__":
    main()
