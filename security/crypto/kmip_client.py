"""
KUVA AI KMIP 2.0 Enterprise Client (NIST SP 800-57 Compliant)
"""
import asyncio
import logging
import ssl
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from cryptography.hazmat.primitives import serialization
from kmip.core import enums, exceptions, objects
from kmip.pie.client import ProxyKmipClient
from opentelemetry import metrics, trace
from pydantic import BaseModel, Field, ValidationError
from pydantic_core import Url

# ----- Observability Setup -----
tracer = trace.get_tracer("kmip.tracer")
meter = metrics.get_meter("kmip.meter")
kmip_ops_counter = meter.create_counter(
    "kmip.operations",
    description="Total KMIP cryptographic operations"
)

# ----- Data Models -----
class KMIPConfig(BaseModel):
    endpoint: Url
    port: int = Field(5696, ge=1, le=65535)
    cert_chain: str
    private_key: str
    ca_cert: str
    timeout: float = 5.0
    max_connections: int = 10
    retry_policy: Dict[str, Any] = {
        "max_attempts": 3,
        "backoff_base": 0.5
    }

class CryptographicMaterial(BaseModel):
    data: bytes
    algorithm: str
    usage_mask: List[str]
    metadata: Dict[str, str] = {}

# ----- Core Implementation -----
class KMIPConnectionPool:
    """TLS connection pool with mutual authentication"""
    def __init__(self, config: KMIPConfig):
        self._pool = asyncio.Queue(maxsize=config.max_connections)
        self.config = config
        self.ssl_ctx = self._create_ssl_context()
        self.logger = logging.getLogger("kuva.kmip")

    def _create_ssl_context(self) -> ssl.SSLContext:
        """Configure mutual TLS authentication"""
        ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
        ctx.load_verify_locations(cadata=self.config.ca_cert)
        ctx.load_cert_chain(
            certfile=self.config.cert_chain,
            keyfile=self.config.private_key
        )
        ctx.verify_mode = ssl.CERT_REQUIRED
        ctx.check_hostname = True
        ctx.set_ciphers('ECDHE-ECDSA-AES256-GCM-SHA384')
        ctx.options |= ssl.OP_NO_TLSv1 | ssl.OP_NO_TLSv1_1
        return ctx

    async def init_pool(self):
        """Pre-warm connection pool"""
        for _ in range(self.config.max_connections):
            client = ProxyKmipClient(
                host=self.config.endpoint.host,
                port=self.config.port,
                cert=self.config.cert_chain,
                key=self.config.private_key,
                ca=self.config.ca_cert,
                ssl_version='TLSv1_2',
                kmip_version=enums.KMIPVersion.KMIP_2_0
            )
            await client.open()
            await self._pool.put(client)

    async def acquire(self) -> ProxyKmipClient:
        """Acquire connection with retry logic"""
        for attempt in range(self.config.retry_policy["max_attempts"]):
            try:
                return await self._pool.get()
            except asyncio.QueueEmpty:
                backoff = self.config.retry_policy["backoff_base"] ** attempt
                await asyncio.sleep(backoff)
        raise exceptions.KmipConnectionError("Connection pool exhausted")

    async def release(self, client: ProxyKmipClient):
        """Return connection to pool"""
        if client.is_connected():
            await self._pool.put(client)

