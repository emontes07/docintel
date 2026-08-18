# Migration Notes

Audit of the current repository state, produced ahead of removing the video and
image generation paths. Everything below was read directly from source; nothing
is inferred from `project-structure.md` or `BACKEND.MD` (both of which are stale
— see section 7).

Legend used throughout: **[video]** = video generation only, **[image]** = image
generation only, **[shared]** = used by both, **[infra]** = neither (storage,
metadata, config, auth).

---

## 1. Backend file inventory

### Package roots

| File | Responsibility |
| --- | --- |
| `backend/__init__.py` | Empty package marker. [infra] |
| `backend/main.py` | App composition root: calls `setup_logging()`, creates `UPLOAD_DIR`/`IMAGE_DIR`/`VIDEO_DIR`, builds the `FastAPI` instance, adds CORS middleware, mounts `/static`, registers all five routers, and defines `GET /` and `GET /api/v1/health`. [shared] |
| `backend/Dockerfile` | Backend container image build. [infra] |
| `backend/.dockerignore` | Docker build context exclusions. [infra] |
| `backend/BACKEND.MD` | Backend documentation (stale — see section 7). [infra] |

### `backend/api/`

| File | Responsibility |
| --- | --- |
| `api/__init__.py` | Empty package marker. [infra] |
| `api/endpoints/__init__.py` | Empty package marker. [infra] |
| `api/endpoints/images.py` | Image router: generation, editing, save, pipeline, listing, deletion, analysis, prompt enhancement/brand protection, filename generation. Delegates generation work to `ImagePipelineService`. [image] |
| `api/endpoints/videos.py` | Video router: Sora job lifecycle, server-side finalization, generate-with-analysis (JSON / upload / SSE stream), content download, analysis, prompt enhancement + moderation-safe rewrite, filename generation, cameo references, remix. Largest module in the backend (~1800 lines). [video] |
| `api/endpoints/gallery.py` | Gallery router: unified listing of image/video assets from Cosmos or blob storage, upload, delete, move, asset streaming, SAS token issuance, health check, metadata-service status, folder listing/creation. [shared] |
| `api/endpoints/metadata_router.py` | Asset metadata CRUD over Cosmos DB, search, folder statistics, recent assets, and background blob→Cosmos metadata sync. [infra] |
| `api/endpoints/env.py` | Reports which configured settings are set vs. missing. Hardcodes `SORA_DEPLOYMENT` and `IMAGEGEN_DEPLOYMENT` in its required list. [shared] |

### `backend/core/`

| File | Responsibility |
| --- | --- |
| `core/__init__.py` | **Service singleton construction at import time.** Builds the shared `DefaultAzureCredential` + `token_provider`, then `sora_client`, `image_client`, `llm_client`, `async_llm_client`, and generates `video_sas_token` / `image_sas_token`. Each client is wrapped in `try/except` and set to `None` on failure. [shared] |
| `core/config.py` | Pydantic `BaseSettings` subclass (`Settings`) plus the module-level `settings` singleton. `extra = Extra.allow`, so unknown env vars are silently accepted. [shared] |
| `core/logging_config.py` | `setup_logging()` — root logging config from `LOG_LEVEL`, and quiets the Azure HTTP logging policy. [infra] |
| `core/sora.py` | `Sora` class: async Sora 2 client built on `httpx.AsyncClient`. Rewrites the Foundry endpoint host to `<resource>.openai.azure.com/openai/v1/videos`, validates sizes/durations, and manages job create/get/list/delete. Also `convert_sora2_response_to_job_format()`. [video] |
| `core/gpt_image.py` | `GPTImageClient`: image generation/editing via the OpenAI or Azure OpenAI SDK, plus `_get_deployment_for_model()` model→deployment mapping. Exports legacy alias `DALLEClient = GPTImageClient` (line 572). [image] |
| `core/image_pipeline.py` | `ImagePipelineService`: orchestrates generate → analyze → save-to-blob → write-metadata. Constructs its own per-request `GPTImageClient` instances. [image] |
| `core/analyze.py` | `VideoExtractor` (OpenCV frame sampling) [video], `VideoAnalyzer` (multi-frame multimodal chat) [video], `ImageAnalyzer` (single-image multimodal chat) [image]. All three take an OpenAI client + model name by constructor injection. |
| `core/azure_storage.py` | `AzureBlobStorageService`: blob upload/download/delete/move/list across the image and video containers, plus blob-service CORS rule configuration. [infra] |
| `core/cosmos_client.py` | `CosmosDBService` + `DatabaseError`: Cosmos SQL API access for asset metadata (CRUD, query, folder stats). [infra] |
| `core/instructions.py` | Prompt/system-message templates: `video_prompt_enhancement_system_message` [video], `img_prompt_enhance_msg` [image], `brand_protect_replace_msg` / `brand_protect_neutralize_msg` [shared], `analyze_video_system_message` [video], `analyze_image_system_message` [image], `filename_system_message` [shared], `video_prompt_moderation_safe_rewrite_message` [video]. |

### `backend/models/`

| File | Responsibility |
| --- | --- |
| `models/__init__.py` | Comment only — models are imported from their concrete modules. [infra] |
| `models/common.py` | `BaseResponse` and shared schema primitives. [shared] |
| `models/images.py` | Request/response schemas for every image endpoint. [image] |
| `models/videos.py` | Request/response schemas for every video endpoint, including Sora 2 audio/cameo/remix types. [video] |
| `models/gallery.py` | Gallery schemas and the `MediaType` enum (imported by `image_pipeline.py`). [shared] |
| `models/metadata_models.py` | Asset-metadata schemas used by the metadata router and gallery upload. [infra] |

### `backend/static/`

| File | Responsibility |
| --- | --- |
| `static/images/mask.png` | Sample edit mask asset. [image] |
| `static/images/no-smile.png` | Sample source image asset. [image] |

---

## 2. Routers registered in `backend/main.py`

`settings.API_V1_STR` is `"/api/v1"`. Registration order as written:

| Module | Prefix | Tag |
| --- | --- | --- |
| `images.router` | `/api/v1/images` | `images` |
| `videos.router` | `/api/v1/videos` | `videos` |
| `gallery.router` | `/api/v1/gallery` | `gallery` |
| `metadata_router.router` | `/api/v1/metadata` | `metadata` |
| `env.router` | `/api/v1` | `env` |

Plus two routes defined directly on the app: `GET /` (returns `{"message": "Welcome to AI Content Lab API"}`) and `GET /api/v1/health`.

### `/api/v1/images` — [image]

| Method | Path | Handler |
| --- | --- | --- |
| POST | `/generate` | `generate_image` |
| POST | `/edit` | `edit_image` |
| POST | `/edit/upload` | `edit_image_upload` |
| POST | `/save` | `save_generated_images` |
| POST | `/pipeline` | `process_image_pipeline` |
| POST | `/generate-with-analysis` | `generate_image_with_analysis` |
| POST | `/list` | `list_images` |
| POST | `/delete` | `delete_image` |
| POST | `/analyze` | `analyze_image` |
| POST | `/analyze-custom` | `analyze_image_custom` |
| POST | `/prompt/enhance` | `enhance_image_prompt` |
| POST | `/prompt/protect` | `protect_image_prompt` |
| POST | `/filename/generate` | `generate_image_filename` |

### `/api/v1/videos` — [video]

| Method | Path | Handler |
| --- | --- | --- |
| POST | `/jobs` | `create_video_generation_job` |
| GET | `/jobs/{job_id}` | `get_video_generation_job` |
| GET | `/jobs` | `list_video_generation_jobs` |
| DELETE | `/jobs/{job_id}` | `delete_video_generation_job` |
| DELETE | `/jobs/failed` | `delete_failed_video_generation_jobs` |
| POST | `/jobs/{job_id}/finalize` | `finalize_video_job` |
| POST | `/generate-with-analysis/stream` | `stream_video_generation_with_analysis` |
| POST | `/generate-with-analysis/upload` | `create_video_generation_with_analysis_upload` |
| POST | `/generate-with-analysis` | `create_video_generation_with_analysis` |
| GET | `/generations/{generation_id}/content` | `download_generation_content` |
| POST | `/analyze` | `analyze_video` |
| POST | `/prompt/enhance` | `enhance_video_prompt` |
| POST | `/prompt/moderation-safe-rewrite` | `moderation_safe_rewrite_video_prompt` |
| POST | `/filename/generate` | `generate_video_filename` |
| POST | `/cameo/upload` | `upload_cameo_reference` |
| GET | `/cameo/references` | `list_cameo_references` |
| DELETE | `/cameo/references/{reference_id}` | `delete_cameo_reference` |
| POST | `/remix` | `create_remix_job` |

> Route-ordering hazard already present: `DELETE /jobs/failed` is declared *after*
> `DELETE /jobs/{job_id}`, so the path parameter route matches `failed` first.

### `/api/v1/gallery` — [shared]

| Method | Path | Handler |
| --- | --- | --- |
| GET | `/images` | `get_gallery_images` |
| GET | `/videos` | `get_gallery_videos` |
| GET | `/` | `get_gallery_items` |
| POST | `/upload` | `upload_asset` |
| DELETE | `/delete` | `delete_asset` |
| PUT | `/move` | `move_asset` |
| GET | `/asset/{media_type}/{blob_name:path}` | `get_asset_content` |
| GET | `/sas-tokens` | `get_sas_tokens` |
| GET | `/health` | `health_check` |
| GET | `/metadata/status` | `metadata_service_status` |
| GET | `/folders` | `list_folders` |
| POST | `/folders` | `create_folder` |

### `/api/v1/metadata` — [infra]

| Method | Path | Handler |
| --- | --- | --- |
| POST | `/` | `create_asset_metadata` |
| GET | `/{asset_id}` | `get_asset_metadata` |
| PUT | `/{asset_id}` | `update_asset_metadata` |
| DELETE | `/{asset_id}` | `delete_asset_metadata` |
| GET | `/` | `list_asset_metadata` |
| POST | `/search` | `search_asset_metadata` |
| GET | `/stats/folders` | `get_folder_statistics` |
| GET | `/recent` | `get_recent_assets` |
| POST | `/sync` | `sync_metadata` |

> `GET /{asset_id}` is declared before `GET /stats/folders` and `GET /recent`, so
> those two literal paths are shadowed by the path-parameter route.

### `/api/v1` (env) — [shared]

| Method | Path | Handler |
| --- | --- | --- |
| GET | `/env/status` | `env_status` |

---

## 3. Dependency map

### Who depends on `backend/core/sora.py`

Direct imports of the module:

1. `backend/core/__init__.py:9` — `from .sora import Sora`, then constructs the
   module-level `sora_client` singleton at line 22.

That is the **only** direct import. Everything else consumes the `sora_client`
singleton:

| Consumer | Reference |
| --- | --- |
| `backend/api/endpoints/videos.py:23` | `from backend.core import ... sora_client ...`; null-guarded at line 69 and re-checked at lines 167, 263, 279, 295, 311, 350 |
| `backend/api/endpoints/gallery.py:989` | Local import inside `health_check` — reports `ai_services["sora"]` |
| `tests/integration/conftest.py` | `sora_client` fixture |
| `tests/integration/test_video_generation.py` | Uses that fixture |

Not a dependency: `notebooks/VideoTools.py` contains its own standalone copy of a
`Sora` class and `convert_sora2_response_to_job_format`. It does not import from
`backend/` and will not break when `backend/core/sora.py` is deleted.

### Who depends on the image generation code

`backend/core/gpt_image.py` (`GPTImageClient`):

