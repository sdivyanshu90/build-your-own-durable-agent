"""Provider-neutral research source ingestion."""

from durable_agent.research.http_fetcher import HttpDocumentFetcher
from durable_agent.research.service import ResearchService

__all__ = ["HttpDocumentFetcher", "ResearchService"]
