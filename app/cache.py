import hashlib
from datetime import datetime, timedelta, timezone
from app.config import Config
from app.embeddings import embed
from app.pool import get_pool


async def cache_migrate(config: Config) -> None:
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS semantic_cache (
                id         TEXT PRIMARY KEY,
                query      TEXT NOT NULL,
                result     TEXT NOT NULL,
                embedding  vector(384) NOT NULL,
                expires_at TIMESTAMPTZ NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        await conn.execute(f"""
            CREATE INDEX IF NOT EXISTS semantic_cache_embedding_idx
            ON semantic_cache USING ivfflat (embedding vector_cosine_ops)
            WITH (lists = {config.ivfflat_lists})
        """)


def _cache_key(query: str) -> str:
    # Stable across processes/replicas — Python's builtin hash() is salted per-process
    # and would produce a different key for the same query on every restart.
    return hashlib.sha256(query.encode()).hexdigest()[:16]


async def cache_get(config: Config, query: str) -> str | None:
    query_emb = await embed(query)  # off the event loop — was blocking async callers before
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT result FROM semantic_cache
            WHERE expires_at > NOW()
              AND 1 - (embedding <=> $1::vector) >= $2
            ORDER BY embedding <=> $1::vector
            LIMIT 1
            """,
            str(query_emb), config.cache_similarity_threshold,
        )
        return row["result"] if row else None


async def cache_set(config: Config, query: str, result: str) -> None:
    query_emb = await embed(query)
    key = _cache_key(query)
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=config.cache_ttl)
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO semantic_cache (id, query, result, embedding, expires_at, created_at)
            VALUES ($1, $2, $3, $4::vector, $5, NOW())
            ON CONFLICT (id) DO UPDATE SET
                result = EXCLUDED.result,
                embedding = EXCLUDED.embedding,
                expires_at = EXCLUDED.expires_at,
                created_at = NOW()
            """,
            key, query, result, str(query_emb), expires_at,
        )


async def cache_count(config: Config) -> int:
    pool = get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval("SELECT COUNT(*) FROM semantic_cache WHERE expires_at > NOW()")
