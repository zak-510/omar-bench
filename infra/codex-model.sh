#!/bin/sh
# Set the model Codex runs on this machine, in CODEX_HOME/config.toml, keeping
# whatever else the file holds. Safe to rerun. The scheduler refuses a Codex
# tier on a machine set to another model.
# Usage: sh infra/codex-model.sh [model]   (default: CODEX_MODEL in versions.env)
set -eu
cd "$(dirname "$0")/.."
. ./versions.env
CODEX_HOME=${CODEX_HOME:-$HOME/codex-home}
mkdir -p "$CODEX_HOME"
python3 - "${1:-$CODEX_MODEL}" "$CODEX_HOME/config.toml" <<'PY'
import os, re, sys
model, path = sys.argv[1:]
text = open(path).read() if os.path.exists(path) else ""
line = f'model = "{model}"'
text = re.sub(r'(?m)^model\s*=.*$', line, text) if re.search(r'(?m)^model\s*=', text) else line + "\n" + text
open(path, "w").write(text)
print(f"{path}: {line}")
PY
