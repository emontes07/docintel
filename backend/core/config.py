from pydantic_settings import BaseSettings
from typing import Optional
from pydantic import Extra, Field, validator


class Settings(BaseSettings):
    # API Settings
    API_V1_STR: str = "/api/v1"
    PROJECT_NAME: str = "DocIntel API"

    # AI Foundry endpoint (unified for all AI services via managed identity)
    AI_FOUNDRY_ENDPOINT: Optional[str] = None
    # Project-scoped endpoint, required by the Foundry Agents/Web Search surface.
    # Format: https://<resource>.services.ai.azure.com/api/projects/<project>
    AI_FOUNDRY_PROJECT_ENDPOINT: Optional[str] = None

    # Model deployment names
    LLM_DEPLOYMENT: Optional[str] = None

    # Azure Blob Storage Settings (managed identity — no keys)
    AZURE_BLOB_SERVICE_URL: Optional[str] = None
    AZURE_STORAGE_ACCOUNT_NAME: Optional[str] = None
    CDN_BLOB_URL: Optional[str] = None

    # Container names
    AZURE_BLOB_IMAGE_CONTAINER: str = "images"
    AZURE_BLOB_VIDEO_CONTAINER: str = "videos"

    # CORS Configuration
    CORS_ALLOWED_ORIGINS: str = Field(
        default="*",
        description="Comma-separated list of allowed CORS origins, or * for all origins"
    )

    # Azure Cosmos DB Settings (managed identity — no keys)
    AZURE_COSMOS_DB_ENDPOINT: Optional[str] = None
    AZURE_COSMOS_DB_ID: str = "visionarylab"
    AZURE_COSMOS_CONTAINER_ID: str = "metadata"

    # Azure OpenAI API Version
    AOAI_API_VERSION: str = "2025-04-01-preview"

    # Web search backend for the tier-3/tier-4 source cascade
    WEBSEARCH_PROVIDER: str = "bing"
    # Only needed for domain-restricted Bing Custom Search; the general Foundry
    # Web Search tool requires no Bing resource or project connection.
    BING_CONNECTION_ID: Optional[str] = None
    # WebIQ — pending Core & Main access approval
    WEBIQ_ENDPOINT: Optional[str] = None
    WEBIQ_API_KEY: Optional[str] = None

    # File storage paths
    UPLOAD_DIR: str = "./static/uploads"

    # Logging Configuration
    LOG_LEVEL: str = "INFO"

    @validator('CORS_ALLOWED_ORIGINS')
    def validate_cors_origins(cls, v):
        """Validate CORS origins configuration to prevent Azure InvalidXmlNodeValue errors"""
        if v == "*":
            return v
        
        origins = [origin.strip() for origin in v.split(",") if origin.strip()]
        
        if "*" in origins and len(origins) > 1:
            raise ValueError(
                "Cannot mix wildcard '*' with specific origins in CORS configuration. "
                "Use either '*' alone for all origins, or specify individual origins without '*'."
            )
        
        for origin in origins:
            if origin != "*" and not (origin.startswith("http://") or origin.startswith("https://")):
                raise ValueError(f"Invalid origin format: {origin}. Origins must start with http:// or https://")
        
        return v

    class Config:
        env_file = "../.env"
        case_sensitive = True
        extra = Extra.allow


settings = Settings()
