#!/usr/bin/env python3
"""
Generate the per-VM iperf3 execution plan for the FABRIC validation ladder.

Input: experiments/validation-ladder-schedule.json (the machine-readable mirror of
SCHEDULE in the CODES repo's scripts/fabric-validation-ladder-trace.py, which is
authoritative; see experiments/README.md).

Output: for every slice VM, the exact iperf3 server and client command lines and
the time offset (seconds after the run's T0) at which each client starts. Every
flow STEP becomes one fixed-rate UDP iperf3 client run:

    server (destination VM):  iperf3 -s -p <port> -1 --json --logfile <dir>/server-<title>.json
    client (source VM):       iperf3 -c <peer> -u -b <rate> -t <dur> -p <port> -i <report>
                                     [-P <n>] --title <title> --json --logfile <dir>/client-<title>.json

iperf3 cannot change its rate mid-run, so a stepped flow (rung 4, flow 401) is
several consecutive runs. Each run gets its own server port (servers are one-off,
`-1`, so a new port avoids racing the previous run's teardown) and its own
`--title`, which the emitted converter mapping matches on.

--scale multiplies every rate by the same factor. Sustaining 80 Gbps of UDP from
one VM may be infeasible; a uniform factor keeps the overload ratios of the ladder
(160/100, 240/100, 110/100 relative to the STAR->MICH link) but NOT the ratio to
the physical link: at --scale 0.1 nothing congests unless the bottleneck is
emulated too. Low --scale runs are for checking the tooling end to end.

With -P N, iperf3 applies -b PER STREAM; the plan divides the rate by N so the
aggregate matches the schedule.

--rungs selects a subset. Offsets are then relative to the first selected rung's
start, and the plan records `origin_interval`: convert with
`--t0 <T0 - origin_interval * interval_seconds>` so the trace's interval numbers
match the simulation's (see experiments/README.md).

Outputs (any combination):
    -o PLAN.json            the full plan (default: stdout)
    --format text           human-readable plan instead of JSON
    --scripts-dir DIR       one bash script per VM that has work: run-<vm>.sh <T0>
    --mapping-out MAP.json  converter mapping (trace/fabric-metrics-to-ffw-trace.py)

stdlib only; Python >= 3.8. Output is deterministic for given inputs and options.
"""

from __future__ import annotations

import argparse
import json
import math
import shlex
import sys
from fractions import Fraction
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_SCHEDULE = HERE / "validation-ladder-schedule.json"
TOOL = "experiments/make-iperf3-plan.py"
STEP_FLOW_ID_STRIDE = 1_000_000  # same default as the converter's --split-flow-id-stride


class PlanError(Exception):
    """Invalid schedule, hosts file, or options. The message is user-facing."""


def load_schedule(path: Path) -> dict:
    try:
        sched = json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise PlanError(f"cannot read schedule {path}: {e}") from e
    if sched.get("schema_version") != 1:
        raise PlanError(f"schedule {path}: schema_version must be 1")
    for key in ("interval_seconds", "sites", "rungs"):
        if key not in sched:
            raise PlanError(f"schedule {path}: missing '{key}'")
    return sched


def load_hosts(path: Path | None) -> dict:
    """VM name -> dataplane address (written by experiments/slice/build_ladder_slice.py hosts)."""
    if path is None:
        return {}
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise PlanError(f"cannot read hosts file {path}: {e}") from e
    hosts = data.get("hosts", data) if isinstance(data, dict) else None
    if not isinstance(hosts, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in hosts.items()
    ):
        raise PlanError(f"hosts file {path}: expected {{\"hosts\": {{vm: address}}}}")
    return hosts


def format_rate(bps: Fraction) -> str:
    """iperf3 -b value. Exact integer K/M/G suffix where possible, else integer bit/s."""
    for suffix, unit in (("G", 10**9), ("M", 10**6), ("K", 10**3)):
        q = bps / unit
        if q.denominator == 1 and q >= 1:
            return f"{q.numerator}{suffix}"
    return str(math.floor(bps + Fraction(1, 2)))


