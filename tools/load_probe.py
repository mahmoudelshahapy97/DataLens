#!/usr/bin/env python3
"""Concurrent load against a running stack, with the connection count beside it.

The point is not to find the throughput ceiling. It is to answer one question
that argument cannot settle: **when N people use this at once, how many database
connections does it actually take, and does anything fail?**

Three numbers get confused in that conversation and this keeps them apart:

    maximum possible    what the configuration permits    (arithmetic)
    maximum configured  what the budget allows            (policy)
    actually observed   what happened                     (this tool)

The first two come from `/metrics` and the settings; the third is sampled from
`pg_stat_activity` in a background thread, so the application is not the only
witness to its own behaviour.

    python tools/load_probe.py --users 24 --rounds 3 \\
        --email demo@example.com --password ... --label baseline

Writes a JSON line per run to `load_probe.jsonl` so two runs can be diffed
directly. Read-only against the app: it signs in, asks for schema and history and
runs one cheap query per user. It does **not** ask the LLM a question -- that adds
seconds of variance per request and tells you nothing about connections.
"""

from __future__ import annotations

import argparse
import json
import statistics
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Endpoints one user touches per round. Deliberately the cheap, control-plane
# heavy ones: they are what every page load does, and they are where pool
# contention shows up first.
CALLS: Tuple[Tuple[str, str], ...] = (
    ("GET", "/api/vanna/v2/me"),
    ("GET", "/api/vanna/v2/conversations?limit=10"),
    ("GET", "/api/vanna/v2/history?limit=10"),
    ("GET", "/api/vanna/v2/schema"),
    ("GET", "/api/vanna/v2/dashboards"),
    ("GET", "/api/vanna/v2/usage"),
)

QUERY = {"sql": "SELECT 1 AS probe"}


class Session:
    """One signed-in user, one cookie jar."""

    def __init__(self, base: str, tenant: str) -> None:
        self.base = base.rstrip("/")
        self.tenant = tenant
        self.jar = CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar)
        )

    def call(self, method: str, path: str, body: Any = None) -> Tuple[int, float]:
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Accept": "application/json", "X-Tenant-Id": self.tenant}
        if data:
            headers["Content-Type"] = "application/json"
        for cookie in self.jar:
            if cookie.name == "vanna_csrf":
                headers["X-CSRF-Token"] = urllib.parse.unquote(cookie.value or "")
        request = urllib.request.Request(
            f"{self.base}{path}", data=data, headers=headers, method=method
        )
        started = time.perf_counter()
        try:
            with self.opener.open(request, timeout=120) as response:
                response.read()
                return response.status, time.perf_counter() - started
        except urllib.error.HTTPError as exc:
            exc.read()
            return exc.code, time.perf_counter() - started
        except Exception:
            return 0, time.perf_counter() - started

    def sign_in(self, email: str, password: str) -> bool:
        self.call("GET", "/")
        status, _ = self.call(
            "POST",
            "/api/vanna/v2/auth/login",
            {"email": email, "password": password, "tenant": self.tenant},
        )
        return status == 200


class Watcher:
    """Samples the server's own connection count while the load runs."""

    def __init__(self, interval: float = 0.25) -> None:
        self.interval = interval
        self.samples: List[int] = []
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def _count(self) -> Optional[int]:
        import subprocess

        # Through the container, because the point is the *server's* view. If
        # docker is not reachable the run still produces latencies; it just
        # cannot report the number that matters most, and says so.
        try:
            out = subprocess.run(
                ["wsl", "-e", "bash", "-lc",
                 'docker exec db_postgres psql -U postgres -d postgres -tAc '
                 '"SELECT count(*) FROM pg_stat_activity WHERE pid <> pg_backend_pid()"'],
                capture_output=True, text=True, timeout=20,
            )
            return int((out.stdout or "").strip())
        except Exception:
            return None

    def _loop(self) -> None:
        while not self._stop.is_set():
            value = self._count()
            if value is not None:
                self.samples.append(value)
            self._stop.wait(self.interval)

    def __enter__(self) -> "Watcher":
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_exc: Any) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)


def _metrics_lines(base: str) -> List[str]:
    """The exposition text, however it can be reached.

    Over HTTP if the deployment exposes `/metrics`. This one does not -- nginx
    serves the app on 3000 and the endpoint lives on the backend's own port -- so
    fall back to asking the container. A probe that silently reported no gauges
    because of a proxy rule would look exactly like a probe reporting a healthy
    pool.
    """
    import subprocess

    try:
        with urllib.request.urlopen(f"{base.rstrip('/')}/metrics", timeout=10) as r:
            return r.read().decode(errors="replace").splitlines()
    except Exception:
        pass

    try:
        out = subprocess.run(
            ["wsl", "-e", "bash", "-lc",
             'docker exec vanna-backend-1 sh -lc '
             '"curl -sf http://127.0.0.1:8000/metrics"'],
            capture_output=True, text=True, timeout=30,
        )
        return (out.stdout or "").splitlines()
    except Exception:
        return []


