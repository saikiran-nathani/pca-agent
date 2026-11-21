# The Linux container the sandbox is developed and audited in.
#
# It exists because containment here is Linux-only: RLIMIT_AS is unsupported on
# Darwin and RLIMIT_DATA does not cap allocation there, so on the Mac a memory
# bomb runs until the wall clock kills it. One case in the adversarial suite
# (test_fork_bomb_is_contained) sizes RLIMIT_NPROC from /proc and is skipped on
# macOS for that reason -- inside here it must RUN, and the suite must report
# 23 rather than 22.
FROM python:3.12-slim

# procps for `ps`. Two tests count surviving sleepers with it to prove killpg
# reaped the whole process group; without it they die on FileNotFoundError
# before testing anything, which is a test that cannot fail for its own reason.
#
# git for `git apply`, which is how verify() applies a model's patch. Not a
# hand-rolled diff parser, and not `--unsafe-paths`: git apply refuses by
# default to write outside the tree it is run in, which is the single property
# a patch applier must have and the hardest one to get right by hand.
RUN apt-get update \
 && apt-get install -y --no-install-recommends procps git \
 && rm -rf /var/lib/apt/lists/*

# pytest and pytest-timeout only. The sandbox itself is stdlib, deliberately:
# a trust boundary with a dependency tree is a trust boundary with someone
# else's release schedule in it.
RUN pip install --no-cache-dir --root-user-action=ignore \
      "pytest>=8.0" "pytest-timeout>=2.3"

# The marker verify() looks for. It says "this is the image built from this
# file", which is the only way to know the run carries the flags chosen in
# scripts/run-in-container.sh -- --network none, --read-only, --cap-drop ALL.
# A generic "am I in a container" check answers a question nobody asked: being
# in SOME container says nothing about whether the network is reachable.
#
# It is half the check and the weaker half. verify() also requires that the only
# network interface is loopback, which tests the claim instead of trusting a
# file we wrote -- so running this image WITH networking is caught even though
# the marker is still here.
RUN printf 'pca sandbox image\nbuilt from docker/Dockerfile\n' > /etc/pca-sandbox

# Never root. The container's own hardening is defence in depth; the point of
# the suite is to prove the EXECUTOR contains things, so nothing here should be
# the thing that stops an exploit.
RUN useradd --create-home --uid 1000 runner
USER runner
WORKDIR /repo
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

COPY --chmod=0755 docker/entrypoint.sh /usr/local/bin/entrypoint.sh
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
# No -q here. pyproject's addopts already carries one, and a second makes -qq,
# which suppresses the summary line -- so the run prints dots, the gate below
# finds no count to assert, and the only visible evidence is a row of dots that
# looks exactly like success.
CMD ["python", "-m", "pytest", "-p", "no:cacheprovider"]
