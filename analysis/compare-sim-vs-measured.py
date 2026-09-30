#!/usr/bin/env python3
"""
Phase 3 comparison: FFW simulation outputs vs FABRIC measurements, per interval.

Everything is reduced to per-interval VOLUME series (Gbit per FFW interval of
--interval-seconds) on the simulation's interval grid, then paired up, aligned and
scored. Rates in the summary are volume / interval_seconds (Gbps).

Subcommands
-----------
compare         align sim series with measured series; write the aligned table and
                summary statistics (and a plot if matplotlib is importable)
list-series     print the series names found in an FFW log directory
extract         write one sim series as a generic `time,value` CSV
iperf3-received turn iperf3 --json results into a `time,value` CSV of RECEIVED volume
                (measured-delivered series for `compare --measured`)

Simulation input (--sim DIR)
----------------------------
An FFW log directory as written by model-net-fluid-flow-wan-trace-traffic
(terminal_log_path / switch_log_path in the config), i.e. the model's committed
CSV logs:

  terminal-events.csv  interval,event,terminal,terminal_name,attached_switch,
                       peer_terminal,gbit        (column may be `mbit` instead)
  switch-events.csv    interval,event,switch,switch_name,port,target_type,
                       target_index,capacity_gbit,queued_before_gbit,sent_gbit,
                       queued_after_gbit,...,dropped_gbit,active_queue_entries

Series derived from them (rows sharing a key and interval are summed):

  offer:S->D   terminal `trace_offer` rows (trace volume added at source S for D)
  send:S->D    terminal `send` rows (volume source S put on its access link)
  recv:S->D    terminal `receive` rows at D with peer_terminal S (delivered)
  link:A->B    switch `egress` rows, switch A to switch B (e.g. link:STAR->MICH)
  link:A->tN   switch `egress` rows, switch A to its terminal N
  drop:A       switch `dropped_gbit`, summed over switch A's rows

S, D, N are FFW terminal ids (LOSA 0-1, SALT 2-3, STAR 4-5, NEWY 6-7, MICH 8-9).
Terminal logs carry no flow_id, so flows are identified by (source, destination)
pair; in the validation ladder no two concurrent flows share a pair.

Measured inputs
---------------
--measured-trace CSV   a converter trace (trace/fabric-metrics-to-ffw-trace.py
                       output: interval,flow_id,source_terminal,destination_terminal,
                       offered_gbit|offered_mbit). Summed by pair into offer:S->D.
                       This is the measured OFFERED series (iperf3 sender bytes).
--measured NAME=CSV    a generic `time,value` CSV: `value` is the volume in Gbit in
                       the bin that starts at `time` (seconds since --measured-t0,
                       default 0; use --measured-t0 <unix T0> for unix times). Each
                       row lands in interval floor((time - t0) / interval_seconds),
                       so measurement bins must nest inside FFW intervals (1 s, 2 s,
                       5 s or 10 s bins for 10 s intervals). The measured DELIVERED
                       series (e.g. from `iperf3-received`).
--measured-logs DIR    another FFW log directory, all of its series; for sim-vs-sim
                       comparisons (a perturbed run standing in for measurements).

Pairing: --compare SIM=MEASURED (repeatable) pairs explicitly; without it, every
series name present on both sides is compared (optionally filtered by --series
REGEX).

Delivery lag (--lag-shift)
--------------------------
The model moves a fluid segment one switch per interval, so volume sent in
interval i reaches the MICH terminals in interval i + 5 from LOSA and i + 4 from
SALT or NEWY (codes-director-ml doc/example/fluid-flow-wan-fabric-validation-ladder.md,
"Reading the output: delivery lag"); real one-way latency is milliseconds.
--lag-shift K moves every sim receiver-side series (recv:*) EARLIER by K
intervals (aligned[i] = sim[i + K]) before comparison. --lag-shift NAME=K sets K
for one sim series (any kind, e.g. link:STAR->MICH) and overrides the global K.
Measured series are never shifted.

Statistics (per pair)
---------------------
An interval is ACTIVE when either side exceeds --active-threshold Gbit. Over the
active intervals: bias = mean(sim - measured), rmse, mean absolute relative error
(|sim - measured| / measured, over active intervals with measured > threshold),
and the relative error of the totals. Volumes are Gbit per interval; *_gbps
columns divide by interval_seconds. With --schedule (experiments/
validation-ladder-schedule.json) the summary also gives each pair's mean and
peak rate over each rung's send window (after lag shift), which is where shares
such as 50/50 and the bottleneck's peak load show up.

stdlib only (Python >= 3.8); `--plot` uses matplotlib when importable and is
skipped with a note otherwise. Output is deterministic.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import re
import sys
from collections import defaultdict
from pathlib import Path

TOOL = "analysis/compare-sim-vs-measured.py"
EPS = 1e-9


class CompareError(Exception):
    """User-facing error."""


# --------------------------------------------------------------------------- reading


def _open_csv(path: Path):
    try:
        text = Path(path).read_text()
    except OSError as e:
        raise CompareError(f"cannot read {path}: {e}") from e
    lines = [l for l in text.splitlines() if l.strip() and not l.lstrip().startswith("#")]
    if not lines:
        raise CompareError(f"{path}: empty")
    return list(csv.DictReader(io.StringIO("\n".join(lines))))


def _float(value, where):
    try:
        v = float(value)
    except (TypeError, ValueError) as e:
        raise CompareError(f"{where}: bad number {value!r}") from e
    if not math.isfinite(v):
        raise CompareError(f"{where}: non-finite {value!r}")
    return v


def _int(value, where):
    try:
        return int(value)
    except (TypeError, ValueError) as e:
        raise CompareError(f"{where}: bad integer {value!r}") from e


def _volume_column(fieldnames, path):
    for name, factor in (("gbit", 1.0), ("mbit", 1e-3)):
        if name in fieldnames:
            return name, factor
    raise CompareError(f"{path}: no gbit/mbit column in {fieldnames}")


def read_sim_logs(directory: Path) -> dict:
    """{series name: {interval: gbit}} from an FFW log directory."""
    directory = Path(directory)
    series: dict = defaultdict(lambda: defaultdict(float))
    term = directory / "terminal-events.csv"
    if not term.exists():
        raise CompareError(f"{directory}: no terminal-events.csv (is this an FFW log directory?)")
    rows = _open_csv(term)
    col, factor = _volume_column(rows[0].keys() if rows else [], term)
    kinds = {"trace_offer": "offer", "send": "send", "receive": "recv"}
    for n, r in enumerate(rows, start=2):
        where = f"{term.name}:{n}"
        kind = kinds.get(r.get("event"))
        if kind is None:
            continue
        i = _int(r["interval"], where)
        terminal, peer = _int(r["terminal"], where), _int(r["peer_terminal"], where)
        src, dst = (peer, terminal) if kind == "recv" else (terminal, peer)
        series[f"{kind}:{src}->{dst}"][i] += _float(r[col], where) * factor

    sw = directory / "switch-events.csv"
    if sw.exists():
        rows = _open_csv(sw)
        names = {}
        for r in rows:
            names[r["switch"]] = r["switch_name"]
        for n, r in enumerate(rows, start=2):
            where = f"{sw.name}:{n}"
            i = _int(r["interval"], where)
            drop = _float(r.get("dropped_gbit", 0) or 0, where)
            if drop:
                series[f"drop:{r['switch_name']}"][i] += drop
            if r.get("event") != "egress":
                continue
            if r["target_type"] == "switch":
                target = names.get(r["target_index"], f"s{r['target_index']}")
            else:
                target = f"t{r['target_index']}"
            series[f"link:{r['switch_name']}->{target}"][i] += _float(r["sent_gbit"], where)
    return {k: dict(v) for k, v in series.items()}


def read_trace(path: Path) -> dict:
    """Converter/FFW trace CSV -> {offer:S->D: {interval: gbit}}."""
    rows = _open_csv(path)
    if not rows:
        raise CompareError(f"{path}: no data rows")
    fields = list(rows[0].keys())
    if "offered_gbit" in fields:
        col, factor = "offered_gbit", 1.0
    elif "offered_mbit" in fields:
        col, factor = "offered_mbit", 1e-3
    else:
        raise CompareError(f"{path}: not a trace CSV (no offered_gbit/offered_mbit column)")
    series: dict = defaultdict(lambda: defaultdict(float))
    for n, r in enumerate(rows, start=2):
        where = f"{Path(path).name}:{n}"
        key = f"offer:{_int(r['source_terminal'], where)}->{_int(r['destination_terminal'], where)}"
        series[key][_int(r["interval"], where)] += _float(r[col], where) * factor
    return {k: dict(v) for k, v in series.items()}


def read_time_value(path: Path, t0: float, dt: float, warnings: list) -> dict:
    """Generic time,value CSV -> {interval: gbit}."""
    rows = _open_csv(path)
    if not rows or set(rows[0].keys()) != {"time", "value"}:
        raise CompareError(f"{path}: expected header 'time,value'")
    out: dict = defaultdict(float)
    dropped = 0
    for n, r in enumerate(rows, start=2):
        where = f"{Path(path).name}:{n}"
        t, v = _float(r["time"], where), _float(r["value"], where)
        rel = (t - t0) / dt
        if rel < -EPS:
            dropped += 1
            continue
        out[int(math.floor(rel + EPS))] += v
    if dropped:
        warnings.append(f"{Path(path).name}: {dropped} row(s) before --measured-t0 ignored")
    return dict(out)


# --------------------------------------------------------------------------- aligning


def parse_lag_shifts(items) -> tuple[int, dict]:
    default, per = 0, {}
    for item in items or []:
        name, sep, value = item.rpartition("=")
        try:
            k = int(value)
        except ValueError as e:
            raise CompareError(f"--lag-shift {item!r}: expected K or NAME=K") from e
        if k < 0:
            raise CompareError(f"--lag-shift {item!r}: K must be >= 0")
        if sep:
            per[name] = k
        else:
            default = k
    return default, per


def lag_for(name: str, default: int, per: dict) -> int:
    if name in per:
        return per[name]
    return default if name.startswith("recv:") else 0


def shift(series: dict, k: int) -> dict:
    """aligned[i] = series[i + k]; volume shifted before interval 0 is kept at i < 0."""
    return {i - k: v for i, v in series.items()}


def pair_series(sim: dict, measured: dict, compare, pattern) -> list[tuple[str, str]]:
    if compare:
        pairs = []
        for item in compare:
            s, sep, m = item.partition("=")
            if not sep or not s or not m:
                raise CompareError(f"--compare {item!r}: expected SIM=MEASURED")
            pairs.append((s, m))
    else:
        common = sorted(set(sim) & set(measured), key=series_sort_key)
        if pattern:
            rx = re.compile(pattern)
            common = [n for n in common if rx.search(n)]
        pairs = [(n, n) for n in common]
    for s, m in pairs:
        if s not in sim:
            raise CompareError(f"sim series {s!r} not found (see list-series)")
        if m not in measured:
            raise CompareError(f"measured series {m!r} not found; have {sorted(measured)}")
    if not pairs:
        raise CompareError("nothing to compare: no series name is present on both sides")
    return pairs


def series_sort_key(name: str):
    kind, _, rest = name.partition(":")
    nums = [int(x) for x in re.findall(r"\d+", rest)]
    return (kind, nums, rest)


def score(sim: dict, meas: dict, lo: int, hi: int, dt: float, threshold: float) -> dict:
    diffs, rels = [], []
    sim_total = sum(sim.values())
    meas_total = sum(meas.values())
    for i in range(lo, hi + 1):
        s, m = sim.get(i, 0.0), meas.get(i, 0.0)
        if s > threshold or m > threshold:
            diffs.append(s - m)
            if m > threshold:
                rels.append(abs(s - m) / m)
    n = len(diffs)
    bias = sum(diffs) / n if n else 0.0
    rmse = math.sqrt(sum(d * d for d in diffs) / n) if n else 0.0
    return {
        "active_intervals": n,
        "sim_total_gbit": sim_total,
        "measured_total_gbit": meas_total,
        "total_rel_error": (sim_total - meas_total) / meas_total if meas_total else None,
        "bias_gbit": bias,
        "rmse_gbit": rmse,
        "bias_gbps": bias / dt,
        "rmse_gbps": rmse / dt,
        "mean_abs_rel_error": sum(rels) / len(rels) if rels else None,
        "max_abs_diff_gbit": max((abs(d) for d in diffs), default=0.0),
    }


def rung_windows(schedule_path: Path) -> list[dict]:
    try:
        sched = json.loads(Path(schedule_path).read_text())
        return [
            {"rung": r["rung"], "name": r["name"], "first": r["start_interval"],
             "last": r["start_interval"] + r["intervals"] - 1}
            for r in sched["rungs"]
        ]
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise CompareError(f"cannot read rung windows from {schedule_path}: {e}") from e


def compare(sim: dict, measured: dict, pairs, dt: float, lag_default: int, lag_per: dict,
            threshold: float, windows=None) -> dict:
    table, summary = [], []
    for s_name, m_name in pairs:
        k = lag_for(s_name, lag_default, lag_per)
        s = shift(sim[s_name], k)
        m = measured[m_name]
        keys = set(s) | set(m)
        lo, hi = min(keys), max(keys)
        for i in range(lo, hi + 1):
            sv, mv = s.get(i, 0.0), m.get(i, 0.0)
            table.append({
                "sim_series": s_name, "measured_series": m_name, "lag_shift": k,
                "interval": i, "time_s": i * dt, "sim_gbit": sv, "measured_gbit": mv,
                "diff_gbit": sv - mv,
            })
        stats = {"sim_series": s_name, "measured_series": m_name, "lag_shift": k}
        stats.update(score(s, m, lo, hi, dt, threshold))
        if windows:
            stats["rungs"] = []
            for w in windows:
                n = w["last"] - w["first"] + 1
                span = range(w["first"], w["last"] + 1)
                sv = [s.get(i, 0.0) / dt for i in span]
                mv = [m.get(i, 0.0) / dt for i in span]
                if max(sv) > threshold / dt or max(mv) > threshold / dt:
                    stats["rungs"].append({
                        "rung": w["rung"], "first": w["first"], "last": w["last"],
                        "sim_mean_gbps": sum(sv) / n, "measured_mean_gbps": sum(mv) / n,
                        "sim_max_gbps": max(sv), "measured_max_gbps": max(mv),
                    })
        summary.append(stats)
    return {"table": table, "summary": summary}


# --------------------------------------------------------------------------- output


def g(x, digits=6):
    if x is None:
        return ""
    text = f"{x:.{digits}f}".rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


def render_table(table) -> str:
    cols = ["sim_series", "measured_series", "lag_shift", "interval", "time_s",
            "sim_gbit", "measured_gbit", "diff_gbit"]
    out = [",".join(cols)]
    for r in table:
        out.append(",".join(
            g(r[c]) if isinstance(r[c], float) else str(r[c]) for c in cols
        ))
    return "\n".join(out) + "\n"


SUMMARY_COLS = ["sim_series", "measured_series", "lag_shift", "active_intervals",
                "sim_total_gbit", "measured_total_gbit", "total_rel_error", "bias_gbit",
                "rmse_gbit", "bias_gbps", "rmse_gbps", "mean_abs_rel_error",
                "max_abs_diff_gbit"]


def render_summary_csv(summary) -> str:
    out = [",".join(SUMMARY_COLS)]
    for s in summary:
        out.append(",".join(
            g(s[c]) if isinstance(s[c], float) or s[c] is None else str(s[c])
            for c in SUMMARY_COLS
        ))
    return "\n".join(out) + "\n"


def render_summary_text(summary, dt) -> str:
    out = [f"interval_seconds={g(dt)}; volumes in Gbit per interval, rates in Gbps"]
    w = max(len(f"{s['sim_series']} vs {s['measured_series']}") for s in summary)
    out.append(
        f"{'pair':<{w}}  lag  act  sim_total  meas_total  tot_rel  bias_Gbps  rmse_Gbps  MARE"
    )
    for s in summary:
        name = f"{s['sim_series']} vs {s['measured_series']}"
        rel = "" if s["total_rel_error"] is None else f"{s['total_rel_error']:+.3%}"
        mare = "" if s["mean_abs_rel_error"] is None else f"{s['mean_abs_rel_error']:.3%}"
        out.append(
            f"{name:<{w}}  {s['lag_shift']:>3}  {s['active_intervals']:>3}  "
            f"{s['sim_total_gbit']:>9.5g}  {s['measured_total_gbit']:>10.5g}  {rel:>7}  "
            f"{s['bias_gbps']:>+9.4g}  {s['rmse_gbps']:>9.4g}  {mare}"
        )
        for r in s.get("rungs", []):
            out.append(
                f"    rung {r['rung']} (intervals {r['first']}-{r['last']}): "
                f"mean sim {r['sim_mean_gbps']:.4g} / measured {r['measured_mean_gbps']:.4g} Gbps; "
                f"max sim {r['sim_max_gbps']:.4g} / measured {r['measured_max_gbps']:.4g} Gbps"
            )
    return "\n".join(out) + "\n"


def plot(result, dt, path: Path) -> str | None:
    """Returns None on success, or a reason string when plotting is unavailable."""
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception as e:  # ImportError, or a broken backend
        return f"matplotlib not importable ({e.__class__.__name__}); plot skipped"
    pairs = [(s["sim_series"], s["measured_series"]) for s in result["summary"]]
    fig, axes = plt.subplots(len(pairs), 1, figsize=(9, 1.9 * len(pairs) + 0.6),
                             sharex=True, squeeze=False)
    for ax, (sn, mn) in zip(axes[:, 0], pairs):
        rows = [r for r in result["table"] if r["sim_series"] == sn and r["measured_series"] == mn]
        # "post" steps: repeat the last value so the final interval gets its width.
        x = [r["interval"] for r in rows] + [rows[-1]["interval"] + 1]
        ys = [r["sim_gbit"] / dt for r in rows]
        ym = [r["measured_gbit"] / dt for r in rows]
        ax.step(x, ys + ys[-1:], where="post", label="sim")
        ax.step(x, ym + ym[-1:], where="post", label="measured", linestyle="--")
        ax.set_ylabel("Gbps")
        ax.set_title(sn if sn == mn else f"{sn} vs {mn}", fontsize=9, loc="left")
        ax.grid(alpha=0.3)
    axes[0, 0].legend(fontsize=8, loc="upper right")
    axes[-1, 0].set_xlabel(f"FFW interval ({g(dt)} s)")
    fig.tight_layout()
    fig.savefig(path, dpi=120)
    plt.close(fig)
    return None


# --------------------------------------------------------------------------- iperf3


def iperf3_received(paths, t0: float | None, warnings: list,
                    grid: float | None = None) -> list[tuple[float, float]]:
    """(time, gbit) rows of received volume per reporting interval, summed over files.

    Server results (`iperf3 -s --json`) report receiver-side intervals; a client
    result made with --get-server-output carries them under server_output_json.
    Times are unix seconds, or seconds since t0 when given. With `grid` (seconds),
    each reporting interval's volume is split across grid bins [k*grid, (k+1)*grid)
    in proportion to overlap (as the converter does), and times are bin starts;
    server reporting intervals do not start on whole seconds, so this avoids
    attributing a straddling interval entirely to one bin.
    """
    acc: dict = defaultdict(float)
    for path in paths:
        try:
            doc = json.loads(Path(path).read_text())
        except (OSError, ValueError) as e:
            raise CompareError(f"cannot read iperf3 JSON {path}: {e}") from e
        if isinstance(doc, dict) and doc.get("server_output_json"):
            doc = doc["server_output_json"]
        if not isinstance(doc, dict) or "start" not in doc or "intervals" not in doc:
            raise CompareError(f"{path}: not an iperf3 --json result")
        if doc.get("error"):
            raise CompareError(f"{path}: iperf3 error: {doc['error']}")
        stamp = doc["start"].get("timestamp", {})
        if "timemillisecs" in stamp:
            origin = stamp["timemillisecs"] / 1000.0
        elif "timesecs" in stamp:
            origin = float(stamp["timesecs"])
            warnings.append(f"{Path(path).name}: start time known to 1 s only")
        else:
            raise CompareError(f"{path}: no start.timestamp")
        n = 0
        for iv in doc["intervals"]:
            s = iv.get("sum", {})
            if s.get("sender") is not False:
                continue  # sender-side interval (client result): not a delivered volume
            a = origin + float(s["start"]) - (t0 or 0.0)
            b = origin + float(s["end"]) - (t0 or 0.0)
            gbit = float(s["bytes"]) * 8 / 1e9
            n += 1
            if grid is None or b <= a:
                acc[round(a if grid is None else math.floor(a / grid) * grid, 6)] += gbit
                continue
            k = math.floor(a / grid)
            while k * grid < b:
                lo, hi = max(a, k * grid), min(b, (k + 1) * grid)
                if hi > lo:
                    acc[round(k * grid, 6)] += gbit * (hi - lo) / (b - a)
                k += 1
        if n == 0:
            raise CompareError(
                f"{path}: no receiver-side intervals (use the SERVER result, or a client "
                "result made with --get-server-output)"
            )
    return sorted(acc.items())


# --------------------------------------------------------------------------- CLI


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Compare FFW simulation outputs with FABRIC measurements per interval.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("compare", help="align and score sim vs measured series")
    c.add_argument("--sim", type=Path, required=True, help="FFW log directory")
    c.add_argument("--interval-seconds", type=float, required=True)
    c.add_argument("--measured-trace", type=Path, action="append", default=[],
                   help="converter trace CSV -> offer:S->D (repeatable)")
    c.add_argument("--measured", action="append", default=[], metavar="NAME=CSV",
                   help="generic time,value CSV (Gbit per bin) as series NAME (repeatable)")
    c.add_argument("--measured-t0", type=float, default=0.0,
                   help="origin of the time column of --measured files (default 0)")
    c.add_argument("--measured-logs", type=Path, help="FFW log directory as measured stand-in")
    c.add_argument("--compare", action="append", default=[], metavar="SIM=MEASURED")
    c.add_argument("--series", metavar="REGEX", help="filter auto-paired series names")
    c.add_argument("--lag-shift", action="append", default=[], metavar="K|NAME=K",
                   help="shift sim receiver-side series earlier by K intervals")
    c.add_argument("--active-threshold", type=float, default=1e-6, metavar="GBIT")
    c.add_argument("--schedule", type=Path, help="schedule JSON: add per-rung mean rates")
    c.add_argument("-o", "--output", type=Path, help="aligned per-interval table CSV")
    c.add_argument("--summary-csv", type=Path, help="summary statistics CSV")
    c.add_argument("--summary-json", type=Path, help="summary statistics JSON (incl. rungs)")
    c.add_argument("--plot", type=Path, help="PNG plot (needs matplotlib; skipped otherwise)")

    ls = sub.add_parser("list-series", help="list series in an FFW log directory")
    ls.add_argument("--sim", type=Path, required=True)

    e = sub.add_parser("extract", help="one sim series as a time,value CSV")
    e.add_argument("--sim", type=Path, required=True)
    e.add_argument("--series", required=True)
    e.add_argument("--interval-seconds", type=float, required=True)
    e.add_argument("--lag-shift", type=int, default=0,
                   help="shift earlier by K intervals (emulate a lag-free measurement)")
    e.add_argument("-o", "--output", type=Path)

    r = sub.add_parser("iperf3-received", help="iperf3 JSON results -> time,value received Gbit")
    r.add_argument("inputs", nargs="+", type=Path)
    r.add_argument("--t0", type=float, help="subtract from unix times (e.g. the run's T0)")
    r.add_argument("--interval-seconds", type=float,
                   help="resample onto bins of this width from t0 (proportional split)")
    r.add_argument("-o", "--output", type=Path)
    return p


def write(text: str, path: Path | None):
    if path is None:
        sys.stdout.write(text)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    warnings: list = []
    try:
        if args.cmd == "list-series":
            sim = read_sim_logs(args.sim)
            for name in sorted(sim, key=series_sort_key):
                vals = sim[name]
                print(f"{name}\tintervals {min(vals)}-{max(vals)}\ttotal {g(sum(vals.values()), 3)} Gbit")
            return 0

        if args.cmd == "extract":
            sim = read_sim_logs(args.sim)
            if args.series not in sim:
                raise CompareError(f"series {args.series!r} not found (see list-series)")
            s = shift(sim[args.series], args.lag_shift)
            rows = ["time,value"] + [
                f"{g(i * args.interval_seconds)},{g(v)}" for i, v in sorted(s.items())
            ]
            write("\n".join(rows) + "\n", args.output)
            return 0

        if args.cmd == "iperf3-received":
            if args.interval_seconds is not None and not args.interval_seconds > 0:
                raise CompareError("--interval-seconds must be positive")
            rows = iperf3_received(args.inputs, args.t0, warnings, args.interval_seconds)
            write("\n".join(["time,value"] + [f"{g(t)},{g(v, 9)}" for t, v in rows]) + "\n",
                  args.output)
            for w in warnings:
                print(f"warning: {w}", file=sys.stderr)
            return 0

        dt = args.interval_seconds
        if not dt > 0:
            raise CompareError("--interval-seconds must be positive")
        sim = read_sim_logs(args.sim)
        measured: dict = {}
        if args.measured_logs:
            measured.update(read_sim_logs(args.measured_logs))
        traces: dict = defaultdict(lambda: defaultdict(float))
        for path in args.measured_trace:  # several traces (e.g. one per rung) are summed
            for name, s in read_trace(path).items():
                for i, v in s.items():
                    traces[name][i] += v
        for name, s in traces.items():
            if name in measured:
                warnings.append(f"{name}: --measured-trace replaces the --measured-logs series")
            measured[name] = dict(s)
        for item in args.measured:
            name, sep, path = item.partition("=")
            if not sep or not name or not path:
                raise CompareError(f"--measured {item!r}: expected NAME=CSV")
            measured[name] = read_time_value(Path(path), args.measured_t0, dt, warnings)
        if not measured:
            raise CompareError("no measured input: give --measured-trace, --measured or --measured-logs")
        lag_default, lag_per = parse_lag_shifts(args.lag_shift)
        pairs = pair_series(sim, measured, args.compare, args.series)
        windows = rung_windows(args.schedule) if args.schedule else None
        result = compare(sim, measured, pairs, dt, lag_default, lag_per,
                         args.active_threshold, windows)
    except CompareError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    for w in warnings:
        print(f"warning: {w}", file=sys.stderr)
    if args.output:
        write(render_table(result["table"]), args.output)
    if args.summary_csv:
        write(render_summary_csv(result["summary"]), args.summary_csv)
    if args.summary_json:
        write(json.dumps({"interval_seconds": dt, "pairs": result["summary"]}, indent=1) + "\n",
              args.summary_json)
    sys.stdout.write(render_summary_text(result["summary"], dt))
    if args.plot:
        reason = plot(result, dt, args.plot)
        print(reason if reason else f"wrote {args.plot}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
