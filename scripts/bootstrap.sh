#!/usr/bin/env bash
# One-time setup in the MAIN checkout: deps, corpus, LibreOffice golden output, baseline.
# Needs: python3 >= 3.9, git, LibreOffice. ~10-30 min (golden generation).
# The library itself is not installed: oracle and tests import it from src/.
set -eo pipefail
cd "$(git rev-parse --show-toplevel)"
python3 -m pip install -r requirements-dev.txt "$@"
python3 oracle/fetch_corpus.py ${FETCH_ARGS:-}
python3 oracle/make_golden.py
python3 -m pytest -q
python3 oracle/compare.py --update-baseline --quiet
echo
echo "Ready. Next: scripts/run_agents.sh wave1"
