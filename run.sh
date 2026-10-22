#!/usr/bin/env bash
# Launches a kimchi bot frontend (bot.py or signal_bot.py) in its own venv.
# Usage: ./run.sh [bot.py|signal_bot.py]   (default: bot.py)
set -euo pipefail
cd "$(dirname "$0")"

SCRIPT="${1:-bot.py}"
case "$SCRIPT" in
  bot.py) CHECKMOD=telegram ;;
  signal_bot.py) CHECKMOD=signalbot ;;
  *) echo "unknown script: $SCRIPT (expected bot.py or signal_bot.py)"; exit 1 ;;
esac

if [ ! -x ".venv/bin/python" ]; then
    echo "[setup] creating virtual environment..."
    python3 -m venv .venv
fi

if ! .venv/bin/python -c "import $CHECKMOD" 2>/dev/null; then
    echo "[setup] installing dependencies..."
    .venv/bin/python -m pip install -r requirements.txt
fi

echo "[run] starting $SCRIPT..."
exec .venv/bin/python "$SCRIPT"
