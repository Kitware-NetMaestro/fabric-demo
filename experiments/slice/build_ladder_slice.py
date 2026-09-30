#!/usr/bin/env python3
"""
Build and prepare the 5-site FABRIC slice for the validation ladder (fablib + MFLib).

    *** UNTESTED -- requires FABRIC project access. ***
    Every subcommand except `plan` and `check-api` talks to FABRIC and has never
    been run. `check-api` only imports fablib/MFLib and checks that the API names
    used here exist; it makes no network calls. See docs/access-checklist.md.

Slice layout (derived from experiments/validation-ladder-schedule.json):

  * 2 VMs per site at LOSA, SALT, STAR, NEWY, MICH, named <site>-vm<k> as in the
    schedule (`--only-used` drops the VMs no ladder flow uses: 5 instead of 10).
  * Each VM gets a DEDICATED NIC (NIC_ConnectX_6, 2 x 100G; `--nic` to change)
    and port 0 of it joins its site's FABNetv4 network `fabnet-<site>`.
    FABNetv4 is FABRIC's routed IPv4 service between sites. It is chosen over
    L2PTP circuits because the ladder deliberately oversubscribes STAR->MICH
    (160-240 Gbps offered into 100): bandwidth-reserved L2 circuits cannot be
    booked beyond the link's 80 Gbps reservable capacity, whereas best-effort
    FABNetv4 traffic contends for the real link. MICH is single-homed to STAR,
    so every path into MICH crosses STAR->MICH regardless of FABNetv4 routing;
    the path into STAR (e.g. LOSA via SALT, not the southern ring) must still be
    verified (docs/access-checklist.md). Every VM routes FABNetv4's 10.128.0.0/10
    via its site gateway.
  * MFLib measurement node (MFLib.addMeasNode; site `--meas-site`, default STAR)
    and MFLib's own per-site measurement networks on NIC_Basic, so metrics do
    not share the experiment NIC. After submit, `instrument` runs
    MFLib.instrumentize(["prometheus"]): Prometheus on the measurement node
    scrapes node-exporter on every VM; export with trace/fetch-prometheus-range.py.

Subcommands:
    plan       print the slice plan as JSON (offline, stdlib only; tested)
    check-api  import fablib + MFLib and check the API names used here (no network)
    create     build and submit the slice                         UNTESTED
    configure  install iperf3, raise socket buffers, set MTU      UNTESTED
    hosts      write VM -> dataplane IP JSON for make-iperf3-plan.py --hosts   UNTESTED
    instrument MFLib init + Prometheus                             UNTESTED
    delete     delete the slice                                    UNTESTED

Dependencies: `plan` needs only the Python standard library. Everything else needs
fabrictestbed-extensions (and fabrictestbed-mflib for MFLib); see
experiments/slice/requirements-fablib.txt. They are imported lazily, so this file
imports and its tests run without them.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_SCHEDULE = HERE.parent / "validation-ladder-schedule.json"
DEFAULT_SLICE_NAME = "ffw-validation-ladder"
UNTESTED = "UNTESTED -- requires FABRIC project access (docs/access-checklist.md)"

# VM sizing for multi-stream 80 Gbps UDP. Ubuntu 24.04 ships iperf3 3.16, the
# first release with one thread per stream (-P); 22.04's 3.9 is single-threaded.
VM_DEFAULTS = {"cores": 16, "ram": 32, "disk": 50, "image": "default_ubuntu_24"}
DEFAULT_NIC = "NIC_ConnectX_6"
FABNETV4_SUPERNET = "10.128.0.0/10"  # fablib's FablibManager.FABNETV4_SUBNET

# Host tuning applied by `configure`. Values are a starting point for 100G UDP and
# are to be validated on the first slice (docs/access-checklist.md).
SYSCTL = {
    "net.core.rmem_max": 268435456,
    "net.core.wmem_max": 268435456,
    "net.core.rmem_default": 67108864,
    "net.core.wmem_default": 67108864,
    "net.core.netdev_max_backlog": 250000,
}
DATAPLANE_MTU = 9000  # FABRIC dataplane supports jumbo frames; verify per path


# --------------------------------------------------------------------------- plan (offline)


def slice_plan(
    schedule: dict,
    slice_name: str = DEFAULT_SLICE_NAME,
    only_used: bool = False,
    nic: str = DEFAULT_NIC,
    meas_site: str = "STAR",
    vm: dict | None = None,
) -> dict:
    """Pure description of the slice: nodes, NICs, networks, routes. No fablib."""
    vm = {**VM_DEFAULTS, **(vm or {})}
    used = set()
    for rung in schedule["rungs"]:
        for flow in rung["flows"]:
            used.add(flow["source"]["vm"])
            used.add(flow["destination"]["vm"])
    # A meas site outside the subset is allowed (MFLib's own default is EDC); the
    # plan flags it because metrics traffic then crosses the core.
    sites = [s["name"] for s in schedule["sites"]]
    nodes, networks = [], []
    for site in schedule["sites"]:
        members = []
        for entry in site["vms"]:
            if only_used and entry["vm"] not in used:
                continue
            nodes.append(
                {
                    "name": entry["vm"],
                    "site": site["name"],
                    "terminal": entry["terminal"],
                    "terminal_name": entry["terminal_name"],
                    "used_by_ladder": entry["vm"] in used,
                    **vm,
                    "nic": {"model": nic, "name": "dp", "port": 0},
                }
            )
            members.append(entry["vm"])
        if members:
            networks.append(
                {
                    "name": f"fabnet-{site['name'].lower()}",
                    "type": "IPv4",  # fablib add_l3network type -> FABNetv4
                    "site": site["name"],
                    "members": members,
                    "route": {"subnet": FABNETV4_SUPERNET, "via": "site gateway"},
                }
            )
    return {
        "slice_name": slice_name,
        "status": UNTESTED,
        "schedule_source": schedule.get("authoritative_source", {}),
        "nodes": nodes,
        "networks": networks,
        "mflib": {"meas_site": meas_site, "services": ["prometheus"],
                  "meas_site_in_subset": meas_site in sites},
    }


def load_schedule(path: Path) -> dict:
    return json.loads(Path(path).read_text())


# --------------------------------------------------------------------------- fablib (online)

# Every fablib/MFLib attribute this script uses, for `check-api`.
API_NAMES = {
    "fabrictestbed_extensions.fablib.fablib:FablibManager": [
        "new_slice", "get_slice", "list_sites", "list_links", "show_config",
        "verify_and_configure", "FABNETV4_SUBNET",
    ],
    "fabrictestbed_extensions.fablib.slice:Slice": [
        "add_node", "add_l3network", "submit", "get_node", "get_nodes", "get_network",
        "get_name", "delete", "list_nodes", "wait_ssh",
    ],
    "fabrictestbed_extensions.fablib.node:Node": [
        "add_component", "add_route", "execute", "get_interface", "get_name", "get_site",
    ],
    "fabrictestbed_extensions.fablib.component:Component": ["get_interfaces"],
    "fabrictestbed_extensions.fablib.interface:Interface": [
        "set_mode", "get_ip_addr", "get_device_name", "get_os_interface",
    ],
    "fabrictestbed_extensions.fablib.network_service:NetworkService": [
        "get_gateway", "get_subnet", "get_name",
    ],
    "mflib.mflib:MFLib": ["addMeasNode", "instrumentize", "grafana_tunnel"],
}


def check_api() -> list[str]:
    """Import fablib/MFLib and return the list of missing names (empty = all good)."""
    import importlib

    missing = []
    for target, names in API_NAMES.items():
        module_name, cls_name = target.split(":")
        try:
            cls = getattr(importlib.import_module(module_name), cls_name)
        except (ImportError, AttributeError) as e:
            missing.append(f"{target} ({e.__class__.__name__}: {e})")
            continue
        missing += [f"{target}.{n}" for n in names if not hasattr(cls, n)]
    return missing


def _fablib(fabric_rc: str | None):
    """UNTESTED -- requires FABRIC project access."""
    try:
        from fabrictestbed_extensions.fablib.fablib import FablibManager
    except ImportError as e:
        raise SystemExit(
            "fablib not installed: pip install -r experiments/slice/requirements-fablib.txt "
            f"(Python >= 3.11) [{e}]"
        )
    return FablibManager(fabric_rc=fabric_rc) if fabric_rc else FablibManager()


def create_slice(plan: dict, fabric_rc: str | None, submit: bool = True):
    """UNTESTED -- requires FABRIC project access.

    Builds the slice described by `plan` and submits it (fablib waits for the
    slice to be StableOK and for SSH, then runs post-boot config, which applies
    the auto IP addressing and routes set below).
    """
    fablib = _fablib(fabric_rc)
    from mflib.mflib import MFLib

    sl = fablib.new_slice(name=plan["slice_name"])
    ifaces = {}
    for n in plan["nodes"]:
        node = sl.add_node(name=n["name"], site=n["site"], cores=n["cores"], ram=n["ram"],
                           disk=n["disk"], image=n["image"])
        nic = node.add_component(model=n["nic"]["model"], name=n["nic"]["name"])
        ifaces[n["name"]] = (node, nic.get_interfaces()[n["nic"]["port"]])
    for net in plan["networks"]:
        members = [ifaces[m][1] for m in net["members"]]
        ns = sl.add_l3network(name=net["name"], interfaces=members, type=net["type"])
        for m in net["members"]:
            node, iface = ifaces[m]
            iface.set_mode("auto")
            node.add_route(subnet=fablib.FABNETV4_SUBNET, next_hop=ns.get_gateway())
    # MFLib must be added before submit (it adds its own NIC_Basic + meas networks).
    MFLib.addMeasNode(sl, site=plan["mflib"]["meas_site"])
    if submit:
        sl.submit()
    return sl


def _configure_commands(mtu: int, device: str) -> list[str]:
    cmds = ["sudo apt-get update -qq", "sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq iperf3",
            "iperf3 --version | head -1"]
    cmds += [f"sudo sysctl -w {k}={v}" for k, v in SYSCTL.items()]
    cmds.append(f"sudo ip link set dev {device} mtu {mtu}")
    return cmds


def configure_slice(slice_name: str, fabric_rc: str | None, mtu: int = DATAPLANE_MTU):
    """UNTESTED -- requires FABRIC project access."""
    fablib = _fablib(fabric_rc)
    sl = fablib.get_slice(name=slice_name)
    for node in sl.get_nodes():
        if node.get_name() == "meas-node":
            continue
        iface = node.get_interface(network_name=f"fabnet-{node.get_site().lower()}")
        for cmd in _configure_commands(mtu, iface.get_os_interface()):
            stdout, stderr = node.execute(cmd, quiet=True)
            print(f"[{node.get_name()}] {cmd}: {(stdout or stderr).strip()[:200]}")


def hosts_for_slice(slice_name: str, fabric_rc: str | None) -> dict:
    """UNTESTED -- requires FABRIC project access. VM -> FABNetv4 dataplane address."""
    fablib = _fablib(fabric_rc)
    sl = fablib.get_slice(name=slice_name)
    hosts = {}
    for node in sl.get_nodes():
        if node.get_name() == "meas-node":
            continue
        iface = node.get_interface(network_name=f"fabnet-{node.get_site().lower()}")
        hosts[node.get_name()] = str(iface.get_ip_addr())
    return {"slice_name": slice_name, "status": UNTESTED, "hosts": dict(sorted(hosts.items()))}


def instrument_slice(slice_name: str):
    """UNTESTED -- requires FABRIC project access. MFLib init + Prometheus."""
    from mflib.mflib import MFLib

    mf = MFLib(slice_name)
    print(mf.instrumentize(["prometheus"]))
    print("Grafana tunnel (run locally):", mf.grafana_tunnel)
    print("Prometheus listens on https://localhost:9090 on the meas node (basic auth); "
          "tunnel it and use trace/fetch-prometheus-range.py --insecure --user ...")


def delete_slice(slice_name: str, fabric_rc: str | None):
    """UNTESTED -- requires FABRIC project access."""
    _fablib(fabric_rc).get_slice(name=slice_name).delete()


# --------------------------------------------------------------------------- CLI


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="FABRIC validation-ladder slice tooling. " + UNTESTED)
    p.add_argument("--schedule", type=Path, default=DEFAULT_SCHEDULE)
    p.add_argument("--slice-name", default=DEFAULT_SLICE_NAME)
    p.add_argument("--fabric-rc", help="fablib fabric_rc (default: fablib's own default)")
    sub = p.add_subparsers(dest="cmd", required=True)
    pl = sub.add_parser("plan", help="print the slice plan (offline)")
    for sp in (pl, sub.add_parser("create", help="build + submit the slice (" + UNTESTED + ")")):
        sp.add_argument("--only-used", action="store_true", help="only VMs the ladder uses")
        sp.add_argument("--nic", default=DEFAULT_NIC)
        sp.add_argument("--meas-site", default="STAR")
    sub.add_parser("check-api", help="check fablib/MFLib API names (no network)")
    cf = sub.add_parser("configure", help="iperf3, sysctl, MTU (" + UNTESTED + ")")
    cf.add_argument("--mtu", type=int, default=DATAPLANE_MTU)
    hs = sub.add_parser("hosts", help="write hosts JSON (" + UNTESTED + ")")
    hs.add_argument("-o", "--output", type=Path)
    sub.add_parser("instrument", help="MFLib + Prometheus (" + UNTESTED + ")")
    sub.add_parser("delete", help="delete the slice (" + UNTESTED + ")")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "plan":
        plan = slice_plan(load_schedule(args.schedule), args.slice_name, args.only_used,
                          args.nic, args.meas_site)
        print(json.dumps(plan, indent=1))
        return 0
    if args.cmd == "check-api":
        missing = check_api()
        if missing:
            print("missing:\n  " + "\n  ".join(missing), file=sys.stderr)
            return 1
        from importlib.metadata import version

        print("fablib/MFLib API names OK: fabrictestbed-extensions "
              f"{version('fabrictestbed-extensions')}, fabrictestbed-mflib "
              f"{version('fabrictestbed-mflib')}")
        return 0
    print(f"WARNING: {UNTESTED}", file=sys.stderr)
    if args.cmd == "create":
        plan = slice_plan(load_schedule(args.schedule), args.slice_name, args.only_used,
                          args.nic, args.meas_site)
        create_slice(plan, args.fabric_rc)
    elif args.cmd == "configure":
        configure_slice(args.slice_name, args.fabric_rc, args.mtu)
    elif args.cmd == "hosts":
        text = json.dumps(hosts_for_slice(args.slice_name, args.fabric_rc), indent=1) + "\n"
        if args.output:
            args.output.write_text(text)
        else:
            sys.stdout.write(text)
    elif args.cmd == "instrument":
        instrument_slice(args.slice_name)
    elif args.cmd == "delete":
        delete_slice(args.slice_name, args.fabric_rc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
