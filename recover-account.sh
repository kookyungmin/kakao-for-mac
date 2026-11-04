#!/bin/sh
set -eu
cd "$(dirname "$0")"
clang -O3 -std=c11 -pthread tools/recover_user.c -o tools/recover-user
exec /usr/bin/python3 -B tools/recover_account.py "$@"
