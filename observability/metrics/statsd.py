"""
KUVA AI StatsD Client (Enterprise Edition)
"""
from __future__ import annotations

import asyncio
import logging
import random
import socket
import time
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncGenerator, Dict, List, Optional, Tuple, Union

from pydantic import BaseModel, Field, PositiveFloat, PositiveInt, ValidationError, validator
from typing_extensions import Literal

# ======================
# Configuration Models
# ======================
class StatsDConfig(BaseModel):
    """Enterprise StatsD client configuration"""
    host: str = Field("localhost", min_length=1)
    port: PositiveInt = 8125
    protocol: Literal["udp", "tcp", "tls"] = "udp"
    max_retries: PositiveInt = 3
    buffer_size: PositiveInt = Field(
        1000, 
        description="Max metrics to buffer before flushing"
    )
    flush_interval: PositiveFloat = Field(
        0.5,
        description="Seconds between automatic flushes"
    )
    sample_rate: float = Field(
        1.0,
        ge=0.0,
        le=1.0,
        description="Default sampling rate"
    )
    mtu: PositiveInt = Field(
        1432,
        description="Maximum transmission unit (bytes)"
    )
    timeout: PositiveFloat = 2.0
    tags: Dict[str, str] = Field(
        {},
        description="Global tags for all metrics"
    )
    tls_context: Optional[Dict[str, Any]] = None

    @validator("tls_context", always=True)
    def validate_tls_context(cls, v, values):
        if values.get("protocol") == "tls" and not v:
            raise ValueError("TLS context required for TLS protocol")
        return v

