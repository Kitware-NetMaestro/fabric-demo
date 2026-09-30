"""
Offline tests for analysis/compare-sim-vs-measured.py (stdlib unittest only).

Fixtures are small FFW log directories written by the tests in the exact column
layout of the model's terminal-events.csv / switch-events.csv.

Run from the repository root:
    python3 -m unittest discover -s analysis/tests -v
"""

import contextlib
import importlib.util
import io
import json
import math
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ANALYSIS = Path(__file__).resolve().parent.parent
REPO = ANALYSIS.parent
SCHEDULE = REPO / "experiments" / "validation-ladder-schedule.json"

spec = importlib.util.spec_from_file_location("compare_tool", ANALYSIS / "compare-sim-vs-measured.py")
cmp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cmp)

TERMINAL_HEADER = "interval,event,terminal,terminal_name,attached_switch,peer_terminal,gbit"
SWITCH_HEADER = (
    "interval,event,switch,switch_name,port,target_type,target_index,capacity_gbit,"
    "queued_before_gbit,sent_gbit,queued_after_gbit,shared_queued_before_gbit,"
    "shared_queued_after_gbit,shared_buffer_gbit,dropped_gbit,active_queue_entries"
)
NAMES = {0: "LOSA.0", 6: "NEWY.0", 8: "MICH.0", 9: "MICH.1"}
SWITCH_OF = {0: 0, 6: 3, 8: 4, 9: 4}
SWITCH_NAMES = {0: "LOSA", 1: "SALT", 2: "STAR", 3: "NEWY", 4: "MICH"}


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cmp.main([str(a) for a in argv])
    return code, out.getvalue(), err.getvalue()


def write_logs(directory, share, lag=4, dt=10):
    """Rung-2-like logs: flows 0->8 and 6->9 send `share` Gbps each in intervals 6-9
    (80 Gbps offered each), delivered `lag` intervals later; STAR->MICH carries both."""
    directory.mkdir(parents=True, exist_ok=True)
    term = [TERMINAL_HEADER]
    sw = [SWITCH_HEADER]
    backlog_intervals = math.ceil(4 * 80 / share) if share else 0
    for src, dst in ((0, 8), (6, 9)):
        for i in range(6, 10):
            term.append(f"{i},trace_offer,{src},{NAMES[src]},{SWITCH_OF[src]},{dst},{80 * dt}")
        remaining = 4 * 80 * dt
        i = 6
        while remaining > 1e-9:
            v = min(share * dt, remaining)
            term.append(f"{i},send,{src},{NAMES[src]},{SWITCH_OF[src]},{dst},{v:g}")
            term.append(f"{i + lag},receive,{dst},{NAMES[dst]},4,{src},{v:g}")
            remaining -= v
            i += 1
    for i in range(6, 6 + backlog_intervals):
        # two rows for the same link and interval: the tool must sum them
        sw.append(f"{i + 2},egress,2,STAR,2,switch,4,1000,0,{share * dt:g},0,0,0,2,0,0")
        sw.append(f"{i + 2},egress,2,STAR,2,switch,4,1000,0,{share * dt:g},0,0,0,2,0,0")
    sw.append("9,egress,4,MICH,1,terminal,8,1000,0,5,0,0,0,2,0.25,0")
    (directory / "terminal-events.csv").write_text("\n".join(term) + "\n")
    (directory / "switch-events.csv").write_text("\n".join(sw) + "\n")
    return directory


class ReadTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_sim_series_names_and_sums(self):
        sim = cmp.read_sim_logs(write_logs(self.tmp / "a", share=50))
        self.assertEqual(
            sorted(sim),
            sorted(["offer:0->8", "offer:6->9", "send:0->8", "send:6->9", "recv:0->8",
                    "recv:6->9", "link:STAR->MICH", "link:MICH->t8", "drop:MICH"]),
        )
        self.assertEqual(sim["offer:0->8"], {6: 800.0, 7: 800.0, 8: 800.0, 9: 800.0})
        self.assertEqual(sim["send:0->8"][6], 500.0)
        self.assertEqual(sum(sim["send:0->8"].values()), 3200.0)
        self.assertEqual(sim["recv:0->8"][10], 500.0)  # delivered 4 intervals later
        self.assertEqual(sim["link:STAR->MICH"][8], 1000.0)  # duplicate rows summed
        self.assertEqual(sim["drop:MICH"], {9: 0.25})

    def test_mbit_column(self):
        d = self.tmp / "m"
        d.mkdir()
        (d / "terminal-events.csv").write_text(
            TERMINAL_HEADER.replace("gbit", "mbit") + "\n0,send,0,LOSA.0,0,8,1500\n"
        )
        self.assertEqual(cmp.read_sim_logs(d), {"send:0->8": {0: 1.5}})

    def test_not_a_log_directory(self):
        with self.assertRaises(cmp.CompareError):
            cmp.read_sim_logs(self.tmp)

    def test_read_trace_sums_flows_of_a_pair(self):
        p = self.tmp / "t.csv"
        p.write_text(
            "# provenance comment\n"
            "interval,flow_id,source_terminal,destination_terminal,offered_mbit\n"
            "0,401,0,8,200000\n1,401,0,8,200000\n1,1000401,0,8,100000\n2,402,6,9,400000\n"
        )
        self.assertEqual(
            cmp.read_trace(p), {"offer:0->8": {0: 200.0, 1: 300.0}, "offer:6->9": {2: 400.0}}
        )

    def test_time_value_binning(self):
        p = self.tmp / "tv.csv"
        rows = ["time,value"] + [f"{1000 + t},{1.5}" for t in range(-2, 25)]
        p.write_text("\n".join(rows) + "\n")
        warnings = []
        s = cmp.read_time_value(p, t0=1000, dt=10, warnings=warnings)
        self.assertEqual(s, {0: 15.0, 1: 15.0, 2: 7.5})
        self.assertEqual(len(warnings), 1)  # the two rows before t0
        p.write_text("t,v\n0,1\n")
        with self.assertRaises(cmp.CompareError):
            cmp.read_time_value(p, 0, 10, [])


class AlignTests(unittest.TestCase):
    def test_lag_shift_parsing_and_scope(self):
        default, per = cmp.parse_lag_shifts(["4", "recv:0->8=5", "link:STAR->MICH=3"])
        self.assertEqual(default, 4)
        self.assertEqual(cmp.lag_for("recv:6->9", default, per), 4)
        self.assertEqual(cmp.lag_for("recv:0->8", default, per), 5)
        self.assertEqual(cmp.lag_for("send:0->8", default, per), 0)  # sender side not shifted
        self.assertEqual(cmp.lag_for("link:STAR->MICH", default, per), 3)
        with self.assertRaises(cmp.CompareError):
            cmp.parse_lag_shifts(["-1"])
        with self.assertRaises(cmp.CompareError):
            cmp.parse_lag_shifts(["x"])
        self.assertEqual(cmp.shift({5: 1.0, 6: 2.0}, 5), {0: 1.0, 1: 2.0})

    def test_score_known_values(self):
        stats = cmp.score({0: 10.0, 1: 20.0, 3: 0.0}, {0: 10.0, 1: 10.0, 2: 5.0}, 0, 3, 10.0, 1e-6)
        self.assertEqual(stats["active_intervals"], 3)  # interval 3 idle on both sides
        self.assertAlmostEqual(stats["bias_gbit"], (0 + 10 - 5) / 3)
        self.assertAlmostEqual(stats["rmse_gbit"], math.sqrt((0 + 100 + 25) / 3))
        self.assertAlmostEqual(stats["bias_gbps"], stats["bias_gbit"] / 10)
        self.assertAlmostEqual(stats["mean_abs_rel_error"], (0 + 1 + 1) / 3)
        self.assertAlmostEqual(stats["total_rel_error"], (30 - 25) / 25)
        self.assertEqual(stats["max_abs_diff_gbit"], 10.0)

    def test_explicit_and_auto_pairs(self):
        sim = {"send:0->8": {0: 1.0}, "recv:0->8": {5: 1.0}}
        meas = {"offer:0->8": {0: 1.0}, "recv:0->8": {0: 1.0}}
        self.assertEqual(cmp.pair_series(sim, meas, [], None), [("recv:0->8", "recv:0->8")])
        self.assertEqual(
            cmp.pair_series(sim, meas, ["send:0->8=offer:0->8"], None),
            [("send:0->8", "offer:0->8")],
        )
        with self.assertRaises(cmp.CompareError):
            cmp.pair_series(sim, meas, ["send:9->8=offer:0->8"], None)
        with self.assertRaises(cmp.CompareError):
            cmp.pair_series(sim, meas, [], "^link:")


class CliTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.base = write_logs(self.tmp / "base", share=50)
        self.pert = write_logs(self.tmp / "pert", share=40)

    def tearDown(self):
        self._tmp.cleanup()

    def test_sim_vs_sim_rung_shares(self):
        code, out, err = run(
            "compare", "--sim", self.base, "--measured-logs", self.pert,
            "--interval-seconds", 10, "--series", "^send:", "--schedule", SCHEDULE,
            "-o", self.tmp / "aligned.csv", "--summary-json", self.tmp / "s.json",
            "--summary-csv", self.tmp / "s.csv",
        )
        self.assertEqual(code, 0, err)
        summary = json.loads((self.tmp / "s.json").read_text())
        pairs = {p["sim_series"]: p for p in summary["pairs"]}
        self.assertEqual(sorted(pairs), ["send:0->8", "send:6->9"])
        for p in pairs.values():
            (r2,) = [r for r in p["rungs"] if r["rung"] == 2]
            self.assertAlmostEqual(r2["sim_mean_gbps"], 50.0)
            self.assertAlmostEqual(r2["measured_mean_gbps"], 40.0)
            self.assertAlmostEqual(p["total_rel_error"], 0.0)  # all volume sent eventually
            self.assertGreater(p["rmse_gbps"], 0)
        self.assertIn("rung 2", out)
        table = (self.tmp / "aligned.csv").read_text().splitlines()
        self.assertEqual(
            table[0],
            "sim_series,measured_series,lag_shift,interval,time_s,sim_gbit,measured_gbit,diff_gbit",
        )
        self.assertIn("send:0->8,send:0->8,0,6,60,500,400,100", table)
        self.assertTrue((self.tmp / "s.csv").read_text().startswith("sim_series,"))

    def test_extract_then_compare_with_lag_shift_is_exact(self):
        # A lag-free "measurement" of the base run's delivered series, compared back
        # against the base run with the right lag shift, must match exactly.
        for pair in ("0->8", "6->9"):
            code, _, err = run(
                "extract", "--sim", self.base, "--series", f"recv:{pair}",
                "--interval-seconds", 10, "--lag-shift", 4, "-o", self.tmp / f"{pair[0]}.csv",
            )
            self.assertEqual(code, 0, err)
        code, out, err = run(
            "compare", "--sim", self.base, "--interval-seconds", 10,
            "--measured", f"recv:0->8={self.tmp / '0.csv'}",
            "--measured", f"recv:6->9={self.tmp / '6.csv'}",
            "--lag-shift", 4, "--summary-json", self.tmp / "s.json",
        )
        self.assertEqual(code, 0, err)
        for p in json.loads((self.tmp / "s.json").read_text())["pairs"]:
            self.assertEqual(p["lag_shift"], 4)
            self.assertEqual(p["rmse_gbit"], 0.0)
        # Without the shift the same data disagrees.
        code, _, _ = run(
            "compare", "--sim", self.base, "--interval-seconds", 10,
            "--measured", f"recv:0->8={self.tmp / '0.csv'}", "--summary-json", self.tmp / "u.json",
        )
        (p,) = json.loads((self.tmp / "u.json").read_text())["pairs"]
        self.assertGreater(p["rmse_gbit"], 0)

    def test_measured_trace_vs_sim_offer(self):
        trace = self.tmp / "t.csv"
        rows = ["interval,flow_id,source_terminal,destination_terminal,offered_gbit"]
        rows += [f"{i},201,0,8,800" for i in range(6, 10)]
        trace.write_text("\n".join(rows) + "\n")
        code, out, err = run(
            "compare", "--sim", self.base, "--interval-seconds", 10,
            "--measured-trace", trace, "--summary-json", self.tmp / "s.json",
        )
        self.assertEqual(code, 0, err)
        (p,) = json.loads((self.tmp / "s.json").read_text())["pairs"]
        self.assertEqual((p["sim_series"], p["rmse_gbit"], p["active_intervals"]), ("offer:0->8", 0.0, 4))

    def test_list_series(self):
        code, out, _ = run("list-series", "--sim", self.base)
        self.assertEqual(code, 0)
        self.assertIn("link:STAR->MICH", out)
        self.assertIn("recv:6->9\tintervals 10-16", out)

    def test_errors_exit_2(self):
        self.assertEqual(run("compare", "--sim", self.base, "--interval-seconds", 10)[0], 2)
        self.assertEqual(
            run("compare", "--sim", self.base, "--interval-seconds", 10,
                "--measured", "nonsense")[0], 2)
        self.assertEqual(run("extract", "--sim", self.base, "--series", "x",
                             "--interval-seconds", 10)[0], 2)

    def test_plot_degrades_without_matplotlib(self):
        result = cmp.compare(
            cmp.read_sim_logs(self.base), cmp.read_sim_logs(self.pert),
            [("send:0->8", "send:0->8")], 10.0, 0, {}, 1e-6,
        )
        with mock.patch.dict(sys.modules, {"matplotlib": None}):
            reason = cmp.plot(result, 10.0, self.tmp / "p.png")
        self.assertIn("matplotlib not importable", reason)
        self.assertFalse((self.tmp / "p.png").exists())

    def test_plot_when_matplotlib_available(self):
        try:
            import matplotlib  # noqa: F401
        except Exception:
            self.skipTest("matplotlib not installed")
        code, _, err = run(
            "compare", "--sim", self.base, "--measured-logs", self.pert,
            "--interval-seconds", 10, "--series", "^send:", "--plot", self.tmp / "p.png",
        )
        self.assertEqual(code, 0, err)
        self.assertTrue((self.tmp / "p.png").stat().st_size > 0)


