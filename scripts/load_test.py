from __future__ import annotations

import argparse
import concurrent.futures
import statistics
import time
import urllib.error
import urllib.request


def main() -> None:
    parser = argparse.ArgumentParser(description="Teste de carga seguro do EBF Técnico V2")
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--requests", type=int, default=5000)
    parser.add_argument("--concurrency", type=int, default=250)
    parser.add_argument("--mode", choices=("mixed", "api", "static"), default="mixed")
    args = parser.parse_args()
    paths = {
        "mixed": ("/healthz", "/", "/styles.css", "/app.js"),
        "api": ("/healthz",),
        "static": ("/", "/styles.css", "/app.js"),
    }[args.mode]

    def hit(index: int) -> tuple[float, bool]:
        started = time.perf_counter()
        try:
            with urllib.request.urlopen(args.url.rstrip("/") + paths[index % len(paths)], timeout=30) as response:
                response.read()
                success = response.status == 200
        except (OSError, urllib.error.URLError):
            success = False
        return time.perf_counter() - started, success

    started = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as pool:
        results = list(pool.map(hit, range(args.requests)))
    elapsed = time.perf_counter() - started
    durations = sorted(item[0] for item in results)
    errors = sum(not item[1] for item in results)
    percentile = lambda value: durations[min(len(durations) - 1, int(len(durations) * value))]
    print({
        "requests": args.requests,
        "concurrency": args.concurrency,
        "errors": errors,
        "seconds": round(elapsed, 3),
        "requests_per_second": round(args.requests / elapsed, 1),
        "mean_ms": round(statistics.mean(durations) * 1000, 1),
        "p50_ms": round(percentile(.50) * 1000, 1),
        "p95_ms": round(percentile(.95) * 1000, 1),
        "p99_ms": round(percentile(.99) * 1000, 1),
        "max_ms": round(max(durations) * 1000, 1),
    })


if __name__ == "__main__":
    main()
