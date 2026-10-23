#!/bin/sh
set -eu
cd "$(dirname "$0")"
export GOCACHE=/tmp/cp4-flash-go-cache
export GOPATH=/tmp/cp4-flash-go-path
go test .
CGO_ENABLED=0 GOOS=linux GOARCH=arm GOARM=7 go build -trimpath -ldflags='-s -w' -o cp4-hi3861-flash .
