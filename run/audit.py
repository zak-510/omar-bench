#!/usr/bin/env python3
"""What each harness defect fixed at prereg-v8 did to the Savina trials run
before it, checked the same way for every trial: either model, either arm,
passed or failed. Nothing in the results it reads is changed.

  python3 run/audit.py RESULTS_DIR [RESULTS_DIR...] --out DIR [--mail DIR]

RESULTS_DIR holds one machine's results (what infra/sync.sh pull brings back).
--mail names a folder with, per machine, a copy of the runner's mail folders
(~/.omar/mass/runs): a run that was killed kept no delivery log, but its mail
is still there, and the log is rebuilt from it. For every trial it writes one
row to DIR/audit.csv:

  recorded   the outcome as scored when the trial ran
  replay     agent arm, from the trial's own delivery log played back through
             the runner (`omar-mass simulate --replay`):
               same        the v8 rules read every reply as the original rules did
               ends-early  under v8 the run ends at a DONE the original rules
                           read as a send; the rest of the record never happens
               diverges    under v8 a different message would have been
                           delivered at some point: only a rerun can say how
                           the trial would have gone
               no-log      the runner was killed at the time limit, the
                           delivery log went with it, and its mail was not
                           given either
  cause      the trial under the v8 checker and headings (savina/check.py),
             on the replayed run where there is one
  leaked     how many earlier agent-arm runs on that machine had been killed
             at their time limit by the time this trial started; each left its
             agents running
  extra      agent arm: the idle agents the runner started beside the real ones

The first three say which outcomes the rule and checker fixes change, and
whether the trial's own record is enough to say so. The last two say which
trials ran on a machine that was not as the pre-registration describes it.
"""
import collections
import csv
import glob
import json
import os
import re
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "savina"))
import check  # noqa: E402
import spec  # noqa: E402

BIN = os.environ.get("OMAR_MASS_BIN", os.path.join(ROOT, "runner", "target", "release", "omar-mass"))
FIELDS = ["machine", "folder", "model", "bench", "arm", "trial", "started_at", "secs", "listed_not_counted",
          "recorded", "recorded_reason", "replay", "replay_detail", "cause", "reason", "changed",
          "leaked", "extra"]


def simulate(bench, messages, rules):
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump({"messages": messages}, f)
    try:
        out = subprocess.run([BIN, "simulate", "--spec", os.path.join(ROOT, "savina", "specs", f"{bench}.json"),
                              "--max-messages", str(spec.FUSE[bench]), "--rules", rules, "--replay", f.name],
                             capture_output=True, text=True)
    finally:
        os.remove(f.name)
    if out.returncode:
        sys.exit(f"replay failed: {out.stderr[-300:]}")
    return json.loads(out.stdout)


def replayed(bench, record, row, killed=False):
    """Play one trial's delivery log back under both rule sets. The record of
    a killed run stops where it was killed, so there the replay running out of
    record is no difference between the rules."""
    messages = record.get("messages") or []
    if not messages:
        return "no-log", "", None
    v1 = simulate(bench, messages, "v1")
    # The replay is only evidence if, under the rules the trial ran with, it retraces the trial.
    retraced = (v1["replay"]["unused"] == 0 and not v1["replay"]["diverged"]
                and v1["reason"] == (row.get("detail") or v1["reason"])
                and (killed or not v1["replay"]["ran_out"]))
    if not retraced:
        return "unresolved", f"the replay does not retrace the record: {v1['reason']}", None
    v2 = simulate(bench, messages, "v2")
    r = v2["replay"]
    if r["diverged"]:
        return "diverges", f"at delivery {r['diverged']['delivery']} of {len(messages)}", None
    if r["ran_out"] and not killed:
        return "diverges", "under v8 the run goes on past its recorded end", None
    if r["unused"]:
        return "ends-early", f"{v2['reason']}; {r['unused']} recorded deliveries never happen", v2
    note = f"{r['reworded']} messages lose only their control lines" if r["reworded"] else ""
    return "same", note, v2


