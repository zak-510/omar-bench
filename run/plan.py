"""The trial plan from PREREGISTRATION.md, read by run/schedule.py.

A unit is what one trial of one sweep runs, in one arm. `trials` is how many
the pre-registration asks for; `agents` the most agents a trial keeps alive at
once (what decides the machine it needs); `steps` and `calls` the sequential
agent turns and all agent turns one trial takes, read off its protocol;
`timeout` is the same in both arms.
"""
from collections import namedtuple

Unit = namedtuple("Unit", "name arm trials agents steps calls timeout argv")

PROBE_DSL = "probes/dsl/dsl_sweep.py"
PROBE_DSL41 = "probes/dsl/dsl41_sweep.py"
PROBE_AGENT = "probes/sweep.py"
SAVINA = "savina/sweep.py"

# One scatter-gather trial runs every corruption level: 5 with 5 workers, 9
# with 9, and the 9 levels of the blind control.
SG_RUNS = [(5, 5), (9, 9), (9, 9)]  # (workers, levels)
SG_AGENT = (sum(lv * (n + 1) for n, lv in SG_RUNS), sum(lv * 2 * n for n, lv in SG_RUNS))
SG_DSL = (sum(lv * 2 for n, lv in SG_RUNS), sum(lv * (n + 1) for n, lv in SG_RUNS))
# One missing-workers trial runs all 7 policy and failure settings, 5 workers.
MISSING_AGENT, MISSING_DSL = (7 * 5, 7 * 8), (7 * 2, 7 * 5)
# Fewest deliveries a correct agent-arm Savina run needs (savina/spec.py):
# Philosophers 5 seeds + 30 meals x (hungry, eat, done); Banking 1 seed +
# 24 approved x 3 + 6 refused x 2 + 5 balance reports x 2.
PHIL_AGENT, BANK_AGENT = 5 + 30 * 3, 1 + 24 * 3 + 6 * 2 + 5 * 2
# BoundedBuffer: 5 seeds + 30 items x (put, stored, take, done). DSL: 30 items
# made and 30 answered, two consumers working at once.
BUFFER_AGENT, BUFFER_DSL = 5 + 30 * 4, (30, 60)
# DSL arbitrator: at most 2 of 5 philosophers eat at once, so 30 meals take 15 rounds.
PHIL_DSL_ROUNDS = 15
# Philosophers-90: the same at 18 meals each.
PHIL90_AGENT, PHIL90_DSL = 5 + 90 * 3, (45, 90)


def units():
    out = []

    def pair(name, trials, agents_dsl, agent_cost, dsl_cost, timeout, agent_argv, dsl_argv,
             agents_agent=None):
        out.append(Unit(name, "agent", trials, agents_agent or agents_dsl, *agent_cost, timeout, agent_argv))
        out.append(Unit(name, "dsl", trials, agents_dsl, *dsl_cost, timeout, dsl_argv))

    pair("scatter-gather", 5, 10, SG_AGENT, SG_DSL, 900,
         [PROBE_AGENT, "--only", "sg"], [PROBE_DSL, "--families", "sg,sg-blind"])
    for n, trials, timeout in ((20, 30, 900), (50, 30, 1800), (100, 40, 2400)):
        pair(f"chain-{n}", trials, min(n, 50), (n, n), (n, n), timeout,
             [PROBE_AGENT, "--only", "chain", "--chain-ns", str(n)],
             [PROBE_DSL, "--families", "chain", "--chain-ns", str(n)], agents_agent=6)
    for n, timeout in ((10, 900), (25, 1800), (40, 3600)):
        pair(f"ring-{n}", 14, n, (2 * n, 2 * n), (2 * n, 2 * n), timeout,
             [PROBE_AGENT, "--only", "ring", "--ring-ns", str(n)],
             [PROBE_DSL41, "--families", "ring-judged", "--ring-ns", str(n)], agents_agent=6)
        out.append(Unit(f"ring-{n}-nostop", "dsl", 10, n, 2 * n, 2 * n, timeout,
                        [PROBE_DSL41, "--families", "ring-bounded", "--ring-ns", str(n)]))
    pair("missing-workers", 15, 6, MISSING_AGENT, MISSING_DSL, 900,
         [PROBE_AGENT, "--only", "sg-missing"], [PROBE_DSL41, "--families", "sg-missing"])
    pair("philosophers", 60, 6, (PHIL_AGENT, PHIL_AGENT), (PHIL_DSL_ROUNDS, 30), 7200,
         [SAVINA, "--bench", "philosophers", "--arm", "agent"],
         [SAVINA, "--bench", "philosophers", "--arm", "dsl"])
    pair("banking", 60, 6, (BANK_AGENT, BANK_AGENT), (30, 30), 7200,
         [SAVINA, "--bench", "banking", "--arm", "agent"],
         [SAVINA, "--bench", "banking", "--arm", "dsl"])
    pair("philosophers-90", 30, 6, (PHIL90_AGENT, PHIL90_AGENT), PHIL90_DSL, 14400,
         [SAVINA, "--bench", "philosophers-90", "--arm", "agent"],
         [SAVINA, "--bench", "philosophers-90", "--arm", "dsl"])
    pair("boundedbuffer", 60, 6, (BUFFER_AGENT, BUFFER_AGENT), BUFFER_DSL, 7200,
         [SAVINA, "--bench", "boundedbuffer", "--arm", "agent"],
         [SAVINA, "--bench", "boundedbuffer", "--arm", "dsl"])
    return out


def totals(selected=None):
    out = {}
    for u in selected or units():
        t = out.setdefault(u.arm, [0, 0, 0])
        t[0] += u.trials
        t[1] += u.trials * u.steps
        t[2] += u.trials * u.calls
    return out
