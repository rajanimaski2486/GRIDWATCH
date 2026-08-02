"""OpenSearch retriever provider for NAT.

NAT 1.8 registers only `milvus_retriever` and `nemo_retriever`, so this adds
`opensearch_retriever` using the same registration API NAT uses internally
(`register_retriever_provider` + `register_retriever_client`).

Why OpenSearch rather than the ChromaDB it replaces:

- It is hosted (Aiven), so state survives container restarts and the 69 MB
  index plus the 167 MB ONNX embedding model leave the image.
- Embeddings come from the NIM embedder declared in the workflow config, so
  the retrieval path is NVIDIA-native instead of running MiniLM locally.
- It can do hybrid retrieval (kNN + BM25 fused with Reciprocal Rank Fusion),
  which a pure vector store cannot.

On hybrid being OFF by default: it was measured on this corpus, not assumed.
Against ~360 indexed documents it produced no improvement on exact-token
queries ("TIEBOUT AVENUE", "Concord St/Navy St sensor" — identical results
either way) and one clear regression: "flooding water depth" pulled an
Illegal Parking record to rank 2 because BM25 matched a stray token, where
vector-only returned three clean flood events. Small corpus plus field-value
text gives BM25 too many spurious matches, and RRF rewards a top-ranked
lexical hit regardless of relevance. Turn it on (`hybrid: true`) if the index
grows large enough for lexical recall to pay for itself, and re-measure.

RRF is used instead of OpenSearch's native `hybrid` query because that requires
a search pipeline configured on the cluster; fusing client-side keeps this
working against any OpenSearch, including a plain Aiven service with no extra
setup.

Credentials never appear in the YAML. Set them in the environment:

    OPENSEARCH_URL=https://user:password@host:port     # Aiven's service URI
    # or
    OPENSEARCH_URL=https://host:port
    OPENSEARCH_USER=avnadmin
    OPENSEARCH_PASSWORD=...
"""

import logging
import os

from pydantic import Field

from nat.builder.builder import Builder, LLMFrameworkEnum
from nat.builder.retriever import RetrieverProviderInfo
from nat.cli.register_workflow import register_retriever_client, register_retriever_provider
from nat.data_models.retriever import RetrieverBaseConfig
from nat.retriever.interface import Retriever
from nat.retriever.models import Document, RetrieverError, RetrieverOutput

logger = logging.getLogger(__name__)

# Reciprocal Rank Fusion damping constant. 60 is the value from the original
# Cormack et al. paper and is what Elastic/OpenSearch use as their default.
RRF_K = 60


class OpenSearchRetrieverConfig(RetrieverBaseConfig, name="opensearch_retriever"):
    """Configuration for a Retriever backed by an OpenSearch cluster."""

    uri: str = Field(
        default="",
        description="OpenSearch endpoint. Falls back to $OPENSEARCH_URL. May embed "
                    "credentials, which is the format Aiven hands out.",
    )
    index_name: str = Field(
        default="",
        description="Index to search. May be a comma-separated list or a wildcard "
                    "such as 'nyc_*' to search every collection at once.",
    )
    embedding_model: str = Field(
        description="Name of the embedder in this config used to vectorize the query",
    )
    content_field: str = Field(default="text", description="Field holding the document text")
    vector_field: str = Field(default="vector", description="Field holding the embedding")
    top_k: int = Field(default=6, gt=0, description="Number of results to return")
    output_fields: list[str] | None = Field(
        default=None,
        description="Source fields to return. None returns everything except the vector.",
    )
    hybrid: bool = Field(
        default=False,
        description="Fuse kNN and BM25 results with RRF. Off by default — see the "
                    "measurement note in this module's docstring.",
    )
    verify_certs: bool = Field(default=True, description="Verify TLS certificates")
    description: str | None = Field(default=None, description="Used as the tool description")


def _resolve_connection(config: OpenSearchRetrieverConfig) -> dict:
    """Build opensearch-py client kwargs from config plus environment."""
    uri = config.uri or os.getenv("OPENSEARCH_URL", "")
    if not uri:
        raise RetrieverError(
            "No OpenSearch endpoint. Set OPENSEARCH_URL in the environment or "
            "`uri` in the retriever config."
        )

    kwargs: dict = {"hosts": [uri], "verify_certs": config.verify_certs}

    # Aiven's service URI embeds credentials; only add explicit auth when the
    # URI does not already carry it.
    user = os.getenv("OPENSEARCH_USER", "")
    password = os.getenv("OPENSEARCH_PASSWORD", "")
    if user and password and "@" not in uri.split("//", 1)[-1]:
        kwargs["http_auth"] = (user, password)

    return kwargs


