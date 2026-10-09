#!/bin/bash
# Cloud sessions only: install what the tests (requirements-dev.txt) and research/model refits (model/train.py,
# /mnt/project-files/research) need, so a new thread can run them right away.
set -euo pipefail

if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

cd "$CLAUDE_PROJECT_DIR"
python3 -m pip install --quiet --disable-pip-version-check --root-user-action=ignore \
  -r requirements-dev.txt pandas pyarrow scikit-learn matplotlib
