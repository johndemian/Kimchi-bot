"""Async bridge from Telegram chats to the kimchi coding agent.

Spawns the kimchi CLI non-interactively (``-p``) per chat turn:

* each chat gets its own workspace directory under ``workspace/`` — files
  the agent creates (docs, etc.) land there,
* each chat gets its own kimchi session, resumed automatically so the
  conversation keeps context across messages,
* runs are serialized per chat and can be cancelled mid-run.

Write access is real: the default allowed tools include ``write``/``edit``.
Empirically (kimchi 1.16.1) a session allow rule overrides deny rules, so
the only real access boundary is the Telegram allowlist in bot.py.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("kimchi_bridge")

DEFAULT_MODEL = "glm-5.3"
DEFAULT_ALLOW_TOOLS = ("read", "write", "edit", "ls", "find", "grep")

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_KIMCHI_JS = (
    Path.home() / "AppData" / "Roaming" / "npm" / "node_modules"
    / "@getkimchi" / "kimchi" / "bin" / "kimchi.js"
)
SESSIONS_ROOT = Path.home() / ".config" / "kimchi" / "harness" / "sessions"

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def sanitize_session_dir_name(cwd: str | Path) -> str:
    """Reproduce kimchi's cwd -> session-directory-name sanitization.

    Every character outside ``[A-Za-z0-9_]`` becomes ``-`` and the result is
    wrapped in ``--`` ... ``--``. Verified against the on-disk layout written
    by kimchi 1.16.1, e.g. ``C:\\Users\\johnc`` -> ``--C--Users-johnc--``.
    """
    return "--" + re.sub(r"[^A-Za-z0-9_]", "-", str(cwd)) + "--"


def _strip_ansi(s: str) -> str:
    return _ANSI_RE.sub("", s)


class ChatBusyError(RuntimeError):
    """A run is already in progress for this chat."""


@dataclass
class RunResult:
    ok: bool
    output: str
    exit_code: int
    duration_s: float
    session_path: str | None
    stderr_tail: str


class KimchiBridge:
    """Owns workspaces, sessions and subprocess lifecycle for all chats."""

    def __init__(
        self,
        kimchi_js: Path | str | None = None,
        workspace_root: Path | str | None = None,
        model: str = DEFAULT_MODEL,
        allowed_tools: list[str] | tuple[str, ...] | None = None,
        timeout_s: float = 600.0,
        mcp_config: Path | str | None = None,
        state_file: Path | str | None = None,
    ) -> None:
        self.kimchi_js = Path(kimchi_js) if kimchi_js else DEFAULT_KIMCHI_JS
        if not self.kimchi_js.is_file():
            raise FileNotFoundError(
                f"kimchi CLI entry point not found at {self.kimchi_js}. "
                "Set KIMCHI_JS to the full path of kimchi.js "
                "(npm global: ...\\npm\\node_modules\\@getkimchi\\kimchi\\bin\\kimchi.js)."
            )
        self.workspace_root = (
            Path(workspace_root).resolve() if workspace_root else BASE_DIR / "workspace"
        )
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        self.model = model
        self.allowed_tools = tuple(allowed_tools) if allowed_tools else DEFAULT_ALLOW_TOOLS
        self.timeout_s = float(timeout_s)

        if mcp_config is None:
            candidate = BASE_DIR / "mcp-empty.json"
            self.mcp_config = candidate if candidate.is_file() else None
        else:
            self.mcp_config = Path(mcp_config)

        self.state_file = Path(state_file) if state_file else BASE_DIR / "bridge_state.json"
        self._locks: dict[int, asyncio.Lock] = {}
        self._procs: dict[int, asyncio.subprocess.Process] = {}
        self._sessions: dict[int, str] = {}
        self._load_state()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def chat_workspace(self, chat_id: int) -> Path:
        ws = self.workspace_root / str(chat_id)
        ws.mkdir(parents=True, exist_ok=True)
        return ws

    def is_busy(self, chat_id: int) -> bool:
        lock = self._locks.get(chat_id)
        return bool(lock and lock.locked())

    def new_session(self, chat_id: int) -> None:
        """Drop the stored session so the next message starts fresh context."""
        self._sessions.pop(chat_id, None)
        self._save_state()

    def current_session(self, chat_id: int) -> str | None:
        return self._sessions.get(chat_id)

    def describe(self, chat_id: int) -> dict[str, object]:
        return {
            "model": self.model,
            "workspace": str(self.chat_workspace(chat_id)),
            "session": self._sessions.get(chat_id) or "(none yet — starts with your next message)",
            "allowed_tools": ", ".join(self.allowed_tools),
            "busy": self.is_busy(chat_id),
            "timeout_s": self.timeout_s,
        }

    def list_workspace_files(self, chat_id: int) -> list[tuple[str, int]]:
        ws = self.chat_workspace(chat_id)
        out: list[tuple[str, int]] = []
        for p in sorted(ws.rglob("*")):
            if p.is_file():
                out.append((str(p.relative_to(ws)), p.stat().st_size))
        return out

    async def cancel(self, chat_id: int) -> bool:
        """Kill the running process for this chat, if any."""
        proc = self._procs.get(chat_id)
        if proc is None or proc.returncode is not None:
            return False
        try:
            proc.kill()
        except ProcessLookupError:
            return False
        return True

    async def run_prompt(self, chat_id: int, text: str) -> RunResult:
        lock = self._locks.setdefault(chat_id, asyncio.Lock())
        if lock.locked():
            raise ChatBusyError(f"a run is already in progress for chat {chat_id}")
        async with lock:
            return await self._run_locked(chat_id, text)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _run_locked(self, chat_id: int, text: str) -> RunResult:
        ws = self.chat_workspace(chat_id)
        started = time.monotonic()
        wall_start = time.time()
        stored_session = self._sessions.get(chat_id)

        args: list[str] = [
            str(self.kimchi_js),
            "-p", text,
            "--model", self.model,
            "--allow-tool", ",".join(self.allowed_tools),
        ]
        if stored_session:
            args += ["--session", stored_session]
        if self.mcp_config:
            args += ["--mcp-config", str(self.mcp_config)]

        env = {
            **os.environ,
            "KIMCHI_NO_UPDATE_CHECK": "1",
            "KIMCHI_TAGS": "source:telegram-bot",
        }

        log.info("chat %s: starting run (%d chars, session=%s)",
                 chat_id, len(text), bool(stored_session))
        try:
            proc = await asyncio.create_subprocess_exec(
                "node", *args,
                cwd=str(ws),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as e:
            return RunResult(
                ok=False,
                output="",
                exit_code=-1,
                duration_s=time.monotonic() - started,
                session_path=stored_session,
                stderr_tail=f"failed to spawn node: {e}",
            )

        self._procs[chat_id] = proc
        try:
            try:
                out_b, err_b = await asyncio.wait_for(
                    proc.communicate(), timeout=self.timeout_s
                )
            except TimeoutError:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                await proc.wait()
                log.warning("chat %s: run timed out after %.0fs", chat_id, self.timeout_s)
                return RunResult(
                    ok=False,
                    output="",
                    exit_code=-1,
                    duration_s=self.timeout_s,
                    session_path=stored_session,
                    stderr_tail=f"Run timed out after {self.timeout_s:.0f}s and was killed. "
                                "Use /new to start a fresh session if it got stuck.",
                )
        finally:
            self._procs.pop(chat_id, None)

        duration = time.monotonic() - started
        output = _strip_ansi(out_b.decode("utf-8", errors="replace")).strip()
        stderr = _strip_ansi(err_b.decode("utf-8", errors="replace")).strip()
        ok = proc.returncode == 0
        log.info("chat %s: run finished exit=%s dur=%.1fs out=%dB",
                 chat_id, proc.returncode, duration, len(output))

        # First successful run in a chat: adopt the session file it created.
        if ok and not stored_session:
            found = self._find_session_file(ws, not_before=wall_start - 5.0)
            if found is not None:
                self._sessions[chat_id] = str(found)
                self._save_state()
                log.info("chat %s: adopted session %s", chat_id, found.name)

        return RunResult(
            ok=ok,
            output=output or "(no output)",
            exit_code=proc.returncode if proc.returncode is not None else -1,
            duration_s=duration,
            session_path=self._sessions.get(chat_id),
            stderr_tail=stderr[-600:],
        )

    def _find_session_file(self, workspace: Path, not_before: float) -> Path | None:
        """Locate the session file kimchi created for this workspace.

        kimchi stores sessions under a sanitized form of the cwd. The sanitized
        directory is unique to this chat's workspace, so the newest .jsonl
        written since the run started is this chat's session.
        """
        d = SESSIONS_ROOT / sanitize_session_dir_name(workspace)
        if not d.is_dir():
            return None
        cands = [
            p for p in d.glob("*.jsonl")
            if p.stat().st_mtime >= not_before
        ]
        if not cands:  # clock-skew fallback: newest overall in this chat's dir
            cands = list(d.glob("*.jsonl"))
        if not cands:
            return None
        return max(cands, key=lambda p: p.stat().st_mtime)

    # ------------------------------------------------------------------
    # State persistence
    # ------------------------------------------------------------------

    def _load_state(self) -> None:
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
            self._sessions = {
                int(k): str(v) for k, v in data.get("sessions", {}).items()
            }
        except FileNotFoundError:
            self._sessions = {}
        except (OSError, ValueError) as e:
            log.warning("could not read %s (%s); starting fresh", self.state_file, e)
            self._sessions = {}

    def _save_state(self) -> None:
        payload = {"sessions": {str(k): v for k, v in self._sessions.items()}}
        try:
            tmp = self.state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            tmp.replace(self.state_file)
        except OSError as e:
            log.warning("could not save %s (%s)", self.state_file, e)
