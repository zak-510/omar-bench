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
  # Trusting $WORK skips a dialog OMAR 0.4.2 left unanswered; 0.4.3 answers it itself.
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

# The agents' session logs: Claude Code transcripts, or Codex rollouts.
session_logs() {
  if [ "$OB_BACKEND" = claude ]; then
    find "$OB_LOGS/sessions/projects" -mindepth 2 -maxdepth 2 -name '*.jsonl'
  else
    find "$OB_LOGS/sessions" -name 'rollout-*.jsonl'
  fi 2>/dev/null
}

# Whether a log's last turn is over. Claude Code writes a turn_duration record when a turn
# ends (a turn that died on an API error included); Codex closes every task_started with a
# task_complete or a turn_aborted. An agent that has had no turn yet is not working either.
turn_over() {
  case "$1" in
    */rollout-*.jsonl)
      [ "$(grep -o -E '"type":"(task_started|task_complete|turn_aborted)"' "$1" | tail -n 1)" \
        != '"type":"task_started"' ] ;;
    *)
      tail -n 40 "$1" | grep -E '"type":"(assistant|user)"|"subtype":"turn_duration"' | tail -n 1 \
        | grep -q -E '"subtype":"turn_duration"|"isApiErrorMessage":true' ;;
  esac
}

# Once no agent has written anything for OB_DEAD_TURN_MINS, prints why OMAR is waiting on
# a step no agent will finish, and the logs that show it: an agent's turn died on an API
# error, which Claude Code does not take up again (the output cap, a usage limit); or every
# agent's turn is over, one having ended without completing its OMAR step.
stalled() {
  logs=$(session_logs)
  [ -n "$logs" ] || return 1
  dead= over=yes
  for f in $logs; do
    [ -n "$(find "$f" -mmin +"$OB_DEAD_TURN_MINS")" ] || return 1
    if tail -n 40 "$f" | grep -E '"type":"(assistant|user)"' | tail -n 1 \
        | grep -q '"isApiErrorMessage":true'; then
      dead=$f
    fi
    turn_over "$f" || over=
  done
  if [ -n "$dead" ]; then
    echo "an agent's turn ended in an API error and"
    echo "no agent has written for $OB_DEAD_TURN_MINS minutes. Its transcript:"
    echo "$dead"
  elif [ -n "$over" ]; then
    echo "every agent's turn was over and no agent had written for"
    echo "$OB_DEAD_TURN_MINS minutes, while OMAR still waited on a step. Their session logs:"
    echo "$logs"
  else
    return 1
  fi
}

# Run from $WORK so the runtime leaves nothing in the task's directory.
cd "$WORK"
# Since 0.4.3 a topology runs in a background OMAR session, and a session `omar run` makes
# for itself is removed, workspaces and all, the moment the run ends. The attempt makes its
# own, to list the workspaces after the run and to end every agent before the tests start.
# --fg waits for the run's end. Checkpoints only serve pause and rollback, never used here,
# and a failed one holds the run at its boundary: off, as there were none before 0.4.3.
session=bench
if ! omar -c "$WORK/config.toml" up --no-ea --name "$session" > "$OB_LOGS/omar-up.txt" 2>&1; then
  cat "$OB_LOGS/omar-up.txt"
  exit 1
fi
omar -s "$session" run "$WORK/topology.omar" \
  --input "t.statement=$WORK/instruction.md" \
  --input "t.cwd=$TASK_CWD" \
  --replace --fast --timeout-seconds "$OB_INVOCATION_TIMEOUT" \
  --fg --keep-workspaces --checkpoint-period off \
  > "$OB_LOGS/omar-run.txt" 2>&1 &
omar_pid=$!
# OMAR waits out the whole time limit for a step whose agent's turn has ended without
# completing it. `claude --print` and `codex exec` exit when their turn ends, so the attempt
# is over: end it rather than wait. The task is graded on disk either way. Ending `omar run
# --fg` only stops the watching; removing the session below ends the run.
(
  while sleep "$OB_WATCH_SECS" && kill -0 "$omar_pid" 2>/dev/null; do
    if why=$(stalled); then
      printf 'omar-bench ended this attempt: %s\n' "$why" > "$OB_LOGS/omar-stall.txt"
      kill "$omar_pid" 2>/dev/null || true
      break
    fi
  done
) > /dev/null 2>&1 &
watch_pid=$!
status=0
wait "$omar_pid" || status=$?
[ -z "${watch_pid:-}" ] || kill "$watch_pid" 2>/dev/null || true
[ ! -e "$OB_LOGS/omar-stall.txt" ] || status=70
# OMAR tells each agent to keep its files in its own workspace; the task is graded on
# its own directory. Record what was left behind, so work done in the wrong place can
# be told from a task that was not solved.
find "$HOME/.omar/sessions/$session/workspaces" -path '*/worktree/*' -type f ! -name .git \
  2>/dev/null | sort > "$OB_LOGS/omar-workspace-files.txt" || true
# Ends the runtime and every agent in it (they have a tmux server of their own); nothing
# should still be running when tests start.
omar -s "$session" rm >> "$OB_LOGS/omar-up.txt" 2>&1 || true
cat "$OB_LOGS/omar-run.txt"
[ ! -e "$OB_LOGS/omar-stall.txt" ] || cat "$OB_LOGS/omar-stall.txt"
exit "$status"