def parse_rungs(text: str | None, available: list[int]) -> list[int]:
    if text is None:
        return list(available)
    try:
        wanted = sorted({int(x) for x in text.split(",") if x.strip()})
    except ValueError as e:
        raise PlanError(f"--rungs: expected comma-separated rung numbers, got {text!r}") from e
    missing = [r for r in wanted if r not in available]
    if missing or not wanted:
        raise PlanError(f"--rungs: unknown rung(s) {missing or text!r}; schedule has {available}")
    return wanted


def build_plan(
    sched: dict,
    scale: float = 1.0,
    hosts: dict | None = None,
    rungs: list[int] | None = None,
    base_port: int = 5201,
    parallel: int = 1,
    report_interval: float = 1.0,
    results_dir: str = "results",
    title_prefix: str = "ladder",
) -> dict:
    if not (scale > 0 and math.isfinite(scale)):
        raise PlanError(f"--scale must be a positive number, got {scale}")
    if parallel < 1:
        raise PlanError("--parallel must be >= 1")
    if not 1 <= base_port <= 65535:
        raise PlanError("--base-port out of range")
    hosts = hosts or {}
    dt = sched["interval_seconds"]
    all_rungs = [r["rung"] for r in sched["rungs"]]
    selected = rungs if rungs is not None else all_rungs
    chosen = [r for r in sched["rungs"] if r["rung"] in selected]
    if not chosen:
        raise PlanError("no rungs selected")
    origin_interval = min(r["start_interval"] for r in chosen)
    origin_s = origin_interval * dt
    vms = {vm["vm"]: vm for site in sched["sites"] for vm in site["vms"]}
    scale_q = Fraction(str(scale))

    runs = []
    port = base_port
    for rung in chosen:
        for flow in rung["flows"]:
            src, dst = flow["source"], flow["destination"]
            for ep in (src, dst):
                if ep["vm"] not in vms:
                    raise PlanError(f"flow {flow['flow_id']}: unknown VM {ep['vm']!r}")
            for k, step in enumerate(flow["steps"]):
                if port > 65535:
                    raise PlanError("ran out of ports; lower --base-port")
                rate_bps = Fraction(str(step["rate_gbps"])) * 10**9 * scale_q
                per_stream = rate_bps / parallel
                title = f"{title_prefix}-r{rung['rung']}-f{flow['flow_id']}-s{k}"
                peer = hosts.get(dst["vm"], "{" + dst["vm"] + "}")
                server = [
                    "iperf3", "-s", "-p", str(port), "-1", "--json",
                    "--logfile", f"{results_dir}/server-{title}.json",
                ]
                client = [
                    "iperf3", "-c", peer, "-u", "-b", format_rate(per_stream),
                    "-t", str(step["duration_s"]), "-p", str(port),
                    "-i", f"{report_interval:g}",
                ]
                if parallel > 1:
                    client += ["-P", str(parallel)]
                client += [
                    "--title", title, "--json",
                    "--logfile", f"{results_dir}/client-{title}.json",
                ]
                runs.append(
                    {
                        "title": title,
                        "rung": rung["rung"],
                        "flow_id": flow["flow_id"],
                        "step": k,
                        # Converter flow id: step 0 keeps the schedule's id; later steps
                        # are separate FFW flows (see the README's stepped-flow note).
                        "trace_flow_id": flow["flow_id"] + k * STEP_FLOW_ID_STRIDE,
                        "source_vm": src["vm"],
                        "source_terminal": src["terminal"],
                        "source_terminal_name": src["terminal_name"],
                        "destination_vm": dst["vm"],
                        "destination_terminal": dst["terminal"],
                        "destination_terminal_name": dst["terminal_name"],
                        "offset_s": step["start_s"] - origin_s,
                        "duration_s": step["duration_s"],
                        "schedule_rate_gbps": step["rate_gbps"],
                        "rate_bps": float(rate_bps),
                        "per_stream_rate_bps": float(per_stream),
                        "port": port,
                        "server_command": shlex.join(server),
                        "client_command": shlex.join(client),
                    }
                )
                port += 1

    per_vm: dict[str, dict] = {}
    for run in runs:
        per_vm.setdefault(run["destination_vm"], {"servers": [], "clients": []})
        per_vm.setdefault(run["source_vm"], {"servers": [], "clients": []})
        per_vm[run["destination_vm"]]["servers"].append(
            {"title": run["title"], "port": run["port"], "command": run["server_command"]}
        )
        per_vm[run["source_vm"]]["clients"].append(
            {
                "title": run["title"],
                "offset_s": run["offset_s"],
                "duration_s": run["duration_s"],
                "command": run["client_command"],
            }
        )
    for work in per_vm.values():
        work["clients"].sort(key=lambda c: (c["offset_s"], c["title"]))

    end = max(r["offset_s"] + r["duration_s"] for r in runs)
    return {
        "plan_version": 1,
        "generated_by": TOOL,
        "schedule_source": sched.get("authoritative_source", {}),
        "interval_seconds": dt,
        "scale": scale,
        "parallel_streams": parallel,
        "report_interval_s": report_interval,
        "rungs": [r["rung"] for r in chosen],
        "origin_interval": origin_interval,
        "t0_note": (
            f"Offsets are seconds after the run's T0. Convert with --t0 <T0 - {origin_s}> "
            f"so trace intervals match the simulation's (interval {origin_interval} = T0)."
        ),
        "duration_s": end,
        "unresolved_hosts": sorted({r["destination_vm"] for r in runs} - set(hosts)),
        "runs": runs,
        "vms": {vm: per_vm[vm] for vm in sorted(per_vm)},
    }


