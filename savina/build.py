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
    buffer = {
        "name": "boundedbuffer",
        "prompts": [spec.producer_agent_prompt(p) for p in range(1, spec.PRODUCERS + 1)]
        + [spec.consumer_agent_prompt(c) for c in range(1, spec.CONSUMERS + 1)]
        + [spec.manager_agent_prompt()],
        "seeds": spec.boundedbuffer_seeds(),
    }
    return {"philosophers": phil, "banking": bank, "boundedbuffer": buffer}


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


MANAGER = r"""
        let _ = &start;
        const P: usize = @P@;
        const C: usize = @C@;
        const B: usize = @B@;
        const PER: usize = @PER@;
        let made = [@MADE@];
        let dones = [@DONES@];
        let clean = |s: &str| -> String {
            s.chars().map(|c| if c == ';' || c == '\n' || c == '\u{1e}' || c == '\u{1f}' { ' ' } else { c })
                .collect::<String>().trim().to_string()
        };
        let num = |s: &str| -> Vec<usize> { s.split(',').map(|x| x.parse().unwrap()).collect() };
        let join = |v: &Vec<usize>| v.iter().map(|x| x.to_string()).collect::<Vec<_>>().join(",");
        let mut buf: Vec<(usize, usize, String)> = self.buf.split('\u{1e}').filter(|e| !e.is_empty())
            .map(|e| {
                let f: Vec<&str> = e.splitn(3, '\u{1f}').collect();
                (f[0].parse().unwrap(), f[1].parse().unwrap(), f[2].to_string())
            })
            .collect();
        let mut next = num(&self.next);
        let mut asked = num(&self.asked);
        let mut hold = num(&self.hold); // p * 100 + k per consumer, 0 when idle
        for p in 0..P {
            if let Some(text) = &made[p] {
                buf.push((p + 1, asked[p], clean(text)));
                self.trace.push_str(&format!("put {} {};", p + 1, asked[p]));
                asked[p] = 0;
            }
        }
        for c in 0..C {
            if let Some(ans) = &dones[c] {
                self.trace.push_str(&format!("done {} {} {} {};", c + 1, hold[c] / 100, hold[c] % 100, clean(ans)));
                hold[c] = 0;
                self.finished += 1;
            }
        }
        let mut gives: Vec<Option<String>> = vec![None; C];
        for c in 0..C {
            if hold[c] == 0 && !buf.is_empty() {
                let (p, k, text) = buf.remove(0);
                hold[c] = p * 100 + k;
                gives[c] = Some(format!("Item P{}-{}: {}", p, k, text));
                self.trace.push_str(&format!("take {} {} {};", c + 1, p, k));
            }
        }
        let mut reqs: Vec<Option<String>> = vec![None; P];
        let mut room = B.saturating_sub(buf.len() + asked.iter().filter(|&&k| k > 0).count());
        let mut order: Vec<usize> = (0..P).collect();
        order.sort_by_key(|&p| (next[p], p));
        for &p in &order {
            if room > 0 && asked[p] == 0 && next[p] <= PER {
                asked[p] = next[p];
                reqs[p] = Some(format!("Make item P{}-{}.", p + 1, next[p]));
                next[p] += 1;
                room -= 1;
            }
        }
        self.next = join(&next);
        self.asked = join(&asked);
        self.hold = join(&hold);
        self.buf = buf.iter().map(|(p, k, t)| format!("{}\u{1f}{}\u{1f}{}", p, k, t)).collect::<Vec<_>>().join("\u{1e}");
        @WRITES@
        if self.finished == @TOTAL@ {
            log = Some(self.trace.clone());
            request_stop();
        }
"""


