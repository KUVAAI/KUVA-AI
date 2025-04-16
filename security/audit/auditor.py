"""
KUVA AI Audit Logging System (NIST 800-92 Compliant)
"""
from __future__ import annotations

import asyncio
import datetime
import hashlib
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from opentelemetry import context, trace
from pydantic import BaseModel, Field, validator
from pydantic_core import Url

# ----- Observability Setup -----
tracer = trace.get_tracer("audit.tracer")
audit_counter = metrics.get_meter("audit.meter").create_counter(
    "audit.records",
    description="Total audit records generated"
)

# ----- Data Models -----
class AuditSubject(BaseModel):
    """Entity initiating audited action"""
    id: str = Field(..., min_length=1)
    type: str = Field(..., regex=r'^[a-zA-Z0-9_-]{1,32}$')
    role: str
    ip: Optional[Url] = None
    geolocation: Optional[str] = None

class AuditTarget(BaseModel):
    """Entity affected by audited action"""
    id: str
    type: str
    version: Optional[str] = None
    integrity_hash: Optional[str] = None

class AuditEntry(BaseModel):
    """Immutable audit log record"""
    timestamp: datetime.datetime = Field(default_factory=datetime.datetime.utcnow)
    action: str = Field(..., min_length=1, max_length=128)
    severity: str = Field("INFO", regex=r'^(DEBUG|INFO|WARNING|CRITICAL)$')
    subject: AuditSubject
    targets: List[AuditTarget]
    context: Dict[str, Any] = {}
    correlation_id: Optional[str] = None
    session_id: Optional[str] = None
    chain_hash: Optional[str] = None

    @validator('timestamp')
    def truncate_microseconds(cls, v):
        """Normalize timestamp precision"""
        return v.replace(microsecond=(v.microsecond // 1000) * 1000)

# ----- Core Implementation -----
class AuditSigner:
    """Cryptographic signature generator"""
    def __init__(self, private_key: rsa.RSAPrivateKey):
        self.private_key = private_key
        self.public_key = self.private_key.public_key()
        self.signer = self.private_key.signer(
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA512()),
                salt_length=padding.PSS.MAX_LENGTH
            ),
            hashes.SHA512()
        )

    def sign_record(self, data: bytes) -> bytes:
        """Generate PKCS#1 v2.1 signature"""
        return self.private_key.sign(
            data,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA512()),
                salt_length=padding.PSS.MAX_LENGTH
            ),
            hashes.SHA512()
        )

class AuditLogger:
    """Tamper-evident audit log generator"""
    def __init__(self, signer: AuditSigner, storage_backend: AuditStorage):
        self.signer = signer
        self.storage = storage_backend
        self.chain_hash = None
        self.lock = asyncio.Lock()
        self.logger = logging.getLogger("kuva.audit")

    async def log(self, entry: AuditEntry) -> str:
        """Submit auditable event with cryptographic chaining"""
        async with self.lock:
            # Generate cryptographic chain
            entry_dict = entry.model_dump()
            entry_json = json.dumps(entry_dict, sort_keys=True).encode()
            
            # Calculate content hash
            content_hash = hashlib.sha3_512(entry_json).hexdigest()
            
            # Build chain hash
            if self.chain_hash:
                chain_input = f"{self.chain_hash}{content_hash}".encode()
                self.chain_hash = hashlib.sha3_512(chain_input).hexdigest()
            else:
                self.chain_hash = content_hash
            
            # Generate digital signature
            signature = self.signer.sign_record(entry_json)
            
            # Build final record
            full_record = {
                **entry_dict,
                "content_hash": content_hash,
                "chain_hash": self.chain_hash,
                "signature": signature.hex(),
                "public_key": self.signer.public_key.public_bytes(
                    encoding=serialization.Encoding.PEM,
                    format=serialization.PublicFormat.SubjectPublicKeyInfo
                ).decode()
            }

            # Submit to storage
            record_id = await self.storage.persist(full_record)
            audit_counter.add(1, {"severity": entry.severity})
            return record_id

class AuditStorage(abc.ABC):
    """Abstract storage backend"""
    @abc.abstractmethod
    async def persist(self, record: Dict[str, Any]) -> str:
        """Store immutable audit record"""
        pass

class LocalAuditStorage(AuditStorage):
    """File-based storage with WAL"""
    def __init__(self, path: str, buffer_size: int = 1000):
        self.path = Path(path)
        self.buffer = []
        self.buffer_size = buffer_size
        self.write_lock = asyncio.Lock()
        self.path.mkdir(parents=True, exist_ok=True)

    async def persist(self, record: Dict[str, Any]) -> str:
        """Write with write-ahead logging"""
        async with self.write_lock:
            # Write to WAL first
            wal_path = self.path / "audit.wal"
            async with aiofiles.open(wal_path, "a") as f:
                await f.write(json.dumps(record) + "\n")
            
            # Buffer for batch write
            self.buffer.append(record)
            
            if len(self.buffer) >= self.buffer_size:
                await self._flush_buffer()
            
            return record['chain_hash']

    async def _flush_buffer(self):
        """Batch write buffer to partitioned files"""
        timestamp = datetime.datetime.utcnow().strftime("%Y%m%dT%H%M")
        filename = f"audit-{timestamp}.ndjson"
        
        async with aiofiles.open(self.path / filename, "a") as f:
            for record in self.buffer:
                await f.write(json.dumps(record) + "\n")
        
        self.buffer.clear()
        # Rotate WAL
        wal_path.rename(wal_path.with_suffix(".wal.bak"))

# ----- Error Handling -----
class AuditError(Exception):
    """Base audit logging exception"""

class IntegrityError(AuditError):
    """Cryptographic validation failure"""

class StorageError(AuditError):
    """Persistence layer failure"""

# ----- Example Usage -----
async def main():
    # Generate cryptographic identity
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=4096)
    signer = AuditSigner(private_key)
    
    # Configure storage
    storage = LocalAuditStorage("/var/log/kuva/audit")
    
    # Initialize logger
    auditor = AuditLogger(signer, storage)
    
    # Create audit entry
    entry = AuditEntry(
        action="policy.update",
        severity="INFO",
        subject=AuditSubject(
            id="user:admin-123",
            type="user",
            role="system-admin",
            ip="192.168.1.100"
        ),
        targets=[
            AuditTarget(
                id="policy:data-retention",
                type="access-policy",
                version="1.2.0"
            )
        ],
        context={
            "old_value": {"retention_days": 30},
            "new_value": {"retention_days": 90}
        }
    )
    
    # Submit entry
    record_id = await auditor.log(entry)

if __name__ == "__main__":
    asyncio.run(main())
