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

# Rebuild when the image's recipe changes, not only when the image is absent.
#
# `docker image inspect` succeeding used to be the whole condition, which meant a
# Dockerfile edit never took effect until someone deleted the image by hand. That
# was survivable while the image only held pytest. It stopped being survivable in
# 1.4: verify() refuses to run unless /etc/pca-sandbox is present, and that file
# is written by the Dockerfile -- so a stale image turns a correct refusal into a
# confusing one, and `git apply` would be missing too.
#
# A content stamp rather than mtimes: a checkout, a branch switch or a `touch`
# all move mtimes without changing what gets built, and comparing timestamps
# across the Docker daemon's clock and the filesystem's is its own small trap.
if command -v sha256sum >/dev/null 2>&1; then
  STAMP="$(cat "$REPO/docker/Dockerfile" "$REPO/docker/entrypoint.sh" | sha256sum | cut -c1-16)"
else
  STAMP="$(cat "$REPO/docker/Dockerfile" "$REPO/docker/entrypoint.sh" | shasum -a 256 | cut -c1-16)"
fi
BUILT="$(docker image inspect -f '{{index .Config.Labels "pca.stamp"}}' "$IMAGE" 2>/dev/null || true)"
if [ "$STAMP" != "$BUILT" ]; then
  echo "building $IMAGE (recipe stamp $STAMP) ..." >&2
  docker build -q -t "$IMAGE" --label "pca.stamp=$STAMP" -f "$REPO/docker/Dockerfile" "$REPO" >/dev/null
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
# 47 tests must RUN -- not "47 passed". The distinction started mattering on
# 2026-11-21, when phase 1.2 added an exploit corpus that the sandbox does not
# yet survive. Asserting a PASS count conflates two unrelated questions: did
# the suite shrink, and did it go red. A gate built on `passed >= N` reports
# success the moment enough other tests pass to cover for a deleted one, and
# reports the same generic failure whether a case vanished or a hole opened.
#
# So: assert the number that RAN, and assert separately that none failed and
# none were skipped. On macOS the suite reports 1 skipped, because
# test_fork_bomb_is_contained sizes RLIMIT_NPROC from a /proc that is not
# there, and since 1.4 nine more skip with it: verify() refuses to execute a
# model's patch outside the container, so its tests decline to pretend. Inside
# Linux all ten must RUN -- a skip in here means a test this container exists to
# make possible did not happen.
EXPECTED_RAN=47

# Do NOT let a non-zero exit short-circuit the parse. pytest exits 1 on a test
# failure, and the old `|| { echo; exit 1; }` threw away every count before
# reading one -- so "the image would not build" and "two exploits were
# accepted" produced the same silent exit 1. The status is captured and
# reported alongside the counts instead.
set +e
out="$(docker run "${DOCKER_FLAGS[@]}" "$IMAGE" 2>&1)"
rc=$?
set -e
echo "$out"

# grep, not sed with a leading-context pattern. The summary line is
# `26 passed in 17.42s` and begins with the number, so any pattern demanding a
# non-digit before it matches nothing -- and "could not read a count" is the
# same dead end as no count at all.
# `|| true` on each, and it is load-bearing. Under `set -euo pipefail` a grep
# that finds nothing fails the pipeline and kills the script -- and finding no
# "failed" is the HEALTHY case here. Without this the gate exits on a perfect
# run, having asserted nothing.
count() { printf '%s' "$out" | grep -oE "[0-9]+ $1" | tail -1 | cut -d' ' -f1 || true; }
passed="$(count passed)"
failed="$(count failed)"
errors="$(count error)"
skipped="$(count skipped)"
ran=$(( ${passed:-0} + ${failed:-0} + ${errors:-0} + ${skipped:-0} ))

if [ -z "$passed" ] && [ -z "$failed" ] && [ -z "$errors" ]; then
  echo "GATE FAILED: no pytest summary in the container output (exit $rc)." >&2
  echo "             That is an image or entrypoint problem, not a test result." >&2
  exit 2
fi

status=0
if [ "$ran" -ne "$EXPECTED_RAN" ]; then
  echo "GATE FAILED: $ran tests ran, expected $EXPECTED_RAN. A case was added or deleted;" >&2
  echo "             update EXPECTED_RAN deliberately rather than letting the count drift." >&2
  status=2
fi
if [ -n "${skipped:-}" ]; then
  echo "GATE FAILED: $skipped skipped. In here nothing may skip -- a skip means the" >&2
  echo "             Linux-only case did not run, which is the whole point of the container." >&2
  status=2
fi
if [ -n "${failed:-}" ] || [ -n "${errors:-}" ]; then
  echo "GATE FAILED: ${failed:-0} failed, ${errors:-0} errored." >&2
  echo "             PLAN.md 1.3 — an accepted exploit blocks everything downstream." >&2
  status=1
fi
[ "$status" -ne 0 ] && exit "$status"

echo "gate ok: $ran ran, $passed passed, nothing failed, nothing skipped"