| Consumer | Reference |
| --- | --- |
| `backend/core/__init__.py:10` | `from .gpt_image import GPTImageClient` → builds the `image_client` singleton at line 36 |
| `backend/core/image_pipeline.py:53, 109, ~575` | Function-local imports; constructs a fresh per-request client each time |
| `tests/integration/conftest.py:36` | `image_client` fixture |
| `tests/integration/test_image_generation.py` | Uses that fixture |

`backend/core/image_pipeline.py` (`ImagePipelineService`):

| Consumer | Reference |
| --- | --- |
| `backend/api/endpoints/images.py:52` | `from backend.core.image_pipeline import ImagePipelineService` |
| `tests/integration/test_pipeline.py:21, 45` | Local imports |

Consumers of the `image_client` singleton:

| Consumer | Reference |
| --- | --- |
| `backend/api/endpoints/gallery.py:989` | Health check only — reports `ai_services["dalle/gpt_image"]` |

> Worth knowing before you cut: the `image_client` singleton built in
> `core/__init__.py` is referenced **only** by the gallery health check. All real
> image generation goes through `ImagePipelineService`, which builds its own
> clients. `images.py` imports `llm_client`, `async_llm_client`, and
> `image_sas_token` from `backend.core` — but never `image_client`.

### Coupling that survives removal of both paths

`backend/core/__init__.py` is imported for `llm_client`, `async_llm_client`,
`image_sas_token`, and `video_sas_token`. Deleting `sora.py` and `gpt_image.py`
requires editing that module regardless, because both imports and both singleton
constructions live at module scope and run on any `backend.core` import.

`gallery.py`, `metadata_router.py`, `azure_storage.py`, and `cosmos_client.py`
are media-type-agnostic but reference `AZURE_BLOB_VIDEO_CONTAINER` and
`AZURE_BLOB_IMAGE_CONTAINER` throughout, and `models/gallery.py::MediaType` has
both variants.

---

## 4. Foundry / Azure OpenAI client construction sites

| # | Location | Client | Deployment / model used |
| --- | --- | --- | --- |
| 1 | `core/__init__.py:22` | `Sora` | `settings.SORA_DEPLOYMENT`, `api_version=settings.SORA_API_VERSION` |
| 2 | `core/__init__.py:36` | `GPTImageClient` | `model=settings.DEFAULT_IMAGE_MODEL` (`"gpt-image-1.5"`) → resolves to `settings.IMAGEGEN_DEPLOYMENT` |
| 3 | `core/__init__.py:47` | `AzureOpenAI` (`llm_client`) | No deployment bound at construction; `api_version` hardcoded `"2025-01-01-preview"` |
| 4 | `core/__init__.py:60` | `AsyncAzureOpenAI` (`async_llm_client`) | Same — hardcoded `"2025-01-01-preview"` |
| 5 | `core/gpt_image.py:52` | `AzureOpenAI` (inside `GPTImageClient`) | `azure_ad_token_provider`, `api_version=settings.AOAI_API_VERSION`; deployment from `_get_deployment_for_model()` |
| 6 | `core/gpt_image.py:68` | `OpenAI` (non-Azure branch) | `settings.OPENAI_API_KEY` / `OPENAI_ORG_ID`; `deployment_name = None` |
| 7 | `core/image_pipeline.py:56` | `GPTImageClient` | `provider=settings.MODEL_PROVIDER`, `model=request.model` (caller-supplied) |
| 8 | `core/image_pipeline.py:112` | `GPTImageClient` | Same, edit path |
| 9 | `core/image_pipeline.py:577` | `GPTImageClient` | Same |
| 10 | `tests/integration/conftest.py:36` | `GPTImageClient` | Real Azure credential, `provider="azure"` |

`core/sora.py` does not use an OpenAI SDK client at all — it builds a raw
`httpx.AsyncClient` and sends `payload["model"] = self.deployment_name`
(i.e. `SORA_DEPLOYMENT`) to `<resource>.openai.azure.com/openai/v1/videos`.

### Deployment name per LLM call site

Every LLM call resolves to `settings.LLM_DEPLOYMENT`:

| Location | Form |
| --- | --- |
| `images.py:441` | `ImageAnalyzer(llm_client, settings.LLM_DEPLOYMENT, async_llm_client)` |
| `images.py:601` | `ImageAnalyzer(llm_client, settings.LLM_DEPLOYMENT, async_llm_client)` |
| `images.py:642` | `chat.completions.create(model=settings.LLM_DEPLOYMENT)` — prompt enhance |
| `images.py:689` | `chat.completions.create(model=settings.LLM_DEPLOYMENT)` — brand protect |
| `images.py:734` | `chat.completions.create(model=settings.LLM_DEPLOYMENT)` — filename |
| `image_pipeline.py:879` | `ImageAnalyzer(llm_client, settings.LLM_DEPLOYMENT)` |
| `videos.py:669` | `VideoAnalyzer(llm_client, settings.LLM_DEPLOYMENT)` |
| `videos.py:935` | `VideoAnalyzer(llm_client, settings.LLM_DEPLOYMENT)` |
| `videos.py:1175` | `VideoAnalyzer(llm_client, settings.LLM_DEPLOYMENT)` |
| `videos.py:1500` | `VideoAnalyzer(llm_client, settings.LLM_DEPLOYMENT)` |
| `videos.py:1559` | `chat.completions.create(model=settings.LLM_DEPLOYMENT)` — prompt enhance |
| `videos.py:1603` | `chat.completions.create(model=settings.LLM_DEPLOYMENT)` — moderation-safe rewrite |
| `videos.py:1662` | `chat.completions.create(model=settings.LLM_DEPLOYMENT)` — filename |

`_get_deployment_for_model()` in `gpt_image.py:88-98` maps:

```
gpt-image-1.5    → IMAGEGEN_DEPLOYMENT
gpt-image-1      → IMAGEGEN_DEPLOYMENT   (legacy alias)
gpt-image-1-mini → IMAGEGEN_1_MINI_DEPLOYMENT
gpt-image-2      → IMAGEGEN_2_DEPLOYMENT
flux-kontext-pro → FLUX_KONTEXT_DEPLOYMENT
(unmapped)       → falls back to IMAGEGEN_DEPLOYMENT with a warning
```