def build_mapping(plan: dict) -> dict:
    """Converter mapping (schema_version 1, iperf3 section) matching runs by --title."""
    entries = []
    for run in plan["runs"]:
        entries.append(
            {
                "match": {"title": run["title"]},
                "flow_id": run["trace_flow_id"],
                "source_terminal": run["source_terminal_name"],
                "destination_terminal": run["destination_terminal_name"],
                "notes": (
                    f"rung {run['rung']} flow {run['flow_id']} step {run['step']}: "
                    f"{run['schedule_rate_gbps']:g} Gbps x {run['duration_s']} s (unscaled)"
                ),
            }
        )
    return {
        "schema_version": 1,
        "description": (
            f"Generated by {TOOL} for the FABRIC validation ladder. Matches iperf3 client "
            "results by --title. Independent of --scale (titles and flow ids do not "
            "depend on it)."
        ),
        "iperf3": entries,
    }


def render_text(plan: dict) -> str:
    out = [
        f"FABRIC validation ladder iperf3 plan: rungs {plan['rungs']}, scale {plan['scale']:g}, "
        f"{plan['parallel_streams']} stream(s)/client, {plan['duration_s']} s",
        plan["t0_note"],
    ]
    if plan["unresolved_hosts"]:
        out.append(
            "UNRESOLVED peers (pass --hosts): " + ", ".join(plan["unresolved_hosts"])
        )
    for vm, work in plan["vms"].items():
        out.append("")
        out.append(f"== {vm}")
        for s in work["servers"]:
            out.append(f"  server (start before T0):  {s['command']}")
        for c in work["clients"]:
            out.append(f"  client at T0+{c['offset_s']:>4} s:     {c['command']}")
    return "\n".join(out) + "\n"


SCRIPT_TEMPLATE = """#!/usr/bin/env bash
# Generated by {tool} -- do not edit; regenerate instead.
# UNTESTED on FABRIC -- requires FABRIC project access (see docs/access-checklist.md).
#
# VM {vm}: rungs {rungs}, scale {scale:g}. Usage: ./run-{vm}.sh <T0 unix seconds>
# Start this script on EVERY VM of the plan before T0 (servers must be listening).
# Pick T0 at least ~60 s in the future, e.g. T0=$(( $(date +%s) + 120 )).
set -euo pipefail
T0=${{1:?usage: $0 <T0 unix seconds>}}
cd "$(dirname "$0")"
mkdir -p {results_dir}
{placeholder_check}
wait_until() {{  # wait_until <unix seconds>
    local now; now=$(date +%s)
    if (( now > $1 )); then echo "warning: $(( now - $1 )) s late for T0+$(( $1 - T0 ))" >&2; fi
    while (( $(date +%s) < $1 )); do sleep 0.2; done
}}
pids=()
{servers}
{clients}
wait "${{pids[@]}}"
echo "{vm}: done; results in {results_dir}/"
"""


