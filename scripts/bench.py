#!/usr/bin/env python3
"""Measure ingest/snapshot wall time, peak RSS and state size at scale.

    python3 scripts/gen_scale.py              # once: writes scale/
    python3 scripts/bench.py [--data scale] [--verify]

Every command runs as a separate engine process, as in evaluation. Peak RSS
is measured per process (wait4 on POSIX, PeakWorkingSetSize on Windows).
--verify additionally compares every snapshot with the in-memory reference
evaluator over the same batches (checks the storage path, not the shared
business rules).
"""
import argparse
import json
import os
import platform
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def engine_cmd():
    if os.name == "posix":
        return [str(ROOT / "run")]
    return [sys.executable, "-B", "-m", "engine.cli"]


def run_measured(args):
    """Run one engine process; return (exit code, seconds, peak RSS bytes, stderr)."""
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    t0 = time.perf_counter()
    if os.name == "posix":
        rfd, wfd = os.pipe()
        pid = os.posix_spawn(args[0], args, env,
                             file_actions=[(os.POSIX_SPAWN_DUP2, wfd, 2), (os.POSIX_SPAWN_CLOSE, rfd)])
        os.close(wfd)
        with os.fdopen(rfd) as f:
            err = f.read()
        _, status, usage = os.wait4(pid, 0)
        code = os.waitstatus_to_exitcode(status)
        peak = usage.ru_maxrss * (1 if sys.platform == "darwin" else 1024)
    else:
        import ctypes
        from ctypes import wintypes

        class PMC(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                        ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]
        p = subprocess.Popen(args, env=env, stderr=subprocess.PIPE, text=True)
        _, err = p.communicate()
        code = p.returncode
        pmc = PMC()
        pmc.cb = ctypes.sizeof(PMC)
        ctypes.windll.psapi.GetProcessMemoryInfo(int(p._handle), ctypes.byref(pmc), pmc.cb)
        peak = pmc.PeakWorkingSetSize
    return code, time.perf_counter() - t0, peak, err


def dir_size(path: Path) -> int:
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def host_info():
    ram = None
    try:
        if sys.platform.startswith("linux"):
            with open("/proc/meminfo") as f:
                ram = int(f.readline().split()[1]) * 1024
        elif os.name == "nt":
            import ctypes

            class MS(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("rest", ctypes.c_ulonglong * 6)]
            ms = MS()
            ms.dwLength = ctypes.sizeof(MS)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(ms))
            ram = ms.ullTotalPhys
    except Exception:
        pass
    return {"platform": platform.platform(), "machine": platform.machine(),
            "processor": platform.processor() or "n/a", "cpus": os.cpu_count(),
            "ram_gib": round(ram / 2**30, 1) if ram else None,
            "python": platform.python_version(), "sqlite": sqlite3.sqlite_version}


def mib(n):
    return f"{n / 2**20:.0f} MiB"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", type=Path, default=ROOT / "scale")
    ap.add_argument("--state", type=Path, help="state dir (default: fresh temp dir)")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--json", type=Path, help="also write results as JSON")
    args = ap.parse_args()

    batches = sorted(args.data.glob("batch_*.jsonl"))
    queries = sorted(args.data.glob("query_*.json"))
    if not batches:
        sys.exit(f"no batches in {args.data}; run scripts/gen_scale.py first")
    tmp = Path(tempfile.mkdtemp(prefix="recon-bench-"))
    state = args.state or tmp / "state"
    results = {"host": host_info(), "ingest": [], "snapshot": []}
    print("host:", json.dumps(results["host"]))
    input_bytes = sum(b.stat().st_size for b in batches)
    print(f"input: {len(batches)} batches, {input_bytes / 2**20:.1f} MiB\n")

    print("| batch | rows | exit | wall s | peak RSS |\n|---|---:|---:|---:|---:|")
    for b in batches:
        rows = sum(1 for _ in b.open(encoding="utf-8"))
        code, secs, peak, err = run_measured(engine_cmd() + ["ingest", "--input", str(b.resolve()),
                                                             "--state", str(state)])
        if code != 0:
            sys.exit(f"ingest {b.name} failed ({code}): {err}")
        results["ingest"].append({"batch": b.name, "rows": rows, "seconds": secs, "peak_rss": peak})
        print(f"| {b.name} | {rows} | {code} | {secs:.2f} | {mib(peak)} |")
    total = sum(r["seconds"] for r in results["ingest"])
    peak_ingest = max(r["peak_rss"] for r in results["ingest"])
    results["ingest_total_seconds"] = total
    results["state_bytes_after_ingest"] = dir_size(state)
    print(f"\ningest total {total:.1f} s, max peak RSS {mib(peak_ingest)}, "
          f"state {results['state_bytes_after_ingest'] / 2**20:.1f} MiB\n")

    if args.verify:
        from engine.core import evaluate, parse_query
        from engine.records import parse_line
        all_rows = [parse_line(l) for b in batches for l in b.open(encoding="utf-8") if l.strip()]

    print("| query | exit | wall s | peak RSS | settlements | trace | exceptions | verified |\n"
          "|---|---:|---:|---:|---:|---:|---:|---|")
    for q in queries:
        out = tmp / f"{q.stem}.out.json"
        code, secs, peak, err = run_measured(engine_cmd() + [
            "snapshot", "--query", str(q.resolve()), "--state", str(state), "--out", str(out)])
        if code != 0:
            sys.exit(f"snapshot {q.name} failed ({code}): {err}")
        rep = json.loads(out.read_text(encoding="utf-8"))
        verified = "-"
        if args.verify:
            verified = "yes" if rep == evaluate(all_rows, parse_query(json.loads(q.read_text()))) else "NO"
        results["snapshot"].append({"query": q.stem, "seconds": secs, "peak_rss": peak,
                                    "settlements": len(rep["settlements"]), "trace": len(rep["trace"]),
                                    "exceptions": len(rep["exceptions"]), "verified": verified})
        print(f"| {q.stem} | {code} | {secs:.2f} | {mib(peak)} | {len(rep['settlements'])} | "
              f"{len(rep['trace'])} | {len(rep['exceptions'])} | {verified} |")
        if verified == "NO":
            sys.exit(f"{q.stem}: storage-backed result differs from in-memory reference")
    results["state_bytes_final"] = dir_size(state)
    print(f"\nfinal state {results['state_bytes_final'] / 2**20:.1f} MiB")
    if args.json:
        args.json.write_text(json.dumps(results, indent=2), encoding="utf-8")
    if not args.state:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
