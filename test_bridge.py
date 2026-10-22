"""End-to-end test of kimchi_bridge: runs two REAL kimchi turns (~30-60s).

Verifies the exact code path the Telegram bot uses: spawn, write
permissions, session adoption, session resume, and reset.

Usage:  py -3 test_bridge.py
"""

import asyncio
import shutil
import sys
from pathlib import Path

from kimchi_bridge import SESSIONS_ROOT, KimchiBridge, sanitize_session_dir_name

CHAT = 999999999  # fake Telegram chat id, used only by this test
STATE = Path(__file__).resolve().parent / "test_bridge_state.json"


async def main() -> int:
    failures: list[str] = []

    def check(name: str, cond: bool) -> None:
        print(f"  {'PASS' if cond else 'FAIL'}  {name}")
        if not cond:
            failures.append(name)

    bridge = KimchiBridge(state_file=STATE)
    ws = bridge.chat_workspace(CHAT)

    print(f"[1/4] write functionality (workspace: {ws})")
    r1 = await bridge.run_prompt(
        CHAT,
        "Create a file named bridge-test.md containing exactly this line: "
        "the bridge can write",
    )
    print(f"      exit={r1.exit_code} dur={r1.duration_s:.1f}s out={r1.output[:120]!r}")
    check("run succeeded", r1.ok)
    created = ws / "bridge-test.md"
    content = created.read_text(encoding="utf-8") if created.is_file() else ""
    check("file created with requested content", "the bridge can write" in content)

    print("[2/4] session adopted + sanitization matches on-disk layout")
    session = bridge.current_session(CHAT)
    check("session stored", bool(session))
    if session:
        expected_dir = SESSIONS_ROOT / sanitize_session_dir_name(ws)
        ok_dir = expected_dir.is_dir() and Path(session).parent == expected_dir
        check("sanitized session dir exists and matches", ok_dir)
        print(f"      session file: {Path(session).name}")

    print("[3/4] conversation continuity via session resume")
    r2 = await bridge.run_prompt(
        CHAT,
        "What exact text did I ask you to write into bridge-test.md? "
        "Reply with just that text.",
    )
    print(f"      exit={r2.exit_code} dur={r2.duration_s:.1f}s out={r2.output[:120]!r}")
    check("run succeeded", r2.ok)
    check("context preserved across runs", "the bridge can write" in r2.output.lower())

    print("[4/4] fresh-session reset")
    bridge.new_session(CHAT)
    check("session cleared", bridge.current_session(CHAT) is None)

    # Cleanup test artifacts.
    shutil.rmtree(ws, ignore_errors=True)
    STATE.unlink(missing_ok=True)
    print("      (test workspace and state cleaned up)")

    print()
    if failures:
        print(f"RESULT: {len(failures)} check(s) FAILED: {failures}")
        return 1
    print("RESULT: all bridge checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
