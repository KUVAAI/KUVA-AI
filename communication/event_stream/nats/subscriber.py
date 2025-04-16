"""
KUVA AI High-Reliability Message Subscriber
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional

from confluent_kafka import Consumer, KafkaException, Message, TopicPartition
from confluent_kafka.schema_registry import SchemaRegistryClient
from confluent_kafka.schema_registry.avro import AvroDeserializer
from pydantic import BaseModel, ValidationError

from kuva.monitoring import SubscriberMetrics, TraceContext, trace_method
from kuva.schemas import MessageSchema
from kuva.utils import CircuitBreaker, ExponentialBackoff, SingletonMeta

@dataclass(frozen=True)
class SubscriberConfig:
    bootstrap_servers: List[str] = ["kafka:9092"]
    schema_registry_url: str = "http://schema-registry:8081"
    group_id: str = "kuva-default-group"
    max_poll_interval_ms: int = 300000
    session_timeout_ms: int = 10000
    auto_offset_reset: str = "earliest"
    enable_auto_commit: bool = False
    max_concurrent_messages: int = 1000
    max_retry_attempts: int = 5
    retry_backoff_ms: int = 1000
    dead_letter_topic: Optional[str] = "dead-letters"
    security_protocol: str = "SASL_SSL"
    sasl_mechanism: str = "SCRAM-SHA-512"
    sasl_username: str = "admin"
    sasl_password: str = "admin-secret"
    ssl_cafile: str = "/certs/ca.pem"
    max_partition_fetch_bytes: int = 1048576
    fetch_max_bytes: int = 52428800

class SchemaDeserializer(metaclass=SingletonMeta):
    """Avro schema deserialization manager"""
    
    def __init__(self, config: SubscriberConfig):
        self._client = SchemaRegistryClient({"url": config.schema_registry_url})
        self._deserializers: Dict[str, AvroDeserializer] = {}
        self._lock = asyncio.Lock()

    async def get_deserializer(self, topic: str) -> AvroDeserializer:
        """Get cached deserializer for topic"""
        async with self._lock:
            if topic not in self._deserializers:
                schema_str = json.dumps(MessageSchema.model_json_schema(topic))
                self._deserializers[topic] = AvroDeserializer(
                    schema_registry_client=self._client,
                    schema_str=schema_str,
                    from_dict=lambda data, ctx: MessageSchema(**data)
                )
            return self._deserializers[topic]

class KafkaSubscriber:
    """Enterprise-grade message subscriber"""
    
    def __init__(self, config: SubscriberConfig):
        self.config = config
        self.logger = logging.getLogger("kuva.subscriber")
        self.metrics = SubscriberMetrics()
        self._consumer: Optional[Consumer] = None
        self._deserializer_manager = SchemaDeserializer(config)
        self._circuit_breaker = CircuitBreaker(
            failure_threshold=5,
            recovery_timeout=60
        )
        self._processing_sem = asyncio.BoundedSemaphore(config.max_concurrent_messages)
        self._retry_queue: deque[Message] = deque()
        self._is_shutting_down = False
        self._assignment: List[TopicPartition] = []

    async def __aenter__(self) -> KafkaSubscriber:
        await self._initialize_consumer()
        return self

    async def __aexit__(self, *args) -> None:
        await self.close()

    def _create_ssl_context(self) -> ssl.SSLContext:
        """Configure SSL context for SASL_SSL"""
        ctx = ssl.create_default_context(cafile=self.config.ssl_cafile)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx

    async def _initialize_consumer(self) -> None:
        """Initialize Kafka consumer with security settings"""
        ssl_context = self._create_ssl_context()
        
        self._consumer = Consumer({
            "bootstrap.servers": ",".join(self.config.bootstrap_servers),
            "group.id": self.config.group_id,
            "max.poll.interval.ms": self.config.max_poll_interval_ms,
            "session.timeout.ms": self.config.session_timeout_ms,
            "auto.offset.reset": self.config.auto_offset_reset,
            "enable.auto.commit": self.config.enable_auto_commit,
            "security.protocol": self.config.security_protocol,
            "sasl.mechanism": self.config.sasl_mechanism,
            "sasl.username": self.config.sasl_username,
            "sasl.password": self.config.sasl_password,
            "ssl.context": ssl_context,
            "max.partition.fetch.bytes": self.config.max_partition_fetch_bytes,
            "fetch.max.bytes": self.config.fetch_max_bytes,
            "on_commit": self._on_commit_callback
        })
        
        self.metrics.subscriber_started.inc()

    def _on_commit_callback(self, err: Optional[KafkaException], partitions: List[TopicPartition]):
        """Handle offset commit results"""
        if err:
            self.logger.error(f"Commit failed: {err}")
            self.metrics.commit_errors.inc()
        else:
            self.metrics.offsets_committed.inc(len(partitions))

    async def _handle_retries(self) -> None:
        """Process messages in retry queue with backoff"""
        while not self._is_shutting_down:
            try:
                if self._retry_queue:
                    msg = self._retry_queue.popleft()
                    await self._process_message(msg)
            except Exception as e:
                self.logger.error("Retry handler error", exc_info=True)
            await asyncio.sleep(self.config.retry_backoff_ms / 1000)

    async def _process_message(self, msg: Message) -> None:
        """Core message processing workflow"""
        async with self._circuit_breaker, self._processing_sem:
            try:
                deserializer = await self._deserializer_manager.get_deserializer(msg.topic())
                parsed = deserializer(msg.value(), None)
                
                self.metrics.messages_received.inc()
                start_time = time.monotonic_ns()
                
                # Business logic execution
                await self.message_handler(parsed)
                
                # Commit offset if manual commit enabled
                if not self.config.enable_auto_commit:
                    self._consumer.commit(msg, asynchronous=False)
                
                self.metrics.processing_success.inc()
                self.metrics.processing_latency.observe(
                    (time.monotonic_ns() - start_time) / 1e9
                )

            except ValidationError as e:
                self.logger.error("Schema validation failed", exc_info=True)
                await self._handle_invalid_message(msg)
                self.metrics.message_validation_failed.inc()

            except Exception as e:
                self.logger.error("Message processing failed", exc_info=True)
                await self._handle_processing_failure(msg)
                self.metrics.processing_failures.inc()

    async def _handle_invalid_message(self, msg: Message) -> None:
        """Route invalid messages to dead-letter queue"""
        if self.config.dead_letter_topic:
            producer = DeserializingProducer(...)  # Reuse publisher logic
            await producer.publish(self.config.dead_letter_topic, {
                "original_topic": msg.topic(),
                "payload": msg.value(),
                "error": "Schema validation failed"
            })
        else:
            self.logger.warning("Dropping invalid message (no DLQ configured)")

    async def _handle_processing_failure(self, msg: Message) -> None:
        """Manage message retries and DLQ routing"""
        attempt = msg.headers().get("retry_attempt", 0)
        if attempt < self.config.max_retry_attempts:
            new_headers = {**msg.headers(), "retry_attempt": attempt + 1}
            self._retry_queue.append(msg._replace(headers=new_headers))
            self.metrics.messages_requeued.inc()
        else:
            await self._handle_invalid_message(msg)
            self.metrics.messages_dlq.inc()

    @trace_method("subscriber.process_batch")
    async def run(self, topics: List[str]) -> None:
        """Main subscription loop"""
        self._consumer.subscribe(topics, on_assign=self._on_assign, on_revoke=self._on_revoke)
        asyncio.create_task(self._handle_retries())
        
        while not self._is_shutting_down:
            try:
                msg = self._consumer.poll(1.0)
                if msg is None:
                    continue
                
                if msg.error():
                    self.logger.error(f"Consumer error: {msg.error()}")
                    self.metrics.poll_errors.inc()
                    continue
                
                asyncio.create_task(self._process_message(msg))

            except KafkaException as e:
                self.logger.error("Polling error", exc_info=True)
                self.metrics.poll_errors.inc()
                await self._rebalance_recovery()

    def _on_assign(self, consumer: Consumer, partitions: List[TopicPartition]) -> None:
        """Handle partition assignment changes"""
        self._assignment = partitions
        self.logger.info(f"Assigned partitions: {partitions}")
        self.metrics.partitions_assigned.inc(len(partitions))

    def _on_revoke(self, consumer: Consumer, partitions: List[TopicPartition]) -> None:
        """Handle partition revocation"""
        self.logger.info(f"Revoking partitions: {partitions}")
        self.metrics.partitions_revoked.inc(len(partitions))
        self._assignment = []

    async def _rebalance_recovery(self) -> None:
        """Handle consumer group rebalance failures"""
        self.logger.warning("Attempting rebalance recovery...")
        try:
            self._consumer.close()
            await self._initialize_consumer()
            self._consumer.subscribe(self._assignment)
        except Exception as e:
            self.logger.critical("Rebalance recovery failed", exc_info=True)
            raise

    async def close(self) -> None:
        """Graceful shutdown procedure"""
        self._is_shutting_down = True
        if self._consumer:
            try:
                self._consumer.close()
                self.metrics.subscriber_stopped.inc()
            except Exception as e:
                self.logger.error("Consumer close failed", exc_info=True)

    @abc.abstractmethod
    async def message_handler(self, message: MessageSchema) -> None:
        """User-implemented message processing logic"""
        raise NotImplementedError

# Example Usage
class OrderMessageHandler(KafkaSubscriber):
    async def message_handler(self, message: MessageSchema) -> None:
        """Business-specific processing logic"""
        if message.payload.get("order_type") == "priority":
            await self.handle_priority_order(message)
        else:
            await self.handle_standard_order(message)

async def main():
    config = SubscriberConfig(
        group_id="order-processors",
        dead_letter_topic="order-dlq",
        max_concurrent_messages=500
    )
    
    async with OrderMessageHandler(config) as subscriber:
        await subscriber.run(["orders", "order-updates"])

if __name__ == "__main__":
    asyncio.run(main())
