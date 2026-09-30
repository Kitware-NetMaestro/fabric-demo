#!/usr/bin/env python3
"""
Convert FABRIC-side traffic measurements into a CODES fluid-flow WAN (FFW) trace CSV.

The output is the traffic trace read by the FFW trace-traffic front end
(load_traffic_trace_csv() in src/network-workloads/model-net-fluid-flow-wan.cxx of
the CODES repo; semantics in doc/example/fluid-flow-wan-trace-traffic.md there):

    interval,flow_id,source_terminal,destination_terminal,offered_gbit
    0,1001,0,8,0.2003

Each row adds `offered` volume (Gbit or Mbit, per the header) to the source backlog
of persistent flow `flow_id` at the start of FFW interval `interval`. Intervals are
`interval_seconds` wide, counted from a common time origin t0.

stdlib only; Python >= 3.8. Output is deterministic byte-for-byte for given inputs
and options (no generation timestamps; input files are named by basename only).

Two input modes, with deliberately different meanings
----------------------------------------------------
iperf3 (controlled validation)
    One iperf3 client JSON result (`iperf3 -c ... --json`) per flow. The volume of
    each FFW interval is taken from the client's per-interval SENDER byte counts
    (`intervals[].sum.bytes`), resampled onto the FFW grid (see "Resampling").
    With `--offered target`, the requested pacing rate
    (`start.test_start.target_bitrate`, i.e. `iperf3 --bitrate`) is used instead,
    as a constant rate over the span covered by the reported intervals.
    For fixed-rate UDP, sender bytes are the load the host actually put on the
    wire, independent of what the network then delivered or dropped: that is
    genuinely *offered* demand, which is what the FFW trace wants. For TCP the
    sender's byte count is already shaped by congestion control, i.e. by the very
    network being modeled, so using it as offered demand is circular; the tool
    accepts TCP but warns. This mode is designed for fixed-rate UDP experiments.

prometheus (achieved-as-offered replay)
    One or more Prometheus HTTP API `query_range` responses (resultType "matrix")
    for monotonic interface byte counters, e.g. node-exporter's
    node_network_transmit_bytes_total. Per-step counter deltas are resampled onto
    the FFW grid and replayed as demand. These are OBSERVED (achieved) volumes, so
    the trace replays what the network carried, not what applications wanted to
    send; it cannot show demand that was suppressed by congestion. This is the
    ESnet-trace-style use case, not a validation of the model's congestion
    response. A negative delta (counter reset: exporter restart, driver reload,
    wrap) is clamped to 0 with a warning; the true volume of that step is lost.

Mapping file (interface contract)
---------------------------------
A JSON file mapping measurement-side identifiers to FFW flows:

    {
      "schema_version": 1,                   required; must be 1
      "description": str,                    optional
      "iperf3": [ <entry>, ... ],            required for iperf3 mode
      "prometheus": [ <entry>, ... ]         required for prometheus mode
    }

    <entry> = {
      "match": {str: str, ...},              required, non-empty; see below
      "flow_id": int,                        required; 0 <= flow_id < 2**64, unique
                                             within its section
      "source_terminal": int | "SITE.k",     required
      "destination_terminal": int | "SITE.k",   required; != source_terminal
      "notes": str                           optional
    }

Unknown keys in an entry are an error (catches typos such as "flowid").

Terminals are FFW terminal ids, or names "SITE.k" (k-th terminal of site SITE)
resolved against the topology JSON (--topology, default topology/fabric-sites.json
in this repo). Ids follow the FFW parser: sites in JSON "sites" order, terminals
numbered sequentially, `terminals` per site (per-site override, else
defaults.terminals_per_site). For the 5-site subset: LOSA 0-1, SALT 2-3, STAR 4-5,
NEWY 6-7, MICH 8-9. Ids must be < the topology's terminal count.

`match` semantics:
  iperf3      keys from {"file", "title", "local_host", "remote_host"}; every given
              key must equal the input's value: "file" = the input file's basename,
              "title" = top-level "title" (`iperf3 --title`), local_host/remote_host
              = start.connected[0]. Each input must match exactly one entry, and no
              entry may match two inputs.
  prometheus  label subset: a series matches when every listed label (including
              "__name__" if given) equals the series' label. Each entry must match
              at most one series across all inputs (0 = warning, entry skipped);
              a series matching two entries is an error; series matching no entry
              (loopback, management NICs, ...) are ignored. Each matched series
              becomes one flow: interface counters aggregate all traffic on the
              interface and cannot be split per destination.

Resampling
----------
Every measurement is first turned into segments (t_start, t_end, bits): one per
iperf3 reporting interval, or one per pair of consecutive Prometheus samples.
Each segment's volume is assumed uniform over its span and split across the FFW
intervals [t0 + i*dt, t0 + (i+1)*dt) in proportion to overlap. This conserves
volume exactly (up to output rounding), except that volume before t0 is dropped
with a warning. Choose interval_seconds equal to, or an integer multiple of, the
measurement cadence (iperf3 -i, Prometheus step); a finer FFW grid only smears
each measurement uniformly and adds no information (the tool warns).

Validation (mirrors the FFW parser; the tool refuses to write an invalid trace)
------------------------------------------------------------------------------
  * header exactly interval,flow_id,source_terminal,destination_terminal,
    offered_mbit|offered_gbit; every row has 5 fields
  * interval integer >= 0 (and < --num-send-intervals when given)
  * flow_id unsigned integer; source/destination terminal ids in range, distinct
  * offered volume finite and STRICTLY positive, after output rounding
  * no duplicate (flow_id, interval); a flow keeps one (source, destination)
  * a flow has one row for EVERY interval from its first to its last: the FFW
    parser rejects gaps. Leading/trailing idle intervals are trimmed (a flow
    starts at its first non-zero interval and completes at its last). An idle
    interval INSIDE a flow is handled per --gap-policy: "error" (default) or
    "split", which ends the flow and continues it as a new flow with id
    flow_id + k * --split-flow-id-stride (k = 1, 2, ...). A split flow loses its
    rate-feedback state across the gap, which is also what a real idle period
    would do to a UDP sender.

Subcommands:
    fabric-metrics-to-ffw-trace.py iperf3 --mapping M.json --interval-seconds 1 R1.json R2.json -o T.csv
    fabric-metrics-to-ffw-trace.py prometheus --mapping M.json --interval-seconds 5 Q.json -o T.csv
    fabric-metrics-to-ffw-trace.py validate T.csv [--num-send-intervals N]
"""

