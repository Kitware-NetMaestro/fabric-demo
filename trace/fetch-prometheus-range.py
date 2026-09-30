#!/usr/bin/env python3
"""
Fetch a Prometheus `query_range` result into a file the converter's prometheus mode reads.

    fetch-prometheus-range.py --base-url URL --query PROMQL --start T --end T --step S -o OUT.json

This issues one GET to <base-url>/api/v1/query_range with the given query, start,
end and step (Prometheus HTTP API), checks that the answer is a successful
resultType "matrix" response, and writes the response body UNCHANGED (byte for
byte) to OUT.json. `trace/fabric-metrics-to-ffw-trace.py prometheus` accepts
exactly that document (the full `{"status": "success", "data": {...}}` envelope),
so no reshaping happens and the file doubles as raw provenance.

Why this exists: MFLib (FABRIC's Measurement Framework library) instruments a
slice with a measurement node running Prometheus, which scrapes node-exporter on
every slice VM. Pulling the experiment window out of that Prometheus as a raw byte
counter is the expected MFLib export path for the converter (see trace/README.md).
On the MFLib measurement node Prometheus listens on https://localhost:9090 with
HTTP basic auth and a self-signed certificate (MFLib's own snapshot helper uses
`curl -k -u user:password https://localhost:9090/...`), so the usual way in is an
SSH tunnel through the FABRIC bastion plus --insecure and --user. UNTESTED against
a real MFLib deployment: requires FABRIC project access (docs/access-checklist.md).

Times: --start/--end accept unix seconds or RFC 3339 (`2026-10-05T14:00:00Z`),
passed through verbatim, as Prometheus accepts both. --step accepts seconds or a
Prometheus duration (`5s`, `1m`). Use a step no smaller than the scrape interval
and query the RAW counter (e.g. node_network_transmit_bytes_total), not rate().

Credentials: --user NAME, password from the environment variable named by
--password-env (default PROM_PASSWORD), never from the command line. Extra headers
(e.g. a bearer token) via --header 'Name: value'.

Prometheus refuses ranges of more than 11000 points per series; the tool checks
this before sending and asks you to raise --step or split the window.

stdlib only; Python >= 3.8. Exit status: 0 ok, 1 HTTP/network/Prometheus error,
2 bad arguments.
"""

from __future__ import annotations

import argparse
import base64
import datetime
import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

MAX_POINTS = 11000  # Prometheus' per-series resolution limit for query_range
DURATION_RE = re.compile(r"^(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m(?!s))?(?:(\d+)s)?(?:(\d+)ms)?$")


class FetchError(Exception):
    """User-facing error; exit_code 1 = remote/transport, 2 = arguments."""

    def __init__(self, msg: str, exit_code: int = 1):
        super().__init__(msg)
        self.exit_code = exit_code


def parse_time(text: str) -> float:
    """Unix seconds of a --start/--end value (for validation only; sent verbatim)."""
    try:
        return float(text)
    except ValueError:
        pass
    try:
        t = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as e:
        raise FetchError(f"bad time {text!r}: use unix seconds or RFC 3339", 2) from e
    if t.tzinfo is None:
        raise FetchError(f"time {text!r} has no timezone; append Z or an offset", 2)
    return t.timestamp()


def parse_step(text: str) -> float:
    try:
        v = float(text)
    except ValueError:
        m = DURATION_RE.match(text)
        if not m or not any(m.groups()):
            raise FetchError(f"bad --step {text!r}: seconds or a duration like 5s, 1m", 2)
        d, h, mi, s, ms = (int(g) if g else 0 for g in m.groups())
        v = d * 86400 + h * 3600 + mi * 60 + s + ms / 1000
    if not v > 0:
        raise FetchError("--step must be positive", 2)
    return v


def build_url(base_url: str, query: str, start: str, end: str, step: str) -> str:
    base = base_url.rstrip("/")
    params = urllib.parse.urlencode({"query": query, "start": start, "end": end, "step": step})
    return f"{base}/api/v1/query_range?{params}"


def check_response(body: bytes) -> dict:
    """Parse and validate a query_range response body; returns the parsed JSON."""
    try:
        doc = json.loads(body)
    except (ValueError, UnicodeDecodeError) as e:
        raise FetchError(f"response is not JSON: {e}") from e
    if not isinstance(doc, dict) or "status" not in doc:
        raise FetchError("response is not a Prometheus API envelope (no 'status')")
    if doc["status"] != "success":
        raise FetchError(
            f"Prometheus status {doc['status']!r}: {doc.get('errorType', '')} {doc.get('error', '')}".strip()
        )
    data = doc.get("data")
    if not isinstance(data, dict) or data.get("resultType") != "matrix":
        got = data.get("resultType") if isinstance(data, dict) else None
        raise FetchError(f"expected resultType 'matrix' (a query_range answer), got {got!r}")
    if not isinstance(data.get("result"), list):
        raise FetchError("response data.result is not a list")
    return doc


