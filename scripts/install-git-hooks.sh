#!/bin/sh
set -eu
cd "$(git rev-parse --show-toplevel)"
existing=$(git config --get core.hooksPath || true)
if [ -n "$existing" ] && [ "$existing" != '.githooks' ]; then
    echo 'Existing core.hooksPath found; integrate the checks there before changing it.' >&2
    exit 1
fi
for hook in pre-commit commit-msg pre-push; do
    old=$(git rev-parse --git-path "hooks/$hook")
    if [ -z "$existing" ] && [ -f "$old" ]; then
        echo 'Existing Git hook found; integrate the checks without replacing it.' >&2
        exit 1
    fi
done
chmod +x .githooks/pre-commit .githooks/commit-msg .githooks/pre-push
git config --local core.hooksPath .githooks
terms=$(git rev-parse --git-path info/public-content-terms)
mkdir -p "$(dirname "$terms")"
if [ ! -e "$terms" ]; then
    (umask 077; printf '%s\n' '# One private identifier per line; local only. Never commit this file.' > "$terms")
fi
echo 'Public-content hooks enabled: staged files, commit messages, and outgoing commits.'
