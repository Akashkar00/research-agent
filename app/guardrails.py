import asyncio
import boto3
from app.config import Config
from app.retry import with_retry

_client = None


def _get_client(config: Config):
    global _client
    if _client is None:
        _client = boto3.client("bedrock-runtime", region_name=config.aws_region)
    return _client


def _apply_guardrail_sync(config: Config, text: str, source: str) -> dict:
    client = _get_client(config)
    return client.apply_guardrail(
        guardrailIdentifier=config.bedrock_guardrail_id,
        guardrailVersion=config.bedrock_guardrail_version,
        source=source,
        content=[{"text": {"text": text}}],
    )


async def validate_input(config: Config, text: str) -> tuple[bool, str]:
    response = await with_retry(
        lambda: asyncio.to_thread(_apply_guardrail_sync, config, text, "INPUT"),
        max_retries=config.llm_max_retries,
        delay=config.llm_retry_delay,
    )
    if response.get("action") == "GUARDRAIL_INTERVENED":
        return False, "Input blocked by safety guardrail."
    return True, ""


async def validate_output(config: Config, text: str) -> tuple[bool, str]:
    response = await with_retry(
        lambda: asyncio.to_thread(_apply_guardrail_sync, config, text, "OUTPUT"),
        max_retries=config.llm_max_retries,
        delay=config.llm_retry_delay,
    )
    if response.get("action") == "GUARDRAIL_INTERVENED":
        return False, "Output blocked by safety guardrail."
    return True, ""
