# Measurement-to-trace conversion (Phase 2)

`fabric-metrics-to-ffw-trace.py` turns traffic measurements taken on FABRIC into
the traffic trace CSV consumed by the CODES fluid-flow WAN (FFW) trace-traffic
front end (`model-net-fluid-flow-wan-trace-traffic`). This is the second artifact
of the interface contract in the [main README](../README.md); the first is
`topology/fabric-sites.json`.

Python 3.8+, standard library only. No FABRIC access is needed to run it or its
tests.

## Layout

```
trace/
  fabric-metrics-to-ffw-trace.py      the converter (its docstring is the normative spec)
  mappings/fabric-5site-example.json  example mapping for the 5-site subset
  samples/iperf3/                     REAL iperf3 JSON results, loopback on one machine
    generate-loopback-samples.sh      how they were produced (rerun to regenerate)
  samples/prometheus/                 SYNTHETIC query_range response (schema-faithful)
    make-synthetic-samples.py         deterministic generator for it
  expected/                           golden trace CSVs for the commands below
  tests/                              unittest suite (golden + unit tests)
```

## The output format

```csv
interval,flow_id,source_terminal,destination_terminal,offered_gbit
0,1001,0,8,0.20010115
```

Each row adds `offered` volume (Gbit, or Mbit with `--unit mbit`) to persistent
flow `flow_id`'s source backlog at the start of FFW interval `interval`. What the
FFW parser (`load_traffic_trace_csv()` in the CODES repo's
`src/network-workloads/model-net-fluid-flow-wan.cxx`) enforces, and what this tool
therefore refuses to violate:

- the exact 5-column header; 5 fields per row; `#` comments and blank lines allowed
- `interval` in `[0, num_send_intervals)` (pass `--num-send-intervals N` to check
  the upper bound against your FFW config)
- `offered` finite and **strictly positive**: a zero row is invalid, not "idle"
- each `flow_id` unique per (flow, interval) and bound to one
  (source, destination) pair for all its rows
- **no gaps**: a flow needs a row for every interval from its first to its last.
  The last row marks the flow complete.

The tool writes a `#` provenance header (mode, offered-volume semantics, t0,
interval width, input basenames, flow list); the FFW parser skips it. Use
`--no-comment-header` for a bare CSV. Output is deterministic for given inputs.

Terminal ids are FFW ids from `topology/fabric-sites.json` (sites in JSON order,
terminals numbered sequentially): **LOSA 0-1, SALT 2-3, STAR 4-5, NEWY 6-7,
MICH 8-9**, matching the header of the CODES repo's
`doc/example/fluid-flow-wan-fabric-topology.yaml`.

## Usage

All commands run from the repository root. Diagnostics (warnings, and the
`num_send_intervals` you need) go to stderr; the CSV goes to `-o` or stdout.

### iperf3 mode: controlled validation

```bash
python3 trace/fabric-metrics-to-ffw-trace.py iperf3 \
    --mapping trace/mappings/fabric-5site-example.json \
    --interval-seconds 1 \
    trace/samples/iperf3/udp-losa-to-mich-200M.json \
    trace/samples/iperf3/udp-newy-to-mich-500M.json \
    trace/samples/iperf3/tcp-salt-to-star.json \
    -o trace/expected/iperf3-sent-1s.csv
```

One iperf3 **client** result (`iperf3 -c ... --json`) per flow. All flows share one
FFW time grid starting at `--t0` (unix seconds; default = the earliest
`start.timestamp` among the inputs), so concurrent flows keep their relative
timing. Add `--offered target` to use the requested `--bitrate` instead of sender
bytes (golden: `expected/iperf3-target-1s.csv`).

Refused inputs: reverse (`-R`) and `--bidir` runs (the client JSON then reports
the receiving side; run the client on the sender instead), `--omit` warm-up
(omitted intervals restart the timeline), iperf3 error results, and
`--offered target` without a target bitrate.

### prometheus mode: achieved-as-offered replay

```bash
python3 trace/fabric-metrics-to-ffw-trace.py prometheus \
    --mapping trace/mappings/fabric-5site-example.json \
    --interval-seconds 5 --gap-policy split \
    trace/samples/prometheus/node-transmit-bytes-query-range.json \
    -o trace/expected/prometheus-5s-split.csv
```

Input: one or more Prometheus HTTP API `query_range` responses (resultType
`matrix`) of a monotonic byte counter, e.g.

```bash
curl -G "$PROM/api/v1/query_range" \
    --data-urlencode 'query=node_network_transmit_bytes_total{device="enp7s0"}' \
    -d start=<unix> -d end=<unix> -d step=5 > tx.json
```

Query the **raw counter**, not `rate()`/`irate()`: the converter differences the
samples itself. Per-step byte deltas ×8 become bits and are resampled onto the FFW
grid. A negative delta (exporter restart, driver reload, wrap) is treated as a
counter reset: clamped to 0 with a warning. That step's true volume is lost, and
the resulting idle interval is a gap, which is why the sample command needs
`--gap-policy split` (see below). `query_range` evaluates the counter at each
step, so use a `step` no smaller than the scrape interval.

