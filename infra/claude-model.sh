#!/bin/sh
# Pin Claude Code's model for every session on this machine, in its user
# settings, keeping whatever else they hold. Safe to rerun.
# Usage: sh infra/claude-model.sh   (the model is CLAUDE_MODEL in versions.env)
set -eu
cd "$(dirname "$0")/.."
. ./versions.env
mkdir -p "$HOME/.claude"
python3 - "$CLAUDE_MODEL" "$HOME/.claude/settings.json" <<'PY'
import json, os, sys
model, path = sys.argv[1:]
settings = json.load(open(path)) if os.path.exists(path) else {}
settings["model"] = model
json.dump(settings, open(path, "w"), indent=2)
print(f"{path}: model {model}")
PY
