#!/bin/sh
set -eu
project_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
if [ -n "${PYTHON_BIN:-}" ]; then
    chosen_python="$PYTHON_BIN"
elif [ -x "$project_dir/.venv/bin/python" ]; then
    chosen_python="$project_dir/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1 && python3 -c 'import pdfplumber, pypdfium2, PIL' >/dev/null 2>&1; then
    chosen_python=python3
else
    chosen_python="$HOME/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3"
    if [ ! -x "$chosen_python" ]; then
        printf '%s\n' '请按 README.md 创建 Python 虚拟环境或设置 PYTHON_BIN。' >&2
        exit 2
    fi
fi
if [ "${1:-}" = "prepare-training" ]; then
    shift
    exec "$chosen_python" "$project_dir/prepare_training.py" "$@"
fi
if [ "${1:-}" = "build-database" ]; then
    shift
    exec "$chosen_python" "$project_dir/build_database.py" "$@"
fi
exec "$chosen_python" "$project_dir/literature_agent.py" "$@"