---

## 5. Environment variables in `backend/core/config.py`

All fields on `Settings`, in declaration order. "Read at" = where the value is
actually consumed; fields with no reader are flagged.

| Setting | Default | Class | Read at |
| --- | --- | --- | --- |
| `API_V1_STR` | `/api/v1` | [infra] | `main.py` router prefixes, health route |
| `PROJECT_NAME` | `Visionary Lab API` | [infra] | `main.py:20` |
| `MODEL_PROVIDER` | `azure` | [image] | `gpt_image.py:38`, `image_pipeline.py:57,113,577` |
| `AI_FOUNDRY_ENDPOINT` | `None` | [shared] | `core/__init__.py:23,49,61`, `gpt_image.py:51` |
| `LLM_DEPLOYMENT` | `None` | [shared] | 13 call sites (section 4) |
| `IMAGEGEN_DEPLOYMENT` | `None` | [image] | `gpt_image.py:88,89,98` |
| `IMAGEGEN_15_DEPLOYMENT` | `None` | [image] | **Never read.** Declared only; not in the model map, not in `env.py` |
| `IMAGEGEN_1_MINI_DEPLOYMENT` | `None` | [image] | `gpt_image.py:90`, `env.py:35` |
| `IMAGEGEN_2_DEPLOYMENT` | `None` | [image] | `gpt_image.py:91`, `env.py:36` |
| `FLUX_KONTEXT_DEPLOYMENT` | `None` | [image] | `gpt_image.py:92`, `env.py:34` |
| `SORA_DEPLOYMENT` | `None` | [video] | `core/__init__.py:24,29`, `env.py:25` |
| `DEFAULT_IMAGE_MODEL` | `gpt-image-1.5` | [image] | `core/__init__.py:39`, `gpt_image.py:39` |
| `OPENAI_API_KEY` | `None` | [image] | `gpt_image.py:64` |
| `OPENAI_ORG_ID` | `None` | [image] | `gpt_image.py:70` |
| `OPENAI_ORG_VERIFIED` | `False` | [image] | `image_pipeline.py:145` (gates n>1) |
| `GPT_IMAGE_MAX_TOKENS` | `150000` | [image] | **Never read.** |
| `AZURE_BLOB_SERVICE_URL` | `None` | [infra] | `core/__init__.py:73`, `azure_storage.py:28` |
| `AZURE_STORAGE_ACCOUNT_NAME` | `None` | [infra] | `core/__init__.py:74,75,92`, `azure_storage.py:29,30`, `gallery.py:882,893,902,909` |
| `CDN_BLOB_URL` | `None` | [infra] | `gallery.py:909` |
| `AZURE_BLOB_IMAGE_CONTAINER` | `images` | [image] | `core/__init__.py:107`, `azure_storage.py:25`, `gallery.py` (×7), `metadata_router.py:320,325,331` |
| `AZURE_BLOB_VIDEO_CONTAINER` | `videos` | [video] | `core/__init__.py:106`, `azure_storage.py:26`, `gallery.py` (×6), `metadata_router.py:322,326` |
| `CORS_ALLOWED_ORIGINS` | `*` | [infra] | `azure_storage.py:52` **only** — see note below |
| `AZURE_COSMOS_DB_ENDPOINT` | `None` | [infra] | `cosmos_client.py:26`, `gallery.py:128`, `images.py:65`, `videos.py:57,1080` |
| `AZURE_COSMOS_DB_ID` | `visionarylab` | [infra] | `cosmos_client.py:27` |
| `AZURE_COSMOS_CONTAINER_ID` | `metadata` | [infra] | `cosmos_client.py:28` |
| `AOAI_API_VERSION` | `2025-04-01-preview` | [image] | `gpt_image.py:55,241,454` only |
| `SORA_API_VERSION` | `preview` | [video] | `core/__init__.py:27` |
| `UPLOAD_DIR` | `./static/uploads` | [infra] | `main.py:15` (`makedirs` only — never otherwise read) |
| `IMAGE_DIR` | `./static/images` | [image] | `main.py:16` (`makedirs` only — never otherwise read) |
| `VIDEO_DIR` | `./static/videos` | [video] | `main.py:17`, `videos.py:66,1395` |
| `LOG_LEVEL` | `INFO` | [infra] | `logging_config.py:21` |
| `GPT_IMAGE_DEFAULT_SIZE` | `1024x1024` | [image] | **Never read.** |
| `GPT_IMAGE_DEFAULT_QUALITY` | `high` | [image] | **Never read.** |
| `GPT_IMAGE_DEFAULT_FORMAT` | `PNG` | [image] | **Never read.** |
| `GPT_IMAGE_ALLOW_TRANSPARENT` | `True` | [image] | **Never read.** |
| `GPT_IMAGE_MAX_FILE_SIZE_MB` | `25` | [image] | `image_pipeline.py:192` |

Notes:

- **`CORS_ALLOWED_ORIGINS` does not control the API's CORS.** `main.py:26`
  hardcodes `allow_origins=["*"]` with `allow_credentials=True`. The setting and
  its validator are used only to configure *blob storage* CORS rules in
  `azure_storage.py`. This is a live security gap, independent of the migration.
- `Settings.Config` sets `env_file = "../.env"` and `extra = Extra.allow`, so
  typo'd or removed env vars are silently absorbed rather than raising.
- Seven settings have no reader at all (`IMAGEGEN_15_DEPLOYMENT`,
  `GPT_IMAGE_MAX_TOKENS`, and the four `GPT_IMAGE_DEFAULT_*` / `ALLOW_TRANSPARENT`
  fields, plus `UPLOAD_DIR`/`IMAGE_DIR` beyond `makedirs`). They can be dropped
  without behavioral change.

---

## 6. Frontend routes under `frontend/app/`

