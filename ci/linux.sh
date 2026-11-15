#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# Run the unit suite on Linux in Docker, once per Python version.
#
#   ci/linux.sh                       # Python 3.11 and 3.13
#   DEVDROP_LINUX_PYTHONS="3.12" ci/linux.sh
#
# The working tree is sent into the container: tracked files with any
# uncommitted edits, plus new files git does not ignore (ignored files,
# including .git/info/exclude, stay out). The tests run without network
# access. Exits non-zero if any version fails.
set -eu
root=$(cd "$(dirname "$0")/.." && pwd)
# macOS tar (bsdtar) adds Apple extended attributes that GNU tar warns about.
tar_flags=""
if tar --version 2>/dev/null | grep -q bsdtar; then
    tar_flags="--no-mac-metadata --no-xattrs"
fi
status=0
for version in ${DEVDROP_LINUX_PYTHONS:-3.11 3.13}; do
    echo "== Linux, python:$version-slim"
    if (cd "$root" && git ls-files -z --cached --others --exclude-standard | tar $tar_flags --null -T - -cf -) |
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
