# fabric-demo

FABRIC-testbed-side tooling and data for validating the NetMaestro / CODES
fluid-flow WAN (FFW) model against the [FABRIC testbed](https://portal.fabric-testbed.net/).

The simulation model itself lives in the CODES repository
(`codes-director-ml`, branch `digital-twin-sbir-fluid-flow-wan-model-stats`).
This repo holds everything that touches FABRIC directly: curated topology
data, the measurement-to-trace converter and Prometheus export, the validation
experiment plans and fablib slice tooling, and the sim-vs-measured comparison. The two repos communicate only through exported
artifacts, so the simulation side never depends on FABRIC being reachable.

## Interface contract with the CODES repo

| Artifact | Producer (this repo) | Consumer (CODES repo) |
|---|---|---|
| Topology JSON (`topology/fabric-sites.json`) | curated by hand today; later exported from live fablib queries | `scripts/fabric-topology-to-ffw.py` converts it to an FFW topology YAML |
| Traffic trace CSV (`interval,flow_id,source_terminal,destination_terminal,offered_gbit`) | `trace/fabric-metrics-to-ffw-trace.py` converts iperf3 results / Prometheus counters, via a mapping file | FFW trace-traffic front end |
| Validation-ladder schedule (`experiments/validation-ladder-schedule.json`) | mirror of `SCHEDULE` in the CODES repo's `scripts/fabric-validation-ladder-trace.py`, which is authoritative | (TODO: have the CODES generator read it) |
| FFW CSV logs (`terminal-events.csv`, `switch-events.csv`) | (consumed here) `analysis/compare-sim-vs-measured.py` | written by the FFW model runs |

The topology JSON schema is documented in the converter script's docstring in
the CODES repo; treat it as the contract when writing export tooling here. The
trace CSV rules and the measurement-to-flow mapping schema are documented in
`trace/fabric-metrics-to-ffw-trace.py`'s docstring and in
[`trace/README.md`](trace/README.md).

## Layout

- `topology/fabric-sites.json` — curated 5-site subset of the FABRIC core
  used by the demo: LOSA–SALT–STAR–NEWY on the 1.2 Tbps TeraCore ring, plus
  MICH attached to STAR at 100 Gbps (a real shared bottleneck: LOSA, SALT,
  and NEWY traffic toward MICH all crosses STAR→MICH). Every link capacity
  carries a source citation or an explicit `"assumed": true` marker.
- `topology/fabric-topology-research.md` — the research note behind the
  subset: full site/link inventory with citations, subset rationale, known
  uncertainties (link-bundle composition, routing policy, switch buffer
  sizes, RTTs), and the list of items to re-verify once we have FABRIC
  portal/project access (`fablib.list_sites()` / `list_links()`).
- `topology/snapshots/2026-09-29/` — raw provenance for that note:
  - `portalresources.json` — unmodified snapshot of FABRIC's public
    orchestrator resource data
    (`https://orchestrator.fabric-testbed.net/portalresources?graph_format=JSON_NODELINK&level=1`),
    the same data the portal's Resources page renders. 38 sites and all
    inter-site links with port speeds and reservable (80%) capacities.
  - `links-table.txt` — the 40 inter-site links extracted from it.
- `trace/` — Phase 2 measurement-to-trace converter
  (`fabric-metrics-to-ffw-trace.py`): iperf3 JSON results (controlled
  fixed-rate UDP validation) or Prometheus `query_range` interface counters
  (achieved-as-offered replay) → FFW trace CSV, with an example mapping for the
  5-site subset, committed samples, golden outputs, and offline tests
  (`python3 -m unittest discover -s trace/tests -v`). Also
  `fetch-prometheus-range.py`, which exports a Prometheus `query_range` window
  (MFLib's metrics store) in the converter's input form. See
  [`trace/README.md`](trace/README.md).
- `experiments/` — Phase 2/3 validation experiments. The machine-readable
  validation-ladder schedule mirrors the CODES simulation's `SCHEDULE` and is
  drift-tested against it. `make-iperf3-plan.py` turns it into per-VM iperf3
  commands and scripts, with a `--scale` factor, plus the converter mapping.
  `slice/` holds the fablib + MFLib slice builder for the 5-site slice
  (**UNTESTED — requires FABRIC project access**; its fablib dependency is
  optional and pinned separately). See [`experiments/README.md`](experiments/README.md).
- `analysis/` — Phase 3 comparison: `compare-sim-vs-measured.py` aligns FFW
  CSV-log series with measured series (converter trace as offered, `time,value`
  or iperf3 server results as delivered). It shifts for the model's delivery lag
  and reports a per-interval table, bias/RMSE/relative error, per-rung shares and
  an optional plot. The README has a worked sim-vs-sim example. See
  [`analysis/README.md`](analysis/README.md).
- `docs/access-checklist.md` — the runbook for when FABRIC access is granted:
  account and keys, JupyterHub vs local fablib, topology re-verification,
  capacity and scrape-cadence checks, and the order of the first experiments.
- `test_all.py` — collects every test directory for root-level discovery (see
  Tests).

## Tests

All tests are offline, stdlib `unittest`, and need no FABRIC access. From the
repository root:

```bash
python3 -m unittest discover -v          # everything: trace/, experiments/, analysis/
python3 -m unittest discover -s trace/tests -v   # one directory (likewise experiments/tests, analysis/tests)
```

110 tests: 69 in trace, 22 in experiments and 19 in analysis. One is skipped
unless `CODES_DIR` points at a CODES checkout; that test compares the schedule
mirror with the generator's `SCHEDULE` directly. The plot test is skipped when
matplotlib is absent. The test directories are deliberately not packages (a
`trace/__init__.py` would shadow the stdlib `trace` module), so root discovery
goes through `test_all.py`. Keep test module names unique across directories.

## Conventions

- Snapshots of FABRIC-derived data are dated (`topology/snapshots/<date>/`)
  and never edited after capture; re-fetch into a new dated directory instead.
- Capacities in the curated JSON are port speeds (1200/100 Gbps); FABRIC's
  *reservable* capacity is 80% of port speed and is recorded in each link's
  notes. The distinction is expected to matter when calibrating the model
  against real slice measurements.

## Roadmap

1. **Phase 1 (done)** — curated topology subset (done here) + FFW model
   configs and CI in the CODES repo.
2. **Phase 2** — trace pipeline: fixed-rate UDP experiment plans, and
   conversion of MFLib/Prometheus measurements and iperf3 output into the
   FFW trace CSV format. **Done offline:** the converter and Prometheus
   fetcher (`trace/`) and the validation-ladder schedule, iperf3 plan and
   mapping (`experiments/`). A loopback rehearsal of the full plan with real
   iperf3 ran through the converter and the model. **Pending access:** runs on
   FABRIC, and the fetcher against a real MFLib Prometheus.
3. **Phase 3** — controlled validation experiments on a FABRIC slice
   spanning the five sites. **Prepared, pre-access:**
   - the fablib + MFLib slice builder (`experiments/slice/`; import-checked
     against fablib 2.0.9 but never run against FABRIC);
   - the comparison tool (`analysis/`; demonstrated sim-vs-sim);
   - the first-session runbook (`docs/access-checklist.md`).

   **Waiting on:** FABRIC project access.

4. **Later — background traffic.** FABRIC publishes network traffic metrics
   for all infrastructure links, no login needed, at
   [public-metrics.fabric-testbed.net](https://public-metrics.fabric-testbed.net/)
   (Grafana dashboards; the optical links also appear on
   [ESnet Stardust dashboards](https://dashboard.stardust.es.net/d/XkxDL5H7z/esnet-public-dashboards?orgId=2)).
   Confirmed reachable 2026-09-30. This is the candidate source for modeling
   cross traffic from other slices, so our validation runs need not assume
   they are alone on the links; programmatic access to the underlying series
   is still to be investigated.
