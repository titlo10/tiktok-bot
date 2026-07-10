#!/bin/sh
set -eu

python scripts/check_no_comments.py

token_pattern='[0-9]{8,}:[A-Za-z0-9_-]{20,}'
secret_pattern='TELEGRAM_(API_HASH|BOT_TOKEN)=[A-Za-z0-9]'
if grep -RInE --exclude-dir=.git --exclude=uv.lock --exclude=check_repository.sh "$token_pattern|$secret_pattern" .; then
    exit 1
fi
