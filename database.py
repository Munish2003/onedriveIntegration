"""
Database Layer — PostgreSQL Pool + AES-256-GCM Encryption
Provides async database access and transparent encryption for sensitive fields.
"""

import os
import io
import base64
import asyncio
import logging
from typing import Optional, List

import asyncpg
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger("Database")


# ==============================================================================
# CONFIGURATION
# ==============================================================================
class DBConfig:
    POSTGRES_USER = os.getenv("POSTGRES_USER")
    POSTGRES_PASSWORD = os.getenv("POSTGRES_PASSWORD")
    POSTGRES_HOST = os.getenv("POSTGRES_HOST")
    POSTGRES_PORT = os.getenv("POSTGRES_PORT", "5432")
    POSTGRES_DATABASE = os.getenv("POSTGRES_DATABASE")
    MASTER_KEY = os.getenv("MASTER_KEY")

db_config = DBConfig()


# ==============================================================================
# ENCRYPTION
# ==============================================================================
class DatabaseEncryptor:
    def __init__(self, key_hex: str):
        if not key_hex:
            raise ValueError("MASTER_KEY not found in environment variables")
        self.key = bytes.fromhex(key_hex)
        self.aesgcm = AESGCM(self.key)

    def encrypt(self, plaintext: str) -> str:
        """Encrypts a string → Base64 encoded blob (IV + Ciphertext + Tag)."""
        if not plaintext:
            return ""
        nonce = os.urandom(12)
        ciphertext = self.aesgcm.encrypt(nonce, plaintext.encode('utf-8'), None)
        return base64.b64encode(nonce + ciphertext).decode('utf-8')

    def decrypt(self, encrypted_blob: str) -> str:
        """Decrypts a Base64 blob → original plaintext."""
        if not encrypted_blob:
            return ""
        try:
            data = base64.b64decode(encrypted_blob.encode('utf-8'))
            nonce = data[:12]
            ciphertext = data[12:]
            plaintext = self.aesgcm.decrypt(nonce, ciphertext, None)
            return plaintext.decode('utf-8')
        except Exception as e:
            logger.error(f"Decryption failed: {e}")
            return "[DECRYPTION_FAILED]"

# Global encryptor
encryptor = DatabaseEncryptor(db_config.MASTER_KEY) if db_config.MASTER_KEY else None


# ==============================================================================
# POSTGRES CLIENT (Same pattern as reference project)
# ==============================================================================
class PostgresClient:
    """PostgreSQL connection pool with lazy initialization."""

    def __init__(self):
        self._pool: Optional[asyncpg.Pool] = None
        self._lock = asyncio.Lock()

    async def _get_pool(self) -> asyncpg.Pool:
        if self._pool:
            return self._pool
        async with self._lock:
            if self._pool is None:
                logger.info("Initializing PostgreSQL connection pool...")
                self._pool = await asyncpg.create_pool(
                    user=db_config.POSTGRES_USER,
                    password=db_config.POSTGRES_PASSWORD,
                    host=db_config.POSTGRES_HOST,
                    port=db_config.POSTGRES_PORT,
                    database=db_config.POSTGRES_DATABASE,
                    min_size=1, max_size=5,
                    command_timeout=60,
                    server_settings={
                        "application_name": "SalezXOneDrive",
                        "tcp_keepalives_idle": "60",
                        "tcp_keepalives_interval": "30",
                        "tcp_keepalives_count": "3",
                    }
                )
                logger.info("✓ PostgreSQL pool initialized")
            return self._pool

    async def _execute(self, method: str, query: str, *args):
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            return await getattr(conn, method)(query, *args)

    async def execute(self, query: str, *args):
        return await self._execute("execute", query, *args)

    async def executemany(self, query: str, args):
        return await self._execute("executemany", query, args)

    async def fetch(self, query: str, *args):
        return await self._execute("fetch", query, *args)

    async def fetchrow(self, query: str, *args):
        return await self._execute("fetchrow", query, *args)

    async def fetchval(self, query: str, *args):
        return await self._execute("fetchval", query, *args)

    async def get_pool(self) -> asyncpg.Pool:
        """Public access for transaction support."""
        return await self._get_pool()

    async def close(self):
        if self._pool:
            await self._pool.close()
            self._pool = None
            logger.info("✓ PostgreSQL pool closed")


# Global database client
db = PostgresClient()