| Route | File | Class | Notes |
| --- | --- | --- | --- |
| `/` | `app/page.tsx` | [image] | Re-exports `NewImagePage` — the home page *is* the image page |
| `/new-image` | `app/new-image/page.tsx` | [image] | Image generation + image gallery browsing (`fetchImages`) |
| `/new-image/upload` | `app/new-image/upload/page.tsx` | [image] | "Upload Images" view |
| `/new-video` | `app/new-video/page.tsx` | [video] | Video generation + video browsing; Sora 2 cameo/remix settings |
| `/edit-image` | `app/edit-image/page.tsx` | [image] | Masked image editing; has its own `layout.tsx` and 5 local components (`EditorContainer`, `GenerateForm`, `ImageCanvas`, `ImageUploader`, `ResultDisplay`, `createDebugMask.ts`) |
| `/analyze` | `app/analyze/page.tsx` | [image] | Custom image analysis (sidebar: "Custom image analysis with AI") |
| `/gallery` | `app/gallery/page.tsx` | [video] | **Video-only** despite the generic name — imports `fetchVideos`/`VideoMetadata` and renders `VideoCard` |
| `/jobs` | `app/jobs/page.tsx` | [video] | Sora job table; `listVideoGenerationJobs`, `finalizeVideoJob`, `VideoJob`. Also `columns.tsx`, `data-table.tsx`, `loading.tsx` |
| `/settings` | `app/settings/page.tsx` | [shared] | Image settings + brand protection; filters env keys on `SORA`/`REPLICATE`/`RUNWAY` |
| `/login` | `app/login/page.tsx` | [shared] | Auth entry; has its own `layout.tsx` |
| `/test-simple` | `app/test-simple/page.tsx` | [shared] | Scratch/test page |
| `/not-found` | `app/not-found.tsx` | [shared] | 404 handler |
| `/api/environment` | `app/api/environment/route.ts` | [shared] | Next route handler |
| `/api/auth/[...nextauth]` | `app/api/auth/[...nextauth]/route.ts` | [shared] | NextAuth handler |
| (layout) | `app/layout.tsx` | [shared] | Root layout |

Sidebar navigation (`components/app-sidebar.tsx`) exposes only six entries —
Create: New Video, New Image, Edit Image, Analyze; Manage: Jobs, Settings.
**`/gallery` is not linked from the sidebar** and is reachable only by direct URL
or in-app links.

---

## 7. Discrepancies vs. `project-structure.md`

`project-structure.md` is substantially out of date. Every item below is a claim
in that file that does not match the code.

### Things it describes that do not exist

1. **An entire Streamlit UI.** It documents a "Creator App" at `creator.py`,
   "Video Generation" in `video-gen.py`, and "Jobs Management" in `jobs.py`.
   None of these files exist anywhere in the repo, and there is no Streamlit
   dependency.
2. **`backend/core/storage.py`.** Listed as the storage service. The real files
   are `azure_storage.py` (blob) and `cosmos_client.py` (metadata).
3. **`requirements.txt`.** Claimed for Python dependency management. The repo
   uses `pyproject.toml` with uv.
4. **`SORA_AOAI_RESOURCE`, `SORA_AOAI_API_KEY`.** Documented as required env
   vars. Neither exists in `Settings`. Auth is managed-identity via
   `AI_FOUNDRY_ENDPOINT` + `DefaultAzureCredential`; there are no API keys for
   Sora anywhere in the backend. (These stale names *do* still appear in
   `docker-compose.yml`, `DOCKER.md`, `scripts/dev.sh`, and the notebooks, so the
   drift is not confined to this one doc.)

### Things that exist but are undocumented

5. **The entire metadata subsystem.** `api/endpoints/metadata_router.py`, the
   `/api/v1/metadata` router, `models/metadata_models.py`, and
   `core/cosmos_client.py` are absent from the doc. Cosmos DB is not mentioned at
   all.
6. **Six of the eleven `core/` modules**: `gpt_image.py`, `image_pipeline.py`,
   `analyze.py`, `instructions.py`, `logging_config.py`, `azure_storage.py`.
7. **`models/gallery.py`** (including the `MediaType` enum).

### Wrong status claims

8. **"Skeleton implementation" is wrong for images and gallery.** The doc calls
   the Images and Gallery endpoints skeletons. Images has 13 fully implemented
   endpoints; Gallery has 12, including upload, delete, move, SAS issuance, and
   folder management.
9. **"Placeholder" is wrong for `/videos/analyze` and `/videos/filename/generate`.**
   Both are fully implemented against the LLM.

### Incomplete endpoint lists

10. **Videos:** the doc lists 8 endpoints; there are 18. Missing:
    `/jobs/{job_id}/finalize`, all three `generate-with-analysis` variants,
    `/prompt/enhance`, `/prompt/moderation-safe-rewrite`, all three `/cameo/*`
    routes, and `/remix`.
11. **Gallery:** the doc lists 3 endpoints; there are 12. Missing: `/upload`,
    `/delete`, `/move`, `/asset/{media_type}/{blob_name}`, `/sas-tokens`,
    `/health`, `/metadata/status`, and both `/folders` routes.
12. **Images:** the doc lists 3 endpoints; there are 13.

### Smaller inaccuracies

13. **Env router prefix.** The doc says the prefix is `/api/v1/env` with a
    `/status` route. The router is actually mounted at `/api/v1` and declares
    `/env/status`. The resulting URL is identical, but the description is wrong.
14. **"Generated videos are stored locally in the static directory."** Videos are
    uploaded to Azure Blob Storage with metadata in Cosmos. `VIDEO_DIR` is created
    at startup and used only as a temp/scratch path; `backend/static/` contains
    just `images/mask.png` and `images/no-smile.png`.
15. **Frontend page list.** The doc lists "Dashboard, Video Editor, Video UI,
    Gallery, Settings". The actual routes are in section 6; there is no Dashboard
    and no Video Editor.
