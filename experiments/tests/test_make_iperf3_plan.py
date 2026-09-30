"""
Offline tests for experiments/make-iperf3-plan.py.

The central check: at --scale 1.0 the plan is exactly the schedule (one iperf3 run
per flow step, same endpoints, start offset, duration and rate). A round-trip test
also synthesizes the iperf3 client JSONs the plan would produce, feeds them through
the real converter with the generated mapping, and checks the result against the
simulation's ladder trace.

Run from the repository root:
    python3 -m unittest discover -s experiments/tests -v
"""

import contextlib
import importlib.util
import io
import json
import shlex
import shutil
import subprocess
import tempfile
import unittest
from collections import defaultdict
from pathlib import Path

EXP = Path(__file__).resolve().parent.parent
REPO = EXP.parent
SCHEDULE = EXP / "validation-ladder-schedule.json"
SIM_TRACE = EXP / "expected" / "validation-ladder-sim-trace.csv"
MAPPING = EXP / "mappings" / "validation-ladder.json"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


planner = load("iperf3_plan", EXP / "make-iperf3-plan.py")
converter = load("ffw_trace_for_plan", REPO / "trace" / "fabric-metrics-to-ffw-trace.py")


def run(mod, *argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = mod.main([str(a) for a in argv])
    return code, out.getvalue(), err.getvalue()


def flag(cmd, name):
    argv = shlex.split(cmd)
    return argv[argv.index(name) + 1]


def trace_by_pair(text):
    """{(interval, src, dst): gbit} from a trace CSV, summing flows of a pair."""
    out = defaultdict(float)
    lines = [l for l in text.splitlines() if l and not l.startswith("#")]
    for line in lines[1:]:
        i, _f, s, d, v = line.split(",")
        out[(int(i), int(s), int(d))] += float(v)
    return dict(out)


class PlanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sched = planner.load_schedule(SCHEDULE)
        cls.hosts = {
            vm["vm"]: f"10.130.{k}.{j + 2}"
            for k, site in enumerate(cls.sched["sites"])
            for j, vm in enumerate(site["vms"])
        }

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_scale_1_plan_matches_schedule(self):
        plan = planner.build_plan(self.sched, scale=1.0, hosts=self.hosts)
        runs = {(r["flow_id"], r["step"]): r for r in plan["runs"]}
        expected = 0
        for rung in self.sched["rungs"]:
            for flow in rung["flows"]:
                for k, step in enumerate(flow["steps"]):
                    expected += 1
                    r = runs[(flow["flow_id"], k)]
                    self.assertEqual(r["rung"], rung["rung"])
                    self.assertEqual(r["source_vm"], flow["source"]["vm"])
                    self.assertEqual(r["destination_vm"], flow["destination"]["vm"])
                    self.assertEqual(r["source_terminal"], flow["source"]["terminal"])
                    self.assertEqual(r["destination_terminal"], flow["destination"]["terminal"])
                    self.assertEqual(r["offset_s"], step["start_s"])
                    self.assertEqual(r["duration_s"], step["duration_s"])
                    self.assertEqual(r["rate_bps"], step["rate_gbps"] * 1e9)
                    c = r["client_command"]
                    self.assertEqual(shlex.split(c)[:2], ["iperf3", "-c"])
                    self.assertEqual(flag(c, "-c"), self.hosts[flow["destination"]["vm"]])
                    self.assertIn("-u", shlex.split(c))
                    self.assertEqual(flag(c, "-b"), f"{step['rate_gbps']}G")
                    self.assertEqual(flag(c, "-t"), str(step["duration_s"]))
                    self.assertEqual(flag(c, "-p"), str(r["port"]))
                    self.assertEqual(flag(c, "--title"), r["title"])
                    self.assertIn("--json", shlex.split(c))
                    self.assertTrue(flag(c, "--logfile").endswith(f"client-{r['title']}.json"))
                    s = r["server_command"]
                    self.assertEqual(shlex.split(s)[:4], ["iperf3", "-s", "-p", str(r["port"])])
        self.assertEqual(len(plan["runs"]), expected)
        self.assertEqual(plan["origin_interval"], 0)
        self.assertEqual(plan["unresolved_hosts"], [])
        self.assertEqual(
            plan["duration_s"],
            max(st["start_s"] + st["duration_s"]
                for r in self.sched["rungs"] for f in r["flows"] for st in f["steps"]),
        )

    def test_per_vm_view_is_consistent(self):
        plan = planner.build_plan(self.sched, hosts=self.hosts)
        ports = [r["port"] for r in plan["runs"]]
        self.assertEqual(len(ports), len(set(ports)), "ports must be unique")
        titles = [r["title"] for r in plan["runs"]]
        self.assertEqual(len(titles), len(set(titles)))
        for r in plan["runs"]:
            servers = plan["vms"][r["destination_vm"]]["servers"]
            self.assertIn(r["server_command"], [s["command"] for s in servers])
            clients = plan["vms"][r["source_vm"]]["clients"]
            self.assertIn(r["client_command"], [c["command"] for c in clients])
        for work in plan["vms"].values():
            offsets = [c["offset_s"] for c in work["clients"]]
            self.assertEqual(offsets, sorted(offsets))
        # Only VMs that the ladder uses appear.
        self.assertEqual(
            sorted(plan["vms"]), ["losa-vm0", "mich-vm0", "mich-vm1", "newy-vm0", "salt-vm0"]
        )

    def test_scale_changes_only_rates(self):
        base = planner.build_plan(self.sched, scale=1.0, hosts=self.hosts)
        small = planner.build_plan(self.sched, scale=0.01, hosts=self.hosts)
        for a, b in zip(base["runs"], small["runs"]):
            self.assertAlmostEqual(b["rate_bps"], a["rate_bps"] * 0.01)
            for key in ("title", "offset_s", "duration_s", "port", "trace_flow_id"):
                self.assertEqual(a[key], b[key])
        r101 = next(r for r in small["runs"] if r["flow_id"] == 101)
        self.assertEqual(flag(r101["client_command"], "-b"), "800M")

    def test_parallel_divides_rate_per_stream(self):
        plan = planner.build_plan(self.sched, parallel=4, hosts=self.hosts)
        r = next(r for r in plan["runs"] if r["flow_id"] == 101)
        self.assertEqual(flag(r["client_command"], "-b"), "20G")
        self.assertEqual(flag(r["client_command"], "-P"), "4")
        self.assertEqual(r["rate_bps"], 80e9)

    def test_format_rate(self):
        from fractions import Fraction as F

        self.assertEqual(planner.format_rate(F(80 * 10**9)), "80G")
        self.assertEqual(planner.format_rate(F(8 * 10**8)), "800M")
        self.assertEqual(planner.format_rate(F(1500)), "1500")  # 1.5K is not an integer K
        self.assertEqual(planner.format_rate(F(80 * 10**9, 3)), "26666666667")

    def test_rung_subset_shifts_origin(self):
        plan = planner.build_plan(self.sched, rungs=[3, 4], hosts=self.hosts)
        self.assertEqual(plan["rungs"], [3, 4])
        self.assertEqual(plan["origin_interval"], 15)
        self.assertEqual(min(r["offset_s"] for r in plan["runs"]), 0)
        r401 = [r for r in plan["runs"] if r["flow_id"] == 401]
        self.assertEqual([r["offset_s"] for r in r401], [100, 120, 140, 160, 180])

    def test_placeholders_without_hosts(self):
        plan = planner.build_plan(self.sched)
        self.assertEqual(plan["unresolved_hosts"], ["mich-vm0", "mich-vm1"])
        r = plan["runs"][0]
        self.assertEqual(flag(r["client_command"], "-c"), "{mich-vm0}")

    def test_cli_rejects_bad_options(self):
        self.assertEqual(run(planner, "--scale", "0")[0], 2)
        self.assertEqual(run(planner, "--rungs", "7")[0], 2)
        self.assertEqual(run(planner, "--report-interval", "3")[0], 2)  # 3 does not divide 10

    def test_cli_writes_scripts_and_mapping(self):
        hosts = self.tmp / "hosts.json"
        hosts.write_text(json.dumps({"hosts": self.hosts}))
        code, out, err = run(
            planner, "--hosts", hosts, "--scripts-dir", self.tmp / "s",
            "--mapping-out", self.tmp / "m.json", "-o", self.tmp / "plan.json",
        )
        self.assertEqual(code, 0, err)
        scripts = sorted(p.name for p in (self.tmp / "s").iterdir())
        self.assertEqual(
            scripts,
            ["run-losa-vm0.sh", "run-mich-vm0.sh", "run-mich-vm1.sh",
             "run-newy-vm0.sh", "run-salt-vm0.sh"],
        )
        text = (self.tmp / "s" / "run-newy-vm0.sh").read_text()
        self.assertIn("UNTESTED", text)
        self.assertIn("wait_until $(( T0 + 250 ))", text)
        self.assertNotIn("placeholders", text)
        bash = shutil.which("bash")
        if bash:
            for p in (self.tmp / "s").iterdir():
                subprocess.run([bash, "-n", str(p)], check=True)
        plan = json.loads((self.tmp / "plan.json").read_text())
        self.assertEqual(len(plan["runs"]), 12)

    def test_committed_mapping_is_current(self):
        plan = planner.build_plan(self.sched)
        self.assertEqual(json.loads(MAPPING.read_text()), planner.build_mapping(plan))
        terminals = converter.load_topology_terminals(REPO / "topology" / "fabric-sites.json")
        entries = converter.load_mapping(MAPPING, "iperf3", terminals)
        self.assertEqual(len(entries), 12)

    def synthesize_client_json(self, run_, t0, directory, rate_factor=1.0):
        """A minimal iperf3 UDP client --json result for one planned run."""
        start = t0 + run_["offset_s"]
        bytes_per_s = run_["rate_bps"] * rate_factor / 8
        intervals = [
            {"sum": {"start": float(k), "end": float(k + 1), "seconds": 1.0,
                     "bytes": bytes_per_s, "bits_per_second": bytes_per_s * 8,
                     "packets": 1, "omitted": False, "sender": True}}
            for k in range(run_["duration_s"])
        ]
        doc = {
            "title": run_["title"],
            "start": {
                "connected": [{"local_host": "10.0.0.1", "remote_host": "10.0.0.2"}],
                "timestamp": {"timesecs": start, "timemillisecs": start * 1000},
                "test_start": {"protocol": "UDP", "num_streams": 1, "omit": 0,
                               "duration": run_["duration_s"], "reverse": 0, "bidir": 0,
                               "target_bitrate": run_["rate_bps"], "interval": 1},
            },
            "intervals": intervals,
            "end": {},
        }
        path = directory / f"client-{run_['title']}.json"
        path.write_text(json.dumps(doc))
        return path

    def test_round_trip_plan_to_converter_reproduces_sim_trace(self):
        """Ideal senders following the plan convert back to the simulation's trace."""
        plan = planner.build_plan(self.sched)
        t0 = 1_790_000_000
        files = [self.synthesize_client_json(r, t0, self.tmp) for r in plan["runs"]]
        out = self.tmp / "trace.csv"
        code, _, err = run(
            converter, "iperf3", "--mapping", MAPPING, "--interval-seconds",
            self.sched["interval_seconds"], "--t0", t0,
            "--num-send-intervals", self.sched["num_send_intervals"], *files, "-o", out,
        )
        self.assertEqual(code, 0, err)
        got, want = trace_by_pair(out.read_text()), trace_by_pair(SIM_TRACE.read_text())
        self.assertEqual(sorted(got), sorted(want))
        for key in want:
            self.assertAlmostEqual(got[key], want[key], places=6, msg=str(key))
        # Stepped flow 401 comes back as one FFW flow per step (see README).
        flow_ids = {int(l.split(",")[1]) for l in out.read_text().splitlines()[1:]
                    if l and not l.startswith("#") and not l.startswith("interval")}
        self.assertIn(401, flow_ids)
        self.assertIn(401 + 4 * planner.STEP_FLOW_ID_STRIDE, flow_ids)

    def test_round_trip_with_rung_subset_uses_shifted_t0(self):
        plan = planner.build_plan(self.sched, rungs=[2])
        run_t0 = 1_790_000_000
        files = [self.synthesize_client_json(r, run_t0, self.tmp) for r in plan["runs"]]
        dt = self.sched["interval_seconds"]
        out = self.tmp / "trace.csv"
        code, _, err = run(
            converter, "iperf3", "--mapping", MAPPING, "--interval-seconds", dt,
            "--t0", run_t0 - plan["origin_interval"] * dt, *files, "-o", out,
        )
        self.assertEqual(code, 0, err)
        got = trace_by_pair(out.read_text())
        want = {k: v for k, v in trace_by_pair(SIM_TRACE.read_text()).items() if 6 <= k[0] <= 9}
        self.assertEqual(sorted(got), sorted(want))


if __name__ == "__main__":
    unittest.main()
