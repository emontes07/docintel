# Backend Documentation

This document provides an overview of the backend system for DocIntel. It is intended to help developers, including future AI systems, understand the project structure, design decisions, and how to navigate the codebase.

## 1. Introduction

The backend is a FastAPI application responsible for handling API requests, extracting structured attributes from documents via Azure AI Foundry, managing storage (Azure Blob Storage), persisting metadata (Azure Cosmos DB), and serving data to the frontend or other clients.

> Status: mid-migration. The extraction pipeline in `core/llm.py` and
> `core/analysis.py` is library code and is not yet mounted behind an HTTP
> router. See [MIGRATION-NOTES.md](../MIGRATION-NOTES.md).

## 2. Project Structure

```
backend/
├── .dockerignore            # Specifies intentionally untracked files for Docker
├── Dockerfile               # Instructions to build the Docker image
├── __init__.py
├── main.py                  # FastAPI application entry point
├── api/                     # API endpoint definitions
│   ├── __init__.py
│   └── endpoints/           # Routers for different API resources
│       ├── __init__.py
│       ├── env.py           # Environment variable status endpoint
│       ├── gallery.py       # Asset listing, upload, delete, move, SAS, folders
│       └── metadata_router.py # Cosmos metadata CRUD, search, stats, sync
├── core/                    # Core business logic, services, and utilities
│   ├── __init__.py          # Shared credential, LLM client re-exports, SAS tokens
│   ├── analysis.py          # ArtifactAnalyzer: artifact ref -> model call -> Cosmos
│   ├── azure_storage.py     # Client for Azure Blob Storage interaction
│   ├── config.py            # Application configuration settings
│   ├── cosmos_client.py     # Client for Azure Cosmos DB interaction
│   ├── instructions.py      # System message templates for AI models
│   ├── llm.py               # LLMClient: schema-constrained structured output
│   └── logging_config.py    # Centralized logging setup
├── models/                  # Pydantic models for validation and serialization
│   ├── __init__.py
│   ├── common.py            # Common/shared Pydantic models
│   ├── gallery.py           # Gallery models and the MediaType enum
│   └── metadata_models.py   # Asset metadata models
└── static/                  # Static files served by the backend
```

### Key Files and Directories

*   **`main.py`**: Main entry point. Initializes the FastAPI app, configures logging and CORS, mounts `/static`, and includes the gallery, metadata, and env routers.
*   **`api/endpoints/`**: Each file defines a FastAPI `APIRouter` for a specific resource. This is the first place to look when trying to understand or modify an API endpoint.
*   **`core/`**: Core business logic.
    *   **`config.py`**: Manages application settings, loaded from environment variables.
    *   **`llm.py`**: The generic Foundry chat client. Callers pass a Pydantic model; the client requests JSON-schema-constrained output from the `LLM_DEPLOYMENT` deployment and returns a validated instance, raising `LLMSchemaValidationError` on failure. Retry, timeout, and logging live here.
    *   **`analysis.py`**: Orchestration for extraction — resolve an artifact reference, call the model, persist the validated result to Cosmos. The domain-specific pieces are marked with `TODO`.
    *   **`instructions.py`**: System message templates sent to the model.
    *   **`azure_storage.py`**: Client for Azure Blob Storage, used for storing and retrieving assets.
    *   **`cosmos_client.py`**: Client for Azure Cosmos DB, used for asset metadata.
*   **`models/`**: Pydantic models defining API request/response bodies and internal data structures.
*   **`Dockerfile` and `.dockerignore`**: Used for building a Docker container for the backend.

## 3. Key Design Decisions & Conventions

*   **Managed Identity Everywhere**: All Azure services (AI Foundry, Blob Storage, Cosmos DB) authenticate via `DefaultAzureCredential`. There are no API keys in the configuration.
*   **Structured Output as a First-Class Concern**: Model calls go through `LLMClient.complete_structured()` / `acomplete_structured()`, which take a Pydantic schema and return a validated instance. Schema violations raise a typed error rather than returning partial data.
*   **API Documentation**: OpenAPI documentation is generated automatically at `/api/v1/openapi.json`.
*   **FastAPI Framework**: Chosen for its performance, asynchronous capabilities, automatic Pydantic validation, and built-in OpenAPI documentation.
*   **Modular Design**: Concerns are separated into `api` (presentation layer), `core` (business logic/services), and `models` (data structures).
*   **Service-Oriented Logic**: Interactions with external services are encapsulated in `core/` service modules (e.g. `core/llm.py`, `core/azure_storage.py`). Endpoints in `api/endpoints/` consume these services.
*   **Pydantic for Data Validation**: All incoming API request data and outgoing responses are validated using models in `models/`.
*   **Configuration Management**: Settings are centralized in `core/config.py` and loaded from environment variables. The configuration includes:
    *   API settings with versioning (`API_V1_STR`)
    *   AI Foundry endpoint and the LLM deployment name
    *   Azure Blob Storage configuration
    *   Azure Cosmos DB configuration
