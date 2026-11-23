#!/bin/sh
# Step Claude Code past its first-run screens (theme, security notes, folder
# trust, bypass mode) in the two folders agents start in: this repo, where the
# agent arm's runner starts them, and the tier's workdir, where OMAR does. An
# agent that meets one of these screens never reads its task.
# Run on the machine, after infra/claude-token.sh: sh infra/claude-onboard.sh
set -eu
cd "$(dirname "$0")/.."
ROOT=$(pwd)
. "$HOME/omar-rerun.env"
for DIR in "$ROOT" "$HOME/wd/haiku55"; do
    cd "$DIR"
    tmux -L onboard kill-server 2>/dev/null || true
    tmux -L onboard new -d -s onboard -x 160 -y 45 'claude --dangerously-skip-permissions'
    sleep 10
    state=stuck
    for _ in 1 2 3 4 5 6 7 8; do
        screen=$(tmux -L onboard capture-pane -p -t onboard)
        case "$screen" in
            *"bypass permissions on"*) state=ready; break ;;
            *"Choose the text style"*|*"Press Enter to continue"*) tmux -L onboard send-keys -t onboard Enter ;;
            *"Yes, I trust this folder"*|*"Yes, I accept"*) tmux -L onboard send-keys -t onboard Down Enter ;;
        esac
        sleep 6
    done
    [ "$state" = ready ] || { echo "$DIR: stuck on a screen:"; echo "$screen" | sed '/^[[:space:]]*$/d' | tail -8; }
    tmux -L onboard kill-server 2>/dev/null || true
    echo "$DIR: $state"
done
claude --version
