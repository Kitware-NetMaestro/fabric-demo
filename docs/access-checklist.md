# Once FABRIC access is granted: runbook

This checklist takes the repository from "tested offline" to "first validation
data". Work through it in order. Tick items off in the PR or issue that records
the first session, and write down what was actually found. Many items exist
because a value here is an assumption.

Everything marked UNTESTED in this repository (`experiments/slice/`, the generated
`run-<vm>.sh` scripts, `trace/fetch-prometheus-range.py` against a real MFLib
Prometheus) is first exercised here.

## 1. Account and project

- [ ] Log in at <https://portal.fabric-testbed.net> with the institutional
      identity (CILogon) the project application used.
- [ ] Confirm membership in the project. Portal: *Projects*, then the project
      page. Record the **project ID** (a UUID); fablib needs it.
- [ ] Check the project's permissions. Some resources need per-project tags: the
      dedicated SmartNICs (`NIC_ConnectX_6`/`_5`) and, possibly, VM sizes above
      the default. Request any that are missing. This step can take days, so do
      it first.

## 2. Keys and tokens

- [ ] **Bastion key.** Portal: *Experiments*, then *Manage SSH Keys*. Generate a
      *bastion* key pair. The private key is shown once; store it at
      `~/.ssh/fabric_bastion` with mode 600. Bastion keys expire; note the date.
- [ ] **Sliver key.** Generate or upload a *sliver* key pair, the one VMs will
      accept, and store it at `~/.ssh/fabric_sliver`.
- [ ] Note the **bastion username** (portal profile, e.g. `<name>_<digits>`). The
      bastion host is `bastion.fabric-testbed.net`.
- [ ] **Token.** Portal: *Experiments*, then *Manage Tokens*. Create a token for
      the project and save it where `FABRIC_TOKEN_LOCATION` will point (fablib's default is `~/.tokens.json`). Tokens expire (hours
      for the ID token; the refresh token lasts longer). fablib refreshes them,
      but a stale file is the most common "it worked yesterday" failure.

## 3. JupyterHub or local fablib

