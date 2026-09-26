#!/usr/bin/env python3
"""Release-gate verification inside the Linux container.

Builds the image, then runs every check under the evaluation constraints:
--read-only root filesystem, arbitrary numeric --user, --network none,
tmpfs /tmp, persistent state in a named volume across separate container
runs, inputs bind-mounted read-only.

    python3 scripts/container_check.py            # DOCKER=/path/to/docker to override

Exits 1 on the first failed check. Prints the exact commands it runs, so the
output can be pasted into EVIDENCE.md.
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from engine.core import evaluate, parse_query  # noqa: E402
from engine.records import parse_line  # noqa: E402

IMAGE = "recon-engine:check"
UID = "23456:23456"  # deliberately not the image's default user
DOCKER = os.environ.get("DOCKER") or shutil.which("docker") or str(
    Path.home() / "AppData/Local/Programs/DockerDesktop/resources/bin/docker.exe")
RUN_ID = uuid.uuid4().hex[:8]
volumes = []


def sh(args, expect=0, quiet=False, capture=True):
    shown = " ".join(a if " " not in a else f'"{a}"' for a in args)
    print(f"$ docker {shown}")
    t0 = time.perf_counter()
    r = subprocess.run([DOCKER, *args], capture_output=capture, text=True)
    elapsed = time.perf_counter() - t0
    if capture and not quiet:
        for line in (r.stderr or "").strip().splitlines()[-6:]:
            print(f"    {line}")
    print(f"    -> exit {r.returncode} ({elapsed:.2f}s)")
    if expect is not None and r.returncode != expect:
        if capture:
            print(r.stdout[-3000:], r.stderr[-3000:])
        fail(f"expected exit {expect}, got {r.returncode}")
    return r


def fail(msg):
    print(f"\nFAIL: {msg}")
    cleanup()
    sys.exit(1)


def volume(name):
    full = f"recon-check-{name}-{RUN_ID}"
    sh(["volume", "create", full], quiet=True)
    volumes.append(full)
    return full


def cleanup():
    for v in volumes:
        subprocess.run([DOCKER, "volume", "rm", "-f", v], capture_output=True)


def run(state, out, *cmd, expect=0):
    """One engine invocation in a fresh, locked-down container."""
    return sh(["run", "--rm", "--read-only", "--user", UID, "--network", "none",
               "--tmpfs", "/tmp", "--mount", f"type=bind,src={ROOT / 'data'},dst=/data,readonly",
               "--mount", f"type=bind,src={ROOT / 'queries'},dst=/queries,readonly",
               "-v", f"{state}:/state", "-v", f"{out}:/out", IMAGE, *cmd], expect=expect)


def ingest(state, out, batch, *extra, expect=0):
    return run(state, out, "ingest", "--input", f"/data/batch_{batch:02d}.jsonl",
               "--state", "/state", *extra, expect=expect)


def snapshot(state, out, name, expect=0):
    return run(state, out, "snapshot", "--query", f"/queries/{name}.json", "--state", "/state",
               "--out", f"/out/{name}.json", expect=expect)


def read_out(out, name):
    r = sh(["run", "--rm", "--user", UID, "-v", f"{out}:/out", IMAGE, "cat", f"/out/{name}.json"],
           quiet=True)
    return r.stdout


ROWS = {}
for p in sorted((ROOT / "data").glob("batch_*.jsonl")):
    ROWS[int(p.stem.split("_")[1])] = [parse_line(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
QUERIES = {p.stem: parse_query(json.loads(p.read_text(encoding="utf-8")))
           for p in sorted((ROOT / "queries").glob("*.json"))}


def oracle(batches, name):
    return evaluate([r for b in batches for r in ROWS[b]], QUERIES[name])


def section(title):
    print(f"\n=== {title} ===")


def main():
    section("build")
    sh(["version", "--format", "client {{.Client.Version}}, server {{.Server.Version}} "
        "{{.Server.Os}}/{{.Server.Arch}}"], capture=False)
    sh(["build", "-t", IMAGE, str(ROOT)], capture=False)
    info = sh(["run", "--rm", "--read-only", "--user", UID, "--network", "none", IMAGE,
               "python3", "-c", "import sys,sqlite3;print(sys.version.split()[0],sqlite3.sqlite_version)"])
    print(f"    python/sqlite in image: {info.stdout.strip()}")

    section("self-test suite (read-only, arbitrary uid, no network)")
    r = sh(["run", "--rm", "--read-only", "--user", UID, "--network", "none", "--tmpfs", "/tmp",
            IMAGE, "selftest"])
    summary = [l for l in r.stderr.splitlines() if l.startswith(("Ran ", "OK", "FAILED"))]
    print("    " + " | ".join(summary))
    if any("skipped" in l for l in summary):
        fail("tests were skipped inside Linux; the run-adapter test must execute there")

    section("/app is not writable at runtime")
    r = sh(["run", "--rm", "--user", UID, IMAGE, "sh", "-c", "touch /app/x"], expect=None)
    if r.returncode == 0:
        fail("/app writable by runtime user")
    sh(["run", "--rm", "--read-only", "--user", UID, IMAGE, "sh", "-c",
        "test -x /app/run && test -r /app/engine/cli.py"])

    section("public fixtures end to end (one container per command)")
    state, out = volume("state"), volume("out")
    for b in sorted(ROWS):
        ingest(state, out, b)
    for name in QUERIES:
        snapshot(state, out, name)
        got = json.loads(read_out(out, name))
        want = json.loads((ROOT / "expected" / f"{name}.json").read_text(encoding="utf-8"))
        if got != want:
            fail(f"{name} differs from expected")
        print(f"    {name}: matches expected")

    section("privacy: no names/emails anywhere in state (db, -wal, -shm)")
    r = sh(["run", "--rm", "--user", UID, "-v", f"{state}:/state", IMAGE, "sh", "-c",
            "ls -la /state && ! grep -rlaE 'SYNTHETIC|example\\.invalid' /state"])
    print("    " + r.stdout.strip().replace("\n", "\n    "))

    for fp in ("after-stage", "before-commit"):
        section(f"failpoint {fp}: exit 86, invisible, replay commits once")
        state, out = volume(f"fp-{fp}"), volume(f"fp-out-{fp}")
        for b in (1, 2):
            ingest(state, out, b)
        ingest(state, out, 3, "--failpoint", fp, expect=86)
        sh(["run", "--rm", "--user", UID, "-v", f"{out}:/out", IMAGE, "sh", "-c",
            "printf 'previous bytes' > /out/q03.json"], quiet=True)
        snapshot(state, out, "q03", expect=65)
        if read_out(out, "q03") != "previous bytes":
            fail("existing --out changed after failed snapshot")
        snapshot(state, out, "q02")
        if json.loads(read_out(out, "q02")) != oracle([1, 2], "q02"):
            fail("q02 changed by uncommitted batch 3")
        ingest(state, out, 3)
        ingest(state, out, 3)
        ingest(state, out, 3, "--failpoint", fp)  # already committed: no-op, exit 0
        snapshot(state, out, "q03")
        if json.loads(read_out(out, "q03")) != oracle([1, 2, 3], "q03"):
            fail("q03 wrong after replay")
        print("    recovered; q02/q03 match the in-memory oracle")

    section("gap rejection preserves existing output byte for byte")
    state, out = volume("gap"), volume("gap-out")
    for b in (1, 2, 4):
        ingest(state, out, b)
    sh(["run", "--rm", "--user", UID, "-v", f"{out}:/out", IMAGE, "sh", "-c",
        "head -c 4096 /dev/urandom > /out/q04.json && sha256sum /out/q04.json"], quiet=True)
    before = sh(["run", "--rm", "--user", UID, "-v", f"{out}:/out", IMAGE,
                 "sha256sum", "/out/q04.json"], quiet=True).stdout
    snapshot(state, out, "q04", expect=65)
    after = sh(["run", "--rm", "--user", UID, "-v", f"{out}:/out", IMAGE,
                "sha256sum", "/out/q04.json"], quiet=True).stdout
    if before != after:
        fail("q04 output changed")
    print(f"    sha256 unchanged: {after.split()[0]}")

    scale = ROOT / "scale"
    if (scale / "batch_01.jsonl").exists():
        section("scale benchmark at the evaluation limits (--cpus 4 --memory 8g)")
        state = volume("scale-state")
        r = sh(["run", "--rm", "--read-only", "--user", UID, "--network", "none", "--tmpfs", "/tmp",
                "--cpus", "4", "--memory", "8g", "--memory-swap", "8g",
                "--mount", f"type=bind,src={scale},dst=/scale,readonly",
                "-v", f"{state}:/state", IMAGE,
                "python3", "/app/scripts/bench.py", "--data", "/scale", "--state", "/state/bench",
                "--verify"], quiet=True)
        print(r.stdout)
    else:
        print("\n(scale/ not generated; skipping scale benchmark: run scripts/gen_scale.py)")

    cleanup()
    print("\nALL CONTAINER CHECKS PASSED")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        cleanup()
        raise
