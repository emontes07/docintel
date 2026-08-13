# DocIntel

**Batch document attribute extraction on Azure AI Foundry — extract structured, validated fields from large document sets.**

## Key Features

### Structured Extraction
- Define the fields you want as a Pydantic model; the extraction client requests JSON-schema-constrained output from the model deployment and returns a validated instance
- Schema violations raise a typed error instead of silently returning partial data
- Built-in retry, timeout, and logging around every model call

### Asset & Metadata Management
- Documents are stored in Azure Blob Storage with folder support
- Extracted attributes are persisted to Azure Cosmos DB alongside each asset
- Query, search, folder statistics, and metadata sync endpoints over the stored results

## Architecture

DocIntel uses **Azure AI Foundry** as a single unified AI resource with all model deployments, and **managed identity** for all service connections (no API keys).

| Component | Service | Auth |
|-----------|---------|------|
| AI Models | Azure AI Foundry (AIServices) | Managed Identity |
| Document Storage | Azure Blob Storage | Managed Identity |
| Metadata | Azure Cosmos DB | Managed Identity |
| Hosting | Azure Container Apps | SystemAssigned MI |

### Supported Model Deployments

| Deployment | Model | Purpose |
|-----------|-------|---------|
| `gpt-4o` | GPT-4o | LLM for structured attribute extraction and analysis |

## Prerequisites

Azure resources:

- Azure AI Foundry resource with deployed models (see table above)
- Azure Storage Account with a Blob Container for documents
- Azure Cosmos DB account

Compute environment:

- Python 3.12+
- Node.js 19+ and npm
- Git
- uv package manager
- Azure CLI (`az login` required for local development)

## Step 1: Installation (One-time)

### Option A: Quick Start with GitHub Codespaces

[![Open in GitHub Codespaces](https://github.com/codespaces/badge.svg)](https://github.com/codespaces/new?hide_repo_select=true&ref=main&repo=Azure-Samples/visionary-lab)

Wait for the Codespace to initialize, then continue with [Step 2: Configure Resources](#step-2-configure-resources).

### Option B: Local Installation

#### 1. Clone the Repository

```bash
git clone https://github.com/Azure-Samples/visionary-lab
```

#### 2. Backend Setup

##### 2.1 Install UV Package Manager

Mac/Linux:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Windows (PowerShell):

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

##### 2.2 Copy environment file template

```bash
cp .env.example .env
```

#### 3. Frontend Setup

```bash
cd frontend
npm install --legacy-peer-deps
```

## Step 2: Configure Resources

1. **Login to Azure** (required for managed identity authentication):

   ```bash
   az login
   ```

2. **Configure environment variables** in `.env`:

   ```bash
   code .env
   ```

   | Setting | Description |
   |---------|-------------|
   | `AI_FOUNDRY_ENDPOINT` | Your AI Foundry endpoint (e.g., `https://your-foundry.cognitiveservices.azure.com/`) |
   | `LLM_DEPLOYMENT` | LLM deployment name (e.g., `gpt-4o`) |
   | `AZURE_BLOB_SERVICE_URL` | Blob Storage URL |
   | `AZURE_STORAGE_ACCOUNT_NAME` | Storage account name |
   | `AZURE_COSMOS_DB_ENDPOINT` | Cosmos DB endpoint URL |

   > **No API keys needed.** All services authenticate via `DefaultAzureCredential` which uses your `az login` session locally and managed identity in Azure.

## Step 3: Running the Application

1. Start the backend:

   ```bash
   cd backend
   uv run fastapi dev
   ```

   The backend server will start on http://localhost:8000.

2. Open a new terminal to start the frontend:

   ```bash
   cd frontend
   npm run build
   npm start
   ```

   The frontend will be available at http://localhost:3000.

## 🚀 Deploy to Azure

For production deployment, use Azure Developer CLI:

**Prerequisites**: [Azure Developer CLI (azd)](https://learn.microsoft.com/en-us/azure/developer/azure-developer-cli/install-azd)

```bash
git clone https://github.com/Azure-Samples/visionary-lab
cd visionary-lab

azd auth login
azd up
```

During `azd up`, you'll be prompted for:
- **AI Foundry name**: Globally unique name for your AI Foundry resource
- **Model deployment names**: Which models to deploy (gpt-4o)

✨ That's it! DocIntel will be running on Azure Container Apps with:
- Azure AI Foundry with all model deployments
- Managed identity for all service connections (no API keys)
- Azure Storage and Cosmos DB for document and metadata management
- RBAC role assignments auto-configured
- Optional Entra ID authentication (configurable per deployment)

📖 For detailed deployment instructions, see [DEPLOYMENT.md](DEPLOYMENT.md)