**Option A: FABRIC JupyterHub** (<https://jupyter.fabric-testbed.net>). fablib,
MFLib, tokens and SSH config are preconfigured. This is the fastest way to a
first slice.

- [ ] Clone this repository in the Hub. Run `build_ladder_slice.py` from a
      terminal there, or call its functions from a notebook.
- [ ] Run `python experiments/slice/build_ladder_slice.py check-api` to see
      which fablib and MFLib versions the Hub ships. If they differ from
      `experiments/slice/requirements-fablib.txt`, record them and update the
      pins once things work.

**Option B: local fablib.** Reproducible and scriptable from this repository.

- [ ] `python3.12 -m venv .venv-fablib && .venv-fablib/bin/pip install -r experiments/slice/requirements-fablib.txt`.
      Use Python ≥ 3.11: fablib 2.0.x fails to import on 3.10.
- [ ] Create `~/work/fabric_config/fabric_rc` (fablib's default path) with
      `FABRIC_PROJECT_ID`, `FABRIC_TOKEN_LOCATION`, `FABRIC_BASTION_USERNAME`,
      `FABRIC_BASTION_KEY_LOCATION`, `FABRIC_SLICE_PRIVATE_KEY_FILE` and
      `FABRIC_SLICE_PUBLIC_KEY_FILE`. Then run
      `FablibManager().verify_and_configure()`, which writes the bastion
      `ssh_config` and validates the keys. Pass `--fabric-rc` to the slice
      script if the file lives elsewhere.
- [ ] `FablibManager().show_config()` shows the project ID and key paths.

## 4. Re-verify the topology snapshot

The curated topology (`topology/fabric-sites.json`, snapshot 2026-09-29) comes from
public data. The items to re-verify are listed in
[`topology/fabric-topology-research.md`](../topology/fabric-topology-research.md),
section "To re-check with portal access (fablib)":

- [ ] **Sites and links.** Run `fablib.list_sites()` and `fablib.list_links()`,
      save both outputs to a new dated directory `topology/snapshots/<date>/`
      (never edit old snapshots), and diff them against the 2026-09-29 snapshot
      and `links-table.txt`. Any site or link change in the subset means
      regenerating the FFW topology YAML in the CODES repository.
- [ ] **TeraCore links.** The LAG composition behind the 1200G links (3×400G?) and
      what that means for per-flow limits.
- [ ] **Routed paths.** The actual path for FABNetv4 (and L2PTP/L2STS) between the
      subset sites, in particular whether LOSA↔STAR/NEWY ever takes the southern
      ring. Measure with `traceroute`/`mtr` between slice VMs. MPLS hops may be
      invisible, so compare latencies too.
- [ ] **RTTs.** Measure RTTs between every subset site pair (`ping -c 100`) and
      record them in the research note.
- [ ] **Switch buffers.** The data-switch model and per-port buffer (FABRIC ops or
      knowledge base). The model's 2 Gb is a placeholder.
- [ ] **Worker and SmartNIC availability** at MICH and NEWY. `list_sites()` shows
      cores, RAM and NIC counts. NEWY had only 2 workers at snapshot time.
- [ ] **Background traffic.** Whether a utilization baseline exists
      (<https://infrastructure-metrics.fabric-testbed.net>).

## 5. Confirm the MICH/NEWY capacity question

This question decides rungs 2 and 3. The ladder doc's two hypotheses are 50/50 and
33.3 each (100 Gbps) versus 40/40 and 26.7 each (80 Gbps reservable, policed).

- [ ] **STAR–MICH link.** Confirm it is still a single 100G link (`list_links()`)
      and whether FABNetv4 traffic across it is policed to 80 Gbps. Ask FABRIC
      support, and confirm empirically with rung 2 at full scale (section 8).
- [ ] **NEWY and MICH capacity.** Confirm that NEWY and MICH can each host a VM
      with a dedicated 100G NIC at the same time as the other sites. If only
      shared NICs (`NIC_Basic`) are free, the ladder cannot reach 80 Gbps per
      flow; decide on a `--scale` from what the NIC sustains.
- [ ] **FABNetv4 MTU.** Confirm whether it passes 9000-byte frames on these
      paths: `ping -M do -s 8972 <peer>`.

## 6. Build the slice

- [ ] `build_ladder_slice.py plan --only-used`: review the plan.
- [ ] `build_ladder_slice.py create --only-used`. Fix whatever breaks: this is
      the first run. Record fablib's slice ID and the final node placement.
- [ ] `configure`: installs iperf3 (expect 3.16 on Ubuntu 24.04), raises the
      socket buffers and sets MTU 9000. Check `iperf3 --version` on every VM.
- [ ] `hosts -o hosts.json`: VM → FABNetv4 address. Sanity-check it with
      `ping` from every source VM to both MICH VMs.

## 7. MFLib and the Prometheus scrape cadence

- [ ] `instrument`: MFLib bootstrap, then Prometheus. Record the Prometheus
      credentials MFLib reports.
- [ ] **Determine the real scrape interval.** In Prometheus, open
      *Status → Configuration*, or query `/api/v1/status/config`, and read
      `scrape_interval` for the node job. It must divide 10 s (the ladder's
      interval) for one-to-one comparison: 1, 2, 5 or 10 s. A 15 s scrape needs
      resampling, and 30 s is 3 intervals. Record it in
      `codes-director-ml/doc/example/fluid-flow-wan-fabric.md` (the "to
      re-verify" list) and here.
- [ ] Find the dataplane device name on each VM (`ip -br link`; the `configure`
      output lists it). node-exporter labels counters with it, and the
      Prometheus mapping entries need it.
- [ ] Tunnel to the measurement node's Prometheus, which listens on
      `https://localhost:9090` there with basic auth and a self-signed
      certificate. Use `ssh -F <fablib ssh_config> -L 9090:localhost:9090 <user>@<meas-node-ip>`;
      MFLib's `grafana_tunnel` property prints the equivalent command for
      Grafana. Then test the fetcher on a short window:

      ```bash
      PROM_PASSWORD=... python3 trace/fetch-prometheus-range.py \
          --base-url https://localhost:9090 --insecure --user <user> \
          --query 'node_network_transmit_bytes_total{device="<dev>"}' \
          --start <t> --end <t+60> --step <scrape interval> -o prom-test.json
      python3 trace/fabric-metrics-to-ffw-trace.py prometheus --mapping <mapping> \
          --interval-seconds 10 prom-test.json
      ```

## 8. First experiments, in this order

Every step uses `experiments/make-iperf3-plan.py`. Keep all raw JSON: iperf3
client and server results and the Prometheus exports. Store them in dated
directories with the exact plan JSON and scripts used.

1. [ ] **Tooling smoke test: rung 1 at low scale.** Run
       `--rungs 1 --scale 0.01` (800 Mbps). Check the chain end to end:
       - the scripts start, and servers and clients meet;
       - the client JSONs convert with the documented command
         (`experiments/README.md`);
       - the server JSONs go through `analysis/compare-sim-vs-measured.py iperf3-received`;
       - the Prometheus export for the same window converts too.

       Loss should be about 0.
2. [ ] **Host ceiling.** Run rung 1 alone at increasing scale (0.1, 0.25, 0.5,
       1.0) and with `-P` 1/4/8. Find the highest rate one VM sustains with the
       sender at ≥ 95% of target (the converter warns below that). This sets
       the usable `--scale` for everything else.
3. [ ] **Rung 1 at the chosen scale.** Calibration: delivered should equal
       offered. Any shortfall is host/NIC/path overhead the model does not
       have; record it.
4. [ ] **Rung 2 at full scale, if the host ceiling allows 80 Gbps.** This is the
       capacity question from section 5: 50/50 or 40/40. At lower scale, rung 2
       does not congest the real link. Say so in the results rather than
       reading shares from it.
5. [ ] **Rung 3, then rung 4.** Then the full ladder in one run
       (`make-iperf3-plan.py` with no `--rungs`).
6. [ ] **Analysis.** Convert the client JSONs to a trace (`--t0` per the plan's
       `origin_interval`), replay it in the FFW model, and compare with
       `analysis/compare-sim-vs-measured.py`:
       - sender side `send:*` against `offer:*`;
       - delivered side against `iperf3-received` series, with
         `--lag-shift 4 --lag-shift 'recv:0->8=5'`;
       - `--schedule` for the per-rung shares.

       Record the numbers next to the ladder doc's registered predictions.

## 9. Housekeeping

- [ ] **Slices.** Delete slices when idle (`build_ladder_slice.py delete`).
      Slices have a lease and expire, so note the end time.
- [ ] **Pins.** Update the pins in `experiments/slice/requirements-fablib.txt` to
      what actually worked.
- [ ] **UNTESTED markers.** Remove them from whatever has now run successfully.
      Keep them where a code path is still unexercised.
- [ ] **Deferred decisions.** Revisit the decisions deferred until access:
      - L2PTP with an explicit route instead of FABNetv4;
      - making `experiments/validation-ladder-schedule.json` the single source
        for both repositories;
      - a converter option to join stepped-flow runs into one flow id.
