#!/bin/bash
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

export PYTHONPATH="/home/frezator/github/Bazar-scraper-ai:/home/frezator/snap/antigravity-cli/common/local/lib/python3.14/dist-packages"
export PYTHONUNBUFFERED=1

exec /snap/core26/current/lib64/ld-linux-x86-64.so.2 \
    --library-path "/snap/core26/current/usr/lib/x86_64-linux-gnu:/snap/antigravity-cli/23/usr/lib:/snap/antigravity-cli/23/usr/lib/x86_64-linux-gnu" \
    /snap/antigravity-cli/23/usr/bin/python3 main.py "$@"
