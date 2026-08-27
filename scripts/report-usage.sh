#!/usr/bin/env sh
# Hook entry point. Its ONE job beyond calling the reporter is to be un-failable:
# a missing python3 or an unwritable box must leave the session untouched, so every
# path exits 0 and prints nothing.
DIR="$(cd "$(dirname "$0")" && pwd)"
PY="${AGENTS_USAGE_PYTHON:-}"
[ -n "$PY" ] || for candidate in python3 python; do
  command -v "$candidate" >/dev/null 2>&1 && { PY="$candidate"; break; }
done
[ -n "$PY" ] || exit 0

"$PY" "$DIR/report_usage.py" "$@" >/dev/null 2>&1
exit 0
