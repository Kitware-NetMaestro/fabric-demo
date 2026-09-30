#!/usr/bin/env bash
# Regenerate the committed iperf3 sample results by running iperf3 over the
# loopback interface. These are REAL iperf3 JSON outputs, but from a single
# machine talking to itself, not from a FABRIC slice: the rates are small
# (hundreds of Mbps) and the "sites" are only the names in the mapping file.
#
# Three concurrent, staggered flows so the converter has overlapping flows with
# different start times to align onto one FFW interval grid:
#   udp-losa-to-mich-200M.json  UDP, --bitrate 200M, 10 s, 1 s reporting interval
#   udp-newy-to-mich-500M.json  UDP, --bitrate 500M,  6 s, 0.5 s reporting interval
#   tcp-salt-to-star.json       TCP, --bitrate 1G,    5 s, 1 s reporting interval
#
# Usage (from anywhere; writes next to this script, overwriting the samples):
#   trace/samples/iperf3/generate-loopback-samples.sh
# Requires iperf3 with --json and --title; the committed samples were produced
# with iperf 3.22 (Homebrew, macOS arm64).
# Regenerating changes timestamps and byte counts, so the golden trace in
# trace/expected/ must be regenerated too (see trace/README.md).

set -euo pipefail

cd "$(dirname "$0")"

command -v iperf3 >/dev/null || { echo "iperf3 not found in PATH" >&2; exit 1; }

# One-shot servers (-1) on private ports so they exit after their test.
for port in 5301 5302 5303; do
    iperf3 -s -p "$port" -1 -D
done
sleep 1

iperf3 -c 127.0.0.1 -p 5301 -u -b 200M -t 10 -i 1 --title losa-to-mich-udp \
    --json > udp-losa-to-mich-200M.json &
sleep 2.3
iperf3 -c 127.0.0.1 -p 5302 -u -b 500M -t 6 -i 0.5 --title newy-to-mich-udp \
    --json > udp-newy-to-mich-500M.json &
sleep 1.6
iperf3 -c 127.0.0.1 -p 5303 -b 1G -t 5 -i 1 --title salt-to-star-tcp \
    --json > tcp-salt-to-star.json &
wait

echo "wrote $(pwd)/{udp-losa-to-mich-200M,udp-newy-to-mich-500M,tcp-salt-to-star}.json"
