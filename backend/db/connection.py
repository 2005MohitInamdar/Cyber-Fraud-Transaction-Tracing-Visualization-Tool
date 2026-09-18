"""
db/connection.py
────────────────
Provides:
  1. A module-level MySQL connection pool  → get_connection()
  2. A module-level Redis client           → get_redis()

All credentials are read from environment variables (loaded via python-dotenv).

Usage
-----
    from db.connection import get_connection, get_redis

    # MySQL
    with get_connection() as conn:
        with conn.cursor(dictionary=True) as cur:
            cur.execute("SELECT 1")

    # Redis
    r = get_redis()
    r.set("key", "value")
"""

import os
from contextlib import contextmanager

import mysql.connector
from mysql.connector import pooling
import redis
from dotenv import load_dotenv

# Load .env from the backend root (works whether you start uvicorn from
# `backend/` or from the repo root with `backend/.env`).
load_dotenv()


# ── MySQL Pool ────────────────────────────────────────────────────────────────
_pool: pooling.MySQLConnectionPool | None = None


def _build_pool() -> pooling.MySQLConnectionPool:
    return pooling.MySQLConnectionPool(
        pool_name="gam_pool",
        pool_size=5,                          # tune to your load
        pool_reset_session=True,
        host=os.environ["DB_HOST"],
        port=int(os.environ.get("DB_PORT", 3306)),
        database=os.environ["DB_NAME"], 
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        charset="utf8mb4",
        collation="utf8mb4_unicode_ci",
        autocommit=False,                     # explicit commit per operation
        connection_timeout=10,
    )


def _get_pool() -> pooling.MySQLConnectionPool:
    """Lazy-init the MySQL pool once per process."""
    global _pool
    if _pool is None:
        _pool = _build_pool()
    return _pool


@contextmanager
def get_connection():
    """
    Context manager that yields a pooled MySQL connection and guarantees
    rollback on error + return to pool on exit.

    Example
    -------
        with get_connection() as conn:
            with conn.cursor(dictionary=True) as cur:
                cur.execute("SELECT 1")
    """
    conn = _get_pool().get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()   # returns the connection to the pool


# ── Redis Client ──────────────────────────────────────────────────────────────
_redis_client: redis.Redis | None = None


def _build_redis() -> redis.Redis:
    """
    Builds a Redis client from REDIS_URL.
    decode_responses=True means all values come back as str, not bytes.
    """
    url = os.environ["REDIS_URL"]
    client = redis.Redis.from_url(url, decode_responses=True)
    # Ping immediately to catch bad credentials / network issues at startup
    client.ping()
    return client


def get_redis() -> redis.Redis:
    """
    Returns the module-level Redis client, initialising it on first call.

    Raises
    ------
    RuntimeError  – if REDIS_URL is missing or the server is unreachable.

    Example
    -------
        r = get_redis()
        r.set("hello", "world")
        val = r.get("hello")   # "world"
    """
    global _redis_client
    if _redis_client is None:
        try:
            _redis_client = _build_redis()
            print("  ✔  Redis connected successfully")
        except redis.exceptions.AuthenticationError as e:
            raise RuntimeError(f"Redis authentication failed: {e}") from e
        except redis.exceptions.ConnectionError as e:
            raise RuntimeError(f"Redis connection error: {e}") from e
        except KeyError:
            raise RuntimeError("REDIS_URL environment variable is not set") from None
    return _redis_client