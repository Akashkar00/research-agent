import asyncio
from sentence_transformers import SentenceTransformer

_model: SentenceTransformer | None = None


def _get_model() -> SentenceTransformer:
    global _model
    if _model is None:
        _model = SentenceTransformer("all-MiniLM-L6-v2")
    return _model


async def embed(text: str) -> list[float]:
    """Lazily loads the shared embedding model on first use and runs it off the event loop."""
    return await asyncio.to_thread(lambda: _get_model().encode(text).tolist())
