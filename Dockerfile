# GridWatch — single container: FastAPI + the NAT workflow in-process.
#
# Inference is NOT in this image. Reasoning runs on NVIDIA-hosted Nemotron NIMs
# at build.nvidia.com, so there is no GPU, no CUDA and no model weights here.
# The container needs exactly one secret to think: NVIDIA_API_KEY.
#
# Port 7860 is the Hugging Face Spaces convention; $PORT overrides it for
# Cloud Run, Render and friends.
#
#   docker build -t gridwatch .
#   docker run -p 7860:7860 --env-file .env gridwatch

FROM python:3.12-slim

# build-essential is needed for chromadb's native deps; removed after install
# to keep the layer small.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependency layer first so code edits don't re-resolve the whole tree.
COPY pyproject.toml README.md ./
COPY src/hackathon_nyc/__init__.py ./src/hackathon_nyc/
# [observability] pulls nvidia-nat-profiler, which registers the avg_llm_latency
# / avg_tokens_per_llm_end / avg_num_llm_calls evaluators referenced by the eval
# block in config_gridwatch.yml. NAT validates the whole config at build time,
# so a missing evaluator plugin fails the workflow — not just `nat eval`.
RUN pip install --no-cache-dir -e ".[observability]" \
    && apt-get purge -y --auto-remove build-essential

COPY src ./src

# Read-only RAG index (~69 MB). The committed ChromaDB was built with Chroma's
# default embedder, which downloads an 80 MB ONNX model on first query — bake
# it in now so the first user request isn't paying for that download.
COPY data/chromadb ./data/chromadb
RUN python -c "\
import chromadb; \
c = chromadb.PersistentClient(path='data/chromadb'); \
cols = c.list_collections(); \
print('collections:', [x.name for x in cols]); \
cols and c.get_collection(cols[0].name).query(query_texts=['warmup'], n_results=1)"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=7860 \
    NAT_CONFIG=/app/src/hackathon_nyc/configs/config_gridwatch.yml \
    GRIDWATCH_DATA_DIR=/app/data

# Fail the build if NAT can't discover the tool groups — a broken entry point
# should not become a running container. Introspect the registry rather than
# grepping `nat info components`: that renders a Rich table which wraps long
# component names across lines, so the grep silently fails.
RUN python -c "\
from nat.runtime.loader import PluginTypes, discover_and_register_plugins; \
discover_and_register_plugins(PluginTypes.ALL); \
from nat.cli.type_registry import GlobalTypeRegistry; \
r = GlobalTypeRegistry.get(); \
names = {k.static_type() for k in r._registered_function_groups}; \
missing = {'nyc_flood_tools','nyc_311_tools','nyc_geo_tools','nyc_crm_tools','nyc_history_tools'} - names; \
print('tool groups:', sorted(n for n in names if 'nyc' in str(n))); \
exit(1) if missing else print('OK')"

EXPOSE 7860

HEALTHCHECK --interval=30s --timeout=10s --start-period=90s --retries=3 \
    CMD curl -fsS "http://localhost:${PORT}/api/agent/status" || exit 1

CMD ["sh", "-c", "uvicorn hackathon_nyc.server:app --host 0.0.0.0 --port ${PORT}"]