def fetch(
    base_url: str,
    query: str,
    start: str,
    end: str,
    step: str,
    user: str | None = None,
    password: str | None = None,
    headers: list[str] | None = None,
    insecure: bool = False,
    ca_file: str | None = None,
    timeout: float = 60.0,
) -> tuple[bytes, dict]:
    t_start, t_end, dt = parse_time(start), parse_time(end), parse_step(step)
    if t_end < t_start:
        raise FetchError("--end is before --start", 2)
    points = int((t_end - t_start) // dt) + 1
    if points > MAX_POINTS:
        raise FetchError(
            f"{points} points per series exceeds Prometheus' limit of {MAX_POINTS}; "
            "raise --step or split the window",
            2,
        )
    url = build_url(base_url, query, start, end, step)
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    if user is not None:
        token = base64.b64encode(f"{user}:{password or ''}".encode()).decode()
        req.add_header("Authorization", f"Basic {token}")
    for h in headers or []:
        name, sep, value = h.partition(":")
        if not sep or not name.strip():
            raise FetchError(f"bad --header {h!r}: expected 'Name: value'", 2)
        req.add_header(name.strip(), value.strip())
    ctx = None
    if url.startswith("https:"):
        ctx = ssl.create_default_context(cafile=ca_file)
        if insecure:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            body = resp.read()
    except urllib.error.HTTPError as e:
        # Prometheus returns a JSON error envelope with 400/422/503; surface it.
        detail = e.read()[:500].decode("utf-8", "replace")
        raise FetchError(f"HTTP {e.code} from {base_url}: {detail}") from e
    except (urllib.error.URLError, OSError) as e:
        raise FetchError(f"cannot reach {base_url}: {getattr(e, 'reason', e)}") from e
    return body, check_response(body)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Fetch a Prometheus query_range result for trace/fabric-metrics-to-ffw-trace.py.",
    )
    p.add_argument("--base-url", required=True,
                   help="Prometheus base URL, e.g. https://localhost:9090 (tunnel) or http://host:9090/prom")
    p.add_argument("--query", required=True,
                   help="PromQL; a raw counter, e.g. node_network_transmit_bytes_total{device=\"enp7s0\"}")
    p.add_argument("--start", required=True, help="unix seconds or RFC 3339")
    p.add_argument("--end", required=True, help="unix seconds or RFC 3339")
    p.add_argument("--step", required=True, help="seconds or duration (5s, 1m); >= scrape interval")
    p.add_argument("-o", "--output", type=Path, help="output file (default: stdout)")
    p.add_argument("--user", help="HTTP basic-auth user")
    p.add_argument("--password-env", default="PROM_PASSWORD",
                   help="environment variable holding the basic-auth password (default PROM_PASSWORD)")
    p.add_argument("--header", action="append", default=[], help="extra header 'Name: value' (repeatable)")
    p.add_argument("--insecure", action="store_true",
                   help="skip TLS verification (MFLib's Prometheus uses a self-signed certificate)")
    p.add_argument("--ca-file", help="CA bundle to verify the server certificate against")
    p.add_argument("--timeout", type=float, default=60.0, help="seconds (default 60)")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    password = os.environ.get(args.password_env) if args.user is not None else None
    try:
        if args.user is not None and password is None:
            raise FetchError(f"--user given but ${args.password_env} is not set", 2)
        body, doc = fetch(
            args.base_url, args.query, args.start, args.end, args.step,
            user=args.user, password=password, headers=args.header,
            insecure=args.insecure, ca_file=args.ca_file, timeout=args.timeout,
        )
    except FetchError as e:
        print(f"error: {e}", file=sys.stderr)
        return e.exit_code
    result = doc["data"]["result"]
    if not result:
        print("warning: query matched no series (empty result)", file=sys.stderr)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_bytes(body)
        print(
            f"wrote {args.output}: {len(result)} series, "
            f"{sum(len(s.get('values') or []) for s in result)} samples",
            file=sys.stderr,
        )
    else:
        sys.stdout.buffer.write(body)
    return 0


if __name__ == "__main__":
    sys.exit(main())
