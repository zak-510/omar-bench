#!/bin/sh
# Runs OMAR on the task. Harbor invokes this as the agent user, from the task's
# working directory.
#   run.sh           one topology, start to end
#   run.sh up        a session with Mission Control's assistant; prints url=, work=, cwd=
#   run.sh stalled   why the team can make no more progress, or nothing
#   run.sh down      ends the session; prints what the attempt left
#   run.sh swarm     pre-DSL OMAR: its executive assistant takes the task, to its end
#   run.sh omnigent  Omnigent's Polly takes the task, to its end (not OMAR at all; the same
#                    CLI setup)
# `up`, `stalled` and `down` are the mission-control arm's steps, which the adapter drives.
set -eu
stage=${1:-run}
: "${OB_BACKEND:?}" "${OB_MODEL:?}" "${OB_LOGS:?}" "${OB_INVOCATION_TIMEOUT:?}"
# How long every agent must have been silent before a turn that ended in an API error
# ends the attempt, and how often to look. Longer than the longest quiet stretch of a
# working agent: one response or one tool call, ten minutes each at most.
: "${OB_DEAD_TURN_MINS:=15}" "${OB_WATCH_SECS:=30}"
# How long the swarm's EA must have shown nothing new, with no agent and no event left,
# before its run counts as finished: its last words written, its turn over.
: "${OB_SWARM_SETTLE_SECS:=60}"
# Where finish() looks for links into a workspace: the whole machine, as a deliverable can
# be anywhere the task names.
: "${OB_LINK_ROOT:=/}"
OB="${OB_DIR:-/installed-agent/omar-bench}"
TASK_CWD=$(pwd)
session=bench
export PATH="$HOME/.local/bin:/usr/local/bin:$PATH"

# The agents' CLI, configured as every arm's is. The adapter's uploads are read from OB;
# everything written goes to WORK: some tasks run the agent as a user who cannot write
# under /installed-agent.
setup() {
  WORK=$(mktemp -d "${TMPDIR:-/tmp}/omar-bench.XXXXXX")
  cp "$OB/instruction.md" "$WORK/"
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
}

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

# Whether a log's last turn ended in an API error. Claude Code says so on the message it
# gave instead of an answer; Codex on the task_complete that closes the turn.
turn_died() {
  case "$1" in
    */rollout-*.jsonl)
      grep -E '"type":"(task_started|task_complete|turn_aborted)"' "$1" | tail -n 1 \
        | grep -q '"type":"task_complete".*"error":{' ;;
    *)
      tail -n 40 "$1" | grep -E '"type":"(assistant|user)"' | tail -n 1 \
        | grep -q '"isApiErrorMessage":true' ;;
  esac
}

# What ended a turn that died on an API error, and the log that shows it.
died() {
  echo "an agent's turn ended in an API error and"
  case "$1" in
    */rollout-*.jsonl)
      echo "no agent has written for $OB_DEAD_TURN_MINS minutes. The error, as Codex gave it:"
      grep '"type":"task_complete"' "$1" | tail -n 1 \
        | grep -o '"error":{"message":"[^"]*' | sed 's/^"error":{"message":"//' || true ;;
    *)
      echo "no agent has written for $OB_DEAD_TURN_MINS minutes. The error, as Claude Code gave it:"
      # Harbor names an attempt's error after the last one it knows in what this script
      # prints, so a safety refusal or the output cap is named here as it is for `solo`.
      tail -n 40 "$1" | grep '"isApiErrorMessage":true' | tail -n 1 \
        | grep -o '"text":"API Error:[^"]*' | sed 's/^"text":"//' || true ;;
  esac
  echo "Its transcript:"
  echo "$1"
}

# Once no agent has written anything for OB_DEAD_TURN_MINS, prints why OMAR is waiting on
# a step no agent will finish, and the logs that show it: an agent's turn died on an API
# error, which neither CLI takes up again (the output cap, a usage limit, a model at
# capacity); or every agent's turn is over, one having ended without completing its OMAR step.
stalled() {
  logs=$(session_logs)
  [ -n "$logs" ] || return 1
  dead= over=yes
  for f in $logs; do
    [ -n "$(find "$f" -mmin +"$OB_DEAD_TURN_MINS")" ] || return 1
    if turn_died "$f"; then dead=$f; fi
    turn_over "$f" || over=
  done
  if [ -n "$dead" ]; then
    died "$dead"
  elif [ -n "$over" ]; then
    echo "every agent's turn was over and no agent had written for"
    echo "$OB_DEAD_TURN_MINS minutes, while OMAR still waited on a step. Their session logs:"
    echo "$logs"
  else
    return 1
  fi
}

