import logging
from datetime import datetime, timedelta, timezone

from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from azure.storage.blob import BlobServiceClient, generate_container_sas, ContainerSasPermissions

from .config import settings
from .llm import LLMClient

logger = logging.getLogger(__name__)

def _get_credential():
    return __getattr__("credential") if "credential" not in globals() else globals()["credential"]


def _get_blob_service_client() -> BlobServiceClient:
    """Get a BlobServiceClient using managed identity."""
    account_url = settings.AZURE_BLOB_SERVICE_URL
    if not account_url and settings.AZURE_STORAGE_ACCOUNT_NAME:
        account_url = f"https://{settings.AZURE_STORAGE_ACCOUNT_NAME}.blob.core.windows.net/"
    return BlobServiceClient(account_url=account_url, credential=_get_credential())


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


def __getattr__(name: str):
    if name == "credential":
        globals()[name] = DefaultAzureCredential()
        return globals()[name]
    if name == "token_provider":
        globals()[name] = get_bearer_token_provider(
            _get_credential(), "https://cognitiveservices.azure.com/.default"
        )
        return globals()[name]
    if name in ("llm", "llm_client", "async_llm_client"):
        try:
            provider = globals().get("token_provider") or __getattr__("token_provider")
            client = LLMClient(token_provider=provider)
            globals().update(llm=client, llm_client=client.sync_client, async_llm_client=client.async_client)
        except Exception:
            logger.error("Failed to initialize LLM client")
            globals().update(llm=None, llm_client=None, async_llm_client=None)
        return globals()[name]
    if name == "image_sas_token":
        token = _generate_sas(settings.AZURE_BLOB_IMAGE_CONTAINER)
        globals()[name] = token
        return token
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
