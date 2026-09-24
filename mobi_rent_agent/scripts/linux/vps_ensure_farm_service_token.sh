#!/usr/bin/env bash
# Ensure FARM_SERVICE_TOKEN is set for mobi-rent-vps-backend (never prints the secret).
set -euo pipefail

ENV_FILE="${ENV_FILE:-/opt/mobi-rent-agent/.env}"
SERVICE="${SERVICE:-mobi-rent-vps-backend}"
UNIT_FILE="/etc/systemd/system/${SERVICE}.service"

fingerprint() {
  printf '%s' "$1" | sha256sum | awk '{print toupper($1)}'
}

read_token_from_env_file() {
  if [[ ! -f "$ENV_FILE" ]]; then
    return 1
  fi
  local line
  line="$(grep -E '^FARM_SERVICE_TOKEN=' "$ENV_FILE" | tail -n 1 || true)"
  if [[ -z "$line" ]]; then
    return 1
  fi
  local val="${line#FARM_SERVICE_TOKEN=}"
  val="${val%\"}"
  val="${val#\"}"
  val="${val%\'}"
  val="${val#\'}"
  if [[ -z "$val" ]]; then
    return 1
  fi
  printf '%s' "$val"
}

token=""
if token="$(read_token_from_env_file)"; then
  echo "FARM_SERVICE_TOKEN: present in ${ENV_FILE}"
  echo "SHA256 fingerprint: $(fingerprint "$token")"
else
  echo "FARM_SERVICE_TOKEN: missing in ${ENV_FILE}"
  token="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"
  install -d -m 750 "$(dirname "$ENV_FILE")"
  touch "$ENV_FILE"
  chmod 640 "$ENV_FILE"
  if grep -qE '^FARM_SERVICE_TOKEN=' "$ENV_FILE" 2>/dev/null; then
    sed -i '/^FARM_SERVICE_TOKEN=/d' "$ENV_FILE"
  fi
  printf 'FARM_SERVICE_TOKEN=%s\n' "$token" >> "$ENV_FILE"
  echo "NEW TOKEN GENERATED — retrieve from ${ENV_FILE} on the VPS (do not log or commit)."
  echo "SHA256 fingerprint: $(fingerprint "$token")"
fi

if [[ -f "$UNIT_FILE" ]] && grep -q "EnvironmentFile=${ENV_FILE}" "$UNIT_FILE"; then
  echo "Systemd EnvironmentFile: references ${ENV_FILE}"
else
  echo "WARN: ${UNIT_FILE} may not load ${ENV_FILE}; check systemd unit."
fi

systemctl daemon-reload
systemctl restart "$SERVICE"
systemctl is-active --quiet "$SERVICE"
echo "Service restart: successful"

status="$(curl -s -o /tmp/vps_farm_avail.json -w '%{http_code}' \
  -H "Authorization: Bearer ${token}" \
  "http://127.0.0.1:8080/farm/slots/available")"
echo "Local /farm/slots/available: HTTP ${status}"
rm -f /tmp/vps_farm_avail.json
