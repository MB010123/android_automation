#!/usr/bin/env bash
# Create venv and install production dependencies (run from mobi_rent_agent/).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"
python3 -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
echo "Done. Copy .env.example to .env and install systemd units from systemd/"
