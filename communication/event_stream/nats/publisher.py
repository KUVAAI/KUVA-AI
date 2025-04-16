"""
KUVA AI High-Performance Message Publisher
"""
from __future__ import annotations

import asyncio
import json
import logging
import ssl
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Dict, List, Optional

from confluent_kafka import DeserializingProducer, KafkaException, Message, TopicPartition
from confluent_kafka.schema_registry import SchemaRegistryClient
from confluent_kafka.schema_registry.avro import AvroSerializer
from pydantic import BaseModel, ValidationError

from kuva.monitoring import PublisherMetrics, TraceContext, trace_method
from kuva.schemas import MessageSchema
from kuva.utils import CircuitBreaker, ExponentialBackoff, SingletonMeta

@dataclass(frozen=True)
class PublisherConfig:
    bootstrap_servers: List[str] = ["kafka:9092"]
    schema_registry_url: str = "http://schema-registry:8081"
    max_connections: int = 10
    delivery_timeout_ms: int = 30000
    request_timeout_ms: int = 5000
    message_send_max_retries: int = 5
    retry_backoff_ms: int = 1000
    security_protocol: str = "SASL_SSL"
    sasl_mechanism: str = "SCRAM-SHA-512"
    sasl_username: str = "admin"
    sasl_password: str = "admin-secret"
    ssl_cafile: str = "/certs/ca.pem"
    transactional_id: Optional[str] = None
    transaction_timeout_ms: int = 60000
    enable_idempotence: bool = True
    compression_type: str = "snappy"
    max_batch_size: int = 1000
    max_in_flight_requests: int = 5
    enable_metrics: bool = True

class SchemaSerializer(metaclass=SingletonMeta):
    """Avro schema serialization manager"""
    
    def __init__(self, config: PublisherConfig):
        self._client = SchemaRegistryClient({"url": config.schema_registry_url})
        self._serializers: Dict[str, AvroSerializer] = {}
        self._lock = asyncio.Lock()

    async def get_serializer(self, topic: str) -> AvroSerializer:
        """Get cached serializer for topic"""
        async with self._lock:
            if topic not in self._serializers:
                schema_str = json.dumps(MessageSchema.model_json_schema(topic))
                self._serializers[topic] = AvroSerializer(
                    schema_registry_client=self._client,
                    schema_str=schema_str,
                    to_dict=lambda obj, ctx: obj.model_dump()
                )
            return self._serializers[topic]

