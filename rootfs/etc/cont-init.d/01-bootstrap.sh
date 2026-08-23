#!/command/with-contenv bash
# shellcheck shell=bash
set -euo pipefail

mkdir -p /config/honcho

ENV_FILE="/config/honcho/generated.env"
touch "${ENV_FILE}"
chown root:users /config/honcho "${ENV_FILE}"
chmod 750 /config/honcho
chmod 640 "${ENV_FILE}"

persist_if_missing() {
	local key="$1"
	local value="$2"
	if grep -q "^${key}=" "${ENV_FILE}"; then
		return
	fi
	printf '%s="%s"\n' "${key}" "${value}" >>"${ENV_FILE}"
}

# Honcho runtime wiring: the single image hosts Honcho, PostgreSQL and Redis.
# Default DB/CACHE point at the local services; operators override via env or
# /config/honcho/generated.env.
persist_if_missing "DB_CONNECTION_URI" "postgresql+psycopg://postgres:postgres@127.0.0.1:5432/honcho"
persist_if_missing "CACHE_URL" "redis://127.0.0.1:6379/0?suppress=true"
persist_if_missing "CACHE_ENABLED" "true"

# Random secret used for JWT signing if auth is enabled.
persist_if_missing "AUTH_JWT_SECRET" "$(python3 -c 'import secrets; print(secrets.token_hex(64))')"

echo "[honcho-aio] first-run values stored at ${ENV_FILE}."
