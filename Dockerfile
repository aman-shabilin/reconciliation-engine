# Build:  docker build -t recon-engine .
# Run:    docker run --rm --read-only --user 12345:12345 --network none --tmpfs /tmp \
#           -v "$PWD/data:/data:ro" -v recon-state:/state -v recon-out:/out \
#           recon-engine ingest --input /data/batch_01.jsonl --state /state
#
# Base image pinned by multi-arch index digest (python:3.12-slim, fetched 2026-09-26).
# The engine uses only the Python standard library: no packages are installed,
# so the runtime needs no network once the image is built.
FROM python:3.12-slim@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHON=python3

WORKDIR /app
# Runtime code, plus tests and public fixtures so the image can verify itself
# (docker run ... recon-engine selftest). Nothing under /app is written at runtime.
COPY engine/ /app/engine/
COPY tests/ /app/tests/
COPY scripts/mutants.py scripts/bench.py scripts/crash_test.py /app/scripts/
COPY data/ /app/data/
COPY queries/ /app/queries/
COPY expected/ /app/expected/
COPY tools/ /app/tools/
COPY run docker-entrypoint.sh /app/

# Readable/executable by any numeric UID; owned by root so no runtime user can modify it.
RUN chmod -R a+rX,go-w /app && chmod 0755 /app/run /app/docker-entrypoint.sh \
 && mkdir -p /state /out && chmod 1777 /state /out

# Arbitrary non-root default; any --user UID works because /app is world-readable.
USER 10001:10001
VOLUME ["/state"]
ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["--help"]