from __future__ import annotations

import argparse
import datetime
import json
import math
import re
import sys
from fractions import Fraction
from pathlib import Path

TOOL = "trace/fabric-metrics-to-ffw-trace.py"
DEFAULT_TOPOLOGY = Path(__file__).resolve().parent.parent / "topology" / "fabric-sites.json"
HEADER_FIELDS = ["interval", "flow_id", "source_terminal", "destination_terminal"]
VOLUME_COLUMNS = {"mbit": "offered_mbit", "gbit": "offered_gbit"}
# Output decimals: 1e-6 Mbit = 1e-9 Gbit = 1 bit.
DECIMALS = {"mbit": 6, "gbit": 9}
MAX_FLOW_ID = 2**64 - 1
IPERF3_MATCH_KEYS = {"file", "title", "local_host", "remote_host"}
ENTRY_KEYS = {"match", "flow_id", "source_terminal", "destination_terminal", "notes"}
TERMINAL_NAME_RE = re.compile(r"^(.+)\.([0-9]+)$")


class TraceError(Exception):
    """Invalid input, mapping, or trace. The message is user-facing."""


class Warnings:
    def __init__(self, stream=None):
        self.messages: list[str] = []
        self.stream = stream

    def warn(self, msg: str) -> None:
        self.messages.append(msg)
        if self.stream is not None:
            print(f"warning: {msg}", file=self.stream)


# --------------------------------------------------------------------------- topology


def load_topology_terminals(path: Path) -> list[str]:
    """Terminal names ("SITE.k") indexed by FFW terminal id."""
    try:
        topo = json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise TraceError(f"cannot read topology JSON {path}: {e}") from e
    try:
        default = topo["defaults"]["terminals_per_site"]
        sites = topo["sites"]
    except (KeyError, TypeError) as e:
        raise TraceError(f"topology JSON {path}: missing defaults/sites ({e})") from e
    names = []
    for site in sites:
        count = site.get("terminals", default)
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise TraceError(f"topology JSON {path}: bad terminal count for {site.get('name')}")
        names.extend(f"{site['name']}.{k}" for k in range(count))
    if len(names) < 2:
        raise TraceError(f"topology JSON {path}: fewer than two terminals")
    return names


def resolve_terminal(value, terminals: list[str], where: str) -> int:
    if isinstance(value, bool):
        raise TraceError(f"{where}: expected terminal id or 'SITE.k', got {value!r}")
    if isinstance(value, int):
        if not 0 <= value < len(terminals):
            raise TraceError(
                f"{where}: terminal id {value} out of range [0,{len(terminals)}) for the topology"
            )
        return value
    if isinstance(value, str):
        if value in terminals:
            return terminals.index(value)
        raise TraceError(
            f"{where}: unknown terminal name {value!r} (known: {', '.join(terminals)})"
        )
    raise TraceError(f"{where}: expected terminal id or 'SITE.k', got {value!r}")