def boundedbuffer_program(backend):
    teams = []
    for p in range(1, spec.PRODUCERS + 1):
        items = "\n".join(spec.item_text(p, k) for k in range(1, spec.PER_PRODUCER + 1))
        prompt = (spec.PRODUCER_JOB.format(p=p, np=spec.PRODUCERS, per=spec.PER_PRODUCER)
                  + "\n\nYour items, in order:\n" + items
                  + "\n\nThe manager has room in the buffer for the item named below. Set `item` to "
                  "that item's question only, exactly as listed above.\n\n$(request)")
        teams.append(f'''team Producer{p}[agent : {backend}]
{{
    input request : string
    output item : string

    // @title Make one item
    // @desc Runs only when the manager has room in the buffer for this producer's next item.
    task agent(request) -> item """
{indent(prompt, "        ")}
    """
}}''')
    for c in range(1, spec.CONSUMERS + 1):
        prompt = (spec.CONSUMER_JOB.format(c=c, nc=spec.CONSUMERS)
                  + "\n\nThe manager has taken the item below out of the buffer for you. Set `done` "
                  "to the answer only.\n\n$(take)")
        teams.append(f'''team Consumer{c}[agent : {backend}]
{{
    input take : string
    output done : string

    // @title Work one item
    // @desc Answers the item the manager has taken out of the buffer for this consumer.
    task agent(take) -> done """
{indent(prompt, "        ")}
    """
}}''')
    P, C = spec.PRODUCERS, spec.CONSUMERS
    code = (MANAGER.replace("@P@", str(P)).replace("@C@", str(C)).replace("@B@", str(spec.BUFFER))
            .replace("@PER@", str(spec.PER_PRODUCER)).replace("@TOTAL@", str(spec.ROUNDS))
            .replace("@MADE@", ", ".join(f"i{p}" for p in range(1, P + 1)))
            .replace("@DONES@", ", ".join(f"d{c}" for c in range(1, C + 1)))
            .replace("@WRITES@", " ".join([f"r{p} = reqs[{p - 1}].take();" for p in range(1, P + 1)]
                                          + [f"g{c} = gives[{c - 1}].take();" for c in range(1, C + 1)])))
    ports = ([f"    input i{p} : string" for p in range(1, P + 1)] + [f"    input d{c} : string" for c in range(1, C + 1)]
             + [f"    output r{p} : string" for p in range(1, P + 1)] + [f"    output g{c} : string" for c in range(1, C + 1)])
    triggers = ", ".join([f"i{p}" for p in range(1, P + 1)] + [f"d{c}" for c in range(1, C + 1)])
    effects = ", ".join([f"r{p}?" for p in range(1, P + 1)] + [f"g{c}?" for c in range(1, C + 1)])
    manager = f'''team Manager
{{
{chr(10).join(ports)}
    output log : string
    timer start(1ms, 0)
    state buf : string = ""
    state next : string = "{",".join(["1"] * P)}"
    state asked : string = "{",".join(["0"] * P)}"
    state hold : string = "{",".join(["0"] * C)}"
    state finished : int = 0
    state trace : string = ""

    // @title Run the buffer
    // @desc Stores each item a producer makes, hands the oldest stored item to every idle
    // consumer, and asks producers for more only while the buffer has room for what is already
    // on its way. Ends the run after the last item is finished. The trace records every item
    // stored, handed out and finished, for the checker.
    reaction(start, {triggers}) -> {effects}, log? {{={code}    =}}
}}'''
    wiring = ([f"    p{p} = Producer{p}()" for p in range(1, P + 1)] + [f"    c{c} = Consumer{c}()" for c in range(1, C + 1)])
    for p in range(1, P + 1):
        wiring += [f"    m.r{p} -> p{p}.request", f"    p{p}.item -> m.i{p} after 0"]
    for c in range(1, C + 1):
        wiring += [f"    m.g{c} -> c{c}.take", f"    c{c}.done -> m.d{c} after 0"]
    return (f"// Savina BoundedBuffer (producer-consumer with a bounded buffer), DSL arm, backend={backend}. "
            "Generated by savina/build.py.\n"
            + "\n\n".join(teams) + "\n\n" + manager + "\n\nmain BoundedBuffer {\n    m = Manager()\n"
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
        with open(os.path.join(HERE, "programs", f"BoundedBuffer{suffix}"), "w") as f:
            f.write(boundedbuffer_program(backend))
    decisions, final = spec.expected_banking()
    print(f"banking: {sum(not ok for ok in decisions.values())} of {spec.ROUNDS} transfers should be "
          f"denied; final balances {final}")


if __name__ == "__main__":
    main()
