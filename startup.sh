#!/bin/sh
set -eu
cd /home/container
[ -f main.py ] || cp main.py.backup main.py
[ -f database.py ] || cp database.py.backup database.py
export PYTHONDONTWRITEBYTECODE=1
export PIP_NO_CACHE_DIR=1
export PIP_DISABLE_PIP_VERSION_CHECK=1
rm -rf .cache __pycache__ 2>/dev/null || true
python -m pip install --no-cache-dir --no-compile --disable-pip-version-check --upgrade -r /home/container/requirements.txt
exec /usr/local/bin/python /home/container/main.py