# --------------------------------------------------------------------------- mapping


def load_mapping(path: Path, mode: str, terminals: list[str]) -> list[dict]:
    """Validated entries of the `mode` section, terminals resolved to ids."""
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise TraceError(f"cannot read mapping file {path}: {e}") from e
    if not isinstance(data, dict):
        raise TraceError(f"mapping {path}: top level must be a JSON object")
    if data.get("schema_version") != 1:
        raise TraceError(f"mapping {path}: schema_version must be 1")
    raw_entries = data.get(mode)
    if not isinstance(raw_entries, list) or not raw_entries:
        raise TraceError(f"mapping {path}: needs a non-empty '{mode}' array for {mode} mode")

    entries = []
    seen_ids: dict[int, int] = {}
    for i, raw in enumerate(raw_entries):
        where = f"mapping {mode}[{i}]"
        if not isinstance(raw, dict):
            raise TraceError(f"{where}: expected an object")
        unknown = set(raw) - ENTRY_KEYS
        if unknown:
            raise TraceError(f"{where}: unknown key(s) {sorted(unknown)}")
        for key in ("match", "flow_id", "source_terminal", "destination_terminal"):
            if key not in raw:
                raise TraceError(f"{where}: missing required key '{key}'")
        match = raw["match"]
        if (
            not isinstance(match, dict)
            or not match
            or not all(isinstance(k, str) and isinstance(v, str) for k, v in match.items())
        ):
            raise TraceError(f"{where}.match: must be a non-empty object of string -> string")
        if mode == "iperf3":
            bad = set(match) - IPERF3_MATCH_KEYS
            if bad:
                raise TraceError(
                    f"{where}.match: unsupported iperf3 key(s) {sorted(bad)}; "
                    f"allowed: {sorted(IPERF3_MATCH_KEYS)}"
                )
        flow_id = raw["flow_id"]
        if isinstance(flow_id, bool) or not isinstance(flow_id, int):
            raise TraceError(f"{where}.flow_id: expected integer, got {flow_id!r}")
        if not 0 <= flow_id <= MAX_FLOW_ID:
            raise TraceError(f"{where}.flow_id: {flow_id} outside [0, 2**64)")
        if flow_id in seen_ids:
            raise TraceError(
                f"{where}.flow_id: {flow_id} already used by {mode}[{seen_ids[flow_id]}]; "
                "flow ids must be globally unique"
            )
        seen_ids[flow_id] = i
        src = resolve_terminal(raw["source_terminal"], terminals, f"{where}.source_terminal")
        dst = resolve_terminal(
            raw["destination_terminal"], terminals, f"{where}.destination_terminal"
        )
        if src == dst:
            raise TraceError(f"{where}: source and destination terminal are both {src}")
        entries.append(
            {"index": i, "match": dict(match), "flow_id": flow_id, "source": src, "destination": dst}
        )
    return entries


# --------------------------------------------------------------------------- inputs


def load_json(path: Path, what: str):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise TraceError(f"cannot read {what} {path}: {e}") from e


