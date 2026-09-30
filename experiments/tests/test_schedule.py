"""
Offline tests for experiments/validation-ladder-schedule.json.

The schedule mirrors SCHEDULE in the CODES repo's
scripts/fabric-validation-ladder-trace.py, which is authoritative. These tests
check the mirror's internal consistency and compare its expansion with
experiments/expected/validation-ladder-sim-trace.csv, a byte-for-byte copy of the
generator's committed output (doc/example/fluid-flow-wan-fabric-validation-ladder.csv).
With CODES_DIR set to a CODES checkout, they also import the generator and compare
against SCHEDULE directly; otherwise that test is skipped.

Run from the repository root:
    python3 -m unittest discover -s experiments/tests -v
"""

import importlib.util
import json
import os
import unittest
from pathlib import Path

EXP = Path(__file__).resolve().parent.parent
REPO = EXP.parent
SCHEDULE = EXP / "validation-ladder-schedule.json"
SIM_TRACE = EXP / "expected" / "validation-ladder-sim-trace.csv"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


converter = load("ffw_trace_for_schedule", REPO / "trace" / "fabric-metrics-to-ffw-trace.py")


def fmt(value):
    # Same number formatting as the CODES generator's fmt().
    text = f"{value:.6f}".rstrip("0").rstrip(".")
    return text if text else "0"


def expand_to_csv(sched):
    """Render the schedule exactly like the CODES generator renders SCHEDULE."""
    dt = sched["interval_seconds"]
    rows = []
    for rung in sched["rungs"]:
        for flow in rung["flows"]:
            for step in flow["steps"]:
                for i in range(step["start_interval"], step["start_interval"] + step["intervals"]):
                    if step["rate_gbps"] > 0:
                        rows.append(
                            (i, flow["flow_id"], flow["source"]["terminal"],
                             flow["destination"]["terminal"], step["rate_gbps"] * dt)
                        )
    rows.sort(key=lambda r: (r[0], r[1]))
    out = ["interval,flow_id,source_terminal,destination_terminal,offered_gbit"]
    out += [f"{i},{f},{s},{d},{fmt(g)}" for i, f, s, d, g in rows]
    return "\n".join(out) + "\n"


class ScheduleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sched = json.loads(SCHEDULE.read_text())

    def test_expansion_matches_sim_trace_copy(self):
        self.assertEqual(expand_to_csv(self.sched), SIM_TRACE.read_text())

    def test_internal_consistency(self):
        s = self.sched
        dt = s["interval_seconds"]
        start = 0
        seen = set()
        for rung in s["rungs"]:
            self.assertEqual(rung["start_interval"], start, f"rung {rung['rung']}")
            self.assertEqual(rung["start_s"], start * dt)
            self.assertEqual(rung["duration_s"], rung["intervals"] * dt)
            for flow in rung["flows"]:
                self.assertNotIn(flow["flow_id"], seen)
                seen.add(flow["flow_id"])
                self.assertEqual(flow["flow_id"] // 100, rung["rung"])  # rung * 100 + n
                i = rung["start_interval"]
                for step in flow["steps"]:
                    self.assertEqual(step["start_interval"], i)
                    self.assertEqual(step["start_s"], i * dt)
                    self.assertEqual(step["duration_s"], step["intervals"] * dt)
                    self.assertGreater(step["rate_gbps"], 0)
                    i += step["intervals"]
                self.assertEqual(i, rung["start_interval"] + rung["intervals"])
            start += rung["intervals"] + rung["gap_after"]
        self.assertEqual(s["num_send_intervals"], start)

    def test_endpoints_match_topology(self):
        terminals = converter.load_topology_terminals(REPO / "topology" / "fabric-sites.json")
        vms = {}
        for k, site in enumerate(self.sched["sites"]):
            self.assertEqual(site["switch"], k)
            for j, vm in enumerate(site["vms"]):
                self.assertEqual(vm["terminal_name"], f"{site['name']}.{j}")
                self.assertEqual(terminals[vm["terminal"]], vm["terminal_name"])
                vms[vm["vm"]] = vm
        self.assertEqual(len(vms), len(terminals))
        for rung in self.sched["rungs"]:
            for flow in rung["flows"]:
                for ep in (flow["source"], flow["destination"]):
                    self.assertEqual(vms[ep["vm"]], ep)

    def test_every_flow_crosses_the_bottleneck(self):
        # The ladder's premise: every flow goes to MICH, and no rate exceeds 80 Gbps.
        for rung in self.sched["rungs"]:
            for flow in rung["flows"]:
                self.assertEqual(flow["destination"]["site"], "MICH")
                self.assertLessEqual(max(st["rate_gbps"] for st in flow["steps"]), 80)

    @unittest.skipUnless(os.environ.get("CODES_DIR"), "set CODES_DIR to a CODES checkout")
    def test_matches_codes_generator_schedule(self):
        gen = load(
            "codes_ladder",
            Path(os.environ["CODES_DIR"]) / "scripts" / "fabric-validation-ladder-trace.py",
        )
        self.assertEqual(self.sched["interval_seconds"], gen.INTERVAL_SECONDS)
        self.assertEqual([s["name"] for s in self.sched["sites"]], gen.SITES)
        rows, _ = gen.expand(gen.SCHEDULE)
        self.assertEqual(expand_to_csv(self.sched), gen.render_csv(rows))
        for mine, theirs in zip(self.sched["rungs"], gen.SCHEDULE):
            for key in ("rung", "name", "purpose", "intervals", "gap_after"):
                self.assertEqual(mine[key], theirs[key])
            got = [
                (f["flow_id"], (f["source"]["site"], int(f["source"]["vm"][-1])),
                 (f["destination"]["site"], int(f["destination"]["vm"][-1])),
                 [(st["intervals"], st["rate_gbps"]) for st in f["steps"]])
                for f in mine["flows"]
            ]
            self.assertEqual(got, [(a, b, c, list(d)) for a, b, c, d in theirs["flows"]])
        self.assertEqual(len(self.sched["rungs"]), len(gen.SCHEDULE))


if __name__ == "__main__":
    unittest.main()
