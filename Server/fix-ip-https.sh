#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
[[ -f Caddyfile && -f compose.yaml && -f .env ]] || { echo 'Run in the installed server directory.'; exit 1; }
backup="Caddyfile.before-ip-fix.$(date -u +%Y%m%dT%H%M%SZ)"
cp -- Caddyfile "$backup"
if ! grep -q 'default_sni' Caddyfile; then
  temp=$(mktemp)
  trap 'rm -f -- "$temp"' EXIT
  { printf '{\n    default_sni {$SITE_ADDRESS}\n}\n\n'; cat Caddyfile; } > "$temp"
  cp -- "$temp" Caddyfile
fi
if ! docker compose exec -T proxy caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile; then
  cp -- "$backup" Caddyfile
  echo 'Validation failed; original configuration restored.'
  exit 1
fi
docker compose exec -T proxy caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile
echo 'IP HTTPS configuration reloaded. Certificate authority and accounts unchanged.'
