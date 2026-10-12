#!/bin/sh
# Runs one OMAR topology on the task. Harbor invokes this as the agent user,
# from the task's working directory.
set -eu
: "${OB_BACKEND:?}" "${OB_MODEL:?}" "${OB_LOGS:?}" "${OB_INVOCATION_TIMEOUT:?}"
# How long every agent must have been silent before a turn that ended in an API error
# ends the attempt, and how often to look. Longer than the longest quiet stretch of a
# working agent: one response or one tool call, ten minutes each at most.
: "${OB_DEAD_TURN_MINS:=15}" "${OB_WATCH_SECS:=30}"
OB="${OB_DIR:-/installed-agent/omar-bench}"
TASK_CWD=$(pwd)
# The adapter's uploads are read here. Everything written goes to WORK: some
# tasks run the agent as a user who cannot write under /installed-agent.
WORK=$(mktemp -d "${TMPDIR:-/tmp}/omar-bench.XXXXXX")
cp "$OB/topology.omar" "$OB/instruction.md" "$WORK/"
export PATH="$HOME/.local/bin:/usr/local/bin:$PATH"
export TERM="${TERM:-xterm-256color}" LANG="${LANG:-C.UTF-8}"
# The CLI version is a pin; it must not update itself mid-run.
export DISABLE_AUTOUPDATER=1 CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
# Lets Claude Code skip permission prompts as root, as Harbor's own agent does.
export IS_SANDBOX=1

# Session logs are written straight into the directory Harbor syncs to the host,
# so an attempt that times out still has its full token record.
mkdir -p "$OB_LOGS/sessions"
if [ "$OB_BACKEND" = claude ]; then
  # OMAR finds the session it delivers a prompt to under ~/.claude and nowhere else, so
  # the CLI keeps its default config directory. Only its transcripts are linked into the
  # synced one; the CLI's own files (plugins, config backups, a hash of the login) stay here.
  unset CLAUDE_CONFIG_DIR
  mkdir -p "$HOME/.claude" "$OB_LOGS/sessions/projects"
  if [ -d "$HOME/.claude/projects" ] && [ ! -L "$HOME/.claude/projects" ]; then
    cp -R "$HOME/.claude/projects/." "$OB_LOGS/sessions/projects/"
    rm -rf "$HOME/.claude/projects"
  fi
  ln -sfn "$OB_LOGS/sessions/projects" "$HOME/.claude/projects"
  export ANTHROPIC_MODEL="$OB_MODEL"
  # Agents start in $WORK. Trusting it here skips a dialog OMAR 0.4.2 leaves unanswered.
  sed "s|@WORKSPACE@|$WORK|" "$OB/claude.json" > "$HOME/.claude.json"
  cp "$OB/claude-settings.json" "$HOME/.claude/settings.json"
else
  # Credentials stay out of the synced directory; only sessions/ points into it.
  export CODEX_HOME="$WORK/codex-home"
  mkdir -p "$CODEX_HOME"
  ln -sfn "$OB_LOGS/sessions" "$CODEX_HOME/sessions"
  cp "$OB/codex-config.toml" "$CODEX_HOME/config.toml"
  if [ -f /tmp/codex-secrets/auth.json ]; then
    ln -sf /tmp/codex-secrets/auth.json "$CODEX_HOME/auth.json"
  elif [ -n "${OPENAI_API_KEY:-}" ]; then
    (umask 077 && printf '{"OPENAI_API_KEY": "%s"}\n' "$OPENAI_API_KEY" > "$CODEX_HOME/auth.json")
  fi
fi

printf '[dashboard]\nsession_prefix = "bench-"\n' > "$WORK/config.toml"

# Prints the transcript of an agent whose last word is an API error, once no agent has
# written anything for OB_DEAD_TURN_MINS. Claude Code ends a turn on such an error (the
# output cap, a usage limit) and does not take it up again.
dead_turn() {
  dead=
  for f in "$OB_LOGS"/sessions/projects/*/*.jsonl; do
    [ -f "$f" ] || continue
    [ -n "$(find "$f" -mmin +"$OB_DEAD_TURN_MINS")" ] || return 1
    if tail -n 40 "$f" | grep -E '"type":"(assistant|user)"' | tail -n 1 \
        | grep -q '"isApiErrorMessage":true'; then
      dead=$f
    fi
  done
  [ -n "$dead" ] && echo "$dead"
}

# Run from $WORK so the runtime leaves nothing in the task's directory.
cd "$WORK"
omar -c "$WORK/config.toml" run "$WORK/topology.omar" \
  --input "t.task=$WORK/instruction.md" \
  --input "t.cwd=$TASK_CWD" \
  --replace --fast --timeout-seconds "$OB_INVOCATION_TIMEOUT" \
  > "$OB_LOGS/omar-run.txt" 2>&1 &
omar_pid=$!
# OMAR 0.4.2 waits out the whole time limit for an agent whose turn died. `claude --print`
# would have exited on the same error, so the attempt is over: end it rather than wait.
if [ "$OB_BACKEND" = claude ]; then
  (
    while sleep "$OB_WATCH_SECS" && kill -0 "$omar_pid" 2>/dev/null; do
      if f=$(dead_turn); then
        {
          echo "omar-bench ended this attempt: an agent's turn ended in an API error and"
          echo "no agent has written for $OB_DEAD_TURN_MINS minutes. Its transcript:"
          echo "$f"
        } > "$OB_LOGS/omar-stall.txt"
        kill "$omar_pid" 2>/dev/null || true
        break
      fi
    done
  ) > /dev/null 2>&1 &
  watch_pid=$!
fi
status=0
wait "$omar_pid" || status=$?
[ -z "${watch_pid:-}" ] || kill "$watch_pid" 2>/dev/null || true
if [ -e "$OB_LOGS/omar-stall.txt" ]; then
  # The agents' sessions outlive OMAR; nothing should still be running when tests start.
  tmux ls -F '#{session_name}' 2>/dev/null | grep '^bench-' | while read -r name; do
    tmux kill-session -t "$name" 2>/dev/null || true
  done
  status=70
fi
# OMAR tells each agent to keep its files in its own workspace; the task is graded on
# its own directory. Record what was left behind, so work done in the wrong place can
# be told from a task that was not solved.
find "$HOME/.omar/workspaces" -path '*/worktree/*' -type f ! -name .git 2>/dev/null \
  | sort > "$OB_LOGS/omar-workspace-files.txt" || true
cat "$OB_LOGS/omar-run.txt"
[ ! -e "$OB_LOGS/omar-stall.txt" ] || cat "$OB_LOGS/omar-stall.txt"
exit "$status"