class KMIPEnterpriseClient:
    def __init__(self, config: KMIPConfig):
        self.pool = KMIPConnectionPool(config)
        self.config = config
        self.logger = logging.getLogger("kuva.kmip")

    async def __aenter__(self):
        await self.pool.init_pool()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.close()

    async def close(self):
        """Cleanup all connections"""
        while not self.pool._pool.empty():
            client = await self.pool.acquire()
            await client.close()

    @tracer.start_as_current_span("kmip.generate_key")
    async def generate_symmetric_key(
        self,
        algorithm: enums.CryptographicAlgorithm,
        length: int,
        usage_mask: List[enums.CryptographicUsageMask],
        metadata: Dict[str, str] = None
    ) -> str:
        """Generate KMIP-managed symmetric key"""
        client = await self.pool.acquire()
        try:
            key_id = await client.create(
                enums.ObjectType.SYMMETRIC_KEY,
                algorithm=algorithm,
                length=length,
                cryptographic_usage_masks=usage_mask,
                operation_policy_name='kuva-policy'
            )
            if metadata:
                await self._add_metadata(key_id, metadata)
            kmip_ops_counter.add(1, {"operation": "generate_key"})
            return key_id
        except exceptions.KmipOperationFailure as e:
            self.logger.error(f"Key generation failed: {e}")
            raise KMIPOperationError("Symmetric key generation failed") from e
        finally:
            await self.pool.release(client)

    @tracer.start_as_current_span("kmip.register_key")
    async def register_existing_key(
        self,
        material: bytes,
        algorithm: enums.CryptographicAlgorithm,
        usage_mask: List[enums.CryptographicUsageMask]
    ) -> str:
        """Register external key material with KMIP"""
        client = await self.pool.acquire()
        try:
            key = objects.SymmetricKey(
                algorithm,
                len(material) * 8,
                material,
                usage_mask=usage_mask
            )
            key_id = await client.register(key)
            kmip_ops_counter.add(1, {"operation": "register_key"})
            return key_id
        except exceptions.KmipInvalidFieldValue as e:
            self.logger.error(f"Key registration invalid: {e}")
            raise KMIPValidationError("Invalid key material") from e
        finally:
            await self.pool.release(client)

    @tracer.start_as_current_span("kmip.encrypt")
    async def encrypt_data(
        self,
        key_id: str,
        plaintext: bytes,
        iv: Optional[bytes] = None
    ) -> bytes:
        """KMIP-managed encryption operation"""
        client = await self.pool.acquire()
        try:
            result = await client.encrypt(
                plaintext,
                uid=key_id,
                iv_counter_nonce=iv,
                cryptographic_parameters={
                    'cryptographic_algorithm': 
                        enums.CryptographicAlgorithm.AES,
                    'block_cipher_mode': enums.BlockCipherMode.GCM
                }
            )
            kmip_ops_counter.add(1, {"operation": "encrypt"})
            return result
        except exceptions.KmipPermissionDenied as e:
            self.logger.warning(f"Encryption permission denied: {e}")
            raise KMIPPermissionError("Encryption not allowed") from e
        finally:
            await self.pool.release(client)

    @tracer.start_as_current_span("kmip.decrypt")
    async def decrypt_data(
        self,
        key_id: str,
        ciphertext: bytes,
        iv: Optional[bytes] = None
    ) -> bytes:
        """KMIP-managed decryption operation"""
        client = await self.pool.acquire()
        try:
            result = await client.decrypt(
                ciphertext,
                uid=key_id,
                iv_counter_nonce=iv,
                cryptographic_parameters={
                    'cryptographic_algorithm': 
                        enums.CryptographicAlgorithm.AES,
                    'block_cipher_mode': enums.BlockCipherMode.GCM
                }
            )
            kmip_ops_counter.add(1, {"operation": "decrypt"})
            return result
        except exceptions.KmipInvalidMessage as e:
            self.logger.error(f"Decryption failed: {e}")
            raise KMIPOperationError("Decryption integrity failure") from e
        finally:
            await self.pool.release(client)

    @tracer.start_as_current_span("kmip.rotate")
    async def rotate_key(
        self,
        key_id: str,
        new_algorithm: Optional[enums.CryptographicAlgorithm] = None
    ) -> str:
        """Key rotation with optional algorithm migration"""
        client = await self.pool.acquire()
        try:
            new_key_id = await client.rekey(
                uid=key_id,
                cryptographic_algorithm=new_algorithm
            )
            kmip_ops_counter.add(1, {"operation": "key_rotate"})
            return new_key_id
        except exceptions.KmipOperationNotSupported as e:
            self.logger.error(f"Rotation failed: {e}")
            raise KMIPOperationError("Rotation not supported") from e
        finally:
            await self.pool.release(client)

    async def _add_metadata(self, key_id: str, metadata: Dict[str, str]):
        """Attach custom metadata to key objects"""
        client = await self.pool.acquire()
        try:
            await client.set_attribute(
                uid=key_id,
                attributes={
                    'Application Metadata': metadata
                }
            )
        finally:
            await self.pool.release(client)

# ----- Error Hierarchy -----
class KMIPError(Exception):
    """Base KMIP exception"""

class KMIPConnectionError(KMIPError):
    """Connection-level failures"""

class KMIPOperationError(KMIPError):
    """Cryptographic operation failures"""

class KMIPValidationError(KMIPError):
    """Invalid input parameters"""

class KMIPPermissionError(KMIPError):
    """Authorization failures"""

# ----- Example Usage -----
async def main():
    config = KMIPConfig(
        endpoint=Url("https://kmip.kuva.ai"),
        cert_chain="-----BEGIN CERT...",
        private_key="-----BEGIN PRIVATE KEY...",
        ca_cert="-----BEGIN CERT..."
    )
    
    async with KMIPEnterpriseClient(config) as client:
        # Generate AES-256 key
        key_id = await client.generate_symmetric_key(
            algorithm=enums.CryptographicAlgorithm.AES,
            length=256,
            usage_mask=[
                enums.CryptographicUsageMask.ENCRYPT,
                enums.CryptographicUsageMask.DECRYPT
            ],
            metadata={"purpose": "data-at-rest"}
        )
        
        # Encrypt sensitive data
        ciphertext = await client.encrypt_data(
            key_id,
            b"Sensitive payload"
        )
        
        # Rotate keys quarterly
        new_key_id = await client.rotate_key(key_id)

if __name__ == "__main__":
    asyncio.run(main())
