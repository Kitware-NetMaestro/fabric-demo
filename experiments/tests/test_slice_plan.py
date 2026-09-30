"""
Offline tests for the pure (fablib-free) part of experiments/slice/build_ladder_slice.py.

The FABRIC-facing subcommands are UNTESTED pending project access; these tests
only cover the slice plan derived from the schedule, and that the module imports
without fablib installed.

Run from the repository root:
    python3 -m unittest discover -s experiments/tests -v
"""

import contextlib
import importlib.util
import io
import json
import unittest
from pathlib import Path

EXP = Path(__file__).resolve().parent.parent
SCHEDULE = EXP / "validation-ladder-schedule.json"

spec = importlib.util.spec_from_file_location("ladder_slice", EXP / "slice" / "build_ladder_slice.py")
slc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(slc)  # must not need fablib


class SlicePlanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sched = json.loads(SCHEDULE.read_text())

    def test_full_plan_two_vms_per_site_with_dedicated_nics(self):
        plan = slc.slice_plan(self.sched)
        self.assertEqual(len(plan["nodes"]), 10)
        per_site = {}
        for n in plan["nodes"]:
            per_site.setdefault(n["site"], []).append(n["name"])
            self.assertEqual(n["nic"]["model"], "NIC_ConnectX_6")
            self.assertEqual(n["name"], f"{n['site'].lower()}-vm{n['terminal'] % 2}")
        self.assertEqual(sorted(per_site), ["LOSA", "MICH", "NEWY", "SALT", "STAR"])
        self.assertTrue(all(len(v) == 2 for v in per_site.values()))
        self.assertEqual(len(plan["networks"]), 5)
        for net in plan["networks"]:
            self.assertEqual(net["type"], "IPv4")
            self.assertEqual(sorted(net["members"]), sorted(per_site[net["site"]]))
            self.assertEqual(net["route"]["subnet"], "10.128.0.0/10")
        self.assertIn("UNTESTED", plan["status"])
        self.assertTrue(plan["mflib"]["meas_site_in_subset"])

    def test_only_used_keeps_every_ladder_endpoint(self):
        plan = slc.slice_plan(self.sched, only_used=True)
        names = {n["name"] for n in plan["nodes"]}
        endpoints = {
            ep["vm"]
            for r in self.sched["rungs"] for f in r["flows"]
            for ep in (f["source"], f["destination"])
        }
        self.assertEqual(names, endpoints)
        self.assertTrue(all(n["used_by_ladder"] for n in plan["nodes"]))
        self.assertEqual(sorted(n["site"] for n in plan["networks"]),
                         sorted({n["site"] for n in plan["nodes"]}))

    def test_cli_plan_is_offline(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = slc.main(["--schedule", str(SCHEDULE), "plan", "--meas-site", "EDC"])
        self.assertEqual(code, 0)
        plan = json.loads(out.getvalue())
        self.assertEqual(plan["mflib"]["meas_site"], "EDC")
        self.assertFalse(plan["mflib"]["meas_site_in_subset"])

    def test_every_online_entry_point_is_marked_untested(self):
        for fn in (slc.create_slice, slc.configure_slice, slc.hosts_for_slice,
                   slc.instrument_slice, slc.delete_slice, slc._fablib):
            self.assertIn("UNTESTED", fn.__doc__ or "", fn.__name__)

    def test_configure_commands(self):
        cmds = slc._configure_commands(9000, "enp7s0")
        self.assertIn("sudo ip link set dev enp7s0 mtu 9000", cmds)
        self.assertTrue(any("iperf3" in c for c in cmds))


if __name__ == "__main__":
    unittest.main()
