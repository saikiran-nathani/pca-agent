#!/usr/bin/env bash
# Run the adversarial suite (or any command) inside the Linux container.
#
#   scripts/run-in-container.sh                  # the suite
#   scripts/run-in-container.sh python -c '...'  # anything else
#
# THE FLAGS ARE THE POINT, and one choice in here is counter-intuitive:
# --memory and --pids-limit are set GENEROUSLY ON PURPOSE. If Docker's limits
# fire before the executor's own RLIMIT_AS and RLIMIT_NPROC do, every test
# still passes -- and passes for the wrong reason, proving Docker contains a
# memory bomb rather than proving the executor does. A suite that cannot fail
# for the right reason is not a suite. The container is defence in depth, not
# the thing under test.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="pca-sandbox:dev"

if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  echo "building $IMAGE ..." >&2
  docker build -q -t "$IMAGE" -f "$REPO/docker/Dockerfile" "$REPO" >/dev/null
fi

DOCKER_FLAGS=(
  --rm
  --network none \
  --read-only \
  --tmpfs /tmp:rw,size=512m \
  --tmpfs /home/runner:rw,size=64m \
  --tmpfs /repo:rw,size=256m,mode=1777 \
  --memory 2g --memory-swap 2g \
  --pids-limit 512 \
  --cap-drop ALL \
  --security-opt no-new-privileges \
  --mount "type=bind,src=$REPO/src,dst=/src-ro/src,readonly" \
  --mount "type=bind,src=$REPO/conftest.py,dst=/src-ro/conftest.py,readonly" \
  --mount "type=bind,src=$REPO/pyproject.toml,dst=/src-ro/pyproject.toml,readonly" \
)

# With a command, run it and get out of the way.
if [ "$#" -gt 0 ]; then
  exec docker run "${DOCKER_FLAGS[@]}" "$IMAGE" "$@"
fi

# With no command this is THE PHASE 1 GATE, and a gate asserts a count.
#
# 23, not "it passed". On macOS the suite reports 22 passed and 1 skipped,
# because test_fork_bomb_is_contained sizes RLIMIT_NPROC from a /proc that is
# not there. Inside Linux that case must RUN. A run that reports 22 here has
# silently skipped the single test this container exists to make possible, and
# "22 passed" reads exactly like success.
EXPECTED=23
out="$(docker run "${DOCKER_FLAGS[@]}" "$IMAGE" 2>&1)" || { echo "$out"; exit 1; }
echo "$out"

# grep, not sed with a leading-context pattern. Under -q the summary line is
# `23 passed in 17.42s` and begins with the number, so any pattern demanding a
# non-digit before it matches nothing -- and "could not read a count" is the
# same dead end as no count at all.
# `|| true` on both, and the second one is load-bearing. Under `set -euo
# pipefail` a grep that finds nothing fails the pipeline and kills the script
# -- and finding no "skipped" is the HEALTHY case here. Without this the gate
# exits 1 on a perfect run, having asserted nothing, which looks like a test
# failure and is the script dying before it checks anything.
passed="$(printf '%s' "$out" | grep -oE '[0-9]+ passed' | tail -1 | cut -d' ' -f1 || true)"
skipped="$(printf '%s' "$out" | grep -oE '[0-9]+ skipped' | tail -1 | cut -d' ' -f1 || true)"

if [ -z "$passed" ]; then
  echo "GATE FAILED: could not read a pass count from the container run." >&2
  exit 2
fi
if [ "$passed" -lt "$EXPECTED" ] || [ -n "${skipped:-}" ]; then
  echo "GATE FAILED: $passed passed${skipped:+, $skipped skipped} -- expected $EXPECTED passed and nothing skipped." >&2
  echo "             A skip in here means the Linux-only case did not run, which is the whole point of the container." >&2
  exit 2
fi
echo "gate ok: $passed passed, nothing skipped"
