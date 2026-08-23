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

# ----- Provider abstraction (P5) -----

# Parse EMBEDDING_MODEL — split on FIRST / to get <provider>/<model>.
if [[ -z ${EMBEDDING_MODEL-} ]]; then
	log "error: EMBEDDING_MODEL is required (format: <provider>/<model>, e.g. local/embeddinggemma)"
	exit 64
fi
EMBEDDING_PROVIDER="${EMBEDDING_MODEL%%/*}"
EMBEDDING_MODEL_NAME="${EMBEDDING_MODEL#*/}"

if [[ -z ${EMBEDDING_PROVIDER} ]] || [[ -z ${EMBEDDING_MODEL_NAME} ]]; then
	log "error: EMBEDDING_MODEL must be '<provider>/<model>' — got '${EMBEDDING_MODEL}'"
	exit 64
fi

# Parse CHAT_MODEL — split on FIRST / to get <provider>/<model>.
# Model name may contain nested slashes (e.g. openai/gpt-4o).
if [[ -z ${CHAT_MODEL-} ]]; then
	log "error: CHAT_MODEL is required (format: <provider>/<model>, e.g. openrouter/openai/gpt-5.4-mini)"
	exit 64
fi
CHAT_PROVIDER="${CHAT_MODEL%%/*}"
CHAT_MODEL_NAME="${CHAT_MODEL#*/}"

if [[ -z ${CHAT_PROVIDER} ]] || [[ -z ${CHAT_MODEL_NAME} ]]; then
	log "error: CHAT_MODEL must be '<provider>/<model>' — got '${CHAT_MODEL}'"
	exit 64
fi

# Map embedding provider → transport + base URL + key env.
EMBEDDING_VECTOR_DIMENSIONS="${EMBEDDING_VECTOR_DIMENSIONS:-768}"
embed_transport="openai"
embed_base_url=""
embed_key_env=""

case "${EMBEDDING_PROVIDER}" in
local)
	embed_base_url="${LOCAL_BASE_URL:?error: LOCAL_BASE_URL required for provider 'local'}"
	embed_key_env="LOCAL_API_KEY"
	# dims from explicit EMBEDDING_VECTOR_DIMENSIONS
	;;
ollama)
	embed_base_url="https://ollama.com/v1"
	embed_key_env="OLLAMA_API_KEY"
	;;
openai)
	embed_key_env="LLM_OPENAI_API_KEY"
	;;
openrouter)
	embed_base_url="https://openrouter.ai/api/v1"
	embed_key_env="LLM_OPENAI_API_KEY"
	;;
together)
	embed_base_url="https://api.together.xyz/v1"
	embed_key_env="LLM_OPENAI_API_KEY"
	;;
gemini)
	embed_transport="gemini"
	embed_key_env="LLM_GEMINI_API_KEY"
	;;
*)
	log "error: unknown embedding provider '${EMBEDDING_PROVIDER}' — valid: local, ollama, openai, openrouter, together, gemini"
	exit 64
	;;
esac

# Map chat provider → transport + base URL + key env.
chat_transport="openai"
chat_base_url=""
chat_key_env=""

case "${CHAT_PROVIDER}" in
local)
	chat_base_url="${LOCAL_BASE_URL:?error: LOCAL_BASE_URL required for provider 'local'}"
	# Single local model with NO cloud fallback (per addendum).
	;;
ollama)
	chat_base_url="https://ollama.com/v1"
	chat_key_env="OLLAMA_API_KEY"
	;;
openai)
	chat_key_env="LLM_OPENAI_API_KEY"
	;;
openrouter)
	chat_base_url="https://openrouter.ai/api/v1"
	chat_key_env="LLM_OPENAI_API_KEY"
	;;
together)
	chat_base_url="https://api.together.xyz/v1"
	chat_key_env="LLM_OPENAI_API_KEY"
	;;
anthropic)
	chat_transport="anthropic"
	chat_key_env="LLM_ANTHROPIC_API_KEY"
	;;