# ======================
# Core Implementation
# ======================
class StatsDClient:
    """Enterprise-grade asynchronous StatsD client"""
    
    def __init__(self, config: StatsDConfig):
        self.config = config
        self._buffer: deque[str] = deque(maxlen=config.buffer_size)
        self._flush_task: Optional[asyncio.Task] = None
        self._transport: Optional[Any] = None
        self._protocol: Optional[Any] = None
        self._conn_lock = asyncio.Lock()
        self._metrics = {
            'sent': 0,
            'dropped': 0,
            'errors': 0
        }
        self._create_socket()

    def _create_socket(self):
        """Initialize appropriate socket type"""
        if self.config.protocol == "udp":
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.sock.settimeout(self.config.timeout)
        elif self.config.protocol == "tcp":
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.sock.settimeout(self.config.timeout)
        elif self.config.protocol == "tls":
            context = self._create_ssl_context()
            self.sock = context.wrap_socket(
                socket.socket(socket.AF_INET),
                server_hostname=self.config.host
            )
        else:
            raise ValueError(f"Unsupported protocol: {self.config.protocol}")

    def _create_ssl_context(self):
        """Create SSL context from configuration"""
        import ssl
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.load_default_certs()
        if self.config.tls_context:
            context.load_cert_chain(
                self.config.tls_context["certfile"],
                self.config.tls_context["keyfile"]
            )
            if "ca_certs" in self.config.tls_context:
                context.load_verify_locations(self.config.tls_context["ca_certs"])
        return context

    async def _ensure_connection(self):
        """Maintain active connection (TCP/TLS only)"""
        if self.config.protocol in ("udp", "unix"):
            return

        async with self._conn_lock:
            if not self.sock or self.sock.fileno() == -1:
                await self._reconnect()

    async def _reconnect(self):
        """Establish new connection with retries"""
        for attempt in range(self.config.max_retries):
            try:
                self.sock.connect((self.config.host, self.config.port))
                return
            except (socket.error, ConnectionRefusedError) as e:
                if attempt == self.config.max_retries - 1:
                    raise StatsDConnectionError(
                        f"Connection failed after {self.config.max_retries} attempts"
                    ) from e
                await asyncio.sleep(0.2 * (attempt + 1))

    async def _send(self, data: str):
        """Core send method with error handling"""
        try:
            if self.config.protocol == "udp":
                self.sock.sendto(data.encode(), (self.config.host, self.config.port))
            else:
                await self._ensure_connection()
                self.sock.sendall(data.encode())
            
            self._metrics['sent'] += 1
        except Exception as e:
            self._metrics['errors'] += 1
            logging.error(f"StatsD send error: {str(e)}")
            raise

    async def _flush(self):
        """Flush buffered metrics efficiently"""
        if not self._buffer:
            return

        try:
            # Chunk data based on MTU
            chunks = []
            current_chunk = []
            current_size = 0
            
            while self._buffer:
                metric = self._buffer.popleft()
                metric_size = len(metric.encode())
                
                if current_size + metric_size > self.config.mtu:
                    chunks.append("\n".join(current_chunk))
                    current_chunk = []
                    current_size = 0
                
                current_chunk.append(metric)
                current_size += metric_size + 1  # +1 for newline
            
            if current_chunk:
                chunks.append("\n".join(current_chunk))

            # Send all chunks
            for chunk in chunks:
                await self._send(chunk)

        except Exception as e:
            self._metrics['dropped'] += len(self._buffer)
            self._buffer.clear()
            raise

    @asynccontextmanager
    async def pipeline(self) -> AsyncGenerator[None, None]:
        """Context manager for metric batching"""
        try:
            yield
        finally:
            await self.flush()

    async def flush(self):
        """Manual buffer flush"""
        async with self._conn_lock:
            await self._flush()

    async def close(self):
        """Cleanup resources"""
        await self.flush()
        if self.sock:
            self.sock.close()
        if self._flush_task:
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass

    # ======================
    # Metric Methods
    # ======================
    def counter(
        self,
        name: str,
        value: Union[int, float] = 1,
        sample_rate: Optional[float] = None,
        tags: Optional[Dict[str, str]] = None
    ):
        """Submit counter metric"""
        self._submit(name, value, "c", sample_rate, tags)

    def gauge(
        self,
        name: str,
        value: Union[int, float],
        tags: Optional[Dict[str, str]] = None
    ):
        """Submit gauge metric"""
        self._submit(name, value, "g", None, tags)

    def timing(
        self,
        name: str,
        value: float,
        sample_rate: Optional[float] = None,
        tags: Optional[Dict[str, str]] = None
    ):
        """Submit timing metric (milliseconds)"""
        self._submit(name, value, "ms", sample_rate, tags)

    def histogram(
        self,
        name: str,
        value: Union[int, float],
        tags: Optional[Dict[str, str]] = None
    ):
        """Submit histogram metric"""
        self._submit(name, value, "h", None, tags)

    def _submit(
        self,
        name: str,
        value: Union[int, float],
        metric_type: str,
        sample_rate: Optional[float],
        tags: Optional[Dict[str, str]]
    ):
        """Core metric submission logic"""
        if sample_rate is None:
            sample_rate = self.config.sample_rate

        if sample_rate < 1 and random.random() > sample_rate:
            return

        parts = [f"{name}:{value}|{metric_type}"]
        
        if sample_rate < 1:
            parts.append(f"@{sample_rate}")
        
        if tags or self.config.tags:
            all_tags = {**self.config.tags, **(tags or {})}
            tag_str = ",".join(f"{k}={v}" for k, v in all_tags.items())
            parts.append(f"|#{tag_str}")

        self._buffer.append("".join(parts))

        if len(self._buffer) >= self.config.buffer_size:
            asyncio.create_task(self.flush())

    # ======================
    # Monitoring
    # ======================
    def get_metrics(self) -> Dict[str, int]:
        """Get client performance metrics"""
        return self._metrics.copy()

# ======================
# Error Classes
# ======================
class StatsDError(Exception):
    """Base StatsD client error"""

class StatsDConnectionError(StatsDError):
    """Connection-related errors"""

class StatsDProtocolError(StatsDError):
    """Invalid metric formatting errors"""

# ======================
# Usage Example
# ======================
async def main():
    config = StatsDConfig(
        host="statsd.prod.kuva.ai",
        port=8125,
        protocol="udp",
        tags={"environment": "production"}
    )
    
    async with StatsDClient(config) as client:
        # Submit metrics
        client.counter("user.logins", tags={"source": "mobile"})
        client.gauge("server.memory", 42.5)
        client.timing("api.response_time", 123.4)
        
        # Manual flush
        await client.flush()
        
        # Get client metrics
        print(client.get_metrics())

if __name__ == "__main__":
    asyncio.run(main())
