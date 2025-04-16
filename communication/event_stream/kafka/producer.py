"""
KUVA AI High-Performance Message Producer
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional

from aiokafka import AIOKafkaProducer
from confluent_kafka.schema_registry import SchemaRegistryClient
from confluent_kafka.schema_registry.avro import AvroSerializer
from pydantic import BaseModel, ValidationError

from kuva.monitoring import ProducerMetrics, TraceContext, trace_method
from kuva.schemas import MessageSchema
from kuva.utils import ExponentialBackoff, SingletonMeta

@dataclass(frozen=True)
class ProducerConfig:
    bootstrap_servers: List[str] = ["kafka:9092"]
    schema_registry_url: str = "http://schema-registry:8081"
    max_connection_pool: int = 10
    max_batch_size: int = 1000
    linger_ms: int = 20
    compression_type: str = "snappy"
    security_protocol: str = "SASL_SSL"
    sasl_mechanism: str = "SCRAM-SHA-512"
    sasl_username: str = "admin"
    sasl_password: str = "admin-secret"
    ssl_cafile: str = "/certs/ca.pem"
    retry_policy: ExponentialBackoff = ExponentialBackoff(
        max_retries=5, 
        initial_delay=0.1,
        max_delay=30.0
    )
    enable_idempotence: bool = True
    transactional_id: Optional[str] = None
    message_timeout_ms: int = 30000

class SchemaManager(metaclass=SingletonMeta):
    """Avro schema registry manager"""
    
    def __init__(self, config: ProducerConfig):
        self._client = SchemaRegistryClient({"url": config.schema_registry_url})
        self._schemas: Dict[str, AvroSerializer] = {}
        self._lock = asyncio.Lock()

    async def get_serializer(self, topic: str) -> AvroSerializer:
        """Get or create schema serializer with caching"""
        async with self._lock:
            if topic not in self._schemas:
                schema_str = json.dumps(MessageSchema.model_json_schema(topic))
                self._schemas[topic] = AvroSerializer(
                    schema_registry_client=self._client,
                    schema_str=schema_str,
                    to_dict=lambda obj, ctx: obj.model_dump()
                )
            return self._schemas[topic]

class MessageProducer:
    """Enterprise-grade async message producer"""
    
    def __init__(self, config: ProducerConfig):
        self.config = config
        self.logger = logging.getLogger("kuva.producer")
        self.metrics = ProducerMetrics()
        self._producer: Optional[AIOKafkaProducer] = None
        self._schema_manager = SchemaManager(config)
        self._transaction_active = False

    async def __aenter__(self) -> MessageProducer:
        await self._connect()
        return self

    async def __aexit__(self, *args) -> None:
        await self.close()

    async def _connect(self) -> None:
        """Initialize connection pool and schema registry"""
        ssl_context = self._create_ssl_context()
        
        self._producer = AIOKafkaProducer(
            bootstrap_servers=self.config.bootstrap_servers,
            security_protocol=self.config.security_protocol,
            sasl_mechanism=self.config.sasl_mechanism,
            sasl_plain_username=self.config.sasl_username,
            sasl_plain_password=self.config.sasl_password,
            ssl_context=ssl_context,
            compression_type=self.config.compression_type,
            linger_ms=self.config.linger_ms,
            max_batch_size=self.config.max_batch_size,
            enable_idempotence=self.config.enable_idempotence,
            transactional_id=self.config.transactional_id,
            request_timeout_ms=self.config.message_timeout_ms,
        )
        
        await self._producer.start()
        self.metrics.connection_established.inc()

    def _create_ssl_context(self) -> Optional[ssl.SSLContext]:
        """Configure SSL context for SASL_SSL"""
        ctx = ssl.create_default_context(cafile=self.config.ssl_cafile)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE  # Managed by CA
        return ctx

    @trace_method("producer.send")
    async def send(
        self,
        topic: str,
        message: MessageSchema,
        key: Optional[bytes] = None,
        headers: Optional[Dict[str, bytes]] = None
    ) -> None:
        """Produce validated message with schema enforcement"""
        if not self._producer:
            raise RuntimeError("Producer not initialized")

        try:
            # Schema validation
            message.model_validate(message.dict())
        except ValidationError as e:
            self.metrics.message_validation_failed.inc()
            self.logger.error("Schema validation failed", exc_info=True)
            raise

        serializer = await self._schema_manager.get_serializer(topic)
        value = serializer(message.dict(), None)  # Serialize to Avro
        
        async for attempt in self.config.retry_policy:
            try:
                if self.config.transactional_id and not self._transaction_active:
                    await self._producer.begin_transaction()
                    self._transaction_active = True

                await self._producer.send(
                    topic=topic,
                    value=value,
                    key=key,
                    headers=headers,
                )
                self.metrics.message_sent.inc()
                return

            except Exception as exc:
                self.metrics.message_errors.inc()
                self.logger.error(
                    "Message delivery failed",
                    attempt=attempt,
                    topic=topic,
                    error=str(exc)
                )
                
                if attempt.should_retry:
                    await asyncio.sleep(attempt.next_delay)
                    continue
                
                if self._transaction_active:
                    await self._producer.abort_transaction()
                    self._transaction_active = False
                
                raise

    async def commit_transaction(self) -> None:
        """Commit active transaction"""
        if self._transaction_active and self._producer:
            await self._producer.commit_transaction()
            self._transaction_active = False
            self.metrics.transaction_committed.inc()

    async def close(self) -> None:
        """Graceful shutdown"""
        if self._producer:
            if self._transaction_active:
                await self.abort_transaction()
                
            await self._producer.stop()
            self.metrics.connection_closed.inc()
            
    # Additional enterprise features
    async def flush(self) -> None:
        """Force message batch delivery"""
        if self._producer:
            await self._producer.flush()
            
    async def metrics_report(self) -> Dict[str, Any]:
        """Get current producer metrics"""
        return {
            "batch_size_avg": self._producer.metrics.batch_size_avg,
            "in_flight_requests": self._producer.metrics.in_flight_requests,
            "request_latency_avg": self._producer.metrics.request_latency_avg,
            "messages_per_sec": self.metrics.messages_sent.rate(),
        }

class MessageSchema(BaseModel):
    """Base message schema with common metadata"""
    event_id: str
    timestamp: float = Field(default_factory=time.time)
    source: str
    correlation_id: Optional[str] = None
    trace_id: Optional[str] = None
    payload: Dict[str, Any]
