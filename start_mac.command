#!/usr/bin/env bash
set -e

cd "$(dirname "$0")"
echo "=================================================="
echo "  Overseas Meeting Translator (macOS / Linux)"
echo "  Path: $(pwd)"
echo "=================================================="
echo ""

export PIP_CACHE_DIR="$(pwd)/.pip_cache"

find_python() {
    local candidates=(
        "python3"
        "/opt/homebrew/bin/python3"
        "/usr/local/bin/python3"
        "/Library/Frameworks/Python.framework/Versions/3.12/bin/python3"
        "/Library/Frameworks/Python.framework/Versions/3.11/bin/python3"
        "/Library/Frameworks/Python.framework/Versions/3.10/bin/python3"
    )
    for py in "${candidates[@]}"; do
        if command -v "$py" >/dev/null 2>&1; then
            if "$py" -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >/dev/null 2>&1; then
                echo "$py"
                return 0
            fi
        fi
    done
    return 1
}

VENV_PY="$(pwd)/.venv/bin/python"

if [ ! -x "$VENV_PY" ]; then
    PY_CMD="$(find_python || true)"
    if [ -z "$PY_CMD" ]; then
        echo "[ERROR] Python 3.10+ not found."
        echo "Please install Python 3.12 via Homebrew or python.org:"
        echo "  brew install python@3.12"
        echo "  or https://www.python.org/downloads/macos/"
        read -r -p "Press Enter to exit..."
        exit 1
    fi
    echo "[1/2] Creating isolated virtual environment (.venv)..."
    echo "      Using: $PY_CMD"
    "$PY_CMD" -m venv .venv
fi

if ! "$VENV_PY" -c "import PyQt6, sounddevice, numpy, requests" >/dev/null 2>&1; then
    echo "[2/2] Installing dependencies (PyQt6, sounddevice, numpy, requests)..."
    "$VENV_PY" -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple || \
    "$VENV_PY" -m pip install -r requirements.txt
fi

echo "[START] Launching floating subtitle window..."
exec "$VENV_PY" main.py
