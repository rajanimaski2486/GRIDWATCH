"""Custom NAT retriever providers for GridWatch.

NAT 1.8 ships only `milvus_retriever` and `nemo_retriever`. GridWatch stores its
historical NYC Open Data in OpenSearch (Aiven-hosted), so the provider below is
registered the same way NAT registers its own.
"""

from hackathon_nyc.retrievers import opensearch  # noqa: F401

__all__ = ["opensearch"]
