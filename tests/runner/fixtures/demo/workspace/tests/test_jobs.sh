#!/bin/sh
# The task's own test: every batch has a log.
set -eu
for f in data/*.csv; do ./bin/jobs submit "$f" >/dev/null; done
[ "$(ls out/*.log | wc -l)" -eq 3 ]