class OpenSearchRetriever(Retriever):
    """Hybrid (kNN + BM25) retriever over one or more OpenSearch indices."""

    def __init__(self, client, embedder, config: OpenSearchRetrieverConfig):
        self._client = client
        self._embedder = embedder
        self._cfg = config

    async def search(self, query: str, **kwargs) -> RetrieverOutput:
        index = kwargs.get("index_name") or self._cfg.index_name
        top_k = int(kwargs.get("top_k") or self._cfg.top_k)
        if not index:
            raise RetrieverError("No index_name configured for opensearch_retriever.")

        vector = await self._embedder.aembed_query(query)

        # Over-fetch each arm so fusion has room to reorder.
        fetch = top_k * 3 if self._cfg.hybrid else top_k

        source = self._cfg.output_fields or {"excludes": [self._cfg.vector_field]}

        knn_body = {
            "size": fetch,
            "_source": source,
            "query": {"knn": {self._cfg.vector_field: {"vector": vector, "k": fetch}}},
        }

        try:
            knn_hits = (await self._asearch(index, knn_body))["hits"]["hits"]
        except Exception as e:
            raise RetrieverError(f"OpenSearch kNN search failed on '{index}': {e}") from e

        if not self._cfg.hybrid:
            return self._to_output(knn_hits[:top_k])

        bm25_body = {
            "size": fetch,
            "_source": source,
            "query": {"match": {self._cfg.content_field: {"query": query}}},
        }
        try:
            bm25_hits = (await self._asearch(index, bm25_body))["hits"]["hits"]
        except Exception as e:
            # Lexical search is the optional half; degrade to vector-only rather
            # than failing the whole retrieval.
            logger.warning("[OpenSearch] BM25 arm failed, using kNN only: %s", e)
            return self._to_output(knn_hits[:top_k])

        return self._to_output(self._rrf(knn_hits, bm25_hits)[:top_k])

    async def _asearch(self, index: str, body: dict) -> dict:
        result = self._client.search(index=index, body=body)
        # opensearch-py returns a coroutine only for the async client.
        if hasattr(result, "__await__"):
            result = await result
        return result

    @staticmethod
    def _rrf(*hit_lists) -> list:
        """Reciprocal Rank Fusion: score = sum(1 / (k + rank)) across result lists.

        Rank-based rather than score-based, so kNN distances and BM25 relevance
        scores — which are not on comparable scales — can be combined without
        normalizing either.
        """
        scores: dict[str, float] = {}
        docs: dict[str, dict] = {}
        for hits in hit_lists:
            for rank, hit in enumerate(hits):
                key = f"{hit.get('_index')}::{hit.get('_id')}"
                scores[key] = scores.get(key, 0.0) + 1.0 / (RRF_K + rank + 1)
                docs.setdefault(key, hit)
        ordered = sorted(scores.items(), key=lambda kv: -kv[1])
        return [docs[key] for key, _ in ordered]

    def _to_output(self, hits: list) -> RetrieverOutput:
        results = []
        for hit in hits:
            src = dict(hit.get("_source") or {})
            content = src.pop(self._cfg.content_field, "")
            src.pop(self._cfg.vector_field, None)
            src["_index"] = hit.get("_index")
            if hit.get("_score") is not None:
                src["_score"] = hit["_score"]
            results.append(
                Document(page_content=content, metadata=src, document_id=str(hit.get("_id")))
            )
        return RetrieverOutput(results=results)


@register_retriever_provider(config_type=OpenSearchRetrieverConfig)
async def opensearch_retriever(retriever_config: OpenSearchRetrieverConfig, builder: Builder):
    yield RetrieverProviderInfo(
        config=retriever_config,
        description="An OpenSearch data store adapter for a NAT Retriever Client",
    )


@register_retriever_client(config_type=OpenSearchRetrieverConfig, wrapper_type=None)
async def opensearch_retriever_client(config: OpenSearchRetrieverConfig, builder: Builder):
    try:
        from opensearchpy import OpenSearch
    except ImportError as e:
        raise RetrieverError(
            "opensearch-py is not installed. Install it with: pip install '.[opensearch]'"
        ) from e

    embedder = await builder.get_embedder(
        embedder_name=config.embedding_model, wrapper_type=LLMFrameworkEnum.LANGCHAIN
    )
    client = OpenSearch(**_resolve_connection(config))

    yield OpenSearchRetriever(client=client, embedder=embedder, config=config)
