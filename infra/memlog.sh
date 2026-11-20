#!/bin/sh
# Sample memory every 5 s until killed: used MiB, available MiB, and the
# resident MiB of agent processes (codex, claude, omar).
# Usage: infra/memlog.sh <out.csv>
OUT=$1
[ -f "$OUT" ] || echo "time,used_mib,available_mib,agents_rss_mib,codex_procs" > "$OUT"
while :; do
    set -- $(free -m | awk '/^Mem:/ {print $3, $7}')
    rss=$(ps -eo rss=,comm= | awk '$2 ~ /^(codex|claude|node|omar)/ {s += $1} END {printf "%d", s / 1024}')
    n=$(pgrep -c -x codex || true)
    echo "$(date -u +%Y-%m-%dT%H:%M:%S),$1,$2,$rss,$n" >> "$OUT"
    sleep 5
done
