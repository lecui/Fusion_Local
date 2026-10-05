#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
umask 077
mkdir -p backups
stamp=$(date -u +%Y%m%dT%H%M%SZ)
target="backups/$stamp"
mkdir "$target"
trap 'docker compose start api >/dev/null' EXIT
docker compose stop api
docker compose cp api:/data "$target/data"
docker compose cp proxy:/data "$target/tls-data"
cp .env Caddyfile tls-mode.conf "$target/"
tar -czf "$target.tar.gz" -C backups "$stamp"
echo "Backup saved: $target.tar.gz (contains account data and TLS keys; keep private)."