def num(value, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TraceError(f"{where}: expected a number, got {value!r}")
    if not math.isfinite(value):
        raise TraceError(f"{where}: non-finite value {value!r}")
    return float(value)


def parse_iperf3(path: Path, offered: str, warnings: Warnings) -> dict:
    """One iperf3 client result -> {identity..., "origin", "segments"}.

    origin is the test start (unix s, exact Fraction); segments are
    (start, end, bits) in seconds relative to origin. Keeping the epoch out of
    the float arithmetic avoids ~1e-7 s rounding at 1.8e9 s.
    """
    name = Path(path).name
    data = load_json(path, "iperf3 JSON")
    if not isinstance(data, dict) or "start" not in data or "intervals" not in data:
        raise TraceError(f"{name}: not an iperf3 --json result (no start/intervals)")
    if data.get("error"):
        raise TraceError(f"{name}: iperf3 reported an error: {data['error']}")
    start = data["start"]
    ts = start.get("test_start", {})
    stamp = start.get("timestamp", {})
    if "timemillisecs" in stamp:
        ms = num(stamp["timemillisecs"], f"{name}: start.timestamp.timemillisecs")
        origin = Fraction(ms) / 1000
    elif "timesecs" in stamp:
        origin = Fraction(num(stamp["timesecs"], f"{name}: start.timestamp.timesecs"))
        warnings.warn(f"{name}: no timemillisecs; start time known to 1 s only")
    else:
        raise TraceError(f"{name}: missing start.timestamp")
    protocol = ts.get("protocol")
    if protocol not in ("UDP", "TCP"):
        raise TraceError(f"{name}: unsupported protocol {protocol!r} (expected UDP or TCP)")
    if ts.get("reverse") or ts.get("bidir"):
        raise TraceError(
            f"{name}: reverse (-R) or bidirectional test; the client JSON then reports the "
            "receiving side. Run the client on the sending host instead."
        )
    if ts.get("omit"):
        raise TraceError(
            f"{name}: --omit warm-up intervals are not supported (their timeline restarts); "
            "rerun without --omit"
        )
    connected = start.get("connected") or [{}]
    identity = {
        "file": name,
        "title": data.get("title"),
        "local_host": connected[0].get("local_host"),
        "remote_host": connected[0].get("remote_host"),
    }

    rows = []
    for k, iv in enumerate(data["intervals"]):
        s = iv.get("sum")
        if not isinstance(s, dict):
            raise TraceError(f"{name}: intervals[{k}] has no sum")
        if s.get("omitted"):
            raise TraceError(f"{name}: intervals[{k}] is an omitted interval")
        if offered == "sent" and s.get("sender") is False:
            raise TraceError(f"{name}: intervals[{k}] is receiver-side; sender bytes unavailable")
        a = num(s.get("start"), f"{name}: intervals[{k}].sum.start")
        b = num(s.get("end"), f"{name}: intervals[{k}].sum.end")
        nbytes = num(s.get("bytes"), f"{name}: intervals[{k}].sum.bytes")
        if b < a or nbytes < 0:
            raise TraceError(f"{name}: intervals[{k}] has end < start or negative bytes")
        if rows and a < rows[-1][1] - 1e-9:
            raise TraceError(f"{name}: intervals[{k}] overlaps the previous interval")
        rows.append((a, b, nbytes * 8.0))
    if not rows:
        raise TraceError(f"{name}: no intervals")

    target = ts.get("target_bitrate", start.get("target_bitrate", 0)) or 0
    span = rows[-1][1] - rows[0][0]
    sent_bits = sum(r[2] for r in rows)
    if offered == "target":
        if not target:
            raise TraceError(
                f"{name}: --offered target needs a target bitrate (iperf3 -b), but "
                "target_bitrate is 0 (unlimited)"
            )
        segments = [(rows[0][0], rows[-1][1], float(target) * span)]
    else:
        segments = list(rows)
        if protocol == "TCP":
            warnings.warn(
                f"{name}: TCP sender bytes are congestion-controlled (achieved) throughput; "
                "using them as offered demand is circular"
            )
        elif target and span > 0 and sent_bits / span < 0.95 * target:
            warnings.warn(
                f"{name}: sender averaged {sent_bits / span / 1e6:.1f} Mbps, below 95% of "
                f"the {target / 1e6:.1f} Mbps target (sender-limited run?)"
            )
    cadence = num(ts.get("interval", 0) or 0, f"{name}: test_start.interval")
    return {
        "name": name,
        "identity": identity,
        "protocol": protocol,
        "origin": origin,
        "segments": segments,
        "cadence": cadence,
        "target_bitrate": target,
    }


def match_iperf3(runs: list[dict], entries: list[dict]) -> list[dict]:
    flows = []
    used: dict[int, str] = {}
    for run in runs:
        hits = [
            e for e in entries if all(run["identity"].get(k) == v for k, v in e["match"].items())
        ]
        if not hits:
            raise TraceError(
                f"{run['name']}: no iperf3 mapping entry matches "
                f"(identity: {json.dumps(run['identity'], sort_keys=True)})"
            )
        if len(hits) > 1:
            raise TraceError(
                f"{run['name']}: matches several mapping entries "
                f"({', '.join('iperf3[%d]' % e['index'] for e in hits)})"
            )
        e = hits[0]
        if e["index"] in used:
            raise TraceError(
                f"mapping iperf3[{e['index']}] matches both {used[e['index']]} and {run['name']}"
            )
        used[e["index"]] = run["name"]
        flows.append(dict(entry=e, label=run["name"], origin=run["origin"], segments=run["segments"]))
    return flows


def parse_prometheus(path: Path) -> list[dict]:
    name = Path(path).name
    data = load_json(path, "Prometheus JSON")
    if isinstance(data, dict) and "status" in data:
        if data["status"] != "success":
            raise TraceError(f"{name}: Prometheus status {data['status']!r}: {data.get('error')}")
        data = data.get("data")
    if not isinstance(data, dict) or data.get("resultType") != "matrix":
        raise TraceError(f"{name}: expected a query_range response with resultType 'matrix'")
    series = []
    for k, res in enumerate(data.get("result") or []):
        metric = res.get("metric")
        values = res.get("values")
        if not isinstance(metric, dict) or not isinstance(values, list):
            raise TraceError(f"{name}: result[{k}] lacks metric/values")
        samples = []
        for j, pair in enumerate(values):
            where = f"{name}: result[{k}].values[{j}]"
            if not isinstance(pair, list) or len(pair) != 2:
                raise TraceError(f"{where}: expected [timestamp, \"value\"]")
            t = num(pair[0], where + " timestamp")
            try:
                v = float(pair[1])
            except (TypeError, ValueError) as e:
                raise TraceError(f"{where}: bad value {pair[1]!r}") from e
            if not math.isfinite(v):
                raise TraceError(f"{where}: non-finite value {pair[1]!r}")
            if samples and t <= samples[-1][0]:
                raise TraceError(f"{where}: timestamps not strictly increasing")
            samples.append((t, v))
        series.append({"file": name, "index": k, "metric": metric, "samples": samples})
    return series


def series_label(s: dict) -> str:
    labels = ",".join(f'{k}="{v}"' for k, v in sorted(s["metric"].items()) if k != "__name__")
    return f"{s['metric'].get('__name__', '')}{{{labels}}}"


def counter_segments(s: dict, warnings: Warnings) -> tuple[Fraction, list[tuple]]:
    """(origin, segments): origin = first sample time; segments relative to it."""
    segs = []
    samples = s["samples"]
    origin = Fraction(samples[0][0]) if samples else Fraction(0)
    for (t0, v0), (t1, v1) in zip(samples, samples[1:]):
        delta = v1 - v0
        if delta < 0:
            warnings.warn(
                f"{s['file']}: {series_label(s)}: counter decreased at t={t1:.3f} "
                f"({v0:.0f} -> {v1:.0f}); treating as reset, delta clamped to 0"
            )
            delta = 0.0
        segs.append((float(Fraction(t0) - origin), float(Fraction(t1) - origin), delta * 8.0))
    return origin, segs


def match_prometheus(series: list[dict], entries: list[dict], warnings: Warnings) -> list[dict]:
    by_entry: dict[int, list[dict]] = {e["index"]: [] for e in entries}
    for s in series:
        hits = [
            e for e in entries if all(s["metric"].get(k) == v for k, v in e["match"].items())
        ]
        if len(hits) > 1:
            raise TraceError(
                f"{s['file']}: series {series_label(s)} matches several mapping entries "
                f"({', '.join('prometheus[%d]' % e['index'] for e in hits)})"
            )
        if hits:
            by_entry[hits[0]["index"]].append(s)
    flows = []
    for e in entries:
        matched = by_entry[e["index"]]
        if not matched:
            warnings.warn(
                f"mapping prometheus[{e['index']}] (flow {e['flow_id']}) matches no series; skipped"
            )
            continue
        if len(matched) > 1:
            raise TraceError(
                f"mapping prometheus[{e['index']}] matches {len(matched)} series "
                f"({'; '.join(series_label(s) for s in matched)}); add labels to disambiguate"
            )
        s = matched[0]
        name = s["metric"].get("__name__")
        if name is None or not name.endswith("_total"):
            warnings.warn(
                f"{series_label(s)}: metric name does not end in _total; the converter "
                "expects a raw monotonic byte counter, not a rate"
            )
        if len(s["samples"]) < 2:
            warnings.warn(f"{series_label(s)}: fewer than two samples; no volume")
        origin, segs = counter_segments(s, warnings)
        flows.append(dict(entry=e, label=series_label(s), origin=origin, segments=segs))
    return flows


# --------------------------------------------------------------------------- resampling


def resample(segments, offset: float, dt: float) -> tuple[dict, float]:
    """Spread (t_start, t_end, bits) segments onto the FFW grid.

    Segment times are relative to an origin that lies `offset` seconds after t0.

    Returns ({interval: bits}, bits_before_t0). Volume is split in proportion to
    the overlap of each segment with [t0 + i*dt, t0 + (i+1)*dt).
    """
    bins: dict[int, float] = {}
    dropped = 0.0
    for a_rel, b_rel, bits in segments:
        if bits <= 0:
            continue
        a, b = offset + a_rel, offset + b_rel
        if b <= a:
            # zero-length report: attribute to the interval containing its start
            if a < 0:
                dropped += bits
            else:
                i = int(math.floor(a / dt))
                bins[i] = bins.get(i, 0.0) + bits
            continue
        rate = bits / (b - a)
        if a < 0:
            dropped += rate * (min(b, 0.0) - a)
            a = 0.0
            if b <= 0:
                continue
        i = int(math.floor(a / dt))
        while True:
            lo, hi = max(a, i * dt), min(b, (i + 1) * dt)
            if hi > lo:
                bins[i] = bins.get(i, 0.0) + rate * (hi - lo)
            if (i + 1) * dt >= b:
                break
            i += 1
    return bins, dropped


def format_volume(mbit: float, unit: str) -> str:
    value = mbit / 1000.0 if unit == "gbit" else mbit
    text = f"{value:.{DECIMALS[unit]}f}".rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


def build_rows(flows, t0, dt, unit, min_mbit, gap_policy, stride, warnings) -> tuple[list, list]:
    """Returns (rows, flow_summaries); rows are string 5-tuples sorted by flow, interval."""
    all_ids = {f["entry"]["flow_id"] for f in flows}
    rows = []
    summaries = []
    for f in sorted(flows, key=lambda f: f["entry"]["flow_id"]):
        e = f["entry"]
        bins, dropped = resample(f["segments"], float(f["origin"] - t0), dt)
        if dropped > 0:
            warnings.warn(
                f"flow {e['flow_id']} ({f['label']}): {dropped / 1e6:.6g} Mbit before t0 dropped"
            )
        values = {}
        below = 0.0
        for i, bits in bins.items():
            mbit = bits / 1e6
            text = format_volume(mbit, unit)
            if mbit < min_mbit or text == "0":
                below += mbit
                continue
            values[i] = text
        if below > 0 and min_mbit > 0:
            warnings.warn(
                f"flow {e['flow_id']} ({f['label']}): {below:.6g} Mbit in intervals below "
                f"--min-offered-mbit treated as idle"
            )
        if not values:
            warnings.warn(f"flow {e['flow_id']} ({f['label']}): no volume; flow omitted")
            continue
        # contiguous runs of non-idle intervals
        runs = []
        for i in sorted(values):
            if runs and i == runs[-1][-1] + 1:
                runs[-1].append(i)
            else:
                runs.append([i])
        if len(runs) > 1 and gap_policy == "error":
            gaps = [f"{r[-1] + 1}-{n[0] - 1}" for r, n in zip(runs, runs[1:])]
            raise TraceError(
                f"flow {e['flow_id']} ({f['label']}) is idle inside its lifetime "
                f"(intervals {', '.join(gaps)}); the FFW parser requires one positive row per "
                "interval from a flow's first to last. Use --gap-policy split, or a coarser "
                "--interval-seconds"
            )
        for k, run in enumerate(runs):
            fid = e["flow_id"] + k * stride
            if k > 0:
                if fid > MAX_FLOW_ID or fid in all_ids:
                    raise TraceError(
                        f"split of flow {e['flow_id']} needs flow_id {fid}, which is taken or "
                        "out of range; change --split-flow-id-stride"
                    )
                all_ids.add(fid)
                warnings.warn(
                    f"flow {e['flow_id']} ({f['label']}): idle gap; intervals "
                    f"{run[0]}-{run[-1]} continue as flow {fid}"
                )
            for i in run:
                rows.append((str(i), str(fid), str(e["source"]), str(e["destination"]), values[i]))
            summaries.append((fid, e["source"], e["destination"], run[0], run[-1], f["label"]))
    if not rows:
        raise TraceError("no traffic: every flow was empty")
    return rows, summaries


# --------------------------------------------------------------------------- validation


def _int_field(text: str, lo: int, hi: int | None) -> int | None:
    if not re.fullmatch(r"[0-9]+", text):
        return None
    v = int(text)
    if v < lo or (hi is not None and v >= hi):
        return None
    return v


def validate_trace_text(text: str, num_terminals: int | None = None,
                        num_send_intervals: int | None = None) -> dict:
    """Validate a trace CSV with the FFW parser's rules. Raises TraceError.

    Comments ('#' to end of line) and blank lines are ignored, as in the parser.
    Stricter than the parser in one place: integer fields must be plain decimal
    digits (the parser's strtol would also accept e.g. '+3'). Returns stats.
    """
    header = None
    meta: dict[int, dict] = {}
    seen = set()
    nrows = 0
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        fields = [x.strip() for x in line.split(",")]
        if header is None:
            if (
                len(fields) != 5
                or fields[:4] != HEADER_FIELDS
                or fields[4] not in VOLUME_COLUMNS.values()
            ):
                raise TraceError(
                    f"line {lineno}: header must be exactly interval,flow_id,source_terminal,"
                    "destination_terminal,offered_mbit (or offered_gbit)"
                )
            header = fields
            continue
        if len(fields) != 5:
            raise TraceError(f"line {lineno}: {len(fields)} fields; expected 5")
        interval = _int_field(fields[0], 0, num_send_intervals)
        if interval is None:
            bound = f",{num_send_intervals})" if num_send_intervals else ",inf)"
            raise TraceError(f"line {lineno}: invalid interval {fields[0]!r}; expected [0{bound}")
        flow_id = _int_field(fields[1], 0, MAX_FLOW_ID + 1)
        if flow_id is None:
            raise TraceError(f"line {lineno}: invalid flow_id {fields[1]!r}")
        src = _int_field(fields[2], 0, num_terminals)
        if src is None:
            raise TraceError(f"line {lineno}: invalid source_terminal {fields[2]!r}")
        dst = _int_field(fields[3], 0, num_terminals)
        if dst is None or dst == src:
            raise TraceError(f"line {lineno}: invalid destination_terminal {fields[3]!r}")
        try:
            vol = float(fields[4])
        except ValueError:
            vol = float("nan")
        if not math.isfinite(vol) or vol <= 0:
            raise TraceError(f"line {lineno}: invalid offered volume {fields[4]!r} (must be > 0)")
        if (flow_id, interval) in seen:
            raise TraceError(f"line {lineno}: duplicate flow_id {flow_id} in interval {interval}")
        seen.add((flow_id, interval))
        m = meta.setdefault(flow_id, {"src": src, "dst": dst, "lo": interval, "hi": interval, "n": 0})
        if (m["src"], m["dst"]) != (src, dst):
            raise TraceError(
                f"line {lineno}: flow_id {flow_id} changes source/destination across records"
            )
        m["lo"], m["hi"] = min(m["lo"], interval), max(m["hi"], interval)
        m["n"] += 1
        nrows += 1
    if header is None:
        raise TraceError("empty trace or missing header")
    if nrows == 0:
        raise TraceError("trace contains no traffic records")
    for fid, m in meta.items():
        if m["n"] != m["hi"] - m["lo"] + 1:
            raise TraceError(
                f"flow_id {fid} must have one positive-volume row for every interval from "
                f"{m['lo']} through {m['hi']}"
            )
    return {
        "rows": nrows,
        "flows": len(meta),
        "last_interval": max(m["hi"] for m in meta.values()),
        "unit": header[4],
    }


# --------------------------------------------------------------------------- output


def render(rows, summaries, unit, header_lines) -> str:
    out = [f"# {line}".rstrip() for line in header_lines]
    for fid, src, dst, lo, hi, label in summaries:
        out.append(f"#   flow {fid}: terminal {src} -> {dst}, intervals {lo}-{hi}  <- {label}")
    out.append(",".join(HEADER_FIELDS + [VOLUME_COLUMNS[unit]]))
    out.extend(",".join(r) for r in rows)
    return "\n".join(out) + "\n"


def utc(t: float) -> str:
    dt = datetime.datetime.fromtimestamp(t, tz=datetime.timezone.utc)
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def convert(args, warnings: Warnings) -> tuple[str, dict]:
    """Run a conversion from parsed args. Returns (csv text, stats)."""
    if not (args.interval_seconds > 0 and math.isfinite(args.interval_seconds)):
        raise TraceError("--interval-seconds must be positive")
    if args.min_offered_mbit < 0:
        raise TraceError("--min-offered-mbit must be >= 0")
    if args.split_flow_id_stride <= 0:
        raise TraceError("--split-flow-id-stride must be positive")
    terminals = load_topology_terminals(args.topology)
    entries = load_mapping(args.mapping, args.mode, terminals)
    dt = args.interval_seconds

    if args.mode == "iperf3":
        runs = [parse_iperf3(p, args.offered, warnings) for p in args.inputs]
        names = [r["name"] for r in runs]
        if len(set(names)) != len(names):
            raise TraceError("iperf3 inputs must have distinct file names")
        flows = match_iperf3(runs, entries)
        starts = [r["origin"] for r in runs]
        for r in runs:
            if r["cadence"] and dt < r["cadence"] - 1e-9:
                warnings.warn(
                    f"{r['name']}: --interval-seconds {dt:g} is finer than the iperf3 "
                    f"reporting interval {r['cadence']:g}; volume is smeared uniformly"
                )
        semantics = (
            "iperf3 sender bytes per reporting interval (offered load put on the wire)"
            if args.offered == "sent"
            else "iperf3 target bitrate (--bitrate) x reported test span"
        )
        mode_line = f"mode: iperf3 (controlled validation), offered = {semantics}"
    else:
        series = []
        for p in args.inputs:
            series.extend(parse_prometheus(p))
        flows = match_prometheus(series, entries, warnings)
        if not flows:
            raise TraceError("no Prometheus series matched the mapping")
        starts = [f["origin"] for f in flows if f["segments"]]
        steps = sorted(
            {round(b - a, 6) for f in flows for a, b, _ in f["segments"]}
        )
        if steps and dt < steps[0] - 1e-9:
            warnings.warn(
                f"--interval-seconds {dt:g} is finer than the Prometheus sample spacing "
                f"{steps[0]:g} s; volume is smeared uniformly"
            )
        mode_line = (
            "mode: prometheus (achieved-as-offered replay): observed interface counter "
            "deltas replayed as demand"
        )
    if not starts:
        raise TraceError("inputs contain no data")
    t0 = Fraction(args.t0) if args.t0 is not None else min(starts)

    rows, summaries = build_rows(
        flows, t0, dt, args.unit, args.min_offered_mbit, args.gap_policy,
        args.split_flow_id_stride, warnings,
    )
    header_lines = [
        f"Generated by {TOOL}. Do not edit by hand; regenerate from the inputs.",
        mode_line,
        f"interval_seconds: {dt:g}   t0: {float(t0):.3f} unix s ({utc(float(t0))})",
        f"inputs: {', '.join(sorted(Path(p).name for p in args.inputs))}   "
        f"mapping: {Path(args.mapping).name}   topology: {Path(args.topology).name}",
        "flows:",
    ]
    if args.no_comment_header:
        header_lines, summaries_out = [], []
    else:
        summaries_out = summaries
    text = render(rows, summaries_out, args.unit, header_lines)
    stats = validate_trace_text(text, len(terminals), args.num_send_intervals)
    stats["t0"] = float(t0)
    return text, stats


# --------------------------------------------------------------------------- CLI


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Convert iperf3 / Prometheus measurements into an FFW trace CSV.",
        epilog="See trace/README.md and this script's docstring for semantics.",
    )
    sub = p.add_subparsers(dest="mode", required=True)

    def common(sp):
        sp.add_argument("inputs", nargs="+", type=Path, help="measurement JSON file(s)")
        sp.add_argument("--mapping", required=True, type=Path, help="mapping JSON (see docstring)")
        sp.add_argument("--interval-seconds", required=True, type=float,
                        help="FFW interval width; must equal interval_seconds in the FFW config")
        sp.add_argument("--t0", type=float, default=None,
                        help="grid origin, unix seconds (default: earliest start across inputs)")
        sp.add_argument("--topology", type=Path, default=DEFAULT_TOPOLOGY,
                        help="fabric-sites.json for terminal ids/names (default: %(default)s)")
        sp.add_argument("--unit", choices=sorted(VOLUME_COLUMNS), default="gbit",
                        help="volume column unit (default: gbit)")
        sp.add_argument("--num-send-intervals", type=int, default=None,
                        help="fail if the trace needs more than this many FFW send intervals")
        sp.add_argument("--gap-policy", choices=["error", "split"], default="error",
                        help="idle interval inside a flow: fail, or split into a new flow_id")
        sp.add_argument("--split-flow-id-stride", type=int, default=1_000_000,
                        help="flow_id offset per split segment (default: %(default)s)")
        sp.add_argument("--min-offered-mbit", type=float, default=0.0,
                        help="treat intervals below this volume as idle (background chatter)")
        sp.add_argument("--no-comment-header", action="store_true",
                        help="omit the '#' provenance comment lines")
        sp.add_argument("-o", "--output", type=Path, default=None,
                        help="output CSV (default: stdout)")

    sp = sub.add_parser("iperf3", help="iperf3 client JSON results (controlled validation)")
    common(sp)
    sp.add_argument("--offered", choices=["sent", "target"], default="sent",
                    help="sent: per-interval sender bytes (default); target: --bitrate x span")

    sp = sub.add_parser("prometheus",
                        help="Prometheus query_range counter matrix (achieved-as-offered replay)")
    common(sp)

    sp = sub.add_parser("validate", help="check an existing trace CSV against the FFW rules")
    sp.add_argument("trace", type=Path)
    sp.add_argument("--num-terminals", type=int, default=None)
    sp.add_argument("--num-send-intervals", type=int, default=None)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    warnings = Warnings(sys.stderr)
    try:
        if args.mode == "validate":
            try:
                text = args.trace.read_text()
            except OSError as e:
                raise TraceError(f"cannot read {args.trace}: {e}") from e
            stats = validate_trace_text(text, args.num_terminals, args.num_send_intervals)
            print(
                f"{args.trace}: valid ({stats['rows']} rows, {stats['flows']} flows, "
                f"intervals 0-{stats['last_interval']}, {stats['unit']})",
                file=sys.stderr,
            )
            return 0
        text, stats = convert(args, warnings)
    except TraceError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    if args.output is None:
        sys.stdout.write(text)
    else:
        args.output.write_text(text)
    print(
        f"{args.output or 'stdout'}: {stats['rows']} rows, {stats['flows']} flows; set "
        f"interval_seconds: {args.interval_seconds:g} and num_send_intervals >= "
        f"{stats['last_interval'] + 1} in the FFW config",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