# Pre-DSL OMAR's swarm (0.3.x) has no program to finish: its executive assistant (EA)
# spawns, watches and kills agents of its own, and wakes them, and itself, with events its
# dashboard delivers. It runs on a tmux server of its own. A killed agent's log can stop
# mid-turn, so the swarm is read from its terminals: an agent at work shows it (a running
# timer, a spinner); one waiting at its prompt shows nothing new.
SWARM=omar-bench-swarm
EA=bench-ea-0   # OMAR's name for the first EA's session, under the prefix setup() gives
swarm() { tmux -L "$SWARM" "$@"; }
# Seconds since anything showed in the busiest of these sessions (all but the dashboard's).
swarm_quiet() {
  now=$(date +%s) last=0
  for at in $(swarm list-windows -a -F '#{session_name} #{window_activity}' 2>/dev/null |
      awk -v only="${1:-}" '$1 != "dashboard" && (only == "" || $1 == only) { print $2 }'); do
    [ "$at" -le "$last" ] || last=$at
  done
  echo $((now - last))
}
# The agents the EA has running; and its events still to come that fire once (a recurring
# one is a heartbeat, not work).
swarm_agents() { swarm list-sessions -F '#S' 2>/dev/null | grep -v -x -e dashboard -e "$EA" || true; }
swarm_pending() { omar -c "$WORK/config.toml" event list 2>/dev/null | awk 'NR > 1 && $5 == "-"'; }

# The swarm has finished: the EA has no agent left and nothing to wait for, and has shown
# nothing new for OB_SWARM_SETTLE_SECS, its last turn over.
swarm_done() {
  swarm has-session -t "=$EA" 2>/dev/null &&
    [ -z "$(swarm_agents)" ] && [ -z "$(swarm_pending)" ] &&
    [ "$(swarm_quiet "$EA")" -ge "$OB_SWARM_SETTLE_SECS" ]
}

# No agent will move again: nothing has shown in any of their terminals, nor been written
# to any log, for OB_DEAD_TURN_MINS, and no event is coming to wake one. As stalled() says
# it, so the queue reads it the same way.
swarm_stalled() {
  [ "$(swarm_quiet)" -ge "$((OB_DEAD_TURN_MINS * 60))" ] && [ -z "$(swarm_pending)" ] || return 1
  logs=$(session_logs) dead=
  for f in $logs; do
    [ -n "$(find "$f" -mmin +"$OB_DEAD_TURN_MINS")" ] || return 1
    if turn_died "$f"; then dead=$f; fi
  done
  if [ -n "$dead" ]; then
    died "$dead"
  else
    echo "every agent's turn was over and no agent had written for"
    echo "$OB_DEAD_TURN_MINS minutes, while the EA still had agents up and no event was coming. Their session logs:"
    echo "$logs"
  fi
}

# OMAR tells each agent to keep its files in its own workspace; the task is graded on
# its own directory. Record what was left behind, so work done in the wrong place can
# be told from a task that was not solved. Then end the runtime and every agent in it
# (they have a tmux server of their own): nothing should still be running when tests start.
finish() {
  ws="$HOME/.omar/sessions/$session/workspaces"
  find "$ws" -path '*/worktree/*' -type f ! -name .git \
    2>/dev/null | sort > "$OB_LOGS/omar-workspace-files.txt" || true
  # Agents told both to keep their files in the workspace and to make the task's changes
  # in place have done both: the file in the workspace, a link to it at the task's path
  # (2026-10-30). Removing the session leaves that link pointing at nothing, and a grader
  # in another container cannot follow it at all; Harbor fails copying it out. So each
  # such link becomes a copy of what it pointed at, and is listed with the attempt.
  : > "$OB_LOGS/omar-linked-files.txt"
  find "$OB_LINK_ROOT" \( -path /proc -o -path /sys -o -path /dev -o -path "$HOME/.omar" \
      -o -path "$OB_LOGS" \) -prune -o -type l -print 2>/dev/null |
    while IFS= read -r link; do
      target=$(readlink -f "$link") || continue
      case "$target" in "$ws"/*) ;; *) continue ;; esac
      copy="$link.omar-bench-copy"
      cp -RLp "$link" "$copy" 2>/dev/null && rm -f "$link" && mv "$copy" "$link" || continue
      printf '%s -> %s\n' "$link" "$target" >> "$OB_LOGS/omar-linked-files.txt"
    done || true
  # An assistant that never took a message: what its terminal and the runtime last said.
  server=$(sed -n 's/.*"tmux_server": *"\([^"]*\)".*/\1/p' "$OB_LOGS/omar-up.txt" 2>/dev/null | head -n 1)
  if [ -e "$OB_LOGS/omar-mission-failed.txt" ]; then
    for pane in $(tmux -L "${server:-none}" list-panes -a -F '#{pane_id}' 2>/dev/null); do
      echo "== $(tmux -L "$server" display -p -t "$pane" '#{session_name}' 2>/dev/null)"
      tmux -L "$server" capture-pane -p -t "$pane" 2>/dev/null
    done > "$OB_LOGS/omar-panes.txt" || true
    cp "$HOME/.omar/sessions/$session/logs/runtime.log" "$OB_LOGS/omar-runtime.log" 2>/dev/null || true
  fi
  omar -s "$session" rm >> "$OB_LOGS/omar-up.txt" 2>&1 || true
}

