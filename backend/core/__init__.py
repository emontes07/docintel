import logging
from datetime import datetime, timedelta, timezone

from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from azure.storage.blob import BlobServiceClient, generate_container_sas, ContainerSasPermissions

from .config import settings
from .llm import LLMClient

logger = logging.getLogger(__name__)

# Shared credential for all Azure services
credential = DefaultAzureCredential()
token_provider = get_bearer_token_provider(
    credential, "https://cognitiveservices.azure.com/.default"
)

# Foundry chat client; `llm_client` / `async_llm_client` remain available as the
# raw SDK handles for callers that predate LLMClient.
try:
    llm = LLMClient(token_provider=token_provider)
    llm_client = llm.sync_client
    async_llm_client = llm.async_client
except Exception as e:
    logger.error(f"Failed to initialize LLM client: {str(e)}")
    llm = None
    llm_client = None
    async_llm_client = None


def _get_blob_service_client() -> BlobServiceClient:
    """Get a BlobServiceClient using managed identity."""
    account_url = settings.AZURE_BLOB_SERVICE_URL
    if not account_url and settings.AZURE_STORAGE_ACCOUNT_NAME:
        account_url = f"https://{settings.AZURE_STORAGE_ACCOUNT_NAME}.blob.core.windows.net/"
    return BlobServiceClient(account_url=account_url, credential=credential)


def _generate_sas(container_name: str) -> str | None:
    """Generate a 4-hour read/list SAS token using User Delegation Key."""
    try:
        blob_client = _get_blob_service_client()
        start_time = datetime.now(timezone.utc)
        expiry_time = start_time + timedelta(hours=4)

        user_delegation_key = blob_client.get_user_delegation_key(
            key_start_time=start_time,
            key_expiry_time=expiry_time,
        )

        token = generate_container_sas(
            account_name=settings.AZURE_STORAGE_ACCOUNT_NAME,
            container_name=container_name,
            user_delegation_key=user_delegation_key,
            permission=ContainerSasPermissions(read=True, list=True),
            expiry=expiry_time,
            start=start_time,
        )
        logger.info(f"Generated User Delegation SAS token for {container_name} container.")
        return token
    except Exception as e:
        logger.error(f"Failed to generate SAS token for {container_name}: {e}")
        return None


image_sas_token = _generate_sas(settings.AZURE_BLOB_IMAGE_CONTAINER)
