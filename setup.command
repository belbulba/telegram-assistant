#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
umask 077
python3 -m venv .venv
.venv/bin/python -m pip install --retries 8 --timeout 60 --only-binary=cryptography -r requirements.txt
if [ ! -f .env ]; then
  cp .env.example .env
fi
chmod 600 .env
echo 'Заполните .env, затем ./authorize.command и ./start.command'
