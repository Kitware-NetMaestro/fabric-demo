#!/usr/bin/env python3
"""
Generate the SYNTHETIC Prometheus sample committed next to this script.

node-transmit-bytes-query-range.json imitates the response of

    GET /api/v1/query_range?query=node_network_transmit_bytes_total
        &start=1790776800&end=1790776860&step=5

against an MFLib-style Prometheus scraping node-exporter on three FABRIC slice
nodes. It is not a capture from FABRIC: the numbers are made up, but the shape
is faithful to the Prometheus HTTP API (status/data/resultType "matrix"/result
[{metric, values: [[<unix seconds>, "<value string>"], ...]}]) and to
node-exporter's label set (__name__, device, instance, job).

Series (all 5 s apart, 13 samples, 60 s):
  losa-w1 enp7s0  ~20 Gbps for the whole window, +-5 % wobble
  newy-w1 enp7s0  idle, then 40 Gbps from t=20 s to t=50 s, then idle
                  (leading/trailing idle intervals are trimmed by the converter)
  salt-w1 enp7s0  10 Gbps with a counter reset between t=30 s and t=35 s
                  (node-exporter restart / NIC driver reload: the counter drops
                  back near zero), exercising the negative-delta clamp
  losa-w1 lo      loopback interface; present in the response but deliberately
                  not in the mapping file, so the converter ignores it
  losa-w1 enp3s0  management interface; likewise unmapped

Deterministic: no randomness, no clocks. Run it to rewrite the sample:
    python3 trace/samples/prometheus/make-synthetic-samples.py
"""

import json
from pathlib import Path

START = 1790776800  # 2026-09-30T13:20:00Z, an arbitrary fixed epoch
STEP = 5
N = 13
WOBBLE = [1.00, 1.03, 0.97, 1.05, 0.95, 1.02, 0.98, 1.04, 0.96, 1.01, 0.99, 1.00]


def bytes_per_step(gbps):
    return int(gbps * 1e9 / 8 * STEP)


def series(instance, device, deltas, base, reset_at=None, reset_value=0):
    """Counter samples from per-step byte deltas; optional reset before sample reset_at."""
    values = []
    v = base
    for i in range(N):
        if i > 0:
            if reset_at is not None and i == reset_at:
                v = reset_value
            else:
                v += deltas[i - 1]
        values.append([START + i * STEP, str(v)])
    return {
        "metric": {
            "__name__": "node_network_transmit_bytes_total",
            "device": device,
            "instance": instance,
            "job": "node",
        },
        "values": values,
    }


def main():
    losa = [int(bytes_per_step(20) * w) for w in WOBBLE]
    newy = [bytes_per_step(40) if 4 <= i < 10 else 0 for i in range(N - 1)]
    salt = [bytes_per_step(10)] * (N - 1)
    lo = [40_000] * (N - 1)
    mgmt = [150_000 + 1000 * i for i in range(N - 1)]

    result = [
        series("losa-w1.fabric-demo:9100", "enp3s0", mgmt, 912_345_678),
        series("losa-w1.fabric-demo:9100", "enp7s0", losa, 81_234_567_890_123),
        series("losa-w1.fabric-demo:9100", "lo", lo, 5_555_555),
        series("newy-w1.fabric-demo:9100", "enp7s0", newy, 42_000_000_000_000),
        # Reset: the sample at t=35 s reads 3.1 GB (bytes sent since the restart)
        # instead of continuing from ~2.2e13; the true volume of that 5 s step is
        # unrecoverable from the counter alone.
        series(
            "salt-w1.fabric-demo:9100",
            "enp7s0",
            salt,
            22_000_000_000_000,
            reset_at=7,
            reset_value=3_100_000_000,
        ),
    ]
    response = {"status": "success", "data": {"resultType": "matrix", "result": result}}
    out = Path(__file__).with_name("node-transmit-bytes-query-range.json")
    out.write_text(json.dumps(response, indent=1) + "\n")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