class KafkaPublisherPool:
    """Enterprise-grade publisher connection pool"""
    
    def __init__(self, config: PublisherConfig):
        self.config = config
        self.logger = logging.getLogger("kuva.publisher")
        self.metrics = PublisherMetrics()
        self._producers: List[DeserializingProducer] = []
        self._serializer_manager = SchemaSerializer(config)
        self._circuit_breaker = CircuitBreaker(
            failure_threshold=5,
            recovery_timeout=60
        )
        self._active_transactions: Dict[str, DeserializingProducer] = {}
        self._lock = asyncio.Lock()
        self._is_shutting_down = False

    async def __aenter__(self) -> KafkaPublisherPool:
        await self._initialize_pool()
        return self

    async def __aexit__(self, *args) -> None:
        await self.close()

    def _create_ssl_context(self) -> ssl.SSLContext:
        """Configure SSL context for SASL_SSL"""
        ctx = ssl.create_default_context(cafile=self.config.ssl_cafile)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx

    async def _initialize_pool(self) -> None:
        """Initialize producer connection pool"""
        ssl_context = self._create_ssl_context()
        
        for _ in range(self.config.max_connections):
            producer = DeserializingProducer({
                "bootstrap.servers": ",".join(self.config.bootstrap_servers),
                "security.protocol": self.config.security_protocol,
                "sasl.mechanism": self.config.sasl_mechanism,
                "sasl.username": self.config.sasl_username,
                "sasl.password": self.config.sasl_password,
                "ssl.context": ssl_context,
                "delivery.timeout.ms": self.config.delivery_timeout_ms,
                "request.timeout.ms": self.config.request_timeout_ms,
                "message.send.max.retries": self.config.message_send_max_retries,
                "retry.backoff.ms": self.config.retry_backoff_ms,
                "enable.idempotence": self.config.enable_idempotence,
                "compression.type": self.config.compression_type,
                "batch.num.messages": self.config.max_batch_size,
                "max.in.flight.requests.per.connection": self.config.max_in_flight_requests,
                "transactional.id": self.config.transactional_id,
                "transaction.timeout.ms": self.config.transaction_timeout_ms
            })
            self._producers.append(producer)
        
        self.metrics.publisher_started.inc()

    def _get_producer(self) -> DeserializingProducer:
        """Round-robin producer selection"""
        return self._producers[len(self._producers) % (time.monotonic_ns() % len(self._producers))]

    @trace_method("publisher.begin_transaction")
    async def begin_transaction(self, transaction_id: str) -> None:
        """Start a new transactional session"""
        async with self._lock:
            if transaction_id in self._active_transactions:
                raise ValueError(f"Transaction {transaction_id} already exists")
            
            producer = self._get_producer()
            producer.begin_transaction()
            self._active_transactions[transaction_id] = producer
            self.metrics.transactions_started.inc()

    @trace_method("publisher.commit_transaction")
    async def commit_transaction(self, transaction_id: str) -> None:
        """Commit an ongoing transaction"""
        async with self._lock:
            producer = self._active_transactions.get(transaction_id)
            if not producer:
                raise ValueError(f"Transaction {transaction_id} not found")
            
            try:
                producer.commit_transaction()
                self.metrics.transactions_committed.inc()
            except KafkaException as e:
                self.metrics.transaction_errors.inc()
                raise
            finally:
                del self._active_transactions[transaction_id]

    @trace_method("publisher.abort_transaction")
    async def abort_transaction(self, transaction_id: str) -> None:
        """Rollback an ongoing transaction"""
        async with self._lock:
            producer = self._active_transactions.get(transaction_id)
            if not producer:
                raise ValueError(f"Transaction {transaction_id} not found")
            
            try:
                producer.abort_transaction()
                self.metrics.transactions_aborted.inc()
            except KafkaException as e:
                self.metrics.transaction_errors.inc()
                raise
            finally:
                del self._active_transactions[transaction_id]

    @trace_method("publisher.publish")
    async def publish(
        self,
        topic: str,
        message: MessageSchema,
        transaction_id: Optional[str] = None,
        headers: Optional[Dict[str, str]] = None
    ) -> None:
        """Publish message with transactional support"""
        async with self._circuit_breaker:
            producer = self._get_producer()
            serializer = await self._serializer_manager.get_serializer(topic)
            
            try:
                serialized_value = serializer(message, None)
                serialized_key = message.correlation_id.encode("utf-8") if message.correlation_id else None
                
                if headers:
                    kafka_headers = [(k, v.encode("utf-8")) for k, v in headers.items()]
                else:
                    kafka_headers = []
                
                if transaction_id:
                    current_producer = self._active_transactions.get(transaction_id)
                    if current_producer != producer:
                        raise ValueError("Transaction spans multiple producers")
                
                def delivery_callback(err: Optional[KafkaException], msg: Message):
                    if err:
                        self.logger.error(f"Delivery failed: {err}")
                        self.metrics.delivery_errors.inc()
                    else:
                        self.metrics.messages_published.inc()
                        self.metrics.publish_latency.observe(
                            (time.monotonic_ns() - start_time) / 1e9
                        )

                start_time = time.monotonic_ns()
                producer.produce(
                    topic=topic,
                    key=serialized_key,
                    value=serialized_value,
                    headers=kafka_headers,
                    on_delivery=delivery_callback
                )
                
                producer.poll(0)
                
                if producer.flush(0) > 0:
                    self.logger.warning("Producer queue full, initiating flush")
                    producer.flush()

            except ValidationError as e:
                self.logger.error("Schema validation failed", exc_info=True)
                self.metrics.message_validation_failed.inc()
                raise

            except KafkaException as e:
                self.logger.error("Publish operation failed", exc_info=True)
                self.metrics.publish_errors.inc()
                raise

    async def close(self) -> None:
        """Graceful shutdown procedure"""
        async with self._lock:
            if self._is_shutting_down:
                return
            
            self._is_shutting_down = True
            for producer in self._producers:
                try:
                    if producer in self._active_transactions.values():
                        producer.abort_transaction()
                    producer.flush()
                    producer.close()
                except Exception as e:
                    self.logger.error("Producer close failed", exc_info=True)
            
            self.metrics.publisher_stopped.inc()

class MessageSchema(BaseModel):
    """Base message schema for publishing"""
    event_id: str
    timestamp: float
    source: str
    correlation_id: Optional[str] = None
    trace_id: Optional[str] = None
    payload: Dict[str, Any]

# Example Usage
async def publish_messages():
    config = PublisherConfig(
        transactional_id="order-publisher",
        compression_type="zstd"
    )
    
    async with KafkaPublisherPool(config) as publisher:
        # Transactional example
        transaction_id = "txn-12345"
        await publisher.begin_transaction(transaction_id)
        
        try:
            msg = MessageSchema(
                event_id="evt-001",
                timestamp=time.time(),
                source="orders",
                payload={"order_id": "1001"}
            )
            
            await publisher.publish(
                topic="orders",
                message=msg,
                transaction_id=transaction_id,
                headers={"priority": "high"}
            )
            
            await publisher.commit_transaction(transaction_id)
        except Exception as e:
            await publisher.abort_transaction(transaction_id)
            raise

if __name__ == "__main__":
    asyncio.run(publish_messages())