case "$stage" in
  run | up | swarm | omnigent) ;;
  stalled)
    if why=$(stalled); then
      printf 'omar-bench ended this attempt: %s\n' "$why" > "$OB_LOGS/omar-stall.txt"
      cat "$OB_LOGS/omar-stall.txt"
    fi
    exit 0 ;;
  down)
    finish
    [ ! -e "$OB_LOGS/omar-run.txt" ] || cat "$OB_LOGS/omar-run.txt"
    # The driver's account of an attempt that never got a team running.
    if [ -e "$OB_LOGS/omar-mission-failed.txt" ]; then cat "$OB_LOGS/omar-mission-failed.txt"; exit 71; fi
    if [ -e "$OB_LOGS/omar-stall.txt" ]; then cat "$OB_LOGS/omar-stall.txt"; exit 70; fi
    exit 0 ;;
  *) echo "run.sh: no step '$stage'" >&2; exit 2 ;;
esac

setup
# Run from $WORK so the runtime leaves nothing in the task's directory.
cd "$WORK"

if [ "$stage" = omnigent ]; then
  # Omnigent's Polly, its bundle rendered for the arm's one model by the adapter. `omnigent
  # run` starts a local server and a session that takes the brief as its first message;
  # it needs a terminal, so it gets a tmux server of its own. The session, and every
  # sub-agent it starts, is followed over the server's API to its end (omnigent_watch.py).
  sed -e "s|@STATEMENT@|$WORK/instruction.md|g" -e "s|@CWD@|$TASK_CWD|g" "$OB/omnigent-brief.md" \
    > "$WORK/brief.md"
  cp -R "$OB/polly" "$WORK/polly"
  tmux -L omnigent new-session -d -s polly -x 200 -y 50 \
    "omnigent run '$WORK/polly' --server local -p \"\$(cat '$WORK/brief.md')\" 2>> '$OB_LOGS/omnigent.txt'"
  status=0
  "${OB_OMNIGENT_PY:-/opt/omnigent/tools/omnigent/bin/python}" "$OB/omnigent_watch.py" \
    "${OB_OMNIGENT_API:-http://127.0.0.1:6767}" "$OB_LOGS" > "$OB_LOGS/omar-run.txt" 2>&1 || status=$?
  tmux -L omnigent capture-pane -p -J -S -200 -t polly: > "$OB_LOGS/omar-panes.txt" 2>/dev/null || true
  # The workers' own logs, beside the rest; their tokens are counted from Omnigent's totals,
  # as the lead's log goes with its turn. Not their Codex homes: those hold the login.
  for d in "$HOME"/.omnigent/codex-native/*/codex-home/sessions; do
    [ -d "$d" ] || continue
    w=$(basename "$(dirname "$(dirname "$d")")")
    mkdir -p "$OB_LOGS/omnigent-workers/$w" && cp -R "$d" "$OB_LOGS/omnigent-workers/$w/"
  done
  # Nothing of the team may still be running when the tests start.
  omnigent stop > /dev/null 2>&1 || true
  tmux -L omnigent kill-server 2>/dev/null || true
  pkill -f /opt/omnigent/ 2>/dev/null || true
  cat "$OB_LOGS/omar-run.txt"
  exit "$status"
fi

if [ "$stage" = swarm ]; then
  # The dashboard runs the EA on the arm's CLI (`-a`, OMAR's own command line for it) and
  # delivers events; it needs a terminal, so it gets a session of the swarm's server. The
  # EA's agents start in the task's directory, as `solo` does.
  export OMAR_TMUX_SERVER="$SWARM"
  printf '\n[agent]\ndefault_workdir = "%s"\n' "$TASK_CWD" >> "$WORK/config.toml"
  # Each CLI asks once per folder whether to trust it, and 0.3.x does not answer (0.4.3
  # does): the EA's folder and the task's are trusted up front, and the question is
  # answered wherever else an agent starts.
  if [ "$OB_BACKEND" = claude ]; then
    sed "s|\"projects\": {|\"projects\": {\"$TASK_CWD\": {\"hasTrustDialogAccepted\": true}, |" \
      "$HOME/.claude.json" > "$HOME/.claude.json.new" && mv "$HOME/.claude.json.new" "$HOME/.claude.json"
  else
    printf '\n[projects."%s"]\ntrust_level = "trusted"\n' "$WORK" "$TASK_CWD" >> "$CODEX_HOME/config.toml"
  fi
  (
    while sleep 2; do
      for s in $(swarm list-sessions -F '#S' 2>/dev/null); do
        if swarm capture-pane -p -t "=$s:" 2>/dev/null |
            grep -q -E 'Trust this folder\?|trust the files in this folder'; then
          swarm send-keys -t "=$s:" Enter
        fi
      done
    done
  ) > /dev/null 2>&1 &
  trust_pid=$!
  swarm new-session -d -s dashboard -x 200 -y 50 \
    "omar -c '$WORK/config.toml' -a '$OB_BACKEND' 2>> '$OB_LOGS/omar-dashboard.txt'"
  waited=0
  until swarm has-session -t "=$EA" 2>/dev/null; do
    if [ "$waited" -ge 120 ]; then
      echo "omar-bench: the EA did not start within 120 s" | tee "$OB_LOGS/omar-mission-failed.txt"
      swarm capture-pane -p -t dashboard: > "$OB_LOGS/omar-panes.txt" 2>/dev/null || true
      kill "$trust_pid" 2>/dev/null || true
      swarm kill-server 2>/dev/null || true
      exit 71
    fi
    sleep 2; waited=$((waited + 2))
  done
  # The task, as a user's message: an event to the EA, delivered by OMAR's own scheduler.
  sed -e "s|@STATEMENT@|$WORK/instruction.md|g" -e "s|@CWD@|$TASK_CWD|g" "$OB/swarm-brief.md" \
    > "$WORK/brief.md"
  omar -c "$WORK/config.toml" event schedule --receiver ea --sender user --in-seconds 0 \
    --payload "$(cat "$WORK/brief.md")" > "$OB_LOGS/omar-run.txt" 2>&1
  status=0
  while sleep "$OB_WATCH_SECS"; do
    if ! swarm has-session -t "=$EA" 2>/dev/null; then
      echo "omar-bench ended this attempt: the EA's session ended" > "$OB_LOGS/omar-stall.txt"
      status=70; break
    fi
    swarm_done && break
    if why=$(swarm_stalled); then
      printf 'omar-bench ended this attempt: %s\n' "$why" > "$OB_LOGS/omar-stall.txt"
      status=70; break
    fi
  done
  # What every terminal last showed, the EA's report among them; then every agent ends.
  for s in $(swarm list-sessions -F '#S' 2>/dev/null); do
    echo "== $s"; swarm capture-pane -p -J -S -200 -t "=$s:" 2>/dev/null
  done > "$OB_LOGS/omar-panes.txt" || true
  kill "$trust_pid" 2>/dev/null || true
  swarm kill-server 2>/dev/null || true
  cat "$OB_LOGS/omar-run.txt"
  [ ! -e "$OB_LOGS/omar-stall.txt" ] || cat "$OB_LOGS/omar-stall.txt"
  exit "$status"
fi

if [ "$stage" = up ]; then
  # The assistant runs on the arm's CLI, whichever OMAR would have found first.
  if [ "$OB_BACKEND" = claude ]; then
    assistant="claude --dangerously-skip-permissions"
  else
    assistant="codex --dangerously-bypass-approvals-and-sandbox"
  fi
  printf '\n[agent]\ndefault_command = "%s"\n' "$assistant" >> "$WORK/config.toml"
  # The language reference of the build that compiles its designs.
  cp "$OB/spec.md" "$WORK/"
  if ! omar -c "$WORK/config.toml" up --name "$session" --json > "$OB_LOGS/omar-up.txt" 2>&1; then
    cat "$OB_LOGS/omar-up.txt"
    exit 1
  fi
  url=$(sed -n 's/.*"url": *"\([^"]*\)".*/\1/p' "$OB_LOGS/omar-up.txt" | head -n 1)
  if [ -z "$url" ]; then
    echo "omar up gave no address:"; cat "$OB_LOGS/omar-up.txt"
    exit 1
  fi
  printf 'url=%s\nwork=%s\ncwd=%s\n' "$url" "$WORK" "$TASK_CWD"
  exit 0
fi

# Since 0.4.3 a topology runs in a background OMAR session, and a session `omar run` makes
# for itself is removed, workspaces and all, the moment the run ends. The attempt makes its
# own, to list the workspaces after the run and to end every agent before the tests start.
# --fg waits for the run's end. Checkpoints only serve pause and rollback, never used here,
# and a failed one holds the run at its boundary: off, as there were none before 0.4.3.
cp "$OB/topology.omar" "$WORK/"
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
finish
cat "$OB_LOGS/omar-run.txt"
[ ! -e "$OB_LOGS/omar-stall.txt" ] || cat "$OB_LOGS/omar-stall.txt"
exit "$status"
