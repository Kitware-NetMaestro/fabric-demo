# Simulation vs measurement comparison (Phase 3)

`compare-sim-vs-measured.py` compares the CODES fluid-flow WAN (FFW) model's
per-interval outputs with per-interval measurements from FABRIC. It aligns the two
on the simulation's interval grid and scores the difference. Python 3.8+, standard
library only. `--plot` uses matplotlib when it can be imported and is skipped with
a note otherwise. The script's docstring is the full reference.

## Inputs

**Simulation.** An FFW log directory, `--sim DIR`. It holds the model's committed
CSV logs, `terminal-events.csv` and `switch-events.csv`, written by
`model-net-fluid-flow-wan-trace-traffic`. The tool derives these series:

| Series | From | Meaning |
|---|---|---|
| `offer:S->D` | terminal `trace_offer` | trace volume added at source S for destination D |
| `send:S->D` | terminal `send` | volume S put on its access link (sender side) |
| `recv:S->D` | terminal `receive` at D, `peer_terminal` S | delivered volume (receiver side) |
| `link:A->B`, `link:A->tN` | switch `egress` | switch-to-switch / switch-to-terminal volume, e.g. `link:STAR->MICH` |
| `drop:A` | switch `dropped_gbit` | drops at switch A |

S, D and N are FFW terminal ids. The terminal logs carry no flow id, so flows are
identified by (source, destination) pair. No two concurrent ladder flows share a
pair. `list-series --sim DIR` prints what a directory contains.

**Measured.** Three kinds of input:

- `--measured-trace CSV`: the converter's trace (`trace/fabric-metrics-to-ffw-trace.py`),
  summed per pair into `offer:S->D`. This is the **measured offered** series:
  iperf3 sender bytes.
- `--measured NAME=CSV`: a generic `time,value` CSV, one per series. `value` is the
  Gbit in the bin that starts at `time`, in seconds from `--measured-t0`. Bins must
  nest in FFW intervals. This is the **measured delivered** series.
  `iperf3-received --t0 T0 --interval-seconds 10 results/server-*-f201-*.json`
  produces one from the iperf3 server JSONs. It splits each reporting interval
  across FFW intervals in proportion to overlap, because server intervals do not
  start on whole seconds.
- `--measured-logs DIR`: another FFW run standing in for measurements (sim vs sim).

Pairs are auto-matched by name (filter with `--series REGEX`) or given explicitly
with `--compare SIM=MEASURED`, e.g. `--compare send:0->8=offer:0->8`.

## Delivery lag

The model advances fluid one switch per interval. Volume sent in interval *i*
therefore reaches MICH in interval *i + 5* from LOSA and *i + 4* from SALT or NEWY
(see "Reading the output: delivery lag" in
`codes-director-ml/doc/example/fluid-flow-wan-fabric-validation-ladder.md`). Real
one-way latency is milliseconds.

- `--lag-shift K` compares sim `recv:*` series K intervals earlier.
- `--lag-shift NAME=K` sets the shift for one series of any kind.

For the ladder: `--lag-shift 4 --lag-shift 'recv:0->8=5'`. Measured series are
never shifted, so a sim-vs-sim comparison of two log directories uses no shift.

## Outputs

- **`-o aligned.csv`**: one row per pair and interval:
  `sim_series,measured_series,lag_shift,interval,time_s,sim_gbit,measured_gbit,diff_gbit`.
- **Summary** (stdout, plus `--summary-csv` / `--summary-json`). Statistics are
  over *active* intervals, those where either side exceeds `--active-threshold`:
  - active interval count
  - totals, and the relative error of the totals
  - bias, which is mean(sim − measured), and RMSE, in Gbit/interval and Gbps
  - mean absolute relative error
  - maximum absolute difference
- **Per-rung rates** (`--schedule experiments/validation-ladder-schedule.json`):
  each pair's mean and peak rate over each rung's send window. This is where
  shares such as 50/50 show up.
- **`--plot out.png`**: sim and measured Gbps per interval, one panel per pair.

## Worked example: sim vs sim (2026-09-30)

The runs themselves stayed in a scratch directory; only these numbers are recorded.

**Setup.**

- **Baseline.** The CODES ladder run exactly as committed: `model-net-fluid-flow-wan-trace-traffic`
  copied from the `codes-director-ml` debug build tree (branch
  `digital-twin-sbir-fluid-flow-wan-fabric-topology`, HEAD `ce288a28` at copy
  time), `--sync=1`, with `fluid-flow-wan-fabric-validation-ladder.yaml` and the
  5-site topology. 1107 net events, the count the ladder doc records.
