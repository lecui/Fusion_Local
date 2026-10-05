#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
umask 077
if ! docker compose version >/dev/null 2>&1; then
  echo 'Docker Compose v2 is required. On Ubuntu/Debian: sudo bash install-docker.sh'
  exit 1
fi
if [[ ! -f .env ]]; then
  read -r -p 'Server IPv4 or domain (without https://): ' host
  if [[ ! "$host" =~ ^[a-zA-Z0-9][a-zA-Z0-9.-]*[a-zA-Z0-9]$ ]]; then echo 'Invalid host'; exit 1; fi
  read -r -p 'Certificate mode: 1 = domain / automatic public HTTPS, 2 = IP / private certificate [1/2]: ' mode
  if [[ "$mode" == 1 && "$host" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo 'For an IP address choose certificate mode 2.'; exit 1
  fi
  case "$mode" in
    1) printf '# Automatic public certificate\n' > tls-mode.conf ;;
    2) printf 'tls internal\n' > tls-mode.conf ;;
    *) echo 'Choose 1 or 2'; exit 1 ;;
  esac
  printf 'SITE_ADDRESS=%s\nMAX_FILE_BYTES=2147483648\nACCOUNT_QUOTA_BYTES=21474836480\n' "$host" > .env
  chmod 644 tls-mode.conf
else
  echo 'Keeping existing server configuration.'
  [[ -f tls-mode.conf ]] || { echo 'Missing tls-mode.conf'; exit 1; }
fi
docker compose config --quiet
docker compose up -d --build --wait --wait-timeout 180
if grep -q '^tls internal' tls-mode.conf; then
  for attempt in $(seq 1 30); do
    if docker compose cp proxy:/data/caddy/pki/authorities/local/root.crt ./client-ca.crt 2>/dev/null; then break; fi
    sleep 2
  done
  [[ -s client-ca.crt ]] || { echo 'Certificate is not ready. Check: docker compose logs proxy'; exit 1; }
  chmod 644 client-ca.crt
  echo 'Copy client-ca.crt to your computer through SSH/SCP. Select its path in Fusion connection settings.'
fi
read -r -p 'Create an account now? Enter login, or leave blank: ' login
if [[ -n "$login" ]]; then docker compose exec api python manage.py add-user "$login"; fi
echo 'Server is running. Account management: bash account.sh add-user LOGIN'
echo 'Open TCP 80 and 443 in your VPS firewall. Do not expose port 8000.'
