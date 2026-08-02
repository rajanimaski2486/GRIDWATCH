#!/usr/bin/env bash
# Bring up everything needed to click around GridWatch locally.
#
#   ./scripts/dev_up.sh
#
# Starts a local OpenSearch if OPENSEARCH_URL is unset, indexes a sample if the
# index is empty, then runs the server on http://localhost:8000.
# Ctrl-C stops the server; the OpenSearch container keeps running so restarts
# are fast. Stop it with: docker rm -f gridwatch-os

set -uo pipefail
cd "$(dirname "$0")/.."

[ -f .env ] && { set -a; . ./.env; set +a; }

PY=".venv/bin/python"
PORT="${PORT:-8000}"
SAMPLE_LIMIT="${SAMPLE_LIMIT:-150}"

[ -x "$PY" ] || { echo "✗ no .venv — run: uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python -e ."; exit 1; }

case "${NVIDIA_API_KEY:-}" in
  ""|*paste-yours-here*)
    echo "✗ NVIDIA_API_KEY missing. Put it in .env — see https://build.nvidia.com"
    exit 1 ;;
esac

# --- OpenSearch ------------------------------------------------------------
if [ -z "${OPENSEARCH_URL:-}" ]; then
  echo "== OpenSearch (local, no OPENSEARCH_URL set)"
  if ! docker ps --filter name=gridwatch-os --format '{{.Names}}' | grep -q gridwatch-os; then
    docker rm -f gridwatch-os >/dev/null 2>&1
    docker run -d --name gridwatch-os -p 9200:9200 \
      -e discovery.type=single-node \
      -e DISABLE_SECURITY_PLUGIN=true \
      -e OPENSEARCH_JAVA_OPTS="-Xms512m -Xmx512m" \
      opensearchproject/opensearch:2.18.0 >/dev/null || { echo "✗ could not start OpenSearch (is Docker running?)"; exit 1; }
    echo "  started container gridwatch-os"
  else
    echo "  container gridwatch-os already running"
  fi
  export OPENSEARCH_URL="http://localhost:9200"
  for i in $(seq 1 40); do
    curl -fsS "$OPENSEARCH_URL" >/dev/null 2>&1 && break
    sleep 3
  done
  curl -fsS "$OPENSEARCH_URL" >/dev/null 2>&1 || { echo "✗ OpenSearch never came up"; exit 1; }
  echo "  ready at $OPENSEARCH_URL"
else
  echo "== OpenSearch: using OPENSEARCH_URL from .env"
fi

# --- Index -----------------------------------------------------------------
docs=$($PY - <<'EOF'
import os
try:
    from opensearchpy import OpenSearch
    kw = {"hosts": [os.environ["OPENSEARCH_URL"]],
          "verify_certs": os.getenv("OPENSEARCH_VERIFY_CERTS", "true") != "false"}
    u, pw = os.getenv("OPENSEARCH_USER", ""), os.getenv("OPENSEARCH_PASSWORD", "")
    if u and pw and "@" not in os.environ["OPENSEARCH_URL"].split("//", 1)[-1]:
        kw["http_auth"] = (u, pw)
    print(OpenSearch(**kw).count(index=os.getenv("OPENSEARCH_INDEX_PREFIX", "nyc_") + "*")["count"])
except Exception:
    print(0)
EOF
)
echo "== Index: $docs documents"
if [ "$docs" -lt 10 ]; then
  echo "  empty — indexing a $SAMPLE_LIMIT-record sample per dataset (uses NIM embeddings)"
  $PY -m hackathon_nyc.ingest_opensearch --all --limit "$SAMPLE_LIMIT" 2>&1 | grep -Ev "^\s+[0-9]+/" | sed 's/^/  /'
fi

# --- Server ----------------------------------------------------------------
echo
echo "== Starting GridWatch on http://localhost:${PORT}"
echo "   dashboard  http://localhost:${PORT}/"
echo "   status     http://localhost:${PORT}/api/agent/status"
echo "   Ctrl-C to stop. OpenSearch keeps running (docker rm -f gridwatch-os)."
echo
exec .venv/bin/uvicorn hackathon_nyc.server:app --host 0.0.0.0 --port "$PORT"
