"""omar-bench command line."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from . import config, harbor, ledger, pricing, report, usage
from .config import ROOT

RUNS = ROOT / "runs"
LEDGER = ROOT / "results" / "attempts"
RUN_RECORDS = ROOT / "results" / "runs"
NAME = r"[A-Za-z0-9_.-]+"
EXCLUSIONS = ROOT / "results" / "exclusions.toml"
TOOLS = ("docker", "uvx", "tmux", "git", "claude", "codex", "omar", "omarc", "aws")
# omarc has no version flag at all: "?" for it means present, version unknown.
VERSION_FLAG = {"tmux": "-V"}
# Harbor drives every task through `docker compose`; task images need BuildKit to build.
DOCKER_PLUGINS = ("compose", "buildx")
# Recorded by name only; a value never reaches a manifest.
CREDENTIALS = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
               "OPENAI_API_KEY", "CODEX_FORCE_AUTH_JSON", "CODEX_AUTH_JSON_PATH")


def _version(tool: str) -> str | None:
    path = shutil.which(tool)
    if not path:
        return None
    # On a Mac without the developer tools, /usr/bin/git is a stub that opens an install dialog.
    if sys.platform == "darwin" and path.startswith("/usr/bin/") and subprocess.run(
            ["xcode-select", "-p"], capture_output=True).returncode != 0:
        return None
    try:
        done = subprocess.run([tool, VERSION_FLAG.get(tool, "--version")],
                              capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return "?"
    return (done.stdout or done.stderr).strip().splitlines()[0] if done.returncode == 0 else "?"


def _docker_plugin(name: str) -> str | None:
    if not shutil.which("docker"):
        return None
    try:
        done = subprocess.run(["docker", name, "version"], capture_output=True, text=True,
                              timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return None
    out = done.stdout.strip()
    return out.splitlines()[0] if done.returncode == 0 and out else None


def _ec2() -> dict:
    """Instance type and AMI from instance metadata; empty off EC2."""
    base = "http://169.254.169.254/latest/"
    try:
        token = urllib.request.urlopen(urllib.request.Request(
            base + "api/token", method="PUT",
            headers={"X-aws-ec2-metadata-token-ttl-seconds": "60"}), timeout=1).read().decode()
        return {key: urllib.request.urlopen(urllib.request.Request(
            base + "meta-data/" + key, headers={"X-aws-ec2-metadata-token": token}),
            timeout=1).read().decode() for key in ("instance-type", "ami-id")}
    except OSError:
        return {}


def _harness_commit() -> str | None:
    """This repo's commit, with +dirty when the tree differs from it."""
    if not shutil.which("git") or not (ROOT / ".git").exists():
        return None
    def git(*args):
        return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True)
    head = git("rev-parse", "HEAD").stdout.strip()
    return head + ("+dirty" if git("status", "--porcelain").stdout.strip() else "")


def _dotenv(path: Path = ROOT / ".env") -> dict[str, str]:
    """KEY=VALUE lines, as both suites' own `.env` files write them (`export` optional)."""
    out = {}
    if path.exists():
        for line in path.read_text().splitlines():
            key, sep, value = line.strip().removeprefix("export ").partition("=")
            if not sep or not key.strip() or key.lstrip().startswith("#"):
                continue
            value = value.strip()
            if value[:1] in ("'", '"'):
                value = value[1:].split(value[0])[0]
            else:
                value = value.split(" #")[0].strip()  # a trailing comment is not part of the value
            out[key.strip()] = value
    return out


def _env() -> dict[str, str]:
    """The environment a run sees. `.env` wins, as it does when Harbor loads it with --env-file."""
    return {**os.environ, **_dotenv()}


