# Validation-ladder slice (fablib + MFLib)

> **UNTESTED — requires FABRIC project access.** Only `plan` (pure Python) and
> `check-api` (an import-only check) have been run. Every other subcommand talks to
> FABRIC and has never been executed. Expect fixes on first contact.

`build_ladder_slice.py` builds the 5-site slice for the validation ladder and
prepares it. The layout is derived from `../validation-ladder-schedule.json`.

- **VMs.** 2 VMs per site at LOSA, SALT, STAR, NEWY and MICH, named `<site>-vm<k>`
  as in the schedule. `--only-used` keeps just the 5 VMs the ladder uses. Default
  size is 16 cores, 32 GB RAM and a 50 GB disk, with `default_ubuntu_24`, which
  ships iperf3 3.16: multi-threaded `-P`, needed for high-rate UDP.
- **Dedicated NICs.** Each VM gets its own `NIC_ConnectX_6` (2 × 100G), and port 0
  carries the experiment traffic.
- **Inter-site connectivity.** One FABNetv4 (routed IPv4) network per site,
  `fabnet-<site>`, and every VM routes `10.128.0.0/10` via its site gateway.
  - **Why FABNetv4 and not L2PTP circuits.** The ladder deliberately offers
    160-240 Gbps into the 100G STAR->MICH link. Bandwidth-reserved L2 circuits
    cannot be booked beyond the link's 80 Gbps reservable capacity. Best-effort
    FABNetv4 traffic genuinely contends on the link.
  - **Why the bottleneck still holds.** MICH is single-homed to STAR, so every path
    into MICH crosses STAR->MICH.
  - **Still to verify.** The path *to* STAR, and whether FABNetv4 shares the link
    with other users' traffic. Both are on the access checklist.
- **Monitoring.** `MFLib.addMeasNode` adds a measurement node (`--meas-site`,
  default STAR; MFLib's own default is EDC) and MFLib's per-site measurement
  networks on `NIC_Basic`, so metrics traffic stays off the experiment NIC.
  `instrument` then runs `MFLib(slice).instrumentize(["prometheus"])`, which starts
  Prometheus on the measurement node, scraping node-exporter on every VM.

## Usage

```bash
# Offline, no dependencies beyond the stdlib:
python3 experiments/slice/build_ladder_slice.py plan [--only-used]

# fablib environment (kept out of the rest of the repo):
python3.12 -m venv .venv-fablib
.venv-fablib/bin/pip install -r experiments/slice/requirements-fablib.txt
.venv-fablib/bin/python experiments/slice/build_ladder_slice.py check-api   # import-only

# UNTESTED -- requires FABRIC project access:
.venv-fablib/bin/python experiments/slice/build_ladder_slice.py create --only-used
.venv-fablib/bin/python experiments/slice/build_ladder_slice.py configure       # iperf3, sysctl, MTU 9000
.venv-fablib/bin/python experiments/slice/build_ladder_slice.py hosts -o hosts.json
.venv-fablib/bin/python experiments/slice/build_ladder_slice.py instrument      # MFLib Prometheus
.venv-fablib/bin/python experiments/slice/build_ladder_slice.py delete
```

`hosts.json` feeds `experiments/make-iperf3-plan.py --hosts`. Credentials come from
fablib's usual configuration (`fabric_rc`, token file, bastion key; `--fabric-rc`
to point elsewhere). On FABRIC's JupyterHub that is preconfigured. See
[`docs/access-checklist.md`](../../docs/access-checklist.md).

## Dependency

`requirements-fablib.txt` pins `fabrictestbed-extensions==2.0.9` and
`fabrictestbed-mflib==1.0.10`. With those, `check-api` passed on 2026-09-30:
fablib and MFLib imported cleanly, and every class attribute this script uses
exists. This is an import-only check, with no FABRIC calls. The requirements are
needed only by this directory; the module imports fablib lazily, so its tests run
without it.

**Python ≥ 3.11 in practice.** fablib 2.0.x declares `Requires-Python >= 3.10`, but
it imports `datetime.UTC`, which only exists from 3.11. On 3.10, pip installs it
anyway and the import fails.

## Known unknowns (to settle on first access)

- **Host tuning.** Whether one VM can source 80 Gbps UDP, and with how many `-P`
  streams. The sysctl buffer sizes and MTU 9000 in `configure` are starting points.
- **NIC availability.** Whether ConnectX-6 NICs are free at MICH and NEWY.
  `--nic NIC_ConnectX_5` or `NIC_Basic` (shared, 25G-class) are fallbacks, but a
  shared NIC would cap the rates.
- **Network settings.** Whether FABNetv4 carries jumbo frames end to end on these
  paths, and whether it polices traffic to the reservable 80 Gbps.
- **MFLib.** The Prometheus credentials and scrape interval on the measurement node.
