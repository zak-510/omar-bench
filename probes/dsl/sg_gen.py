#!/usr/bin/env python3
"""Generate scatter-gather DSL-arm programs (upstream OMAR runtime).

Prompts mirror arm A with the answer-bracket protocol swapped for set-port.
Questions arrive per-worker via --input, so one frozen program serves every
dose. Gather question is baked in: triggers are OR-semantics, so a question
input port would fire the reaction early.

Usage: python3 sg_gen.py {check9|blind9|check5} --backend Claude > SGCheck9.omar
"""
import sys

SG_Q = "What is 12 times 12? Give ONLY the number."
BLIND_GATHER_Q = "What is the vault code for terminal 7? Give ONLY the 4-digit code."

WORKER = '''team Worker(idx : int, nn : int)[agent : {backend}]
{{
    input q : string
    output report : string

    task agent(q) -> report
    "
        You are worker $(idx) of $(nn) answering a shared task independently. Task:

        $(q)

        Set `report` to your answer and nothing else.
    "
}}
'''

GATHER_HEAD = '''team Gather[agent : {backend}]
{{
{inputs}
    output answer : string

    task agent({triggers}) -> answer
    "
        Below are candidate final answers to the question. Decide which is most
        consistent and correct, then output that answer in full as your own.
        Reproduce the complete answer expression itself; never refer to a
        candidate by number or label (do not write 'Candidate 2' or 'Agent 4')
        and do not add a 'consensus' prefix. Set `answer` to that final answer.

        Question: {gather_q}
        Candidates:
{candidates}
    "
}}
'''

DEAD_WORKER = '''team DeadWorker(idx : int, nn : int)[agent : {backend}]
{{
    input q : string
    output report : string

    task agent(q) -> report? within(1ms)
    "
        You are worker $(idx) of $(nn) answering a shared task independently. Task:

        $(q)

        Set `report` to your answer and nothing else.
    "
}}
'''

# Arm A gather policies (sweep.py POLICIES) minus the "keep waiting" clauses:
# waiting is runtime-owned now, the decision half stays verbatim.
MISS_POLICIES = {
    "none": None,
    "all": "Answer only if you have reports from all 5 workers."
           " If any report is missing, do not answer.",
    "quorum3": "Answer if you have reports from at least 3 of the 5 workers."
               " With fewer than 3 reports, do not answer.",
}

GATHER_MISS = '''team Gather[agent : {backend}]
{{
{inputs}
    output answer : string

    task agent({triggers}) -> answer?
    "
        Below are candidate final answers to the question. Decide which is most
        consistent and correct, then output that answer in full as your own.
        Reproduce the complete answer expression itself; never refer to a
        candidate by number or label (do not write 'Candidate 2' or 'Agent 4')
        and do not add a 'consensus' prefix. Either set `answer` to that final
        answer, or complete without setting `answer` if you should not answer.

        Question: {gather_q}
        Candidates:
{candidates}
    "
}}
'''

def missing_program(backend, fail, policy):
    # First `fail` workers go missing (runner.rs convention), realized as an
    # unmeetable deadline: absorbed expiry -> <absent> at the gather.
    n = 5
    prose = MISS_POLICIES[policy]
    gather_q = SG_Q + (f" {prose}" if prose else "")
    inputs = "\n".join(f"    input r{i} : string" for i in range(1, n + 1))
    triggers = ", ".join(f"r{i}" for i in range(1, n + 1))
    candidates = "\n".join(f"        [Candidate {i}] $(r{i})" for i in range(1, n + 1))
    out = [WORKER.format(backend=backend)]
    if fail:
        out.append(DEAD_WORKER.format(backend=backend))
    out.append(GATHER_MISS.format(backend=backend, inputs=inputs,
                                  triggers=triggers, gather_q=gather_q,
                                  candidates=candidates))
    out.append(f"main SGMiss5{policy.capitalize()}F{fail} {{")
    for i in range(1, n + 1):
        team = "DeadWorker" if i <= fail else "Worker"
        out.append(f"    w{i} = {team}({i}, {n})")
    out.append("    g = Gather()")
    out.append("")
    for i in range(1, n + 1):
        out.append(f"    w{i}.report -> g.r{i}")
    out.append("}")
    return "\n".join(out) + "\n"

def program(kind, backend, fail=0):
    n = 5 if kind == "check5" else 9
    gather_q = BLIND_GATHER_Q if kind == "blind9" else SG_Q
    main = {"check9": "SGCheck9", "blind9": "SGBlind9", "check5": "SGCheck5"}[kind]
    if fail:
        main += f"Fail{fail}"
    # Missing workers are never instantiated; their reports interpolate as
    # <absent> in the gather's one invocation.
    live = list(range(fail + 1, n + 1))

    inputs = "\n".join(f"    input r{i} : string" for i in range(1, n + 1))
    triggers = ", ".join(f"r{i}" for i in range(1, n + 1))
    candidates = "\n".join(f"        [Candidate {i}] $(r{i})" for i in range(1, n + 1))

    out = [WORKER.format(backend=backend)]
    out.append(GATHER_HEAD.format(backend=backend, inputs=inputs,
                                  triggers=triggers, gather_q=gather_q,
                                  candidates=candidates))
    out.append(f"main {main} {{")
    for i in live:
        out.append(f"    w{i} = Worker({i}, {n})")
    out.append("    g = Gather()")
    out.append("")
    for i in live:
        out.append(f"    w{i}.report -> g.r{i}")
    out.append("}")
    return "\n".join(out) + "\n"

if __name__ == "__main__":
    kind = sys.argv[1]
    backend = sys.argv[sys.argv.index("--backend") + 1] if "--backend" in sys.argv else "Claude"
    fail = int(sys.argv[sys.argv.index("--fail") + 1]) if "--fail" in sys.argv else 0
    if kind == "missing5":
        policy = sys.argv[sys.argv.index("--policy") + 1] if "--policy" in sys.argv else "none"
        sys.stdout.write(missing_program(backend, fail, policy))
    else:
        sys.stdout.write(program(kind, backend, fail))