def render_script(vm: str, work: dict, plan: dict, results_dir: str) -> str:
    servers = [f"{s['command']} &\npids+=($!)" for s in work["servers"]]
    clients = [
        f"wait_until $(( T0 + {c['offset_s']} ))\n{c['command']} &\npids+=($!)"
        for c in work["clients"]
    ]
    unresolved = any("{" in c["command"] for c in work["clients"])
    check = (
        'echo "error: this script still contains {vm} placeholders; regenerate with --hosts" >&2\nexit 2'
        if unresolved
        else ""
    )
    return SCRIPT_TEMPLATE.format(
        tool=TOOL,
        vm=vm,
        rungs=",".join(map(str, plan["rungs"])),
        scale=plan["scale"],
        results_dir=results_dir,
        placeholder_check=check,
        servers="\n".join(servers) if servers else "# (no servers on this VM)",
        clients="\n".join(clients) if clients else "# (no clients on this VM)",
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Generate per-VM iperf3 commands for the FABRIC validation ladder.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--schedule", type=Path, default=DEFAULT_SCHEDULE,
                   help="schedule JSON (default: experiments/validation-ladder-schedule.json)")
    p.add_argument("--scale", type=float, default=1.0,
                   help="multiply every rate by this factor (default 1.0)")
    p.add_argument("--hosts", type=Path,
                   help="JSON {\"hosts\": {vm: dataplane address}}; default: {vm} placeholders")
    p.add_argument("--rungs", help="comma-separated rung numbers (default: all)")
    p.add_argument("--base-port", type=int, default=5201, help="first server port (default 5201)")
    p.add_argument("--parallel", "-P", type=int, default=1,
                   help="iperf3 streams per client; -b is divided among them (default 1)")
    p.add_argument("--report-interval", type=float, default=1.0,
                   help="iperf3 -i reporting interval, s (default 1; must divide interval_seconds)")
    p.add_argument("--results-dir", default="results",
                   help="directory (relative to the script on the VM) for JSON logs")
    p.add_argument("--format", choices=["json", "text"], default="json")
    p.add_argument("-o", "--output", type=Path, help="plan output (default: stdout)")
    p.add_argument("--scripts-dir", type=Path, help="write run-<vm>.sh per VM here")
    p.add_argument("--mapping-out", type=Path, help="write the converter mapping JSON here")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        sched = load_schedule(args.schedule)
        hosts = load_hosts(args.hosts)
        rungs = parse_rungs(args.rungs, [r["rung"] for r in sched["rungs"]])
        dt = sched["interval_seconds"]
        if args.report_interval <= 0 or (dt / args.report_interval) % 1 != 0:
            raise PlanError(
                f"--report-interval {args.report_interval} must divide interval_seconds {dt}"
            )
        plan = build_plan(
            sched, scale=args.scale, hosts=hosts, rungs=rungs, base_port=args.base_port,
            parallel=args.parallel, report_interval=args.report_interval,
            results_dir=args.results_dir,
        )
    except PlanError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    text = render_text(plan) if args.format == "text" else json.dumps(plan, indent=1) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text)
    else:
        sys.stdout.write(text)
    if args.scripts_dir:
        args.scripts_dir.mkdir(parents=True, exist_ok=True)
        for vm, work in plan["vms"].items():
            path = args.scripts_dir / f"run-{vm}.sh"
            path.write_text(render_script(vm, work, plan, args.results_dir))
            path.chmod(0o755)
    if args.mapping_out:
        args.mapping_out.parent.mkdir(parents=True, exist_ok=True)
        args.mapping_out.write_text(json.dumps(build_mapping(plan), indent=2) + "\n")
    if plan["unresolved_hosts"]:
        print(
            "warning: no --hosts address for " + ", ".join(plan["unresolved_hosts"])
            + "; commands use {vm} placeholders",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
