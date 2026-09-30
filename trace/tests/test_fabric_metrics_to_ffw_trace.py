"""
Offline tests for trace/fabric-metrics-to-ffw-trace.py (stdlib unittest only).

Run from the repository root:
    python3 -m unittest discover -s trace/tests -v
"""

import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path

TRACE_DIR = Path(__file__).resolve().parent.parent
REPO = TRACE_DIR.parent
SCRIPT = TRACE_DIR / "fabric-metrics-to-ffw-trace.py"
MAPPING = TRACE_DIR / "mappings" / "fabric-5site-example.json"
IPERF3_DIR = TRACE_DIR / "samples" / "iperf3"
IPERF3_SAMPLES = [
    IPERF3_DIR / "udp-losa-to-mich-200M.json",
    IPERF3_DIR / "udp-newy-to-mich-500M.json",
    IPERF3_DIR / "tcp-salt-to-star.json",
]
PROM_SAMPLE = TRACE_DIR / "samples" / "prometheus" / "node-transmit-bytes-query-range.json"
EXPECTED = TRACE_DIR / "expected"

spec = importlib.util.spec_from_file_location("ffw_trace", SCRIPT)
ffw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ffw)


def run_cli(*argv):
    """Run main(); returns (exit code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = ffw.main([str(a) for a in argv])
    return code, out.getvalue(), err.getvalue()


def data_rows(text):
    """Trace rows as tuples (interval, flow_id, src, dst, volume float), comments dropped."""
    lines = [l for l in text.splitlines() if l and not l.startswith("#")]
    rows = []
    for line in lines[1:]:
        i, f, s, d, v = line.split(",")
        rows.append((int(i), int(f), int(s), int(d), float(v)))
    return lines[0], rows


class TempDirCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def write_json(self, name, obj):
        path = self.tmp / name
        path.write_text(json.dumps(obj))
        return path


# --------------------------------------------------------------------------- golden


class GoldenOutputTest(TempDirCase):
    """End-to-end against the committed samples; regenerate goldens per trace/README.md."""

    def check(self, golden, argv):
        out = self.tmp / "out.csv"
        code, _, err = run_cli(*argv, "-o", out)
        self.assertEqual(code, 0, err)
        self.assertEqual(out.read_text(), (EXPECTED / golden).read_text())

    def test_iperf3_sent(self):
        self.check("iperf3-sent-1s.csv",
                   ["iperf3", "--mapping", MAPPING, "--interval-seconds", "1", *IPERF3_SAMPLES])

    def test_iperf3_target(self):
        self.check("iperf3-target-1s.csv",
                   ["iperf3", "--mapping", MAPPING, "--interval-seconds", "1",
                    "--offered", "target", *IPERF3_SAMPLES])

    def test_prometheus_split(self):
        self.check("prometheus-5s-split.csv",
                   ["prometheus", "--mapping", MAPPING, "--interval-seconds", "5",
                    "--gap-policy", "split", PROM_SAMPLE])

    def test_input_order_does_not_matter(self):
        out = self.tmp / "out.csv"
        code, _, err = run_cli("iperf3", "--mapping", MAPPING, "--interval-seconds", "1",
                               *reversed(IPERF3_SAMPLES), "-o", out)
        self.assertEqual(code, 0, err)
        self.assertEqual(out.read_text(), (EXPECTED / "iperf3-sent-1s.csv").read_text())

    def test_goldens_pass_validator(self):
        for path in EXPECTED.glob("*.csv"):
            stats = ffw.validate_trace_text(path.read_text(), num_terminals=10)
            self.assertGreater(stats["rows"], 0, path)
            self.assertEqual(stats["unit"], "offered_gbit")


class Iperf3SemanticsTest(TempDirCase):
    def convert(self, *extra, inputs=IPERF3_SAMPLES):
        code, out, err = run_cli("iperf3", "--mapping", MAPPING, "--interval-seconds", "1",
                                 *extra, *inputs)
        self.assertEqual(code, 0, err)
        return data_rows(out), err

    def test_sent_volume_conserved(self):
        (_, rows), _ = self.convert()
        for path, flow_id in [(IPERF3_SAMPLES[0], 1001), (IPERF3_SAMPLES[1], 1002),
                              (IPERF3_SAMPLES[2], 1003)]:
            data = json.loads(path.read_text())
            sent_gbit = sum(iv["sum"]["bytes"] for iv in data["intervals"]) * 8 / 1e9
            trace_gbit = sum(r[4] for r in rows if r[1] == flow_id)
            # 9-decimal output rounding: at most 0.5e-9 Gbit per row
            self.assertAlmostEqual(trace_gbit, sent_gbit, delta=1e-8, msg=path.name)

    def test_target_is_constant_rate(self):
        (_, rows), _ = self.convert("--offered", "target")
        full = [r[4] for r in rows if r[1] == 1001][:-1]  # last interval is partial
        self.assertTrue(all(v == 0.2 for v in full), full)
        full = [r[4] for r in rows if r[1] == 1002][1:-1]  # first/last partial
        self.assertTrue(all(v == 0.5 for v in full), full)

    def test_flow_start_offsets_from_common_t0(self):
        (_, rows), _ = self.convert()
        first = {}
        for r in rows:
            first.setdefault(r[1], r[0])
        starts = {f: json.loads(p.read_text())["start"]["timestamp"]["timemillisecs"]
                  for f, p in zip((1001, 1002, 1003), IPERF3_SAMPLES)}
        t0 = min(starts.values())
        for f, ms in starts.items():
            self.assertEqual(first[f], (ms - t0) // 1000, f)

    def test_tcp_warns_circular(self):
        _, err = self.convert(inputs=IPERF3_SAMPLES[2:])
        self.assertIn("circular", err)
        _, err = self.convert(inputs=IPERF3_SAMPLES[:1])
        self.assertNotIn("circular", err)

    def test_explicit_t0_drops_earlier_volume(self):
        t0 = json.loads(IPERF3_SAMPLES[0].read_text())["start"]["timestamp"]["timemillisecs"]
        (_, rows), err = self.convert("--t0", str(t0 / 1000 + 2.0), inputs=IPERF3_SAMPLES[:1])
        self.assertIn("before t0 dropped", err)
        self.assertEqual(rows[0][0], 0)
        self.assertAlmostEqual(sum(r[4] for r in rows), 1.6, delta=0.01)  # 8 s at 200 Mbps

    def test_mbit_unit(self):
        (header, rows), _ = self.convert("--unit", "mbit", inputs=IPERF3_SAMPLES[:1])
        self.assertTrue(header.endswith("offered_mbit"))
        self.assertAlmostEqual(rows[1][4], 200.0, delta=1.0)

    def test_num_send_intervals_enforced(self):
        out = self.tmp / "out.csv"
        code, _, err = run_cli("iperf3", "--mapping", MAPPING, "--interval-seconds", "1",
                               "--num-send-intervals", "5", *IPERF3_SAMPLES, "-o", out)
        self.assertEqual(code, 1)
        self.assertIn("invalid interval", err)
        self.assertFalse(out.exists(), "an invalid trace must not be written")

    def test_finer_grid_than_cadence_warns(self):
        code, _, err = run_cli("iperf3", "--mapping", MAPPING, "--interval-seconds", "0.25",
                               IPERF3_SAMPLES[0])
        self.assertEqual(code, 0, err)
        self.assertIn("finer than the iperf3 reporting interval", err)


# --------------------------------------------------------------------------- resampling


class ResampleTest(unittest.TestCase):
    def test_split_by_overlap(self):
        bins, dropped = ffw.resample([(0.5, 2.5, 200.0)], 0.0, 1.0)
        self.assertEqual(bins, {0: 50.0, 1: 100.0, 2: 50.0})
        self.assertEqual(dropped, 0.0)

    def test_offset_origin(self):
        bins, _ = ffw.resample([(0.0, 1.0, 100.0)], 3.25, 1.0)
        self.assertEqual(bins, {3: 75.0, 4: 25.0})

    def test_coarse_grid_sums(self):
        segs = [(k, k + 1, 10.0 * (k + 1)) for k in range(6)]
        bins, _ = ffw.resample(segs, 0.0, 3.0)
        self.assertEqual(bins, {0: 60.0, 1: 150.0})

    def test_volume_conserved_irregular(self):
        segs = [(0.0, 0.37, 1e6), (0.37, 1.91, 2e6), (1.91, 2.0, 5e5), (2.3, 4.05, 3e6)]
        bins, dropped = ffw.resample(segs, 0.8, 0.7)
        self.assertAlmostEqual(sum(bins.values()), 6.5e6, delta=1e-3)
        self.assertEqual(dropped, 0.0)

    def test_before_t0_dropped(self):
        bins, dropped = ffw.resample([(0.0, 4.0, 400.0)], -1.0, 1.0)
        self.assertEqual(dropped, 100.0)
        self.assertEqual(bins, {0: 100.0, 1: 100.0, 2: 100.0})

    def test_zero_length_segment(self):
        bins, _ = ffw.resample([(2.0, 2.0, 7.0)], 0.0, 1.0)
        self.assertEqual(bins, {2: 7.0})

    def test_zero_volume_ignored(self):
        bins, _ = ffw.resample([(0.0, 5.0, 0.0)], 0.0, 1.0)
        self.assertEqual(bins, {})


# --------------------------------------------------------------------------- counters


def matrix(series):
    return {"status": "success", "data": {"resultType": "matrix", "result": series}}


def counter_series(instance, values, start=1000, step=5, device="enp7s0"):
    return {
        "metric": {"__name__": "node_network_transmit_bytes_total", "instance": instance,
                   "device": device, "job": "node"},
        "values": [[start + i * step, str(v)] for i, v in enumerate(values)],
    }


def prom_mapping(*instances):
    return {
        "schema_version": 1,
        "prometheus": [
            {"match": {"instance": inst, "device": "enp7s0"}, "flow_id": 10 + k,
             "source_terminal": k, "destination_terminal": 9}
            for k, inst in enumerate(instances)
        ],
    }


class PrometheusTest(TempDirCase):
    MB = 125_000  # bytes per Mbit

    def convert(self, series, mapping, *extra):
        q = self.write_json("q.json", matrix(series))
        m = self.write_json("m.json", mapping)
        return run_cli("prometheus", "--mapping", m, "--interval-seconds", "5", "--unit",
                       "mbit", *extra, q)

    def test_deltas_to_mbit(self):
        code, out, err = self.convert(
            [counter_series("a", [0, 10 * self.MB, 30 * self.MB, 60 * self.MB])],
            prom_mapping("a"))
        self.assertEqual(code, 0, err)
        _, rows = data_rows(out)
        self.assertEqual(rows, [(0, 10, 0, 9, 10.0), (1, 10, 0, 9, 20.0), (2, 10, 0, 9, 30.0)])

    def test_counter_reset_clamped_and_split(self):
        values = [5000 * self.MB, 5010 * self.MB, 5020 * self.MB, 3 * self.MB, 13 * self.MB]
        series = [counter_series("a", values)]
        code, _, err = self.convert(series, prom_mapping("a"))
        self.assertEqual(code, 1)
        self.assertIn("counter decreased", err)
        self.assertIn("idle inside its lifetime", err)

        code, out, err = self.convert(series, prom_mapping("a"), "--gap-policy", "split")
        self.assertEqual(code, 0, err)
        self.assertIn("counter decreased", err)
        _, rows = data_rows(out)
        self.assertEqual(rows, [(0, 10, 0, 9, 10.0), (1, 10, 0, 9, 10.0),
                                (3, 1000010, 0, 9, 10.0)])

    def test_reset_at_edge_needs_no_split(self):
        values = [0, 10 * self.MB, 20 * self.MB, 1 * self.MB]
        code, out, err = self.convert([counter_series("a", values)], prom_mapping("a"))
        self.assertEqual(code, 0, err)
        self.assertIn("counter decreased", err)
        self.assertEqual(len(data_rows(out)[1]), 2)

    def test_idle_edges_trimmed(self):
        values = [7, 7, 7, 7 + 4 * self.MB, 7 + 8 * self.MB, 7 + 8 * self.MB]
        code, out, err = self.convert([counter_series("a", values)], prom_mapping("a"))
        self.assertEqual(code, 0, err)
        _, rows = data_rows(out)
        self.assertEqual([(r[0], r[4]) for r in rows], [(2, 4.0), (3, 4.0)])

    def test_min_offered_threshold(self):
        values = [0, 1000, 2000 + 50 * self.MB, 3000 + 100 * self.MB, 4000 + 100 * self.MB]
        code, out, err = self.convert([counter_series("a", values)], prom_mapping("a"),
                                      "--min-offered-mbit", "1")
        self.assertEqual(code, 0, err)
        _, rows = data_rows(out)
        self.assertEqual([r[0] for r in rows], [1, 2])

    def test_unmatched_series_ignored_unmatched_entry_warns(self):
        series = [counter_series("a", [0, self.MB]), counter_series("a", [0, self.MB], device="lo")]
        code, out, err = self.convert(series, prom_mapping("a", "missing"))
        self.assertEqual(code, 0, err)
        self.assertIn("matches no series", err)
        self.assertEqual(len(data_rows(out)[1]), 1)

    def test_ambiguous_entry_rejected(self):
        series = [counter_series("a", [0, self.MB]),
                  counter_series("a", [0, self.MB], start=2000)]
        code, _, err = self.convert(series, prom_mapping("a"))
        self.assertEqual(code, 1)
        self.assertIn("add labels to disambiguate", err)

    def test_series_matching_two_entries_rejected(self):
        m = prom_mapping("a")
        m["prometheus"].append({"match": {"instance": "a"}, "flow_id": 99,
                                "source_terminal": 3, "destination_terminal": 4})
        code, _, err = self.convert([counter_series("a", [0, self.MB])], m)
        self.assertEqual(code, 1)
        self.assertIn("several mapping entries", err)

    def test_bad_responses_rejected(self):
        m = self.write_json("m.json", prom_mapping("a"))
        cases = {
            "vector": {"status": "success", "data": {"resultType": "vector", "result": []}},
            "status": {"status": "error", "error": "bad query"},
            "nan": matrix([counter_series("a", [0, "NaN"])]),
            "order": matrix([{"metric": {"instance": "a", "device": "enp7s0"},
                              "values": [[10, "0"], [5, "1"]]}]),
            "value": matrix([{"metric": {"instance": "a", "device": "enp7s0"},
                              "values": [[10, "zero"]]}]),
        }
        for name, obj in cases.items():
            q = self.write_json(f"{name}.json", obj)
            code, _, err = run_cli("prometheus", "--mapping", m, "--interval-seconds", "5", q)
            self.assertEqual(code, 1, name)
            self.assertIn("error:", err, name)

    def test_bare_data_object_accepted(self):
        q = self.write_json("q.json", matrix([counter_series("a", [0, self.MB])])["data"])
        m = self.write_json("m.json", prom_mapping("a"))
        code, _, err = run_cli("prometheus", "--mapping", m, "--interval-seconds", "5", q)
        self.assertEqual(code, 0, err)


# --------------------------------------------------------------------------- mapping


class MappingTest(TempDirCase):
    def base(self):
        return json.loads(MAPPING.read_text())

    def expect_error(self, mapping, needle, mode="iperf3"):
        m = self.write_json("m.json", mapping)
        inputs = IPERF3_SAMPLES if mode == "iperf3" else [PROM_SAMPLE]
        code, _, err = run_cli(mode, "--mapping", m, "--interval-seconds", "1", *inputs)
        self.assertEqual(code, 1, err)
        self.assertIn(needle, err)

    def test_terminal_ids_match_ffw_parser(self):
        names = ffw.load_topology_terminals(REPO / "topology" / "fabric-sites.json")
        self.assertEqual(names, ["LOSA.0", "LOSA.1", "SALT.0", "SALT.1", "STAR.0", "STAR.1",
                                 "NEWY.0", "NEWY.1", "MICH.0", "MICH.1"])

    def test_example_mapping_loads(self):
        terms = ffw.load_topology_terminals(REPO / "topology" / "fabric-sites.json")
        for mode in ("iperf3", "prometheus"):
            entries = ffw.load_mapping(MAPPING, mode, terms)
            self.assertEqual(len(entries), 3)

    def test_schema_version(self):
        m = self.base()
        del m["schema_version"]
        self.expect_error(m, "schema_version")

    def test_missing_section(self):
        m = self.base()
        del m["iperf3"]
        self.expect_error(m, "non-empty 'iperf3' array")

    def test_unknown_key(self):
        m = self.base()
        m["iperf3"][0]["flowid"] = 5
        self.expect_error(m, "unknown key")

    def test_missing_key(self):
        m = self.base()
        del m["iperf3"][0]["destination_terminal"]
        self.expect_error(m, "missing required key 'destination_terminal'")

    def test_duplicate_flow_id(self):
        m = self.base()
        m["iperf3"][1]["flow_id"] = m["iperf3"][0]["flow_id"]
        self.expect_error(m, "globally unique")

    def test_bad_flow_ids(self):
        for bad in (True, -1, 2**64, "7", 1.5):
            m = self.base()
            m["iperf3"][0]["flow_id"] = bad
            self.expect_error(m, "flow_id")

    def test_terminal_out_of_range(self):
        m = self.base()
        m["iperf3"][0]["destination_terminal"] = 10
        self.expect_error(m, "out of range")

    def test_unknown_terminal_name(self):
        m = self.base()
        m["iperf3"][0]["source_terminal"] = "DALL.0"
        self.expect_error(m, "unknown terminal name")

    def test_same_source_and_destination(self):
        m = self.base()
        m["iperf3"][0]["destination_terminal"] = "LOSA.0"
        self.expect_error(m, "source and destination terminal are both 0")

    def test_bad_iperf3_match_key(self):
        m = self.base()
        m["iperf3"][0]["match"] = {"hostname": "x"}
        self.expect_error(m, "unsupported iperf3 key")

    def test_input_without_entry(self):
        m = self.base()
        m["iperf3"][0]["match"] = {"file": "other.json"}
        self.expect_error(m, "no iperf3 mapping entry matches")

    def test_entry_matching_two_inputs(self):
        m = self.base()
        m["iperf3"] = [
            {"match": {"local_host": "127.0.0.1"}, "flow_id": 1, "source_terminal": 0,
             "destination_terminal": 8},
        ]
        self.expect_error(m, "matches both")

    def test_input_matching_two_entries(self):
        m = self.base()
        m["iperf3"].append({"match": {"local_host": "127.0.0.1"}, "flow_id": 7,
                            "source_terminal": 0, "destination_terminal": 8})
        self.expect_error(m, "several mapping entries")

    def test_split_stride_collision(self):
        m = self.base()
        m["prometheus"][0]["flow_id"] = m["prometheus"][2]["flow_id"] + 1_000_000
        m2 = self.write_json("m.json", m)
        code, _, err = run_cli("prometheus", "--mapping", m2, "--interval-seconds", "5",
                               "--gap-policy", "split", PROM_SAMPLE)
        self.assertEqual(code, 1)
        self.assertIn("split-flow-id-stride", err)


class Iperf3InputTest(TempDirCase):
    def mutated(self, fn, name="udp-losa-to-mich-200M.json"):
        data = json.loads(IPERF3_SAMPLES[0].read_text())
        fn(data)
        return self.write_json(name, data)

    def expect_error(self, path, needle, *extra):
        code, _, err = run_cli("iperf3", "--mapping", MAPPING, "--interval-seconds", "1",
                               *extra, path)
        self.assertEqual(code, 1, err)
        self.assertIn(needle, err)

    def test_reverse_rejected(self):
        p = self.mutated(lambda d: d["start"]["test_start"].update(reverse=1))
        self.expect_error(p, "reverse")

    def test_omit_rejected(self):
        p = self.mutated(lambda d: d["start"]["test_start"].update(omit=2))
        self.expect_error(p, "--omit")

    def test_error_result_rejected(self):
        p = self.mutated(lambda d: d.update(error="unable to connect to server"))
        self.expect_error(p, "iperf3 reported an error")

    def test_not_iperf3(self):
        p = self.write_json("udp-losa-to-mich-200M.json", {"hello": 1})
        self.expect_error(p, "not an iperf3")

    def test_target_needs_bitrate(self):
        def zero(d):
            d["start"]["test_start"]["target_bitrate"] = 0
            d["start"]["target_bitrate"] = 0
        p = self.mutated(zero)
        self.expect_error(p, "needs a target bitrate", "--offered", "target")

    def test_negative_bytes_rejected(self):
        p = self.mutated(lambda d: d["intervals"][3]["sum"].update(bytes=-1))
        self.expect_error(p, "negative bytes")

    def test_sender_limited_udp_warns(self):
        def slow(d):
            for iv in d["intervals"]:
                iv["sum"]["bytes"] //= 2
        p = self.mutated(slow)
        code, _, err = run_cli("iperf3", "--mapping", MAPPING, "--interval-seconds", "1", p)
        self.assertEqual(code, 0, err)
        self.assertIn("below 95%", err)

    def test_title_match(self):
        # the example mapping matches the 500M sample by --title, not by file name
        data = json.loads(IPERF3_SAMPLES[1].read_text())
        p = self.write_json("renamed.json", data)
        code, out, err = run_cli("iperf3", "--mapping", MAPPING, "--interval-seconds", "1", p)
        self.assertEqual(code, 0, err)
        self.assertEqual({r[1] for r in data_rows(out)[1]}, {1002})


# --------------------------------------------------------------------------- validator


GOOD = """# comment
interval,flow_id,source_terminal,destination_terminal,offered_gbit
0,1,0,8,1.5   # trailing comment
1,1,0,8,2
3,2,2,4,0.1
"""


class ValidatorTest(TempDirCase):
    def test_accepts_good(self):
        stats = ffw.validate_trace_text(GOOD, num_terminals=10, num_send_intervals=4)
        self.assertEqual(stats, {"rows": 3, "flows": 2, "last_interval": 3,
                                 "unit": "offered_gbit"})

    def test_rejections(self):
        header = "interval,flow_id,source_terminal,destination_terminal,offered_mbit\n"
        cases = {
            "header must be exactly": "interval,flow,source_terminal,destination_terminal,offered_mbit\n0,1,0,1,1\n",
            "header must be exactly ": "interval,flow_id,source_terminal,destination_terminal,offered_bits\n",
            "fields; expected 5": header + "0,1,0,1\n",
            "invalid interval": header + "-1,1,0,1,1\n",
            "invalid flow_id": header + "0,x,0,1,1\n",
            "invalid source_terminal": header + "0,1,10,1,1\n",
            "invalid destination_terminal": header + "0,1,1,1,1\n",
            "must be > 0": header + "0,1,0,1,0\n",
            "must be > 0 ": header + "0,1,0,1,nan\n",
            "duplicate flow_id": header + "0,1,0,1,1\n0,1,0,1,1\n",
            "changes source/destination": header + "0,1,0,1,1\n1,1,0,2,1\n",
            "every interval from 0 through 2": header + "0,1,0,1,1\n2,1,0,1,1\n",
            "no traffic records": header,
            "missing header": "# only a comment\n",
        }
        for needle, text in cases.items():
            with self.assertRaises(ffw.TraceError, msg=needle) as cm:
                ffw.validate_trace_text(text, num_terminals=10)
            self.assertIn(needle.strip(), str(cm.exception))

    def test_num_send_intervals_bound(self):
        with self.assertRaises(ffw.TraceError):
            ffw.validate_trace_text(GOOD, num_terminals=10, num_send_intervals=3)

    def test_validate_subcommand(self):
        p = self.tmp / "t.csv"
        p.write_text(GOOD)
        self.assertEqual(run_cli("validate", p, "--num-terminals", "10")[0], 0)
        p.write_text(GOOD.replace("1,1,0,8,2", "1,1,0,8,-2"))
        self.assertEqual(run_cli("validate", p)[0], 1)

    def test_rounding_to_zero_is_idle(self):
        # 0.4 bit rounds to 0 at 9 Gbit decimals: must not be emitted as a 0 row
        self.assertEqual(ffw.format_volume(0.4e-6, "gbit"), "0")
        self.assertEqual(ffw.format_volume(123.4567894, "gbit"), "0.123456789")
        self.assertEqual(ffw.format_volume(250.0, "mbit"), "250")


if __name__ == "__main__":
    unittest.main()