gemini)
	chat_transport="gemini"
	chat_key_env="LLM_GEMINI_API_KEY"
	;;
*)
	log "error: unknown chat provider '${CHAT_PROVIDER}' — valid: local, ollama, openai, openrouter, together, anthropic, gemini"
	exit 64
	;;
esac

# Write all config into runtime.env.
umask 077
{
	echo "# --- Database / cache / auth ---"
	echo "DB_CONNECTION_URI=postgresql+psycopg://${POSTGRES_USER}:${POSTGRES_PASSWORD}@127.0.0.1:5432/${POSTGRES_DB}"
	echo "CACHE_URL=redis://127.0.0.1:6379/0?suppress=true"
	echo "CACHE_ENABLED=true"
	echo "AUTH_JWT_SECRET=${AUTH_JWT_SECRET}"

	echo ""
	echo "# --- Embedding model config ---"
	echo "EMBEDDING_MODEL_CONFIG__model=${EMBEDDING_MODEL_NAME}"
	echo "EMBEDDING_MODEL_CONFIG__transport=${embed_transport}"
	echo "EMBEDDING_VECTOR_DIMENSIONS=${EMBEDDING_VECTOR_DIMENSIONS}"
	if [[ -n ${embed_base_url} ]]; then
		echo "EMBEDDING_MODEL_CONFIG__overrides__base_url=${embed_base_url}"
	fi
	if [[ -n ${embed_key_env} ]]; then
		echo "EMBEDDING_MODEL_CONFIG__overrides__api_key_env=${embed_key_env}"
	fi

	echo ""
	echo "# --- Text-gen model config (all modules share CHAT_MODEL) ---"
	# Write model+transport unconditionally, overrides only when non-empty.
	for _pfx in \
		DERIVER_MODEL_CONFIG \
		DIALECTIC_LEVELS__minimal__MODEL_CONFIG \
		DIALECTIC_LEVELS__low__MODEL_CONFIG \
		DIALECTIC_LEVELS__medium__MODEL_CONFIG \
		DIALECTIC_LEVELS__high__MODEL_CONFIG \
		DIALECTIC_LEVELS__max__MODEL_CONFIG \
		SUMMARY_MODEL_CONFIG \
		DREAM_DEDUCTION_MODEL_CONFIG \
		DREAM_INDUCTION_MODEL_CONFIG; do
		echo "${_pfx}__model=${CHAT_MODEL_NAME}"
		echo "${_pfx}__transport=${chat_transport}"
		if [[ -n ${chat_base_url} ]]; then
			echo "${_pfx}__overrides__base_url=${chat_base_url}"
		fi
		if [[ -n ${chat_key_env} ]]; then
			echo "${_pfx}__overrides__api_key_env=${chat_key_env}"
		fi
	done
} >/var/lib/honcho/runtime.env
chown honcho:users /var/lib/honcho/runtime.env
chmod 600 /var/lib/honcho/runtime.env
log "runtime env written to /var/lib/honcho/runtime.env"

# ----- Dimension guard -----
# Probe the embedding model's actual output dimension and compare.
# exit 0 = verified, exit 1 = dimension mismatch (fatal), exit 2 = probe
# skippable (connect/auth failure — runtime check covers it), exit 3 = misconfig.
if command -v honcho-probe-embedding >/dev/null 2>&1; then
	log "probing embedding dimension..."
	set +e
	honcho-probe-embedding
	_p=$?
	set -euo pipefail
	case "${_p}" in
	0)
		log "embedding dimension verified: ${EMBEDDING_VECTOR_DIMENSIONS}d"
		;;
	1)
		log "error: embedding dimension mismatch — boot blocked"
		exit 1
		;;
	3)
		log "error: embedding probe misconfiguration"
		exit 64
		;;
	*)
		log "warning: embedding probe skipped (exit ${_p}) — runtime check will catch mismatches"
		;;
	esac
fi
