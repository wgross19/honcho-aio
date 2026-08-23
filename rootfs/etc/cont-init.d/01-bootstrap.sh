#!/command/with-contenv bash
# shellcheck shell=bash
# shellcheck disable=SC2312 # intentional: log() masks return
set -euo pipefail

log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2; }

install -d -m 0755 /var/lib/honcho /data/postgres /data/redis /run/postgresql
chown -R honcho:users /var/lib/honcho
chown -R postgres:postgres /data/postgres /run/postgresql
chown -R redis:redis /data/redis

# Require POSTGRES_PASSWORD — operator must provide it via env or secrets.
if [[ -z ${POSTGRES_PASSWORD-} ]]; then
	log "error: POSTGRES_PASSWORD is required (set via environment or Docker secrets)"
	exit 64
fi

POSTGRES_USER="${POSTGRES_USER:-postgres}"
POSTGRES_DB="${POSTGRES_DB:-honcho}"

# Generate a random JWT signing secret on first boot.
AUTH_JWT_SECRET="$(python3 -c 'import secrets; print(secrets.token_hex(64))')"

umask 077
cat >/var/lib/honcho/runtime.env <<EOF
DB_CONNECTION_URI=postgresql+psycopg://${POSTGRES_USER}:${POSTGRES_PASSWORD}@127.0.0.1:5432/${POSTGRES_DB}
CACHE_URL=redis://127.0.0.1:6379/0?suppress=true
CACHE_ENABLED=true
AUTH_JWT_SECRET=${AUTH_JWT_SECRET}
EOF
chown honcho:users /var/lib/honcho/runtime.env
chmod 600 /var/lib/honcho/runtime.env
log "runtime env written to /var/lib/honcho/runtime.env"