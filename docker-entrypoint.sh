#!/bin/sh
# Dispatch: `ingest`/`snapshot`/`--help` go to the adapter, `selftest` runs the
# test suite, anything else is executed as given (e.g. `/app/run ingest ...`).
set -eu
case "${1:-}" in
  ingest|snapshot|-h|--help) exec /app/run "$@" ;;
  selftest) shift; cd /app && exec python3 -B -m unittest discover -s tests -t . "$@" ;;
  *) exec "$@" ;;
esac