- **"Measured" stand-in.** The same run with the topology's STAR<->MICH link cut
  from 100 to 80 Gbps. This is the "FABRIC polices to the 80 Gbps reservable
  capacity" hypothesis from the ladder doc. 1180 net events.
- **Delivered side.** The stand-in's `recv:*` series were exported as lag-free
  `time,value` files, the way a real receiver measurement would look, with
  `extract --lag-shift 5` for LOSA and `4` for the others. They were then compared
  against the baseline with `--lag-shift 4 --lag-shift 'recv:0->8=5'`.

```bash
T=analysis/compare-sim-vs-measured.py
python3 $T compare --sim baseline/logs --measured-logs perturbed/logs --interval-seconds 10 \
    --series '^(send:|link:STAR->MICH)' --schedule experiments/validation-ladder-schedule.json
python3 $T extract --sim perturbed/logs --series 'recv:0->8' --interval-seconds 10 \
    --lag-shift 5 -o measured-recv-0-to-8.csv            # likewise 6->9, 2->9, 6->8 with 4
python3 $T compare --sim baseline/logs --interval-seconds 10 \
    --measured 'recv:0->8=measured-recv-0-to-8.csv' ... \
    --lag-shift 4 --lag-shift 'recv:0->8=5' --schedule experiments/validation-ladder-schedule.json
```

**Per-rung mean rate** over the rung's send window. The sender side (`send:*`) and
the lag-shifted delivered side (`recv:*`) agree to 0.01 Gbps:

| Rung | Pair | Baseline (100G) | Stand-in (80G) | Ladder doc prediction for 80G |
|---|---|---|---|---|
| 1 | 0->8 | 80.00 | 80.00 | 80 (fits) |
| 2 | 0->8 / 6->9 | 50.01 / 50.01 | 40.01 / 40.01 | 40 / 40 |
| 3 | 0->8 / 2->9 / 6->8 | 33.34 / 33.35 / 33.34 | 26.67 / 26.69 / 26.67 | 26.7 each |
| 4 | 6->9 | 40.00 | 40.00 | 40 |
| 4 | 0->8 (peak) | 50 | 40 | 40 (cap 40/40) |

**Scores** over active intervals:

| Pair | Active intervals | RMSE (Gbps) | Mean abs. rel. error | Total rel. error |
|---|---|---|---|---|
| send:0->8 | 32 | 12.77 | 28.1% | 0.000% |
| send:6->9 | 18 | 12.01 | 16.7% | 0.000% |
| send:2->9 | 9 | 12.57 | 38.9% | 0.000% |
| send:6->8 | 9 | 12.57 | 38.9% | 0.000% |
| recv:0->8 (lag 5) | 32 | 12.78 | 28.1% | 0.000% |
| recv:6->9 (lag 4) | 18 | 12.01 | 16.7% | 0.000% |
| link:STAR->MICH | 35 | 22.91 | 27.0% | 0.000% |

`link:STAR->MICH` peaks at 100.0 Gbps in the baseline and 80.0 Gbps in the
stand-in.

**How to read it.**

- **Totals match to 0.000%.** The model's elastic sources hold unsent demand in a
  backlog and deliver all 24600 Gb in both runs.
- **The per-interval scores carry the capacity difference.** The lower capacity
  shows up as lower shares, a longer drain and later completion: 20-30% of the
  per-interval volume in overloaded rungs, and zero bias.
- **Against real UDP it will look different.** Fixed-rate UDP drops the excess
  instead of queueing it, so the offered-minus-delivered gap appears as loss.
  Expect a nonzero *total* error there, and read it against the model's backlog
  growth (ladder doc, "Mapping onto the real FABRIC experiment").
- **Rung 2 and 3 shares cleanly separate the capacity hypotheses.** The tool
  recovers 50/50 → 40/40 and 33.3 → 26.7 from both the sender-side and the
  lag-shifted delivered series.

A second rehearsal used real iperf3 data from a loopback run. The full plan ran at
`--scale 0.001` (see `experiments/README.md`). Its trace was converted, replayed in
the model, and compared with the server-side received series. That run shows sim
delivering 3-5% more per pair than measured: exactly the loopback UDP loss.

## Tests

```bash
python3 -m unittest discover -s analysis/tests -v
```

19 tests, offline, under a second, on small FFW log fixtures written in the model's
exact column layout. They cover:

- series derivation, `mbit` logs, and summing of duplicate rows
- trace and `time,value` readers, including bin nesting and `t0`
- lag-shift scoping and statistics against hand-computed values
- a sim-vs-sim run recovering 50 → 40 Gbps rung shares
- extract then compare, exact with the right lag and wrong without it
- `iperf3-received`, including grid resampling and `--get-server-output` wrappers
- plotting degrading without matplotlib
