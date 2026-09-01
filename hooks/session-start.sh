#!/bin/sh
# wheel: inject the optional Claude Code gate into session context

wheel_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd) || wheel_root=""
wheel_python=""

for candidate in python3.14 python3.13 python3.12 python3.11 python3 python; do
  if command -v "$candidate" >/dev/null 2>&1 \
    && "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1; then
    wheel_python=$(command -v "$candidate")
    break
  fi
done

if [ -z "$wheel_python" ] || [ -z "$wheel_root" ]; then
  printf '%s\n' 'Wheel: Python 3.11+ is unavailable; managed DonSeTch bootstrap was skipped.' >&2
elif [ "${WHEEL_NO_BOOTSTRAP:-}" = "1" ]; then
  printf '%s\n' 'Wheel: managed DonSeTch bootstrap skipped (WHEEL_NO_BOOTSTRAP=1).' >&2
  "$wheel_python" "$wheel_root/scripts/wheel.py" dependencies --json >/dev/null 2>&1 || true
else
  # No generic install hook exists. SessionStart is the first portable activation point.
  printf '%s\n' 'Wheel: DonSeTch 3.4.4, AGPL-3.0-only, https://github.com/dondai44423/donsetch.' >&2
  if ! "$wheel_python" "$wheel_root/scripts/wheel.py" dependencies --ensure --check-latest --json >/dev/null 2>&1; then
    printf '%s\n' 'Wheel: managed DonSeTch bootstrap failed; dependent routes will report their own status.' >&2
  fi
fi

cat <<'EOF'
<wheel-rule>
For any non-trivial request that creates or changes functionality, invoke the `wheel` skill first, before planning or writing code. The user may explicitly decline this gate.

Skip the gate for trivial mechanical edits or when the user explicitly declines it.
</wheel-rule>
EOF