16. **Product naming is inconsistent across the codebase.** The doc says "AI
    Content Lab"; `PROJECT_NAME` is `"Visionary Lab API"`; the root endpoint
    returns "Welcome to AI Content Lab API"; `AZURE_COSMOS_DB_ID` defaults to
    `visionarylab`.

### Also stale: `backend/BACKEND.MD`

Not part of the request, but flagged since you will likely touch it: it describes
`gpt_image.py` as "OpenAI GPT-Image-1" and `sora.py` as "OpenAI Sora", omits the
metadata router and Cosmos entirely, and its directory tree does not match
section 1.

---

## 8. Deletion-impact summary

Files that are exclusively video and can be deleted outright: `core/sora.py`,
`api/endpoints/videos.py`, `models/videos.py`, and the `VideoExtractor` /
`VideoAnalyzer` classes in `core/analyze.py`.

Files exclusively image: `core/gpt_image.py`, `core/image_pipeline.py`,
`api/endpoints/images.py`, `models/images.py`, `ImageAnalyzer` in
`core/analyze.py`, `backend/static/images/`.

Files that must be **edited rather than deleted** for either removal:

- `backend/main.py` — router registration, `makedirs` calls.
- `backend/core/__init__.py` — imports and singleton construction at module scope.
- `backend/core/config.py` — the deployment/container/dir settings.
- `backend/core/instructions.py` — mixed video/image/shared prompt templates.
- `backend/api/endpoints/env.py` — `SORA_DEPLOYMENT` and `IMAGEGEN_DEPLOYMENT`
  are hardcoded in the required list, so removal without editing this will make
  `/env/status` permanently report a missing required var.
- `backend/api/endpoints/gallery.py` — health check imports the singletons;
  container selection branches on media type.
- `backend/models/gallery.py` — `MediaType` enum.
- `backend/api/endpoints/metadata_router.py` — branches on image vs. video
  containers.
- `tests/conftest.py`, `tests/integration/*` — fixtures set `SORA_DEPLOYMENT` and
  build both clients.
- `infra/` — `SORA_DEPLOYMENT`, `IMAGEGEN_*`, and `FLUX_KONTEXT_DEPLOYMENT` flow
  through `main.bicep`, `main.parameters.json`, and `modules/containerApp.bicep`.
- Frontend — with both paths removed, `/`, `/new-image*`, `/new-video`,
  `/edit-image`, `/analyze`, `/gallery`, and `/jobs` all disappear, leaving only
  `/settings`, `/login`, and auth. `app/page.tsx` re-exports the image page, so
  the site root breaks first.

---

## Step 3 — infra to-do

Video generation was removed from the application in step 2b, but `infra/` and
`azure.yaml` were deliberately left untouched. The following items are
outstanding and must be handled in a dedicated infra pass.

### 1. `SORA_DEPLOYMENT` is now a dead env var

It is still declared and plumbed through the whole infra chain:

- `infra/main.bicep` — `param SORA_DEPLOYMENT` and the `deploySoraModel` /
  `soraModelName` / `soraModelVersion` parameters
- `infra/main.parameters.json` — `"SORA_DEPLOYMENT": { "value": "${SORA_DEPLOYMENT}" }`
- `infra/modules/containerApp.bicep` — `param SORA_DEPLOYMENT string = 'sora'`,
  injected into the backend container as a runtime environment variable

Nothing in the backend reads it any more: `Settings.SORA_DEPLOYMENT` was deleted
from `backend/core/config.py`, and `backend/api/endpoints/env.py` no longer lists
it as required. The variable is therefore set on the running container and
silently ignored.

This is harmless at runtime — `Settings` sets `extra = Extra.allow`, so the
unknown value is absorbed rather than raising — but it is misleading, and
`azd env set SORA_DEPLOYMENT` still appears in `DEPLOYMENT.md`. Remove the
parameter from all three Bicep files and from the azd environment.

### 2. Pending model deployment swap

The AI Foundry deployments provisioned by `infra/main.bicep` no longer match what
the application uses.

**Drop:**

| Deployment | Reason |
| --- | --- |
| `sora-2` | Video generation removed in step 2b. Gated behind `deploySoraModel` (currently `false`), so it may never have been provisioned — confirm before deleting the parameters. |
| `gpt-image-1.5` | Image generation is removed in the next step. |
| `gpt-image-1-mini` | Same. |
| `flux-kontext-pro` | Same. |

**Keep:**

| Deployment | Reason |
| --- | --- |
| `gpt-4o` | Backing model for `LLM_DEPLOYMENT`, used by every surviving analysis and prompt call site. Untouched by this migration. |

**Add:** a cheaper deployment for bulk extraction. Not yet selected; size it for
high-volume document throughput rather than latency.

> Resolved in step 3.1: the four image/video deployment modules and their
> parameters were removed from `infra/main.bicep`, `infra/main.parameters.json`,
> and `infra/modules/containerApp.bicep`.

#### 2a. `LLM_DEPLOYMENT` is `gpt-5`, not `gpt-4o` — docs are wrong

`infra/main.bicep` deploys **`gpt-5` (version `2025-08-07`)**: `llmModelType`,
`llmModelVersion`, and the `LLM_DEPLOYMENT` default all say `gpt-5`. This is
intentional — the inline comment records that gpt-4o `2024-08-06` / `2024-11-20`
are in deprecating state and cannot be used for new deployments in `eastus2`.

`README.md` and `.env.example` still say `gpt-4o`. Those two files should be
corrected to `gpt-5` in a follow-up documentation pass. No code or infra change
is needed — the infra is correct and the docs are stale.

#### 2b. Bulk extraction deployment — HARD GATE before `extract.py`

`infra/main.bicep` carries a commented-out `bulkExtractionDeployment` module and
three commented `bulkExtraction*` parameters. **This is not a someday item.** The
model name, version, and SKU must be confirmed against what is actually
available in the Foundry resource's region *before* step 3.2's `extract.py` work
begins, because the extraction code's batching, token budgeting, and cost model
all depend on which model is chosen:

