# Validation-ladder experiments (Phase 2/3)

The FABRIC side of the validation ladder: the experiment schedule in machine-readable
form, the iperf3 execution plan generated from it, and slice tooling. Python 3.8+,
standard library only, except `slice/` (fablib; optional, see below).

> **Status: pre-access.** FABRIC project access is not granted yet. Everything here
> is tested offline. Nothing has run on FABRIC. Code that talks to FABRIC is marked
> **UNTESTED — requires FABRIC project access**. The first-run order is in
> [`docs/access-checklist.md`](../docs/access-checklist.md).

## Layout

```
experiments/
  validation-ladder-schedule.json   the schedule (mirror of the CODES SCHEDULE)
  make-iperf3-plan.py               schedule -> per-VM iperf3 commands, scripts, mapping
  mappings/validation-ladder.json   converter mapping for the plan's iperf3 results (generated)
  expected/validation-ladder-sim-trace.csv
                                    copy of the CODES ladder trace, for the drift test
  slice/                            fablib slice builder (UNTESTED) -- see slice/README.md
  tests/                            offline unittest suite
```

## The schedule

`validation-ladder-schedule.json` is the ladder from the CODES repository
(`codes-director-ml`), rung for rung. For each flow it lists the flow id, both
endpoints as site, VM name, FFW terminal name and FFW terminal id, and each rate
step as start interval, length, start/duration in seconds, and rate in Gbps.

| Rung | Name | Intervals (10 s) | Flows (all to MICH) |
|---|---|---|---|
| 1 | single flow | 0-3 | 101 LOSA.0->MICH.0 80 Gbps |
| 2 | two flows share the bottleneck | 6-9 | 201 LOSA.0->MICH.0, 202 NEWY.0->MICH.1, 80 Gbps each |
| 3 | overload | 15-17 | 301 LOSA.0->MICH.0, 302 SALT.0->MICH.1, 303 NEWY.0->MICH.0, 80 Gbps each |
| 4 | time-varying | 25-34 | 401 LOSA.0->MICH.0 20/40/70/40/20 Gbps (2 intervals each), 402 NEWY.0->MICH.1 40 Gbps |

VMs are `<site>-vm<k>` and FFW terminals are `SITE.k` (LOSA 0-1, SALT 2-3, STAR 4-5,
NEWY 6-7, MICH 8-9).

**Source of truth.** For now, the simulation generator is authoritative:
`SCHEDULE` in `codes-director-ml/scripts/fabric-validation-ladder-trace.py`. Its
registered predictions are in `codes-director-ml/doc/example/fluid-flow-wan-fabric-validation-ladder.md`.
The JSON here is a mirror, and the tests guard it:

- `tests/test_schedule.py` expands the JSON exactly as the generator expands
  `SCHEDULE` and compares the result byte for byte with
  `expected/validation-ladder-sim-trace.csv`. That file is a verbatim copy of the
  generator's committed output, `doc/example/fluid-flow-wan-fabric-validation-ladder.csv`,
  at CODES commit `9720c41d`.
- With `CODES_DIR=/path/to/codes-director-ml` set, the same test also imports the
  generator and compares against `SCHEDULE` directly: names, purposes, gaps and
  steps.

To change the ladder: edit `SCHEDULE` in CODES first and regenerate its CSV. Then
update the JSON and the CSV copy here, and regenerate the mapping (see below).

**TODO.** Make this JSON the single source that both sides consume: the CODES
generator would read it instead of its inline `SCHEDULE`. This is not done yet,
deliberately.

## The iperf3 plan

`make-iperf3-plan.py` turns each flow step into one fixed-rate UDP iperf3 run. It
schedules the run at the step's offset after the run's start time `T0`:

```
server (destination VM, started before T0):
  iperf3 -s -p <port> -1 --json --logfile results/server-<title>.json
client (source VM, at T0 + offset):
  iperf3 -c <peer> -u -b <rate> -t <dur> -p <port> -i 1 [-P n] --title <title> --json --logfile results/client-<title>.json
```

`<title>` is `ladder-r<rung>-f<flow_id>-s<step>`. Each run gets its own port. The
servers are one-off (`-1`), so each writes one receiver-side JSON.

```bash
# Readable plan (placeholders {vm} until the slice exists):
python3 experiments/make-iperf3-plan.py --format text

# On access: first rung only, at 1% of the rates, with real dataplane addresses
python3 experiments/slice/build_ladder_slice.py hosts -o hosts.json      # UNTESTED
python3 experiments/make-iperf3-plan.py --rungs 1 --scale 0.01 --hosts hosts.json \
    --scripts-dir run/ -o run/plan.json
```

`--scripts-dir` writes one `run-<vm>.sh <T0>` per VM that has work. Copy each
script to its VM, pick a `T0` at least a minute ahead, and start every script
before `T0`. Each script starts its servers immediately, waits for each client's
`T0 + offset`, and leaves the JSONs in `results/`. A script refuses to run while
peers are still `{vm}` placeholders.