def _auth(backend: str, env: dict[str, str]) -> tuple[str, dict[str, str]]:
    """What the agents bill to, and the switch that makes Harbor and the adapter honor it.

    Declared once in OMAR_BENCH_AUTH, never inferred: with a subscription token
    and an API key both present (one usually is, for a grader), Harbor's default
    is to spend the API key.
    """
    mode = env.get("OMAR_BENCH_AUTH", "")
    if mode not in ("subscription", "api-key"):
        raise config.ConfigError("set OMAR_BENCH_AUTH in .env to `subscription` or `api-key`: "
                                 "what the agents bill to")
    if backend == "claude":
        switch = "CLAUDE_FORCE_OAUTH"
        login = bool(env.get("CLAUDE_CODE_OAUTH_TOKEN"))
        key = bool(env.get("ANTHROPIC_API_KEY") or env.get("ANTHROPIC_AUTH_TOKEN"))
        how = "CLAUDE_CODE_OAUTH_TOKEN in .env (`claude setup-token`)"
    else:
        switch = "CODEX_FORCE_AUTH_JSON"
        login = bool(env.get("CODEX_AUTH_JSON_PATH")) or (Path.home() / ".codex/auth.json").exists()
        key = bool(env.get("OPENAI_API_KEY"))
        how = "`codex login` on this machine"
    forced = env.get(switch, "").lower() in ("1", "true", "yes", "on")
    if mode == "subscription":
        if not login:
            raise config.ConfigError(f"OMAR_BENCH_AUTH=subscription needs {how}")
        if switch in env and not forced:
            raise config.ConfigError(f"{switch}={env[switch]} contradicts OMAR_BENCH_AUTH=subscription")
        return mode, {switch: "1"}
    if not key:
        raise config.ConfigError("OMAR_BENCH_AUTH=api-key needs this backend's API key in .env")
    if forced or (backend == "codex" and env.get("CODEX_AUTH_JSON_PATH")):
        raise config.ConfigError(f"{switch} or an auth.json path is set, which contradicts "
                                 "OMAR_BENCH_AUTH=api-key")
    return mode, {}


def _cells(cfg: dict, suite: str, arms: list[str], models: list[str]):
    for arm in arms or list(cfg["arms"]):
        for model in models or list(cfg["models"]):
            try:
                yield arm, model, config.cell(cfg, suite, arm, model), None
            except config.ConfigError as err:
                yield arm, model, None, str(err)


def cmd_doctor(args) -> int:
    print("tools")
    for tool in TOOLS:
        print(f"  {tool:8} {_version(tool) or 'MISSING'}")
    for plugin in DOCKER_PLUGINS:
        print(f"  docker {plugin:8} {_docker_plugin(plugin) or 'MISSING'}")
    env = _env()
    print("declared")
    for name in ("OMAR_BENCH_OPERATOR", "OMAR_BENCH_AUTH"):
        print(f"  {name:26} {env.get(name) or '-'}")
    print("credentials (names only)")
    for name in CREDENTIALS:
        print(f"  {name:26} {'set' if env.get(name) else '-'}")
    cfg = config.load()
    print(f"bench.toml sha256 {cfg['_sha256'][:12]}")
    missing = sorted({name for suite in cfg["suites"]
                      for _, _, c, _ in _cells(cfg, suite, [], []) if c is not None
                      for name in config.unpinned(c)})
    print(f"unpinned ({len(missing)}; `omar-bench plan SUITE` shows which cells need which)")
    for name in missing:
        print(f"  {name}")
    if args.topologies:
        if not shutil.which("omarc"):
            print("omarc MISSING: cannot compile topologies")
            return 1
        failed = 0
        with tempfile.TemporaryDirectory() as tmp:
            for source in sorted((ROOT / "topologies").glob("*.omar")):
                done = subprocess.run(["omarc", str(source), f"{tmp}/{source.stem}.json"],
                                      capture_output=True, text=True)
                print(f"  {source.name}: {'ok' if done.returncode == 0 else done.stderr.strip()}")
                failed += done.returncode != 0
        return 1 if failed else 0
    return 0


def cmd_plan(args) -> int:
    cfg = config.load()
    for arm, model, c, err in _cells(cfg, args.suite, args.arm, args.model):
        if c is None:
            print(f"# {arm} / {model}: SKIP {err}")
            continue
        missing = config.unpinned(c)
        print(f"# {arm} / {model}: " + (f"UNPINNED {', '.join(missing)}" if missing else "ready"))
        print(shlex.join(harbor.command(c, RUNS / "<run-id>" / "jobs", "<run-id>")), end="\n\n")
    return 0


