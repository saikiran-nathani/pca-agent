#!/bin/sh
# The host's source is mounted READ-ONLY at /src-ro and copied to a tmpfs at
# /repo before anything runs.
#
# The suite deliberately executes hostile code, and one case
# (test_filesystem_escape_is_flagged_or_denied) asserts that a write outside the
# working directory SUCCEEDS -- because Level 1 containment runs as the same uid
# and genuinely cannot stop it, only flag it. That assertion is the suite
# recording ground truth about the threat model.
#
# So the tree the suite runs against must be writable, and it must not be the
# host's. A read-only bind mount makes the test fail for the wrong reason (the
# mount blocked the write, not the executor); a read-write bind mount hands
# adversarial code the real repository. A throwaway copy on tmpfs is the only
# option that is honest about the executor AND safe for the host.
set -e
# cp -R, not cp -a: -a preserves ownership and timestamps, which means setting
# them on /repo itself -- a tmpfs owned by root that this unprivileged user may
# write into but not stamp. `cp: preserving times for '/repo/.': Operation not
# permitted`. Nothing here needs the source's mtimes.
cp -R /src-ro/. /repo/
cd /repo
exec "$@"
