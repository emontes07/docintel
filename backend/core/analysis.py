"""Generic artifact analysis orchestration.

Pipeline: artifact reference -> model call -> structured metadata -> Cosmos.

The orchestration here is domain-agnostic. The parts that describe *what* is
being extracted (artifact loading, system prompt, result schema) are marked
with TODOs and will be filled in by the document-intelligence step.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Type

from pydantic import BaseModel, Field

from backend.core.cosmos_client import CosmosDBService
from backend.core.instructions import extraction_system_message
from backend.core.llm import LLMClient, LLMError

logger = logging.getLogger(__name__)


class AnalysisError(Exception):
    """Raised when an artifact cannot be analyzed."""


@dataclass
class ArtifactRef:
    """Points at a stored artifact and its Cosmos metadata record.

    ``asset_id`` and ``media_type`` together form the Cosmos key (media_type is
    the partition key).
    """

    asset_id: str
    media_type: str
    name: Optional[str] = None
    url: Optional[str] = None


class ArtifactAnalysis(BaseModel):
    """Structured result written back to Cosmos.

    TODO: replace with the document-intelligence extraction schema (fields,
    types, and per-field descriptions the model should populate).
    """

    summary: str = Field(description="Short summary of the artifact's content")
    tags: list[str] = Field(default_factory=list, description="Retrieval tags")


class ArtifactAnalyzer:
    """Runs an artifact through the LLM and persists the structured result."""

    def __init__(
        self,
        llm: LLMClient,
        cosmos_service: Optional[CosmosDBService] = None,
        system_message: str = extraction_system_message,
    ):
        self.llm = llm
        self.cosmos_service = cosmos_service
        self.system_message = system_message

    async def analyze(
        self,
        artifact: ArtifactRef,
        *,
        schema: Type[BaseModel] = ArtifactAnalysis,
        system_message: Optional[str] = None,
        persist: bool = True,
    ) -> BaseModel:
        """Analyze one artifact and optionally persist the result to Cosmos."""
        content = await self._load_artifact(artifact)

        try:
            result = await self.llm.acomplete_structured(
                system_message or self.system_message,
                content,
                schema,
            )
        except LLMError as exc:
            logger.error("Analysis failed for %s: %s", artifact.asset_id, exc)
            raise AnalysisError(str(exc)) from exc

        if persist:
            await self._persist(artifact, result)

        return result

    async def _load_artifact(self, artifact: ArtifactRef) -> Any:
        """Resolve an artifact reference into model input.

        TODO: implement domain-specific retrieval and preprocessing (download
        from blob storage, page splitting, text extraction, or multimodal
        content parts). Returning the reference alone is a placeholder.
        """
        if not artifact.url and not artifact.name:
            raise AnalysisError(
                f"Artifact {artifact.asset_id} has no url or name to load"
            )
        return artifact.url or artifact.name

    async def _persist(self, artifact: ArtifactRef, result: BaseModel) -> None:
        """Write the validated result into the artifact's Cosmos record."""
        if not self.cosmos_service:
            logger.debug(
                "No Cosmos service configured; skipping persist for %s",
                artifact.asset_id,
            )
            return

        updates: Dict[str, Any] = {
            "analysis": {
                **result.model_dump(),
                "analyzed_at": datetime.now(timezone.utc).isoformat(),
            },
            "has_analysis": True,
        }

        # Cosmos SDK is synchronous; keep it off the event loop.
        await asyncio.to_thread(
            self.cosmos_service.update_asset_metadata,
            artifact.asset_id,
            artifact.media_type,
            updates,
        )
        logger.info("Persisted analysis for asset %s", artifact.asset_id)
