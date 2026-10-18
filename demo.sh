#!/usr/bin/env bash
# Demo: run pipeline-sage in offline (heuristic) mode on the sample failing log.
set -euo pipefail
cd "$(dirname "$0")"

echo "=== pipeline-sage demo (heuristic mode, no API key needed) ==="
echo
python3 pipeline_sage.py analyze sample_data/failing-build.log