```bash
az cognitiveservices model list -l <aiFoundryLocation> -o table
```

Uncomment the parameters and the module, fill in the confirmed values, and
redeploy before writing `extract.py`.

### 3. Public network access on the new AI resources

Step 3.1 provisions Document Intelligence and AI Search with
`publicNetworkAccess: 'Enabled'`, matching the existing AI Foundry account.
Cosmos DB and Blob Storage in the same template sit behind **private endpoints**
with private DNS zones, so the data plane is split: documents and metadata are
private, but the AI services that read them are reachable from the internet.

Both new services have `disableLocalAuth: true`, so access still requires an
Entra token — this is a network-exposure gap, not an auth gap. A follow-up pass
should add private endpoints plus `privatelink.cognitiveservices.azure.com` and
`privatelink.search.windows.net` DNS zones once the pipeline is proven
end-to-end. Deliberately deferred to keep step 3.1 reviewable.

### 4. Parked on a working `uv sync`

Two changes are blocked by the PyPI network restriction on the current machine
and must land together once `uv sync` can run:

- **`opencv-python` removal.** Dropped from `pyproject.toml` in step 2b when
  `VideoExtractor` was deleted, but `uv.lock` still pins it.
- **`pyproject.toml` `name` rename** (`video-gen` → `docintel`). Deliberately
  skipped in step 2d: `[project].name` is recorded in `uv.lock`, so renaming it
  without regenerating the lock would leave the two out of sync. Only
  `description` was updated.

### 5. Web search backends — known blockers

`backend/core/websearch.py` defines the provider-agnostic interface
(`SearchResult`, `WebSearchClient`, `get_websearch_client()`), with two backends:
`websearch_bing.py` (Grounding with Bing Search) and `websearch_webiq.py`
(stub, pending Core & Main access approval). The Bing backend is written against
the real API shape but **cannot run yet**. Five separate blockers, each
independent of the others:

