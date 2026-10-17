#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
umask 077
if [ ! -x .venv/bin/python ]; then
  echo 'Сначала выполните ./setup.command'
  exit 1
fi
exec .venv/bin/python -m assistant.main
