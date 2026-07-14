#!/usr/bin/env python3
"""Measure endpoint P95 from a probe host in Shenzhen or Guangzhou."""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run this script on a host physically located in Shenzhen or Guangzhou."
    )
    parser.add_argument("--location", required=True, choices=("shenzhen", "guangzhou"))
    parser.add_argument("--url", required=True, help="HTTPS health or lightweight API URL")
    parser.add_argument("--samples", type=int, default=30)
    parser.add_argument("--warmup", type=int, default=3)
    parser.add_argument("--interval", type=float, default=0.2)
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument("--expected-status", type=int, default=200)
    parser.add_argument("--p95-threshold-ms", type=float, default=250.0)
    return parser.parse_args()


def one_probe(url: str, timeout: float, expected_status: int) -> float:
    started = time.perf_counter_ns()
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "skill-hub-p95-probe/0.1", "Cache-Control": "no-cache"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        response.read(1)
        if response.status != expected_status:
            raise RuntimeError(f"expected HTTP {expected_status}, received {response.status}")
    return (time.perf_counter_ns() - started) / 1_000_000


def percentile_nearest_rank(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    rank = max(1, math.ceil(percentile * len(ordered)))
    return ordered[rank - 1]


def main() -> int:
    args = parse_args()
    if args.samples < 2 or args.warmup < 0 or args.interval < 0:
        raise SystemExit("samples must be >=2; warmup and interval must be non-negative")
    if not args.url.startswith("https://"):
        raise SystemExit("probe URL must use HTTPS")

    failures: list[str] = []
    latencies: list[float] = []
    for index in range(args.warmup + args.samples):
        try:
            elapsed_ms = one_probe(args.url, args.timeout, args.expected_status)
            if index >= args.warmup:
                latencies.append(elapsed_ms)
        except (OSError, RuntimeError, urllib.error.URLError) as exc:
            if index >= args.warmup:
                failures.append(str(exc))
        if index + 1 < args.warmup + args.samples:
            time.sleep(args.interval)

    if not latencies:
        result = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "location": args.location,
            "url": args.url,
            "success": 0,
            "failures": len(failures),
            "error": "all probes failed",
        }
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 2

    p95_ms = percentile_nearest_rank(latencies, 0.95)
    result = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "location": args.location,
        "url": args.url,
        "success": len(latencies),
        "failures": len(failures),
        "min_ms": round(min(latencies), 2),
        "mean_ms": round(statistics.fmean(latencies), 2),
        "p50_ms": round(statistics.median(latencies), 2),
        "p95_ms": round(p95_ms, 2),
        "max_ms": round(max(latencies), 2),
        "threshold_ms": args.p95_threshold_ms,
    }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if not failures and p95_ms <= args.p95_threshold_ms else 1


if __name__ == "__main__":
    sys.exit(main())
