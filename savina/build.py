#!/usr/bin/env python3
"""Render both arms of each Savina benchmark from spec.py.

  specs/<bench>.json      agent arm: one prompt per agent plus the seed messages,
                          for the runner's `--kind custom`
  programs/<Bench>.omar   DSL arm, one file per backend: .omar (Claude),
                          .codex.omar, .stub.omar (harness dry runs)

Usage: python3 savina/build.py
"""
import json
import os

import spec

HERE = os.path.dirname(os.path.abspath(__file__))
BACKENDS = {"Claude": ".omar", "Codex": ".codex.omar", "Stub": ".stub.omar"}


def indent(text, pad):
    return "\n".join((pad + line) if line else "" for line in text.splitlines())


# ---------------------------------------------------------------- agent arm
def agent_specs():
    phil = {
        "name": "philosophers",
        "prompts": [spec.philosopher_agent_prompt(i) for i in range(1, spec.N + 1)]
        + [spec.arbitrator_agent_prompt()],
        "seeds": spec.philosophers_seeds(),
    }
    bank = {
        "name": "banking",
        "prompts": [spec.account_agent_prompt(i) for i in range(1, spec.N + 1)]
        + [spec.teller_agent_prompt()],
        "seeds": spec.banking_seeds(),
    }
    return {"philosophers": phil, "banking": bank}


# ------------------------------------------------------------------ DSL arm
def philosophers_program(backend):
    teams = []
    for i in range(1, spec.N + 1):
        left, right = spec.forks(i)
        job = spec.PHILOSOPHER_JOB.format(i=i, n=spec.N, left=left, right=right,
                                          lnb=(i - 2) % spec.N + 1, rnb=i % spec.N + 1,
                                          meals=spec.MEALS)
        prompt = (job + "\n\nThe arbitrator has given you both of your forks for this meal; "
                  "they go back when you answer. Do the meal below and set `done` to the answer only."
                  "\n\n$(grant)")
        teams.append(f'''team Philosopher{i}[agent : {backend}]
{{
    input grant : string
    output done : string

    // @title Eat one meal
    // @desc Runs only while the arbitrator has given this philosopher both of its forks.
    task agent(grant) -> done """
{indent(prompt, "        ")}
    """
}}''')
    q = ", ".join("[" + ", ".join(f"({a}, {b})" for a, b in
                                  (spec.meal(i, k) for k in range(1, spec.MEALS + 1))) + "]"
                  for i in range(1, spec.N + 1))
    ports_in = "\n".join(f"    input d{i} : string" for i in range(1, spec.N + 1))
    ports_out = "\n".join(f"    output g{i} : string" for i in range(1, spec.N + 1))
    triggers = ", ".join(f"d{i}" for i in range(1, spec.N + 1))
    effects = ", ".join(f"g{i}?" for i in range(1, spec.N + 1))
    writes = " ".join(f"g{i} = grants[{i - 1}].take();" for i in range(1, spec.N + 1))
    arbitrator = f'''team Arbitrator
{{
{ports_in}
{ports_out}
    output log : string
    timer start(1ms, 0)
    state holder : string = "{"0" * spec.N}"
    state eaten : string = "{"0" * spec.N}"
    state trace : string = ""

    // @title Hand out forks
    // @desc Frees a philosopher's forks when it finishes a meal, then gives both forks to every
    // philosopher with meals left whose forks are free, least-fed first. Ends the run after the
    // last meal. The trace records every grant and every finished meal for the checker.
    reaction(start, {triggers}) -> {effects}, log? {{=
        let _ = &start;
        const N: usize = {spec.N};
        const MEALS: u8 = {spec.MEALS};
        const Q: [[(u32, u32); {spec.MEALS}]; {spec.N}] = [{q}];
        let dones = [{triggers}];
        let mut holder: Vec<u8> = self.holder.bytes().map(|b| b - b'0').collect();
        let mut eaten: Vec<u8> = self.eaten.bytes().map(|b| b - b'0').collect();
        for i in 0..N {{
            if let Some(ans) = &dones[i] {{
                let p = (i + 1) as u8;
                for f in [i, (i + 1) % N] {{
                    if holder[f] == p {{ holder[f] = 0; }}
                }}
                eaten[i] += 1;
                let clean: String = ans.chars().map(|c| if c == ';' || c == '\\n' {{ ' ' }} else {{ c }}).collect();
                self.trace.push_str(&format!("done {{}} {{}} {{}};", p, eaten[i], clean.trim()));
            }}
        }}
        let mut order: Vec<usize> = (0..N).collect();
        order.sort_by_key(|&i| (eaten[i], i));
        let mut grants: Vec<Option<String>> = vec![None; N];
        for &i in &order {{
            let p = (i + 1) as u8;
            let (l, r) = (i, (i + 1) % N);
            if eaten[i] < MEALS && holder[l] == 0 && holder[r] == 0 {{
                holder[l] = p;
                holder[r] = p;
                let k = eaten[i] as usize + 1;
                let (a, b) = Q[i][k - 1];
                grants[i] = Some(format!("Meal {{}}: what is {{}} + {{}}? Answer with the number only.", k, a, b));
                self.trace.push_str(&format!("grant {{}} {{}};", p, k));
            }}
        }}
        self.holder = holder.iter().map(|d| (b'0' + d) as char).collect();
        self.eaten = eaten.iter().map(|d| (b'0' + d) as char).collect();
        {writes}
        if eaten.iter().all(|&e| e == MEALS) {{
            log = Some(self.trace.clone());
            request_stop();
        }}
    =}}
}}'''
    wiring = []
    for i in range(1, spec.N + 1):
        wiring.append(f"    p{i} = Philosopher{i}()")
    for i in range(1, spec.N + 1):
        wiring.append(f"    a.g{i} -> p{i}.grant")
        wiring.append(f"    p{i}.done -> a.d{i} after 0")
    return (f"// Savina Philosophers (arbitrator variant), DSL arm, backend={backend}. "
            "Generated by savina/build.py.\n"
            + "\n\n".join(teams) + "\n\n" + arbitrator + "\n\nmain Philosophers {\n    a = Arbitrator()\n"
            + "\n".join(wiring) + "\n}\n")