*   **Cloud Storage (Azure Blob Storage)**:
    *   Assets are stored in Azure Blob Storage, addressed by container and blob name.
    *   Container names are configured via `AZURE_BLOB_IMAGE_CONTAINER` and `AZURE_BLOB_VIDEO_CONTAINER`. Both are retained by the gallery layer, which remains media-type-agnostic via the `MediaType` enum in `models/gallery.py`.
    *   `core/azure_storage.py` handles direct interactions with Azure Blob Storage.
*   **Metadata (Azure Cosmos DB)**:
    *   Asset metadata, including extraction results, is stored in Cosmos DB.
    *   `media_type` is the partition key; `asset_id` is the item id.
    *   `core/cosmos_client.py` handles all Cosmos interactions. The SDK is synchronous, so async callers wrap it with `asyncio.to_thread`.
*   **Asynchronous Operations**: FastAPI's async capabilities are used for I/O-bound operations to keep the backend responsive.

## 4. Directory Structure Details

*   **Storage Directories**: The application creates and uses a local directory defined in `config.py`:
    *   **UPLOAD_DIR**: `./static/uploads` — for temporary uploads

    This directory is automatically created at startup in `main.py` if it doesn't exist.

*   **API Endpoints**:
    *   **Gallery**: Asset listing, upload, delete, move, streaming, SAS token issuance, health, and folder management
    *   **Metadata**: Cosmos metadata CRUD, search, folder statistics, recent assets, and blob-to-Cosmos sync
    *   **Environment**: Reports which settings are set or missing

## 5. How to Find Information

*   **To understand an API endpoint**:
    1.  Start in `backend/api/endpoints/`.
    2.  Identify the specific route (e.g., `/gallery/upload`).
    3.  Examine the request model in `backend/models/` to see expected input.
    4.  Trace the function calls into `backend/core/` to understand the underlying business logic.
*   **To understand how the model is called**:
    1.  Go to `backend/core/llm.py`.
    2.  `complete_structured()` and `acomplete_structured()` build the messages and the `json_schema` response format, then validate the result.
    3.  `_strictify()` shows how a Pydantic schema is adapted to Azure OpenAI strict structured outputs.
*   **To understand the extraction pipeline**:
    1.  Go to `backend/core/analysis.py` and read `ArtifactAnalyzer.analyze()`.
    2.  The `TODO` comments mark the domain-specific artifact loading, prompt, and result schema.
*   **To find data structures (request/response schemas)**:
    1.  Navigate to `backend/models/`.
*   **To understand configuration settings**:
    1.  Look at `backend/core/config.py`.
*   **To understand storage operations**:
    1.  Check `backend/core/azure_storage.py` for blob operations and `backend/core/cosmos_client.py` for metadata.
*   **To understand the application's entry point and overall setup**:
    1.  Review `backend/main.py`.

## 6. Development Guidelines for Future AI Systems

When developing new features or modifying existing ones, please adhere to the following:

*   **Maintain Modularity**: Place API endpoint definitions in `api/endpoints/`, business logic in `core/`, and Pydantic models in `models/`.
*   **Route Model Calls Through `LLMClient`**: Do not call the OpenAI SDK directly from endpoints. Add a Pydantic schema and use `complete_structured()` so validation, retry, and logging behavior stay consistent.
*   **Use Pydantic Models**: Define Pydantic models for all new API request/response bodies and significant internal data structures.
*   **Prioritize Asynchronous Code**: Use `async` and `await` for I/O-bound operations. Wrap synchronous SDK calls (such as Cosmos) in `asyncio.to_thread`.
*   **Configuration**: Add new configuration variables to `core/config.py` and ensure they can be set via environment variables.
*   **Error Handling**: Implement robust error handling for external API calls and I/O operations. Prefer typed exceptions over returning partial data.
*   **Logging**: Add appropriate logging to help with debugging and monitoring.
*   **Testing Recommendations**: Write unit and integration tests for new features. Mock external dependencies so tests are reliable without live API calls.
*   **Update This Document**: If significant structural changes or new architectural patterns are introduced, update this `BACKEND.MD` file accordingly.
