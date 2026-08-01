import httpx
from app.config import Config
from app.retry import with_retry


async def web_search(config: Config, query: str, max_results: int = 6) -> list[dict]:
    """
    Returns [{"title", "url", "content", "published_date"}].
    This is the ONLY source of facts in the pipeline. If it returns nothing,
    the job fails loudly rather than silently falling back to model recall.
    """
    async def _once():
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                "https://api.tavily.com/search",
                json={
                    "api_key": config.tavily_api_key,
                    "query": query,
                    "search_depth": "advanced",
                    "max_results": max_results,
                    "include_raw_content": False,
                },
            )
            r.raise_for_status()
            return r.json()["results"]

    results = await with_retry(_once, max_retries=config.llm_max_retries, delay=config.llm_retry_delay)
    if not results:
        raise RuntimeError(f"No search results for query: {query}")
    return results