def banking_program(backend):
    teams = []
    for i in range(1, spec.N + 1):
        job = spec.ACCOUNT_JOB.format(i=i)
        prompt = (job + "\n\nThe teller is asking about one transfer from your account. The request "
                  "gives the amount and your account's current balance. Set `yes` to let it go ahead "
                  "or `no` to refuse it; either way the value is a one-line reason.\n\n$(request)")
        # The stub cannot choose between two effects, so its dry-run twin always approves.
        contract = "yes" if backend == "Stub" else "(yes | no)"
        teams.append(f'''team Account{i}[agent : {backend}]
{{
    input request : string
    output yes : string
    output no : string

    // @title Approve or refuse a transfer
    // @desc Decides one transfer from this account, given its amount and the account's balance.
    task agent(request) -> {contract} """
{indent(prompt, "        ")}
    """
}}''')
    sched = ", ".join(f"({k}, {p}, {q}, {x})" for k, p, q, x in spec.schedule())
    ports_in = "\n".join(f"    input y{i} : string\n    input n{i} : string" for i in range(1, spec.N + 1))
    ports_out = "\n".join(f"    output r{i} : string" for i in range(1, spec.N + 1))
    triggers = ", ".join(f"y{i}, n{i}" for i in range(1, spec.N + 1))
    effects = ", ".join(f"r{i}?" for i in range(1, spec.N + 1))
    pairs = ", ".join(f"(y{i}.is_some(), n{i}.is_some())" for i in range(1, spec.N + 1))
    sends = "\n            ".join(f"{i} => r{i} = Some(msg)," for i in range(1, spec.N + 1))
    teller = f'''team Teller
{{
{ports_in}
{ports_out}
    output log : string
    timer start(1ms, 0)
    state bal : string = "{",".join([str(spec.START_BALANCE)] * spec.N)}"
    state k : int = 0
    state trace : string = ""

    // @title Run the transfers
    // @desc Sends each transfer, in order, to the paying account with that account's balance,
    // applies the account's verdict to the ledger, then sends the next. Ends the run after the
    // last one. The trace records every verdict and the final balances for the checker.
    reaction(start, {triggers}) -> {effects}, log? {{=
        const S: [(usize, usize, usize, i64); {spec.ROUNDS}] = [{sched}];
        let mut bal: Vec<i64> = self.bal.split(',').map(|s| s.parse().unwrap()).collect();
        let verdicts = [{pairs}];
        let mut advance = start.is_some() && self.k == 0;
        if self.k > 0 {{
            let (k, payer, payee, amount) = S[self.k as usize - 1];
            for (j, (y, n)) in verdicts.iter().enumerate() {{
                if !*y && !*n {{ continue; }}
                if j + 1 != payer {{
                    self.trace.push_str(&format!("T{{}} stray verdict from account {{}};", k, j + 1));
                    continue;
                }}
                if *y {{
                    bal[payer - 1] -= amount;
                    bal[payee - 1] += amount;
                }}
                self.trace.push_str(&format!("T{{}} {{}};", k, if *y {{ "approve" }} else {{ "deny" }}));
                advance = true;
            }}
        }}
        if advance && (self.k as usize) < S.len() {{
            self.k += 1;
            let (k, payer, payee, amount) = S[self.k as usize - 1];
            let msg = format!("Transfer T{{}}: pay ${{}} to account {{}}. Account {{}}'s current balance is ${{}}.",
                              k, amount, payee, payer, bal[payer - 1]);
            match payer {{
            {sends}
            _ => {{}}
            }}
        }} else if advance {{
            let b: Vec<String> = bal.iter().map(|x| x.to_string()).collect();
            log = Some(format!("{{}}balances {{}}", self.trace, b.join(",")));
            request_stop();
        }}
        let b: Vec<String> = bal.iter().map(|x| x.to_string()).collect();
        self.bal = b.join(",");
    =}}
}}'''
    wiring = [f"    a{i} = Account{i}()" for i in range(1, spec.N + 1)]
    for i in range(1, spec.N + 1):
        wiring.append(f"    t.r{i} -> a{i}.request")
        wiring.append(f"    a{i}.yes -> t.y{i} after 0")
        wiring.append(f"    a{i}.no -> t.n{i} after 0")
    return (f"// Savina Banking (bank transaction, one transfer at a time), DSL arm, backend={backend}. "
            "Generated by savina/build.py.\n"
            + "\n\n".join(teams) + "\n\n" + teller + "\n\nmain Banking {\n    t = Teller()\n"
            + "\n".join(wiring) + "\n}\n")


def main():
    os.makedirs(os.path.join(HERE, "specs"), exist_ok=True)
    os.makedirs(os.path.join(HERE, "programs"), exist_ok=True)
    for name, s in agent_specs().items():
        with open(os.path.join(HERE, "specs", f"{name}.json"), "w") as f:
            json.dump(s, f, indent=1)
    for backend, suffix in BACKENDS.items():
        with open(os.path.join(HERE, "programs", f"Philosophers{suffix}"), "w") as f:
            f.write(philosophers_program(backend))
        with open(os.path.join(HERE, "programs", f"Banking{suffix}"), "w") as f:
            f.write(banking_program(backend))
    decisions, final = spec.expected_banking()
    print(f"banking: {sum(not ok for ok in decisions.values())} of {spec.ROUNDS} transfers should be "
          f"denied; final balances {final}")


if __name__ == "__main__":
    main()