def metrics_snapshot(base: str) -> Dict[str, float]:
    """The gauges the app publishes about itself, if Prometheus is enabled.

    Note what this can and cannot see: with four uvicorn workers, a scrape hits
    **one** of them. The pool gauges are therefore per-worker -- which is the point
    when the whole problem is that every worker holds its own pool -- while
    `pg_stat_activity` in `Watcher` is the only view of the total.
    """
    wanted = (
        "vanna_control_plane_pool_ceiling",
        "vanna_control_plane_pool_in_use",
        "vanna_control_plane_pool_waiters",
        "vanna_control_plane_pool_saturated_total",
        "vanna_tenant_runtimes",
    )
    out: Dict[str, float] = {}
    for line in _metrics_lines(base):
        if line.startswith("#") or " " not in line:
            continue
        name, _, value = line.rpartition(" ")
        base_name = name.split("{")[0]
        if base_name in wanted:
            try:
                out[base_name] = out.get(base_name, 0.0) + float(value)
            except ValueError:
                continue
    return out


def one_user(base: str, tenant: str, email: str, password: str,
             rounds: int) -> Tuple[List[float], List[int]]:
    session = Session(base, tenant)
    latencies: List[float] = []
    statuses: List[int] = []

    if not session.sign_in(email, password):
        return latencies, [401]

    for _ in range(rounds):
        for method, path in CALLS:
            status, seconds = session.call(method, path)
            latencies.append(seconds)
            statuses.append(status)
        status, seconds = session.call("POST", "/api/vanna/v2/run-sql", QUERY)
        latencies.append(seconds)
        statuses.append(status)
    return latencies, statuses


def percentile(values: List[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round(fraction * (len(ordered) - 1))))
    return ordered[index]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://localhost:3000")
    parser.add_argument("--email", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--users", type=int, default=16)
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--tenant", action="append",
                        help="Repeatable. Spread users across these workspaces.")
    parser.add_argument("--label", default="run",
                        help="Recorded with the result, e.g. 'baseline' or 'after-P0'.")
    parser.add_argument("--out", default="load_probe.jsonl")
    args = parser.parse_args()

    tenants = args.tenant or ["chinook"]
    print(f"{args.users} users x {args.rounds} rounds over {len(tenants)} workspace(s)")

    before = metrics_snapshot(args.url)
    started = time.time()
    with Watcher() as watcher:
        with ThreadPoolExecutor(max_workers=args.users) as pool:
            futures = [
                pool.submit(
                    one_user, args.url, tenants[i % len(tenants)],
                    args.email, args.password, args.rounds,
                )
                for i in range(args.users)
            ]
            results = [f.result() for f in futures]
    elapsed = time.time() - started
    after = metrics_snapshot(args.url)

    latencies = [x for pair in results for x in pair[0]]
    statuses = [s for pair in results for s in pair[1]]
    failures = [s for s in statuses if s == 0 or s >= 400]

    record = {
        "label": args.label,
        "users": args.users,
        "rounds": args.rounds,
        "tenants": tenants,
        "requests": len(statuses),
        "failures": len(failures),
        "failure_codes": sorted(set(failures)),
        "seconds": round(elapsed, 2),
        "rps": round(len(statuses) / elapsed, 1) if elapsed else 0,
        "p50_ms": round(percentile(latencies, 0.50) * 1000, 1),
        "p95_ms": round(percentile(latencies, 0.95) * 1000, 1),
        "p99_ms": round(percentile(latencies, 0.99) * 1000, 1),
        "max_ms": round(max(latencies) * 1000, 1) if latencies else 0,
        "mean_ms": round(statistics.mean(latencies) * 1000, 1) if latencies else 0,
        "pg_connections_peak": max(watcher.samples) if watcher.samples else None,
        "pg_connections_mean": (
            round(statistics.mean(watcher.samples), 1) if watcher.samples else None
        ),
        "pg_samples": len(watcher.samples),
        "metrics_before": before,
        "metrics_after": after,
    }

    with Path(args.out).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")

    print(f"\n  requests        {record['requests']}  ({record['rps']}/s)")
    print(f"  failures        {record['failures']} {record['failure_codes']}")
    print(f"  p50 / p95 / p99 {record['p50_ms']} / {record['p95_ms']} / "
          f"{record['p99_ms']} ms   (max {record['max_ms']})")
    print(f"  pg connections  peak {record['pg_connections_peak']}, "
          f"mean {record['pg_connections_mean']} "
          f"({record['pg_samples']} samples)")
    if after:
        print(f"  pool ceiling    {after.get('vanna_control_plane_pool_ceiling')}"
              f" per worker")
        print(f"  saturated       "
              f"{after.get('vanna_control_plane_pool_saturated_total', 0)} total")
    else:
        print("  (no /metrics -- set VANNA_METRICS_ENABLED=true for pool gauges)")
    print(f"\nappended to {args.out} as '{args.label}'")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
