import redis.asyncio as redis

from app.core.config import Settings


def create_redis_client(settings: Settings) -> redis.Redis:
    """Cliente para la instancia de Redis existente. No abre conexión hasta el primer uso."""
    return redis.Redis(
        host=settings.redis_host,
        port=settings.redis_port,
        password=settings.redis_password,
        db=settings.redis_db,
        ssl=settings.redis_ssl,
        socket_connect_timeout=3,
        socket_timeout=3,
        decode_responses=True,
    )