class Iperf3ReceivedTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def server_json(self, name, start_ms, nbytes, sender=False):
        doc = {
            "start": {"timestamp": {"timemillisecs": start_ms}},
            "intervals": [
                {"sum": {"start": k, "end": k + 1, "bytes": nbytes, "sender": sender}}
                for k in range(3)
            ],
        }
        p = self.tmp / name
        p.write_text(json.dumps(doc))
        return p

    def test_server_results_summed_on_shared_times(self):
        a = self.server_json("a.json", 1_790_000_000_000, 125_000_000)  # 1 Gbit per second
        b = self.server_json("b.json", 1_790_000_001_000, 125_000_000)
        code, out, err = run("iperf3-received", a, b, "--t0", 1_790_000_000)
        self.assertEqual(code, 0, err)
        self.assertEqual(out.splitlines(), ["time,value", "0,1", "1,2", "2,2", "3,1"])

    def test_grid_resampling_splits_straddling_intervals(self):
        # Three 1 Gbit reporting intervals at [0.5,1.5), [1.5,2.5), [2.5,3.5) on a
        # 2 s grid: bin 0 gets 1 + 0.5, bin 2 gets 0.5 + 1.
        a = self.server_json("a.json", 1_790_000_000_500, 125_000_000)
        code, out, err = run("iperf3-received", a, "--t0", 1_790_000_000,
                             "--interval-seconds", 2)
        self.assertEqual(code, 0, err)
        self.assertEqual(out.splitlines(), ["time,value", "0,1.5", "2,1.5"])

    def test_client_only_result_rejected(self):
        a = self.server_json("c.json", 1_790_000_000_000, 1, sender=True)
        code, _, err = run("iperf3-received", a)
        self.assertEqual(code, 2)
        self.assertIn("receiver-side", err)

    def test_get_server_output_wrapper(self):
        inner = json.loads(self.server_json("s.json", 1_000, 125_000_000).read_text())
        p = self.tmp / "client.json"
        p.write_text(json.dumps({"start": {}, "intervals": [], "server_output_json": inner}))
        code, out, err = run("iperf3-received", p)
        self.assertEqual(code, 0, err)
        self.assertEqual(out.splitlines()[1], "1,1")


if __name__ == "__main__":
    unittest.main()
