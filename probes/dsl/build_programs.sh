#!/bin/sh
# Regenerate every DSL-arm program from gen.py / sg_gen.py, for each backend.
# The original study's programs are exactly these generators' output (Claude
# backend), so nothing here is hand-edited. Naming, as the sweeps expect:
#   Claude  X.omar            (chain-100 halves: Chain100a.claude.omar, Chain100b.claude.omar)
#   Codex   X.codex.omar
#   Stub    X.stub.omar       (harness dry runs; judged rings cannot be stubbed)
set -eu
cd "$(dirname "$0")"
rm -f ./*.omar

emit() { # backend suffix
    b=$1; s=$2
    python3 gen.py chain 20 --backend "$b" > "Chain20$s"
    python3 gen.py chain 50 --backend "$b" > "Chain50$s"
    if [ "$b" = Claude ]; then a=Chain100a.claude.omar; z=Chain100b.claude.omar
    else a="Chain100a$s"; z="Chain100b$s"; fi
    python3 gen.py chain 100 --backend "$b" --slice 1:50 > "$a"
    python3 gen.py chain 100 --backend "$b" --slice 51:100 > "$z"
    for n in 10 25 40; do
        [ "$b" = Stub ] || python3 gen.py ring-judged "$n" --backend "$b" > "RingJ$n$s"
        python3 gen.py ring-bounded "$n" --backend "$b" > "RingB$n$s"
    done
    python3 sg_gen.py check5 --backend "$b" > "SGcheck5$s"
    python3 sg_gen.py check9 --backend "$b" > "SGcheck9$s"
    python3 sg_gen.py blind9 --backend "$b" > "SGblind9$s"
    for spec in none:1 none:3 all:1 all:3 quorum3:1 quorum3:2 quorum3:3; do
        p=${spec%:*}; f=${spec#*:}
        python3 sg_gen.py missing5 --policy "$p" --fail "$f" --backend "$b" > "SGmiss5-$p-f$f$s"
    done
}

emit Claude .omar
emit Codex .codex.omar
emit Stub .stub.omar
ls ./*.omar | wc -l | tr -d ' '
