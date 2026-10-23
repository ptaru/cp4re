#!/bin/sh
set -eu
cd "$(dirname "$0")"
export GOCACHE="${TMPDIR:-/tmp}/cp4-diag-go-cache"
CGO_ENABLED=0 GOOS=linux GOARCH=arm GOARM=7 go build -trimpath -ldflags='-s -w' -o cp4-independent-status .