Options:

- **`--scale F`** multiplies every rate by `F` (default 1.0). A single VM may not
  sustain 80 Gbps of UDP. Scaling every rate by the same factor keeps the ladder's
  demand ratios (160/100, 240/100 and 110/100 of the STAR->MICH capacity), but only
  relative to a bottleneck scaled by the same factor. On the real 100G link, a
  scaled ladder congests nothing. Low `--scale` runs check the tooling end to end.
  They are not a test of the model.
- **`-P N`** runs N parallel streams per client. iperf3 applies `-b` per stream, so
  the plan divides the rate by N. iperf3 3.16+ runs one thread per stream; the
  slice uses Ubuntu 24.04 for that reason.
- **`--rungs 2,3`** selects a subset. Offsets are then relative to the first
  selected rung, and the plan records `origin_interval`.
- **`--mapping-out`** writes the converter mapping. The mapping does not depend on
  `--scale` or `--hosts`. The committed `mappings/validation-ladder.json` is its
  output, and a test keeps it current.

**Stepped flows.** iperf3 cannot change rate mid-run, so flow 401 (rung 4) is five
consecutive runs. The converter maps each input file to one flow. The steps
therefore come back as flow ids `401`, `1000401`, ..., `4000401` (`flow_id + step
* 1000000`, the converter's `--split-flow-id-stride` convention). They are
contiguous in time and share a (source, destination) pair. The comparison tool
works per pair, so it is unaffected. When the trace is replayed in the FFW model,
however, each step starts a new flow with fresh rate-feedback state, where the
simulation's own ladder has a single flow 401. This is a small, known divergence
for rung 4. A future converter option could concatenate time-disjoint inputs into
one flow.

## From iperf3 results to an FFW trace

The client JSONs, the generated mapping and the existing converter produce the
measured offered trace. Pass `--t0` so that interval numbers match the
simulation's:

```bash
T0=<the T0 the run scripts were started with>
ORIGIN=<plan.json origin_interval; 0 for a full-ladder run>
python3 trace/fabric-metrics-to-ffw-trace.py iperf3 \
    --mapping experiments/mappings/validation-ladder.json \
    --interval-seconds 10 \
    --t0 $(( T0 - ORIGIN * 10 )) \
    --num-send-intervals 37 \
    --min-offered-mbit <about 1% of one interval's volume> \
    results/client-ladder-*.json \
    -o ladder-measured-trace.csv
```

- **`--interval-seconds 10`** is the ladder's interval. The iperf3 reports are 1 s
  (`-i 1`), which nests evenly.
- **`--min-offered-mbit`** drops edge slivers. An iperf3 client starts a few tens of
  milliseconds after its scheduled time (process start plus control connection).
  A 40 s run therefore leaves a sliver of its volume in the interval after it ends,
  and the first interval is correspondingly under-filled by about 1-2%. That was
  measured in the loopback rehearsal below. Without the threshold, each sliver
  becomes a tiny extra trace row.
- **Server JSONs** (`results/server-*.json`) are the measured delivered side. Turn
  them into per-pair `time,value` series with
  `analysis/compare-sim-vs-measured.py iperf3-received`. See
  [`analysis/README.md`](../analysis/README.md).

`tests/test_make_iperf3_plan.py` checks this chain offline. It synthesizes the
client JSONs an ideal run of the plan would produce and converts them with the
command above. The resulting trace, summed per (interval, source, destination),
must equal the simulation's ladder trace.

### Loopback rehearsal (2026-09-30, not committed)

The complete plan (`--scale 0.001`, all rungs, 350 s) was run on one laptop with
every VM mapped to `127.0.0.1`, using iperf3 3.22 and the generated scripts. All 12
runs completed. Their client JSONs convert with the command above into a trace
that matches 0.001 × the simulation trace within 2.4% per interval (mean 0.45%).
The only structural difference is the start-delay slivers described above. The
server JSONs report 1-6% loopback UDP loss (macOS socket buffers), and the
comparison tool attributes that loss to the right pairs. Nothing from this run is
committed. The scripts and commands are the ones documented here.

## Tests

```bash
python3 -m unittest discover -s experiments/tests -v
CODES_DIR=../codes-director-ml python3 -m unittest discover -s experiments/tests -v   # + generator check
```

22 tests, offline, well under a second. One is skipped unless `CODES_DIR` is set.
They cover:

- `test_schedule.py`: the mirror against the sim trace copy, its internal
  consistency, and its endpoints against `topology/fabric-sites.json`.
- `test_make_iperf3_plan.py`: the plan at scale 1.0 equals the schedule run for
  run, plus scaling, `-P`, rung subsets, placeholders, scripts (`bash -n`), mapping
  currency, and the round trip through the converter.
- `test_slice_plan.py`: the fablib-free slice plan.

The whole repository: `python3 -m unittest discover` from the root (see the main
README).