### Checking an existing trace

```bash
python3 trace/fabric-metrics-to-ffw-trace.py validate trace/expected/iperf3-sent-1s.csv \
    --num-terminals 10 --num-send-intervals 11
```

### Useful options

| Option | Meaning |
|---|---|
| `--interval-seconds S` | FFW interval width; must equal `interval_seconds` in the FFW config |
| `--t0 T` | grid origin, unix seconds; volume before it is dropped (warning) |
| `--unit gbit\|mbit` | volume column (default `gbit`, as in the README contract; the FFW model reports in Gb only when every configured quantity is giga) |
| `--num-send-intervals N` | fail if the trace needs more than N intervals |
| `--gap-policy error\|split` | idle interval inside a flow: fail (default), or end the flow and continue under `flow_id + k*stride` |
| `--split-flow-id-stride K` | the stride (default 1000000); collisions with other flow ids are an error |
| `--min-offered-mbit X` | intervals below X Mbit count as idle (filters ARP/ssh chatter on real NICs) |
| `--topology PATH` | topology JSON for terminal ids and `SITE.k` names (default `topology/fabric-sites.json`) |

## Mapping file

The mapping says which measurement is which FFW flow. The full schema is in the
converter's module docstring and is part of the interface contract; in short:

```json
{
  "schema_version": 1,
  "iperf3": [
    {"match": {"title": "losa-to-mich-udp"}, "flow_id": 1001,
     "source_terminal": "LOSA.0", "destination_terminal": "MICH.0"}
  ],
  "prometheus": [
    {"match": {"instance": "losa-w1:9100", "device": "enp7s0"}, "flow_id": 2001,
     "source_terminal": 0, "destination_terminal": 8}
  ]
}
```

- Terminals are FFW ids (int) or `"SITE.k"` names resolved against the topology.
- `flow_id` must be unique within the section, `0 <= flow_id < 2**64`, and
  source != destination. Unknown entry keys are errors (typo guard).
- **iperf3** `match` keys: `file` (input basename), `title` (`iperf3 --title`),
  `local_host`, `remote_host` (`start.connected[0]`). All given keys must match.
  Every input must match exactly one entry, and no entry may match two inputs.
  On a slice, tag each run with `--title` (the example matches one sample that way)
  or match on the sender's dataplane IP via `local_host`.
- **prometheus** `match` is a label subset (`__name__`, `instance`, `device`, ...).
  Series that match no entry (loopback, management NIC) are ignored; an entry
  that matches no series is skipped with a warning; an entry matching two series,
  or a series matching two entries, is an error. One matched series = one flow.

## Offered vs achieved: what each mode measures

The FFW trace is **offered demand**: the model decides how much of it the network
delivers (access capacity, max-min rate feedback, PAUSE). So the input must be a
measure of what the sources *wanted* to send, or the comparison is circular.

- **iperf3, UDP, `--offered sent` (default).** Volume = the client's
  per-interval sender byte counts (`intervals[].sum.bytes`, `sender: true`). A
  fixed-rate UDP sender paces to `--bitrate` regardless of loss, so these bytes
  are the load actually put on the wire: genuinely offered. Sender bytes also
  capture pacing jitter and any shortfall; the tool warns if a UDP run averaged
  below 95% of its target (sender-limited host, small socket buffer). Receiver
  bytes/loss (`end.sum_received`, `lost_packets`) are *not* used; they are the
  measured outcome to compare against the model's delivered volume in Phase 3.
- **iperf3, `--offered target`.** Volume = `start.test_start.target_bitrate` × the
  span covered by the reported intervals, as a constant rate. The idealized
  demand; use it to separate "what we asked for" from host pacing artifacts.
- **iperf3, TCP.** Accepted, with a warning. TCP sender bytes are throttled by
  congestion control, i.e. by the network being modeled, so achieved == offered
  and the trace just replays the answer. `--offered target` with `-b` gives a
  demand ceiling, but not what a TCP application would have offered. This
  pipeline targets fixed-rate UDP validation experiments first.
- **prometheus.** Interface transmit counters are **achieved** volumes: what the
  NIC sent, after any application-level backoff. Replaying them as demand
  (like the ESnet trace examples) reproduces observed load; it cannot show demand
  that congestion suppressed, and a counter aggregates everything on the
  interface (one flow per interface, no per-destination split). Good for
  realistic background load and what-if studies, not for validating the model's
  congestion response.

## Interval alignment and resampling

Each measurement becomes segments `(start, end, bits)`: one per iperf3 reporting
interval, one per pair of consecutive Prometheus samples. A segment's volume is
assumed uniform over its span and split across FFW intervals
`[t0 + i*dt, t0 + (i+1)*dt)` in proportion to overlap. This conserves volume
(the tests check it against iperf3's own totals, to output rounding of 1 bit per
row); only volume before `t0` is dropped, with a warning.