**1. Grounding with Bing does not return raw content to developers.** Per
[Microsoft Learn](https://learn.microsoft.com/en-us/azure/ai-foundry/agents/how-to/tools/bing-grounding),
the tool "does NOT return the tool output to developers and end users" and
"Developers and end users don't have access to raw content returned from
Grounding with Bing Search." What is exposed is the model's answer plus URL
citation annotations. So `SearchResult.url` is populated, `title` is best-effort
from the annotation, and `snippet` is **always empty** for this backend. WebIQ,
if approved, would populate all three — meaning result quality is not comparable
across backends. Bing's use-and-display terms additionally require showing both
the website URLs and the Bing query URL to end users; nothing in the app does
this yet.

**2. ~~`gpt-5` is not supported~~ — RESOLVED for the Web Search tool.** The
exclusion is real but applies to **classic Grounding with Bing Search**, whose
docs state it works with all Agent Service models "except `gpt-4o-mini,
2024-07-18` and gpt-5 models." It does **not** apply to the Web Search tool,
which is what `websearch_bing.py` targets (see 4 below).

Confirmed by live test in the Microsoft Foundry portal: project `gpt-test`,
agent `Test`, model **`gpt-5`**, Web Search tool attached. Query "what is the
current weather in Denver?" returned a correctly cited answer sourced from the
National Weather Service in ~21s / 21,790 tokens, with no error. This settles
the earlier ambiguity — the docs samples only ever showed `gpt-5-mini`, leaving
full `gpt-5` unverified.

Consequence: **no second, non-gpt-5 deployment is needed.** `LLM_DEPLOYMENT`
(`gpt-5`, see 2a above) is usable as-is for web search, and this no longer
constrains the bulk-extraction deployment decision in 2b.

**3. The SDK is not installed and cannot be installed right now.** Grounding with
Bing requires `azure-ai-projects` / `azure-ai-agents`, not the `AzureOpenAI`
client in `core/llm.py`. Neither package is in `pyproject.toml` nor in the
environment, and `uv sync` is still network-blocked (see 4 above). The backend
therefore imports the SDK **lazily inside `search()`** and raises
`WebSearchError` with remediation text rather than an `ImportError` at module
scope, so application import stays clean. Add both packages when `uv sync` works.

**4. Infra requirement — lifted, by switching to the Web Search tool.** Two
distinct tools exist. **Classic Grounding with Bing Search** (GA, retiring
2027-03-31) requires a `Microsoft.Bing/accounts` resource you create and manage,
Contributor/Owner to create it, and **Foundry Project Manager** to create the
project connection. The newer **Web Search tool** (GA, recommended) does not:
per Microsoft Learn its Grounding-with-Bing resource is *"Managed by Microsoft"*,
and *"Web Search requires no extra roles beyond your Foundry project access."*
Attaching `WebSearchTool` directly to a prompt agent *"doesn't require a toolbox
or a separate Bing project connection."*

`websearch_bing.py` therefore targets the **Web Search tool** path
(`azure-ai-projects` → `AIProjectClient` → `PromptAgentDefinition(tools=[WebSearchTool()])`
→ Responses API), not classic Grounding with Bing. **No new Azure resource, no
project connection, and no extra RBAC are required**, so this blocker is
resolved. `BING_CONNECTION_ID` is retained in config but is no longer required
by the client; it is only relevant if domain-restricted Bing Custom Search is
adopted later, which *does* reintroduce a Bing resource, an instance, and a
project connection.

Two caveats that survive the switch:

- **Domain restriction is still not free.** General Web Search has no
  server-side domain filter, so `allowed_domains` is applied client-side (see
  the TODO in `websearch_bing.py`). Server-side restriction requires the Bing
  Custom Search path and its infra.
- **~~Endpoint form is unverified~~ — RESOLVED.** The Web Search tool requires a
  *project* endpoint
  (`https://<resource>.services.ai.azure.com/api/projects/<project>`), which is a
  different value from the account endpoint
  (`https://<name>.cognitiveservices.azure.com/`). Both are now carried as
  separate settings: `AI_FOUNDRY_ENDPOINT` (account, used by `core/llm.py`) and
  the new `AI_FOUNDRY_PROJECT_ENDPOINT` (project, used by
  `FoundryWebSearchClient`). The project endpoint must be populated per
  environment; the client raises `NotConfiguredError` naming it when blank.

**Data boundary — applies to both variants, and is a governance item.** The
Microsoft [Data Protection Addendum](https://aka.ms/dpa) does **not** apply to
data sent to Grounding with Bing Search or Grounding with Bing Custom Search, and
queries flow *outside the Azure compliance and geographic boundary*. Web Search
is built on Grounding with Bing, so this exclusion applies **identically** to
both tool variants — choosing the newer tool does not change it. Microsoft also
notes this waives elevated Government Community Cloud commitments, including
data sovereignty, where applicable. This is a **Core & Main governance
conversation, independent of which Bing tool is used**, and independent of the
WebIQ approval track. Resolve it before any real customer or vendor data is put
into a web search query.

**5. Networking — confirmed VNet-integrated, but not necessarily fatal.** The
docs state Grounding with Bing "only works with agents that are not using VPN or
Private Endpoints. The agent must have normal network access."
`infra/modules/containerAppEnv.bicep` sets `vnetConfiguration.infrastructureSubnetId`
from `vnetMod.outputs.containerAppsSubnetId`, so the Container Apps environment
**is** VNet-integrated — but with `internal: false`, i.e. external ingress and
default (unrestricted) outbound internet access. The restriction in the docs
targets the *Foundry agent's* network configuration rather than the calling
container, and the Foundry account is currently `publicNetworkAccess: 'Enabled'`
with no private endpoint (see 3 above). Best reading: this is **not** a blocker
today, but it becomes one the moment the follow-up pass puts Foundry behind a
private endpoint. Verify against a live call before relying on it, and treat it
as a constraint on that private-endpoint work.

Consequence: `WEBSEARCH_PROVIDER` defaults to `"bing"`, so calling
`get_websearch_client().search(...)` today raises `WebSearchError` (the
`azure-ai-projects` SDK is not installed) or `NotConfiguredError` (no
`AI_FOUNDRY_PROJECT_ENDPOINT` / `LLM_DEPLOYMENT`). Both are explicit; neither
fails silently.

Sequencing note: do not remove the image deployments until the image generation
step lands, since `IMAGEGEN_DEPLOYMENT` and `FLUX_KONTEXT_DEPLOYMENT` are still
read by `backend/core/gpt_image.py` and still listed in
`backend/api/endpoints/env.py`.

> Update after step 2c: `gpt_image.py` and the image deployment settings are
> gone. The image deployments are now safe to drop from `infra/`.

---

## LLM client usage

`backend/core/llm.py` wraps the `gpt-4o` Foundry deployment. Callers describe the
output they want with a Pydantic model; the client asks the deployment for
JSON-schema-constrained output and returns a validated instance.

```python
from pydantic import BaseModel, Field

from backend.core import llm  # module-level LLMClient, or construct your own


class InvoiceFields(BaseModel):
    vendor: str = Field(description="Legal name of the issuing company")
    total_eur: float = Field(description="Invoice total in EUR")
    due_days: int = Field(description="Payment window in days")


result = llm.complete_structured(
    "Extract the invoice fields from the user's text.",
    "Invoice 4471 from Contoso Ltd for 12,500 EUR, payable within 30 days.",
    InvoiceFields,
)

result.vendor      # 'Contoso Ltd'  -> str, already validated
result.total_eur   # 12500.0        -> float, not the string the model emitted
```

`await llm.acomplete_structured(...)` is the async equivalent and takes the same
arguments.

The model's JSON is parsed and passed through `InvoiceFields.model_validate()`.
If it does not conform, the call is retried (`max_retries=3`, 2s linear backoff);
once retries are exhausted the client raises a typed error rather than returning
a partial dict:

```python
from backend.core.llm import LLMSchemaValidationError

try:
    result = llm.complete_structured(system, user, InvoiceFields)
except LLMSchemaValidationError as exc:
    exc.schema        # <class 'InvoiceFields'> - the model that was requested
    exc.raw_content   # the raw string the deployment returned
    exc.errors        # the pydantic ValidationError (or JSON decode error)
```

`LLMSchemaValidationError` subclasses `LLMError`, so catching `LLMError` covers
every failure this client raises. Malformed JSON and schema mismatches both
surface as `LLMSchemaValidationError` — the distinguishing detail is in
`exc.errors`.

Notes:

- Schemas are sent in strict mode by default. `_strictify()` rewrites the
  generated JSON schema so every object sets `additionalProperties: false` and
  lists all properties as required, which Azure OpenAI requires for strict
  structured outputs. Pass `strict=False` for schemas that can't satisfy that.
- Extra keyword arguments (`temperature`, `max_tokens`, ...) are forwarded to the
  underlying chat completions call.

### Caveats

Two ways this fails silently with a `None` instead of a clear error:

1. **The module-level client can be `None`.** `from backend.core import llm`
   returns `None` when construction fails at import time (e.g. unset
   `AI_FOUNDRY_ENDPOINT`) — the same fallback the pre-existing `llm_client` has.
   Calling into it raises `AttributeError: 'NoneType' object has no attribute
   'complete_structured'`, which reads like a code bug but is a config problem.
   Real callers should construct their own `LLMClient()` or guard for `None`.
2. **`env_file` does not resolve locally.** `Settings.Config.env_file` is
   `"../.env"`, which points at the parent of the repo root rather than the
   actual `docintel/.env`. Locally, `AI_FOUNDRY_ENDPOINT` and the storage
   settings quietly fall back to `None`. Deployed environments are unaffected —
   Azure Container Apps injects env vars directly, so the file path is
   irrelevant there. This will matter in Step 3, when extraction first runs
   against real Foundry deployments from a dev machine.

Both surface as a silent `None` rather than a loud failure, so a failed local
extraction run is more likely a config problem than a model or code problem.



