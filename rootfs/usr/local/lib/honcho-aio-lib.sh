#!/bin/sh
# Helpers for honcho-aio s6 services. Sourced (.) by run scripts.

# shellcheck disable=SC2312
log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2; }

# Load operator-provided first-run values persisted by 01-bootstrap.sh.
# Prefer /var/lib/honcho/runtime.env (the canonical path) over the legacy
# /config/honcho/generated.env for backward compatibility during migration.
load_runtime() {
	ENV_FILE="/var/lib/honcho/runtime.env"
	if [ ! -f "${ENV_FILE}" ]; then
		ENV_FILE="/config/honcho/generated.env"
	fi
	if [ -f "${ENV_FILE}" ]; then
		set -a
		# shellcheck disable=SC1090
		. "${ENV_FILE}"
		set +a
	fi
}

wait_postgres() {
	for _ in $(seq 1 60); do
		if pg_isready -h 127.0.0.1 -p 5432 >/dev/null 2>&1; then
			return 0
		fi
		sleep 1
	done
	log "postgres did not become ready"
	return 1
}

# Block until redis is accepting commands on the loopback interface.
wait_redis() {
	for _ in $(seq 1 60); do
		if redis-cli -h 127.0.0.1 -p 6379 ping >/dev/null 2>&1; then
			return 0
		fi
		sleep 1
	done
	log "redis did not become ready"
	return 1
}

# Ensure the postgres superuser password matches runtime config, create the
# honcho database if absent, and enable the pgvector extension. Idempotent.
# Serialized with flock because both honcho-api and honcho-deriver may start
# concurrently on first boot.
ensure_postgres() {
	LOCK_FILE="/run/lock/honcho-postgres.lock"
	mkdir -p /run/lock
	exec 9>"${LOCK_FILE}"
	flock 9

	PGUSER_NAME="${POSTGRES_USER:-postgres}"
	PGDB_NAME="${POSTGRES_DB:-honcho}"
	export PGPASSWORD="${POSTGRES_PASSWORD:-postgres}"

	gosu postgres psql -v ON_ERROR_STOP=1 -d postgres <<SQL
DO \$\$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '${PGUSER_NAME}') THEN
    CREATE ROLE ${PGUSER_NAME} LOGIN PASSWORD '${POSTGRES_PASSWORD:-postgres}';
  ELSE
    ALTER ROLE ${PGUSER_NAME} LOGIN PASSWORD '${POSTGRES_PASSWORD:-postgres}';
  END IF;
END
\$\$;
SQL

	if ! gosu postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname = '${PGDB_NAME}'" | grep -q 1; then
		log "creating database ${PGDB_NAME}"
		gosu postgres createdb -O "${PGUSER_NAME}" "${PGDB_NAME}"
	fi

	gosu postgres psql -v ON_ERROR_STOP=1 -d "${PGDB_NAME}" -c 'CREATE EXTENSION IF NOT EXISTS vector;'

	flock -u 9
}
