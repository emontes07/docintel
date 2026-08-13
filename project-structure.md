# DocIntel Project Structure

## Overview

DocIntel is a batch document attribute extraction pipeline. It has a Python
backend (FastAPI) and a Next.js frontend, backed by Azure AI Foundry, Azure Blob
Storage, Azure Cosmos DB, and Azure Container Apps.

> Status: mid-migration. The video and image generation paths have been removed
> and the extraction pipeline is not yet wired to an HTTP router. See
> [MIGRATION-NOTES.md](MIGRATION-NOTES.md) for the full history and remaining
> work.

## Core Components

### 1. Backend (Python / FastAPI)

```
backend/
├── main.py                     # App composition root: logging, CORS, static mount, router registration
├── Dockerfile
├── BACKEND.MD
├── api/
│   └── endpoints/
│       ├── gallery.py          # Asset listing, upload, delete, move, SAS tokens, health, folders
│       ├── metadata_router.py  # Cosmos metadata CRUD, search, folder stats, sync
│       └── env.py              # Environment variable status check
├── core/
│   ├── __init__.py             # Shared credential, LLM client re-exports, SAS token generation
│   ├── llm.py                  # LLMClient: schema-constrained structured output over gpt-4o
│   ├── analysis.py             # ArtifactAnalyzer: artifact ref -> model call -> Cosmos
│   ├── instructions.py         # System message templates
│   ├── config.py               # Pydantic Settings
│   ├── azure_storage.py        # Blob Storage service
│   ├── cosmos_client.py        # Cosmos DB service
│   └── logging_config.py       # Centralized logging setup
├── models/
│   ├── common.py               # Shared schema primitives
│   ├── gallery.py              # Gallery schemas and the MediaType enum
│   └── metadata_models.py      # Asset metadata schemas
└── static/
    └── images/                 # Static sample assets
```

### 2. Frontend (Next.js 15 / React 19)

```
frontend/
├── app/
│   ├── layout.tsx              # Root layout, providers, sidebar shell
│   ├── page.tsx                # Landing placeholder for the extraction UI
│   ├── settings/               # Settings and environment status
│   ├── login/                  # Auth entry
│   ├── test-simple/            # Scratch page
│   ├── not-found.tsx
│   └── api/
│       ├── auth/[...nextauth]/ # NextAuth handler
│       └── environment/        # Environment route handler
├── components/
│   ├── app-sidebar.tsx         # Navigation shell
│   ├── page-header.tsx
│   ├── brand-*.tsx             # Brand theming components
│   ├── theme-*.tsx
│   ├── AuthStatus.tsx, login-form.tsx, animated-layout.tsx
│   └── ui/                     # shadcn/Radix primitives
├── context/
│   ├── brand-context.tsx
│   ├── folder-context.tsx
│   └── image-settings-context.tsx
├── services/
│   ├── api.ts                  # Gallery, folder, and metadata client
│   └── sas-token.ts            # SAS token retrieval and blob URL building
├── utils/                      # gallery-utils, brands, env-utils, date, cn
├── hooks/
└── public/                     # Logos, manifest, service worker
```

### 3. Infrastructure

```
infra/
├── main.bicep                  # Root deployment template
├── main.parameters.json        # azd parameter bindings
└── modules/                    # Per-resource Bicep modules
```

```
docker/                         # Standalone backend/frontend dockerfiles
scripts/dev.sh                  # Local dev server launcher
tests/                          # pytest suite (unit + integration)
notebooks/                      # Supporting notebook utilities and sample images
```

## API Endpoints

### Gallery (`/api/v1/gallery`)

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/` | List all gallery items |
| GET | `/images` | List image assets |
| GET | `/videos` | List video assets |
| POST | `/upload` | Upload an asset |
| DELETE | `/delete` | Delete an asset |
| PUT | `/move` | Move an asset between folders |
| GET | `/asset/{media_type}/{blob_name}` | Stream asset content |
| GET | `/sas-tokens` | Issue container SAS tokens |
| GET | `/health` | Service health |
| GET | `/metadata/status` | Metadata service status |
| GET | `/folders` | List folders |
| POST | `/folders` | Create a folder |

### Metadata (`/api/v1/metadata`)

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/` | Create asset metadata |
| GET | `/{asset_id}` | Get asset metadata |
| PUT | `/{asset_id}` | Update asset metadata |
| DELETE | `/{asset_id}` | Delete asset metadata |
| GET | `/` | List asset metadata |
| POST | `/search` | Search metadata |
| GET | `/stats/folders` | Folder statistics |
| GET | `/recent` | Recent assets |
| POST | `/sync` | Sync blob storage into Cosmos |

### Environment (`/api/v1`)

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/env/status` | Report which settings are set or missing |

Plus two app-level routes: `GET /` and `GET /api/v1/health`.

## Extraction Pipeline

`backend/core/llm.py` and `backend/core/analysis.py` hold the extraction
plumbing. Neither is mounted behind an HTTP route yet.

1. `ArtifactAnalyzer.analyze()` receives an `ArtifactRef` (asset id, media type,
   and a name or URL).
2. `_load_artifact()` resolves the reference into model input. **TODO:** the
   domain-specific fetch and preprocessing step.
3. `LLMClient.acomplete_structured()` calls the `gpt-4o` deployment with a
   JSON-schema-constrained response format derived from a Pydantic model, and
   validates the result. Failures raise `LLMSchemaValidationError`.
4. The validated result is written to the asset's Cosmos record under
   `analysis`, with `has_analysis` set.

The extraction prompt (`instructions.py`) and result schema
(`analysis.ArtifactAnalysis`) are placeholders marked with TODOs.

## External Dependencies

- **Azure AI Foundry** — `gpt-4o` deployment for extraction
- **Azure Blob Storage** — document and asset storage
- **Azure Cosmos DB** — extracted metadata
- **Azure Container Apps** — hosting for both services

All authenticate via `DefaultAzureCredential`; no API keys are used.

## Configuration

Settings live in `backend/core/config.py` and are read from environment
variables. Key values:

| Setting | Purpose |
| --- | --- |
| `AI_FOUNDRY_ENDPOINT` | AI Foundry resource endpoint |
| `LLM_DEPLOYMENT` | Deployment name used for extraction (`gpt-4o`) |
| `AZURE_BLOB_SERVICE_URL` / `AZURE_STORAGE_ACCOUNT_NAME` | Blob Storage |
| `AZURE_BLOB_IMAGE_CONTAINER` / `AZURE_BLOB_VIDEO_CONTAINER` | Container names retained by the gallery layer |
| `AZURE_COSMOS_DB_ENDPOINT` / `AZURE_COSMOS_DB_ID` / `AZURE_COSMOS_CONTAINER_ID` | Cosmos DB |
| `LOG_LEVEL` | Logging verbosity |

## Development Workflow

- Backend: `uv run fastapi dev` from `backend/`
- Frontend: `npm run dev` from `frontend/`
- Both: `scripts/dev.sh`
- Tests: `uv run pytest`
