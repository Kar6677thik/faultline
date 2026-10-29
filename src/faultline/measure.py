"""Measure wall time, container memory, and leaked resources over serial runs (AT-026).

    python -m faultline.measure examples/orders/lost-response.yaml --variant correct \
        --runs 20 --output .faultline/measure

Each run is an ordinary `faultline run` in fresh containers. A background thread samples
one-shot `docker stats` for Faultline-owned containers. Memory is the working set (usage
minus inactive file cache), the same figure `docker stats` displays. Samples are periodic,
so a spike shorter than the interval can be missed; peaks are lower bounds.
"""

import argparse
import json
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path

import docker

from faultline.artifacts import atomic_json, utc_now

LABEL = {"label": "io.faultline.managed=true"}


def working_set(stats: dict) -> int:
    memory = stats.get("memory_stats") or {}
    usage = memory.get("usage", 0)
    detail = memory.get("stats") or {}
    return max(0, usage - detail.get("inactive_file", detail.get("total_inactive_file", 0)))


class Sampler(threading.Thread):
    def __init__(self, client, interval: float):
        super().__init__(daemon=True)
        self.client, self.interval = client, interval
        self.stop_event = threading.Event()
        self.peaks, self.peak_total, self.samples = {}, 0, 0

    def run(self):
        while not self.stop_event.is_set():
            total = 0
            for container in self.client.containers.list(filters=LABEL):
                try:
                    used = working_set(container.stats(stream=False, one_shot=True))
                except docker.errors.APIError:
                    continue  # the container ended between listing and sampling
                role = container.name.rsplit("-", 1)[-1]
                self.peaks[role] = max(self.peaks.get(role, 0), used)
                total += used
            self.peak_total = max(self.peak_total, total)
            self.samples += 1
            self.stop_event.wait(self.interval)


def leaked(client) -> dict:
    return {
        "containers": len(client.containers.list(all=True, filters=LABEL)),
        "networks": len(client.networks.list(filters=LABEL)),
    }


def measure(scenario: Path, variant: str, runs: int, output: Path, interval: float) -> dict:
    client = docker.from_env()
    info = client.info()
    output.mkdir(parents=True, exist_ok=True)
    record = {
        "measurement_schema_version": 1,
        "scenario": str(scenario),
        "variant": variant,
        "started_at": utc_now(),
        "host": {
            "cpus": info.get("NCPU"),
            "memory_bytes": info.get("MemTotal"),
            "kernel": info.get("KernelVersion"),
            "docker": info.get("ServerVersion"),
            "operating_system": info.get("OperatingSystem"),
        },
        "leaked_before": leaked(client),
        "runs": [],
    }
    sampler = Sampler(client, interval)
    sampler.start()
    try:
        for index in range(runs):
            started = time.monotonic()
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "faultline",
                    "--output",
                    str(output),
                    "--format",
                    "json",
                    "run",
                    str(scenario),
                    "--variant",
                    variant,
                ],
                capture_output=True,
                text=True,
                timeout=900,
            )
            seconds = round(time.monotonic() - started, 2)
            envelope = json.loads(result.stdout)
            record["runs"].append(
                {
                    "index": index + 1,
                    "wall_seconds": seconds,
                    "verdict": envelope["outcome"],
                    "run_ids": envelope["run_ids"],
                    "leaked_after": leaked(client),
                }
            )
            atomic_json(output / "measurement.json", record)
            print(f"run {index + 1}/{runs}: {envelope['outcome']} {seconds}s", file=sys.stderr)
    finally:
        sampler.stop_event.set()
        sampler.join()
    walls = [run["wall_seconds"] for run in record["runs"]]
    record.update(
        completed_at=utc_now(),
        wall_seconds={
            "first": walls[0],
            "median": round(statistics.median(walls), 2),
            "max": max(walls),
            "total": round(sum(walls), 2),
        },
        verdicts={
            v: sum(r["verdict"] == v for r in record["runs"])
            for v in {r["verdict"] for r in record["runs"]}
        },
        peak_memory_bytes={"by_role": sampler.peaks, "all_containers": sampler.peak_total},
        memory_samples=sampler.samples,
        leaked_after_all=leaked(client),
    )
    atomic_json(output / "measurement.json", record)
    client.close()
    return record


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("scenario", type=Path)
    parser.add_argument("--variant", required=True)
    parser.add_argument("--runs", type=int, default=20)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--interval", type=float, default=0.5)
    args = parser.parse_args(argv)
    if not 1 <= args.runs <= 100 or not 0.1 <= args.interval <= 10:
        parser.error("runs must be 1-100 and interval 0.1-10 seconds")
    record = measure(
        args.scenario.resolve(), args.variant, args.runs, args.output.resolve(), args.interval
    )
    summary = {k: record[k] for k in ("wall_seconds", "peak_memory_bytes", "leaked_after_all")}
    print(json.dumps(summary))
    clean = record["leaked_after_all"] == {"containers": 0, "networks": 0}
    return 0 if clean and set(record["verdicts"]) == {"PASS"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
