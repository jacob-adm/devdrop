#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# Run the unit suite on Linux in Docker, once per Python version.
#
#   ci/linux.sh                       # Python 3.11 and 3.13
#   DEVDROP_LINUX_PYTHONS="3.12" ci/linux.sh
#
# Only files git tracks are sent into the container (working-tree
# contents, so uncommitted edits are included); the tests run without
# network access. Exits non-zero if any version fails.
set -eu
root=$(cd "$(dirname "$0")/.." && pwd)
status=0
for version in ${DEVDROP_LINUX_PYTHONS:-3.11 3.13}; do
    echo "== Linux, python:$version-slim"
    if (cd "$root" && git ls-files -z | tar --null -T - -cf -) |
        docker run --rm -i --network none "python:$version-slim" \
            sh -c 'mkdir /work && cd /work && tar -xf - && python -m unittest discover -s tests'
    then
        echo "== python:$version passed"
    else
        echo "== python:$version FAILED"
        status=1
    fi
done
exit $status
