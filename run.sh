#!/usr/bin/env bash
set -euo pipefail
cd /root/text-world
export PYTHONPATH=/root/text-world
exec python3 -m uvicorn server.main:app --host 0.0.0.0 --port "${TW_PORT:-8000}" --log-level info