def from_mail(mail, machine, started_at, secs):
    """The delivery log of a killed run, rebuilt from the requests the runner
    wrote and the replies the agents wrote back. The run is the one whose
    first request was written just after the trial started. Its agents
    outlived it, so a reply written after the kill is one the runner never saw."""
    start = time.mktime(time.strptime(started_at, "%Y-%m-%dT%H:%M:%S")) - time.timezone
    killed_at = start + float(secs or 0)
    best = None
    for run in glob.glob(os.path.join(mail, machine, "*", "inbox")):
        requests = []
        for path in glob.glob(os.path.join(run, "*.json")):
            try:
                requests.append(json.load(open(path)))
            except ValueError:
                pass
        if requests:
            first = min(r["timestamp_ns"] for r in requests) / 1e9
            if 0 <= first - start < 600 and (best is None or first < best[0]):
                best = (first, run, requests)
    if not best:
        return None
    messages = []
    for r in sorted(best[2], key=lambda r: r["timestamp_ns"]):
        m = re.search(r"New message from (the experimenter|node (\d+)):\n\n(.*?)\n\nHow to act", r["payload"], re.S)
        to = re.search(r"-actor-(\d+)$", r["receiver"])
        if not m or not to:
            return None
        reply_path = os.path.join(os.path.dirname(best[1]), "outbox", r["id"] + ".json")
        reply = None
        if os.path.exists(reply_path) and os.path.getmtime(reply_path) <= killed_at:
            raw = open(reply_path).read()
            try:
                reply = json.loads(raw)["content"]
            except (ValueError, KeyError, TypeError):
                reply = raw
        messages.append({"from": int(m.group(2) or 0), "to": int(to.group(1)), "body": m.group(3), "reply": reply})
    return messages


def kills(folder_rows):
    """When each agent-arm run on a machine was killed at its time limit."""
    out = []
    for row in folder_rows:
        if row.get("arm") == "agent" and str(row.get("rc")) in ("-15", "-9", "timeout") and row.get("started_at"):
            start = time.mktime(time.strptime(row["started_at"], "%Y-%m-%dT%H:%M:%S"))
            out.append(start + float(row.get("secs") or 0))
    return sorted(out)


