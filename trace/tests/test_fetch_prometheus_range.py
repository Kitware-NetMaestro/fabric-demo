"""
Offline tests for trace/fetch-prometheus-range.py (stdlib unittest only).

A local http.server in a background thread plays Prometheus; nothing leaves the
machine. The round-trip test feeds the fetched file to the converter and checks it
reproduces the committed prometheus golden trace.

Run from the repository root:
    python3 -m unittest discover -s trace/tests -v
"""

import base64
import contextlib
import http.server
import importlib.util
import io
import json
import os
import tempfile
import threading
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

TRACE_DIR = Path(__file__).resolve().parent.parent
PROM_SAMPLE = TRACE_DIR / "samples" / "prometheus" / "node-transmit-bytes-query-range.json"
MAPPING = TRACE_DIR / "mappings" / "fabric-5site-example.json"
GOLDEN = TRACE_DIR / "expected" / "prometheus-5s-split.csv"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


fetcher = load("prom_fetch", TRACE_DIR / "fetch-prometheus-range.py")
converter = load("ffw_trace_for_fetch", TRACE_DIR / "fabric-metrics-to-ffw-trace.py")


class MockPrometheus:
    """Serves one canned (status, body) for /api/v1/query_range and records requests."""

    def __init__(self, body: bytes, status: int = 200):
        self.body, self.status, self.requests = body, status, []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                parsed = urllib.parse.urlsplit(self.path)
                outer.requests.append(
                    {
                        "path": parsed.path,
                        "params": dict(urllib.parse.parse_qsl(parsed.query)),
                        "headers": {k.lower(): v for k, v in self.headers.items()},
                    }
                )
                if parsed.path != "/api/v1/query_range":
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(outer.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(outer.body)))
                self.end_headers()
                self.wfile.write(outer.body)

            def log_message(self, *args):  # keep test output clean
                pass

        self.server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


def run(mod, *argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = mod.main([str(a) for a in argv])
    return code, out.getvalue(), err.getvalue()


class FetchTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.sample = PROM_SAMPLE.read_bytes()

    def tearDown(self):
        self._tmp.cleanup()

    def fetch_args(self, url, out, **over):
        args = {
            "--base-url": url,
            "--query": 'node_network_transmit_bytes_total{job="node"}',
            "--start": "1790776800",
            "--end": "1790776860",
            "--step": "5",
            "-o": out,
        }
        args.update(over)
        flat = []
        for k, v in args.items():
            flat += [k, v]
        return flat

    def test_writes_body_verbatim_and_sends_query(self):
        out = self.tmp / "tx.json"
        with MockPrometheus(self.sample) as prom:
            code, _, err = run(fetcher, *self.fetch_args(prom.url + "/", out))
        self.assertEqual(code, 0, err)
        self.assertEqual(out.read_bytes(), self.sample)
        (req,) = prom.requests
        self.assertEqual(req["path"], "/api/v1/query_range")  # trailing slash in base URL handled
        self.assertEqual(
            req["params"],
            {
                "query": 'node_network_transmit_bytes_total{job="node"}',
                "start": "1790776800",
                "end": "1790776860",
                "step": "5",
            },
        )
        self.assertNotIn("authorization", req["headers"])

    def test_round_trip_through_converter_matches_golden(self):
        # Same basename as the committed sample, so the converter's provenance
        # header (which names inputs by basename) is identical too.
        fetched = self.tmp / PROM_SAMPLE.name
        with MockPrometheus(self.sample) as prom:
            code, _, err = run(fetcher, *self.fetch_args(prom.url, fetched))
        self.assertEqual(code, 0, err)
        trace = self.tmp / "trace.csv"
        code, _, err = run(
            converter, "prometheus", "--mapping", MAPPING, "--interval-seconds", "5",
            "--gap-policy", "split", fetched, "-o", trace,
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(trace.read_text(), GOLDEN.read_text())

    def test_basic_auth_from_env_and_extra_header(self):
        out = self.tmp / "tx.json"
        with MockPrometheus(self.sample) as prom, mock.patch.dict(
            os.environ, {"PROM_PASSWORD": "s3cret"}
        ):
            code, _, err = run(
                fetcher, *self.fetch_args(prom.url, out), "--user", "mfuser",
                "--header", "X-Scope-OrgID: fabric",
            )
        self.assertEqual(code, 0, err)
        headers = prom.requests[0]["headers"]
        self.assertEqual(
            headers["authorization"], "Basic " + base64.b64encode(b"mfuser:s3cret").decode()
        )
        self.assertEqual(headers["x-scope-orgid"], "fabric")

    def test_user_without_password_env_is_argument_error(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            code, _, err = run(
                fetcher, *self.fetch_args("http://127.0.0.1:9", self.tmp / "x.json"),
                "--user", "mfuser",
            )
        self.assertEqual(code, 2)
        self.assertIn("PROM_PASSWORD", err)

    def test_prometheus_error_envelope_is_reported(self):
        body = json.dumps(
            {"status": "error", "errorType": "bad_data", "error": "parse error at char 5"}
        ).encode()
        out = self.tmp / "tx.json"
        with MockPrometheus(body, status=400) as prom:
            code, _, err = run(fetcher, *self.fetch_args(prom.url, out))
        self.assertEqual(code, 1)
        self.assertIn("HTTP 400", err)
        self.assertIn("parse error", err)
        self.assertFalse(out.exists())

    def test_non_matrix_result_rejected(self):
        body = json.dumps({"status": "success", "data": {"resultType": "vector", "result": []}}).encode()
        out = self.tmp / "tx.json"
        with MockPrometheus(body) as prom:
            code, _, err = run(fetcher, *self.fetch_args(prom.url, out))
        self.assertEqual(code, 1)
        self.assertIn("matrix", err)
        self.assertFalse(out.exists())

    def test_empty_result_warns_but_writes(self):
        body = json.dumps({"status": "success", "data": {"resultType": "matrix", "result": []}}).encode()
        out = self.tmp / "tx.json"
        with MockPrometheus(body) as prom:
            code, _, err = run(fetcher, *self.fetch_args(prom.url, out))
        self.assertEqual(code, 0)
        self.assertIn("no series", err)
        self.assertEqual(out.read_bytes(), body)

    def test_unreachable_server(self):
        code, _, err = run(
            fetcher, *self.fetch_args("http://127.0.0.1:9", self.tmp / "x.json"),
            "--timeout", "2",
        )
        self.assertEqual(code, 1)
        self.assertIn("cannot reach", err)

    def test_too_many_points_refused_before_sending(self):
        code, _, err = run(
            fetcher, *self.fetch_args("http://127.0.0.1:9", self.tmp / "x.json",
                                      **{"--end": "1790820000", "--step": "1"})
        )
        self.assertEqual(code, 2)
        self.assertIn("11000", err)

    def test_time_and_step_parsing(self):
        self.assertEqual(fetcher.parse_time("1790776800"), 1790776800.0)
        self.assertEqual(fetcher.parse_time("2026-10-01T00:00:00Z"), 1790812800.0)
        self.assertEqual(fetcher.parse_step("5s"), 5)
        self.assertEqual(fetcher.parse_step("1m30s"), 90)
        self.assertEqual(fetcher.parse_step("2.5"), 2.5)
        with self.assertRaises(fetcher.FetchError):
            fetcher.parse_time("2026-10-01T00:00:00")  # no timezone
        with self.assertRaises(fetcher.FetchError):
            fetcher.parse_step("fast")
        with self.assertRaises(fetcher.FetchError):
            fetcher.parse_step("0")


if __name__ == "__main__":
    unittest.main()
