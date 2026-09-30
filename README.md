# fabric-demo

FABRIC-testbed-side tooling and data for validating the NetMaestro / CODES
fluid-flow WAN (FFW) model against the [FABRIC testbed](https://portal.fabric-testbed.net/).

The simulation model itself lives in the CODES repository
(`codes-director-ml`, branch `digital-twin-sbir-fluid-flow-wan-model-stats`).
This repo holds everything that touches FABRIC directly: curated topology
data, and (in later phases) fablib slice setup, experiment runners, and
measurement-export tooling. The two repos communicate only through exported
artifacts, so the simulation side never depends on FABRIC being reachable.

## Interface contract with the CODES repo

| Artifact | Producer (this repo) | Consumer (CODES repo) |
|---|---|---|
| Topology JSON (`topology/fabric-sites.json`) | curated by hand today; later exported from live fablib queries | `scripts/fabric-topology-to-ffw.py` converts it to an FFW topology YAML |
| Traffic trace CSV (`interval,flow_id,source_terminal,destination_terminal,offered_gbit`) | `trace/fabric-metrics-to-ffw-trace.py` converts iperf3 results / Prometheus counters, via a mapping file | FFW trace-traffic front end |

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
  (`python3 -m unittest discover -s trace/tests -v`). See
  [`trace/README.md`](trace/README.md).

## Conventions

- Snapshots of FABRIC-derived data are dated (`topology/snapshots/<date>/`)
  and never edited after capture; re-fetch into a new dated directory instead.
- Capacities in the curated JSON are port speeds (1200/100 Gbps); FABRIC's
  *reservable* capacity is 80% of port speed and is recorded in each link's
  notes. The distinction is expected to matter when calibrating the model
  against real slice measurements.

## Roadmap

1. **Phase 1 (current)** — curated topology subset (done here) + FFW model
   configs and CI in the CODES repo.
2. **Phase 2** — trace pipeline: fixed-rate UDP experiment plans, and
   conversion of MFLib/Prometheus measurements and iperf3 output into the
   FFW trace CSV format. The converter is in `trace/`; experiment plans and
   the MFLib export step are still to do.
3. **Phase 3** — controlled validation experiments on a FABRIC slice
   spanning the five sites (fablib notebooks/scripts, MFLib setup,
   comparison scripts).