def main():
    out_dir = sys.argv[sys.argv.index("--out") + 1]
    mail = sys.argv[sys.argv.index("--mail") + 1] if "--mail" in sys.argv else None
    roots = [a for a in sys.argv[1:] if a not in ("--out", out_dir, "--mail", mail)]
    os.makedirs(out_dir, exist_ok=True)
    rows = []
    for root in roots:
        machine = os.path.basename(os.path.normpath(root))
        files = sorted(glob.glob(os.path.join(root, "**", "savina-*.csv"), recursive=True))
        files = [f for f in files if not f.endswith("-attempts.csv")]
        # Every agent-arm run on this machine that was killed, Savina or probe.
        killed = []
        for f in files + glob.glob(os.path.join(root, "**", "sweep-*-agent.csv"), recursive=True):
            if "/pilot/" not in f:
                killed += kills(csv.DictReader(open(f)))
        killed.sort()
        for f in files:
            folder = os.path.relpath(os.path.dirname(f), root)
            excl_path = os.path.join(os.path.dirname(f), "exclusions.csv")
            excluded = ({(e["unit"], e["arm"], e["trial"]) for e in csv.DictReader(open(excl_path))}
                        if os.path.exists(excl_path) else set())
            raw = f[:-4] + ".jsonl"
            records = ({json.loads(l)["trial"]: json.loads(l) for l in open(raw)} if os.path.exists(raw) else {})
            for row in csv.DictReader(open(f)):
                bench, arm, trial = row["bench"], row["arm"], int(row["trial"])
                record = records.get(trial, {})
                if arm == "agent":
                    rebuilt = None
                    if not record.get("messages") and mail and row.get("started_at"):
                        rebuilt = from_mail(mail, machine, row["started_at"], row.get("secs"))
                    if rebuilt:
                        # The record of a killed run ends where it was killed:
                        # compare only how the two rule sets read it up to there.
                        record = {"messages": rebuilt}
                        kind, detail, v2 = replayed(bench, record, {}, killed=True)
                        last = rebuilt[-1]
                        detail = (f"rebuilt from its mail: {sum(1 for m in rebuilt if m['reply'] is not None)} "
                                  f"deliveries answered, then node {last['to']} "
                                  + ("never answered" if last["reply"] is None else "answered and the run was killed")
                                  + (f"; {detail}" if detail else ""))
                    else:
                        kind, detail, v2 = replayed(bench, record, row)
                    if v2 is not None and rebuilt and v2["end"] != "done":
                        # Killed while one agent had not answered: that silence is all the run shows.
                        score = check.agent(bench, {**v2, "end": "no-reply" if rebuilt[-1]["reply"] is None
                                                    else "deadline", "completed": False})
                    elif v2 is not None:
                        score = check.agent(bench, v2)
                    elif kind == "no-log":
                        # Killed at the time limit: all that is known is that it did not finish.
                        score = {"success": False, "reason": "not finished",
                                 "cause": "runtime" if row["reason"] == "timed out" else
                                 check.agent(bench, None, rc=1, stderr=row.get("stderr", ""))["cause"]}
                    else:
                        score = check.agent(bench, {"messages": record.get("messages") or [],
                                                    "completed": row["completed"] == "True",
                                                    "end": {"done": "done", "went": "quiet", "fuse": "fuse",
                                                            "stal": "no-reply"}.get((row.get("detail") or "")[:4], ""),
                                                    "hops": row.get("deliveries")})
                else:
                    kind, detail = "", ""
                    score = check.dsl(bench, record.get("outputs") or {}, row["completed"] == "True",
                                      rc=int(row["rc"] or 0), stderr=row.get("stderr", ""))
                start = (time.mktime(time.strptime(row["started_at"], "%Y-%m-%dT%H:%M:%S"))
                         if row.get("started_at") else 0)
                recorded = "success" if row["success"] == "True" else "failure"
                now = "success" if score["success"] else "failure"
                rows.append({
                    "machine": machine, "folder": folder, "model": row["model"], "bench": bench, "arm": arm,
                    "trial": trial, "started_at": row.get("started_at", ""), "secs": row.get("secs", ""),
                    "listed_not_counted": "yes" if (bench, arm, str(trial)) in excluded else "",
                    "recorded": recorded, "recorded_reason": row.get("detail") or row.get("reason", ""),
                    "replay": kind, "replay_detail": detail, "cause": score["cause"],
                    "reason": score.get("reason", ""), "changed": "yes" if now != recorded else "",
                    "leaked": sum(1 for t in killed if t <= start),
                    "extra": spec.N + 1 if arm == "agent" else "",
                })
    with open(os.path.join(out_dir, "audit.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)

    print(f"{len(rows)} trials audited -> {os.path.join(out_dir, 'audit.csv')}\n")
    agent = [r for r in rows if r["arm"] == "agent"]
    print("agent arm, each trial's own delivery log replayed under the v8 rules:")
    for kind, n in collections.Counter(r["replay"] for r in agent).most_common():
        print(f"  {n:4}  {kind}")
    print("\noutcomes the v8 rules and checker change:")
    changed = [r for r in rows if r["changed"]]
    for r in changed:
        print(f"  {r['machine']} {r['folder']} {r['bench']} {r['arm']} trial {r['trial']}: "
              f"{r['recorded']} ({r['recorded_reason']}) -> {r['cause']} [{r['replay']}: {r['replay_detail']}]")
    if not changed:
        print("  none")
    print("\ntrials whose record cannot settle how they would have gone:")
    for r in rows:
        if r["replay"] in ("diverges", "unresolved", "no-log"):
            print(f"  {r['machine']} {r['folder']} {r['bench']} agent trial {r['trial']}: {r['replay']} "
                  f"{r['replay_detail']} (recorded: {r['recorded_reason']})"
                  + (" [listed as not counted]" if r["listed_not_counted"] else ""))
    print("\ntrials started on a machine with agents left by killed runs, by machine:")
    by = collections.defaultdict(lambda: [0, 0])
    for r in rows:
        by[r["machine"]][0] += 1
        by[r["machine"]][1] += r["leaked"] > 0
    for m, (n, dirty) in sorted(by.items()):
        print(f"  {m:12} {dirty:4} of {n}")
    print(f"\nagent-arm trials run with {spec.N + 1} idle extra agents: {len(agent)} of {len(agent)}")


if __name__ == "__main__":
    main()