Choose `--interval-seconds` equal to the measurement cadence (iperf3 `-i`,
Prometheus `step`) or an integer multiple of it. A finer FFW grid only smears each
measurement evenly and invents structure (the tool warns). Flows that start
mid-interval get a partial first and last interval; e.g. in the iperf3 sample,
flow 1002 starts 2.3 s after t0, so interval 2 carries 0.7 s of its 500 Mbps.
Times are handled relative to each input's start with exact arithmetic, so
unix-epoch magnitudes do not add float noise.

Leading and trailing idle intervals are trimmed: a flow starts at its first
non-idle interval and completes at its last. An idle interval *inside* a flow
cannot be expressed (zero rows are invalid and gaps are rejected by the model), so
by default the tool errors; `--gap-policy split` ends the flow and continues it as
`flow_id + k * stride`. The continuation starts with fresh rate-feedback state in
the model, which is what a real idle period would do to a paced sender anyway.

Clock sync: with several hosts, t0 alignment across iperf3 files is only as good
as the hosts' clocks (FABRIC VMs normally run NTP; expect ms-level skew). iperf3
timestamps have millisecond resolution.

## Tests

```bash
python3 -m unittest discover -s trace/tests -v
```

59 tests, stdlib `unittest`, offline, under a second: golden comparisons of both
modes against the committed samples, volume conservation against iperf3 totals,
resampling edge cases, counter reset clamp + split, idle-edge trimming, the
threshold, and rejection of invalid mappings, iperf3/Prometheus inputs, and
traces (the validator is tested against every parser rule).

If a converter change intentionally alters output, regenerate the goldens with the
three commands above (their `-o` paths are the golden files) plus:

```bash
python3 trace/fabric-metrics-to-ffw-trace.py iperf3 \
    --mapping trace/mappings/fabric-5site-example.json --interval-seconds 1 --offered target \
    trace/samples/iperf3/udp-losa-to-mich-200M.json \
    trace/samples/iperf3/udp-newy-to-mich-500M.json \
    trace/samples/iperf3/tcp-salt-to-star.json \
    -o trace/expected/iperf3-target-1s.csv
```

and review the diff.

## Sample provenance

- `samples/iperf3/*.json` are **real** iperf3 3.22 results, but from loopback on
  a single laptop (2026-09-30), not from FABRIC: three concurrent, staggered
  flows (UDP 200 Mbps / 1 s reports; UDP 500 Mbps / 0.5 s reports; TCP capped at
  1 Gbps). Rates are tiny next to FABRIC's 100 Gbps access links; site names come
  only from the mapping. Regenerate with `samples/iperf3/generate-loopback-samples.sh`
  (then regenerate the goldens).
- `samples/prometheus/node-transmit-bytes-query-range.json` is **synthetic**:
  hand-designed numbers in the exact Prometheus `query_range` matrix schema with
  node-exporter labels. Three dataplane series (LOSA ~20 Gbps; NEWY 40 Gbps for
  30 s with idle edges; SALT 10 Gbps with a counter reset) plus unmapped `lo` and
  management-NIC series. Regenerate with `samples/prometheus/make-synthetic-samples.py`.

## Running a converted trace in CODES

In a CODES build directory, copy `fluid-flow-wan-trace-traffic.yaml` and
`fluid-flow-wan-fabric-topology.yaml` from `doc/example/` next to the CSV and
edit the copy: `topology_yaml_file: fluid-flow-wan-fabric-topology.yaml`,
`traffic_trace_file: <your csv>`, 5 switch LPs and 10 terminal LPs,
`interval_seconds` = `--interval-seconds`, `num_send_intervals` at least what the
converter printed, and enough `num_drain_intervals` to drain. Then:

```bash
mpirun -np 1 src/model-net-fluid-flow-wan-trace-traffic --sync=1 -- <your config>.yaml
```

Both golden traces have been run this way against the 5-site topology: every
terminal's `generated_gbit` equals the trace volume for its flows, and all of it
is delivered (these samples do not congest STAR→MICH).

## Where this sits in the plan

- **Phase 2 (this directory):** the conversion half of the trace pipeline, with
  the mapping contract, validation, and offline tests. Still to do in Phase 2:
  fixed-rate UDP experiment plans (which site pairs, rates, durations, `--title`
  naming convention so mappings are trivial), and exporting MFLib's Prometheus
  data (`query_range` over the experiment window) into this input format.
- **Phase 3:** run those plans on a slice spanning the five sites, convert the
  iperf3 sender results with this tool (mode 1), replay them in the FFW model, and
  compare the model's delivered volume/loss per flow and STAR→MICH link load
  against iperf3 receiver results and MFLib interface counters. Mode 2 then
  supplies realistic background load from observed counters.