def cmd_run(args) -> int:
    cfg = config.load()
    c = config.cell(cfg, args.suite, args.arm, args.model)
    missing = config.unpinned(c)
    if not c["model_cfg"]["id"]:
        raise config.ConfigError(f"models.{args.model}.id is empty; nothing to run")
    if missing and not args.allow_unpinned:
        print("refusing to run with unpinned values (set them in bench.toml, or pass "
              "--allow-unpinned for a smoke test):\n  " + "\n  ".join(missing), file=sys.stderr)
        return 2

    env = _env()
    # Results from several people end up in one ledger; each row says whose run it was.
    operator = env.get("OMAR_BENCH_OPERATOR", "")
    if not re.fullmatch(NAME, args.study):
        raise config.ConfigError("--study takes letters, digits, . _ -")
    if not re.fullmatch(NAME, operator):
        raise config.ConfigError("set OMAR_BENCH_OPERATOR in .env to your GitHub handle "
                                 "(letters, digits, . _ -); every run records who ran it")
    auth, switch = _auth(c["backend"], env)
    env |= switch

    now = datetime.now(timezone.utc)
    run_id = f"{now:%Y%m%dT%H%M%SZ}-{args.suite}-{args.arm}-{args.model}-{operator}"
    run_dir = RUNS / run_id
    run_dir.mkdir(parents=True)  # a run directory is never reused
    cmd = harbor.command(c, run_dir / "jobs", run_id, attempts=args.attempts,
                         concurrency=args.concurrency, tasks=args.tasks, limit=args.limit,
                         extra=args.harbor_args)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(ROOT), env.get("PYTHONPATH")]))
    topology = c["arm_cfg"].get("topology")
    topology_sha256 = (hashlib.sha256((ROOT / topology).read_bytes()).hexdigest()
                       if topology else None)
    manifest = {
        "run_id": run_id, "created_at": now.isoformat(), "operator": operator, "auth": auth,
        # Attempts are only ever pooled within a study, and only under one cell identity.
        "study": args.study,
        "cell_sha256": config.identity(c, auth=auth, topology_sha256=topology_sha256,
                                       harbor_args=list(args.harbor_args)),
        "suite": args.suite, "arm": args.arm, "model": args.model,
        "suite_label": c["suite_cfg"].get("label"),
        "model_id": c["model_cfg"]["id"],
        "config_sha256": cfg["_sha256"],
        "pins": {k: c[k] for k in ("suite_cfg", "arm_cfg", "model_cfg", "omar", "cli_version")},
        "unpinned": missing,
        "topology_sha256": topology_sha256,
        "selection": {"attempts": args.attempts or c["suite_cfg"]["attempts"],
                      "tasks": list(args.tasks), "limit": args.limit,
                      "concurrency": args.concurrency, "harbor_args": list(args.harbor_args)},
        "command": cmd,
        "harness_commit": _harness_commit(),
        "host": {"platform": platform.platform(), "python": platform.python_version(),
                 "tools": {t: _version(t) for t in TOOLS}, "ec2": _ec2()},
        "credentials_present": sorted(n for n in CREDENTIALS if env.get(n)),
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    (run_dir / "bench.toml").write_bytes(cfg["_raw"])  # the bytes this run was configured from

    print(shlex.join(cmd), flush=True)
    interrupted = False
    with open(run_dir / "harbor.log", "wb") as log:
        proc = subprocess.Popen(cmd, cwd=ROOT, env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT)
        while True:
            try:
                chunk = proc.stdout.read1(65536)
                if not chunk:
                    break
                sys.stdout.buffer.write(chunk)
                sys.stdout.buffer.flush()
                log.write(chunk)
            except KeyboardInterrupt:
                # Harbor got the same Ctrl-C. Keep draining so it can wind its trials down,
                # then account for whatever it had started.
                interrupted = True
        returncode = proc.wait()
    (run_dir / "exit.json").write_text(json.dumps(
        {"returncode": returncode, "interrupted": interrupted,
         "finished_at": datetime.now(timezone.utc).isoformat()}) + "\n")
    added, account = _ingest(run_dir)
    print(f"{run_id}: harbor exit {returncode}, {added} attempts added to "
          f"{LEDGER.relative_to(ROOT)}/" + ("" if account["complete"] else
          f" (INCOMPLETE: planned {account['planned']}, attempted {account['attempted']}, "
          f"{len(account['without_result'])} without a result)"))
    return returncode or (130 if interrupted else 0)


def _ingest(run_dir: Path) -> tuple[int, dict]:
    """One ended run into the ledger, priced from the bench.toml it ran under, not today's."""
    manifest = json.loads((run_dir / "manifest.json").read_text())
    snapshot = config.load(run_dir / "bench.toml")
    if snapshot["_sha256"] != manifest["config_sha256"]:
        raise config.ConfigError(f"{run_dir.name}: bench.toml there is not the one the run "
                                 "recorded in its manifest; refusing to price from it")
    rows = harbor.rows(run_dir, manifest, snapshot["prices"])
    added = ledger.append(LEDGER, manifest["run_id"], rows)
    account = harbor.account(run_dir, rows)
    ended = json.loads((run_dir / "exit.json").read_text())
    if ended.get("interrupted"):
        account["complete"] = False
    ledger.record(RUN_RECORDS, manifest["run_id"], {
        **{k: manifest[k] for k in ("run_id", "study", "suite", "arm", "model", "operator",
                                    "cell_sha256", "created_at", "selection")},
        "harbor_returncode": ended.get("returncode"),
        "interrupted": bool(ended.get("interrupted")), **account})
    return added, account


def cmd_ingest(args) -> int:
    if args.interrupted and not args.run_dir:
        raise config.ConfigError("--interrupted needs the run directories named: it declares "
                                 "them dead")
    status = 0
    for run_dir in map(Path, args.run_dir or sorted(p for p in RUNS.glob("*") if p.is_dir())):
        if not (run_dir / "exit.json").exists():
            if not args.interrupted:
                # Ingesting a live run would record its in-flight trials as cut off, for good.
                print(f"{run_dir.name}: no exit.json, so it is still running or was cut off. "
                      f"If it is dead: omar-bench ingest --interrupted {run_dir}", file=sys.stderr)
                status = 1
                continue
            (run_dir / "exit.json").write_text(json.dumps(
                {"returncode": None, "interrupted": True,
                 "declared_at": datetime.now(timezone.utc).isoformat()}) + "\n")
        added, account = _ingest(run_dir)
        print(f"{run_dir.name}: {added} new" + ("" if account["complete"] else " (incomplete run)"))
    return status


def cmd_report(args) -> int:
    rows = ledger.read(LEDGER)
    if args.suite:
        rows = [r for r in rows if r["suite"] == args.suite]
    if args.study:
        rows = [r for r in rows if r["study"] == args.study]
    records = ledger.records(RUN_RECORDS)
    # A run with no record of finishing its plan is not known to be complete.
    incomplete = {r["run_id"] for r in rows if not records.get(r["run_id"], {}).get("complete")}
    try:
        text = report.markdown(report.summarize(rows, report.exclusions(EXCLUSIONS), incomplete))
    except report.ReportError as err:
        print(f"omar-bench: {err}", file=sys.stderr)
        return 2
    if args.out:
        Path(args.out).write_text(text)
    print(text, end="")
    return 0


def cmd_usage(args) -> int:
    prices = config.load()["prices"]
    by_model = usage.scan(Path(args.path), pricing.long_over(prices))
    usd, unpriced = pricing.cost(by_model, prices)
    print(json.dumps({"by_model": by_model, "total": usage.total(by_model),
                      "cost_usd": usd, "unpriced_models": unpriced,
                      "prices_as_of": prices.get("as_of")}, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="omar-bench", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("doctor", help="tools, credentials and unpinned values on this machine")
    p.add_argument("--topologies", action="store_true", help="also compile topologies/ with omarc")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("plan", help="print the harbor command for each cell; runs nothing")
    p.add_argument("suite")
    p.add_argument("--arm", action="append", default=[])
    p.add_argument("--model", action="append", default=[])
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser("run", help="run one cell and append its attempts to the ledger")
    p.add_argument("suite")
    p.add_argument("--arm", required=True)
    p.add_argument("--model", required=True)
    p.add_argument("--study", required=True,
                   help="which result these attempts belong to, e.g. pilot; studies are never pooled")
    p.add_argument("-k", "--attempts", type=int, help="attempts per task (default: the suite's)")
    p.add_argument("-n", "--concurrency", type=int, default=1)
    p.add_argument("--tasks", action="append", default=[], help="task name glob; repeatable")
    p.add_argument("--limit", type=int, help="at most this many tasks")
    p.add_argument("--allow-unpinned", action="store_true", help="smoke tests only")
    p.add_argument("harbor_args", nargs="*", metavar="-- HARBOR_ARG",
                   help="after `--`: passed to harbor run as-is, and recorded in the manifest")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("ingest", help="read ended runs into the ledger (idempotent)")
    p.add_argument("run_dir", nargs="*")
    p.add_argument("--interrupted", action="store_true",
                   help="the named runs were cut off and will not finish: record their "
                        "unfinished attempts as errors")
    p.set_defaults(func=cmd_ingest)

    p = sub.add_parser("report", help="success rate, cost per success and Pareto frontier")
    p.add_argument("--suite")
    p.add_argument("--study")
    p.add_argument("--out")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("usage", help="tokens and cost for any Claude Code or Codex session logs")
    p.add_argument("path")
    p.set_defaults(func=cmd_usage)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except config.ConfigError as err:
        print(f"omar-bench: {err}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
