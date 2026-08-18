"""Foundry **Web Search tool** backend (the GA successor to Grounding with Bing Search).

Uses the same managed-identity credential pattern as ``backend/core/llm.py``
(``DefaultAzureCredential``); no API keys and no new auth path.

Why this tool rather than classic Grounding with Bing Search
------------------------------------------------------------
Per Microsoft Learn, the Web Search tool's Grounding-with-Bing resource is
"Managed by Microsoft", while classic Grounding with Bing Search is "Managed by
you — requires creating a Grounding with Bing Search resource first". Web Search
also "requires no extra roles beyond your Foundry project access", whereas the
classic tool needs Contributor/Owner to create the Bing resource and Foundry
Project Manager to create the project connection. Attaching ``WebSearchTool``
directly to a prompt agent "doesn't require a toolbox or a separate Bing project
connection". Classic agents retire 2027-03-31.

KNOWN LIMITATION — results are URL-only
---------------------------------------
Web grounding returns the model's synthesized answer plus ``url_citation``
annotations, not raw search results. So:

* ``SearchResult.url``     — populated from the citation annotation
* ``SearchResult.title``   — best-effort; present in some annotation shapes and
                             absent in others (the REST sample omits it), so
                             often empty
* ``SearchResult.snippet`` — **always empty**; raw page content is never exposed

This aligns with the module-level rule in ``websearch.py``: there is no raw
content to persist here even by accident.

DATA BOUNDARY
-------------
Web Search is built on Grounding with Bing. The Microsoft Data Protection
Addendum does **not** apply to data sent to it, and queries flow outside the
Azure compliance and geographic boundary. This is identical for both Bing tool
variants — see MIGRATION-NOTES.md, "Web search backends — known blockers".
"""

import logging
from datetime import datetime, timezone
from typing import Any, List, Optional

from azure.identity import DefaultAzureCredential

from backend.core.config import settings
from backend.core.websearch import (
    NotConfiguredError,
    SearchResult,
    WebSearchError,
    filter_by_domain,
)

logger = logging.getLogger(__name__)

AGENT_NAME = "docintel-websearch"
AGENT_INSTRUCTIONS = (
    "Search the public web for the user's query and cite every source you use. "
    "Do not summarize entire pages."
)


class FoundryWebSearchClient:
    """Web search backed by the Foundry Web Search tool on a prompt agent."""

    def __init__(
        self,
        project_endpoint: Optional[str] = None,
        model_deployment: Optional[str] = None,
        credential: Optional[Any] = None,
    ):
        self.project_endpoint = project_endpoint or settings.AI_FOUNDRY_PROJECT_ENDPOINT
        self.model_deployment = model_deployment or settings.LLM_DEPLOYMENT
        self._credential = credential

    def _get_credential(self) -> Any:
        if self._credential is None:
            self._credential = DefaultAzureCredential()
        return self._credential

    def _require_config(self) -> None:
        missing = [
            name
            for name, value in (
                ("AI_FOUNDRY_PROJECT_ENDPOINT", self.project_endpoint),
                ("LLM_DEPLOYMENT", self.model_deployment),
            )
            if not value
        ]
        if missing:
            raise NotConfiguredError(
                "Foundry Web Search is not configured; missing: " + ", ".join(missing)
            )

    def search(
        self, query: str, allowed_domains: Optional[List[str]] = None
    ) -> List[SearchResult]:
        self._require_config()

        # Imported lazily: azure-ai-projects is not yet declared in
        # pyproject.toml, so importing at module scope would break app import.
        try:
            from azure.ai.projects import AIProjectClient
            from azure.ai.projects.models import PromptAgentDefinition, WebSearchTool
        except ImportError as exc:
            raise WebSearchError(
                "The Foundry Web Search tool requires the azure-ai-projects "
                "package, which is not installed. Add it to pyproject.toml and "
                "run `uv sync`."
            ) from exc

        project = None
        agent = None
        try:
            project = AIProjectClient(
                endpoint=self.project_endpoint,
                credential=self._get_credential(),
            )
            openai_client = project.get_openai_client()

            agent = project.agents.create_version(
                agent_name=AGENT_NAME,
                definition=PromptAgentDefinition(
                    model=self.model_deployment,
                    instructions=AGENT_INSTRUCTIONS,
                    tools=[WebSearchTool()],
                ),
                description="DocIntel tier-3/tier-4 web source lookup.",
            )

            response = openai_client.responses.create(
                tool_choice="required",
                input=query,
                extra_body={
                    "agent_reference": {"name": agent.name, "type": "agent_reference"}
                },
            )
            results = self._results_from_response(response)
        except WebSearchError:
            raise
        except Exception as exc:
            raise WebSearchError(f"Foundry web search failed: {exc}") from exc
        finally:
            if project is not None and agent is not None:
                try:
                    project.agents.delete_version(
                        agent_name=agent.name, agent_version=agent.version
                    )
                except Exception:  # cleanup must not mask the original error
                    logger.warning("Failed to delete agent version %s", agent.name)

        # TODO: general Web Search has no server-side domain filter. Restricting
        # to specific domains requires `custom_search_configuration` backed by a
        # Bing Custom Search resource + instance and a project connection — i.e.
        # it reintroduces the infra the general tool avoids. Filtering
        # client-side means allowed_domains can shrink the set to nothing.
        return filter_by_domain(results, allowed_domains)

    @staticmethod
    def _results_from_response(response: Any) -> List[SearchResult]:
        """Map ``url_citation`` annotations on the response to SearchResults.

        Written defensively with ``getattr``: annotation shape varies between the
        SDK and REST surfaces (``title`` is present in some, absent in others).
        """
        retrieved_at = datetime.now(timezone.utc)
        seen: set[str] = set()
        results: List[SearchResult] = []

        for item in getattr(response, "output", None) or []:
            if getattr(item, "type", None) != "message":
                continue
            for part in getattr(item, "content", None) or []:
                for annotation in getattr(part, "annotations", None) or []:
                    if getattr(annotation, "type", None) != "url_citation":
                        continue
                    url = getattr(annotation, "url", "") or ""
                    if not url or url in seen:
                        continue
                    seen.add(url)
                    results.append(
                        SearchResult(
                            url=url,
                            title=getattr(annotation, "title", "") or "",
                            snippet="",  # never exposed by web grounding
                            retrieved_at=retrieved_at,
                        )
                    )

        return results
