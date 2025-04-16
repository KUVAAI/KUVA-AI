"""
KUVA AI High-Reliability Consumer Group (Enterprise Edition)
"""
from __future__ import annotations

import asyncio
import json
import logging
import ssl
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional

from confluent_kafka import DeserializingConsumer, KafkaException
from confluent_kafka.schema_registry import SchemaRegistryClient
from confluent_kafka.schema_registry.avro import AvroDeserializer
from pydantic import BaseModel, ValidationError

from kuva.monitoring import ConsumerMetrics, TraceContext, trace_method
from kuva.schemas import MessageSchema
from kuva.utils import CircuitBreaker, ExponentialBackoff, SingletonMeta

@dataclass(frozen=True)
class ConsumerConfig:
    bootstrap_servers: List[str] = ["kafka:9092"]
    schema_registry_url: str = "http://schema-registry:8081"
    group_id: str = "default-consumer-group"
    session_timeout_ms: int = 45000
    heartbeat_interval_ms: int = 3000
    max_poll_interval_ms: int = 600000
    auto_offset_reset: str = "earliest"
    enable_auto_commit: bool = False
    isolation_level: str = "read_committed"
    security_protocol: str = "SASL_SSL"
    sasl_mechanism: str = "SCRAM-SHA-512"
    sasl_username: str = "admin"
    sasl_password: str = "admin-secret"
    ssl_cafile: str = "/certs/ca.pem"
    max_batch_size: int = 1000
    max_queue_size: int = 10000
    retry_policy: ExponentialBackoff = ExponentialBackoff(
        max_retries=5, 
        initial_delay=0.1,
        max_delay=30.0
    )
    dead_letter_topic: Optional[str] = None
    enable_metrics: bool = True

class SchemaDeserializer(metaclass=SingletonMeta):
    """Avro schema deserialization manager"""
    
    def __init__(self, config: ConsumerConfig):
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

class KafkaConsumerGroup:
    """Enterprise-grade consumer group implementation"""
    
    def __init__(
        self,
        config: ConsumerConfig,
        processor: Callable[[MessageSchema], Awaitable[None]]
    ):
        self.config = config
        self.processor = processor
        self.logger = logging.getLogger("kuva.consumer")
        self.metrics = ConsumerMetrics()
        self._consumer: Optional[DeserializingConsumer] = None
        self._deserializer_manager = SchemaDeserializer(config)
        self._circuit_breaker = CircuitBreaker(
            failure_threshold=5,
            recovery_timeout=60
        )
        self._running = False
        self._current_assignments: Dict[str, List[int]] = defaultdict(list)
        self._offset_commits: Dict[str, Dict[int, int]] = defaultdict(dict)

    async def __aenter__(self) -> KafkaConsumerGroup:
        await self._connect()
        return self

    async def __aexit__(self, *args) -> None:
        await self.close()

    def _create_ssl_context(self) -> Optional[ssl.SSLContext]:
        """Configure SSL context for SASL_SSL"""
        ctx = ssl.create_default_context(cafile=self.config.ssl_cafile)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx

    async def _connect(self) -> None:
        """Initialize consumer connection"""
        ssl_context = self._create_ssl_context()
        
        self._consumer = DeserializingConsumer({
            "bootstrap.servers": ",".join(self.config.bootstrap_servers),
            "security.protocol": self.config.security_protocol,
            "sasl.mechanism": self.config.sasl_mechanism,
            "sasl.username": self.config.sasl_username,
            "sasl.password": self.config.sasl_password,
            "ssl.context": ssl_context,
            "group.id": self.config.group_id,
            "session.timeout.ms": self.config.session_timeout_ms,
            "heartbeat.interval.ms": self.config.heartbeat_interval_ms,
            "max.poll.interval.ms": self.config.max_poll_interval_ms,
            "auto.offset.reset": self.config.auto_offset_reset,
            "enable.auto.commit": self.config.enable_auto_commit,
            "isolation.level": self.config.isolation_level,
            "max.poll.records": self.config.max_batch_size,
            "queued.max.messages.kbytes": self.config.max_queue_size,
            "on_commit": self._handle_commit,
            "partition.assignment.strategy": "cooperative-sticky",
        })
        
        self._running = True
        self.metrics.consumer_started.inc()

    def _handle_commit(
        self,
        err: Optional[KafkaException],
        partitions: List[Any]
    ) -> None:
        """Offset commit callback handler"""
        if err:
            self.logger.error(f"Commit failed: {err}")
            self.metrics.commit_errors.inc()
        else:
            self.metrics.committed_offsets.inc(len(partitions))

    async def _handle_rebalance(
        self,
        consumer: DeserializingConsumer,
        event: Any
    ) -> None:
        """Rebalance event handler"""
        self.logger.info(f"Rebalance event: {event.name}")
        if event.name in ["PARTITIONS_REVOKED", "PARTITIONS_LOST"]:
            await self._commit_offsets()
            self._current_assignments.clear()
            
        if event.name == "PARTITIONS_ASSIGNED":
            for tp in event.partitions:
                self._current_assignments[tp.topic].append(tp.partition)
            consumer.assign(event.partitions)

    async def _commit_offsets(self) -> None:
        """Manual offset commit with batching"""
        if not self._consumer:
            return

        commit_list = []
        for topic, partitions in self._offset_commits.items():
            for partition, offset in partitions.items():
                commit_list.append(TopicPartition(topic, partition, offset+1))
                
        if commit_list:
            self._consumer.commit_async(commit_list)
            self._offset_commits.clear()

    @trace_method("consumer.process_message")
    async def _process_message(
        self,
        message: Any
    ) -> None:
        """Process single message with circuit breaker"""
        async with self._circuit_breaker:
            try:
                deserializer = await self._deserializer_manager.get_deserializer(message.topic())
                msg_value = deserializer(message.value(), None)
                validated_msg = MessageSchema.model_validate(msg_value)
                
                start_time = time.monotonic()
                await self.processor(validated_msg)
                processing_time = time.monotonic() - start_time
                
                self.metrics.messages_processed.inc()
                self.metrics.processing_latency.observe(processing_time)
                
                self._offset_commits[message.topic()][message.partition()] = message.offset()

            except ValidationError as e:
                self.logger.error("Schema validation failed", exc_info=True)
                self.metrics.message_validation_failed.inc()
                await self._send_to_dlq(message)
                
            except Exception as e:
                self.logger.error("Message processing failed", exc_info=True)
                self.metrics.processing_errors.inc()
                await self._handle_retry(message, e)

    async def _send_to_dlq(self, message: Any) -> None:
        """Dead letter queue handling"""
        if self.config.dead_letter_topic and self._consumer:
            headers = [
                ("failure_reason", str(e).encode("utf-8")),
                ("original_topic", message.topic().encode("utf-8")),
                ("original_partition", str(message.partition()).encode("utf-8")),
                ("original_offset", str(message.offset()).encode("utf-8"))
            ]
            
            await self._consumer.produce(
                self.config.dead_letter_topic,
                key=message.key(),
                value=message.value(),
                headers=headers
            )
            self.metrics.dead_letter_messages.inc()

    async def _handle_retry(self, message: Any, error: Exception) -> None:
        """Retry policy implementation"""
        if not self.config.retry_policy:
            return
            
        async for attempt in self.config.retry_policy:
            try:
                await self.processor(message)
                return
            except Exception as retry_error:
                self.logger.warning(
                    f"Retry attempt {attempt} failed",
                    error=str(retry_error)
                )
                if attempt.should_retry:
                    await asyncio.sleep(attempt.next_delay)
                else:
                    await self._send_to_dlq(message)
                    break

    async def run_consumption_loop(self) -> None:
        """Main consumption loop with backpressure control"""
        if not self._consumer:
            raise RuntimeError("Consumer not initialized")
            
        try:
            while self._running:
                message = await asyncio.to_thread(
                    self._consumer.poll,
                    timeout=1.0
                )
                
                if message is None:
                    continue
                    
                if message.error():
                    self.logger.error(f"Consumer error: {message.error()}")
                    self.metrics.consumer_errors.inc()
                    continue
                    
                await self._process_message(message)
                
                if len(self._offset_commits) >= self.config.max_batch_size:
                    await self._commit_offsets()
        finally:
            await self._commit_offsets()

    async def close(self) -> None:
        """Graceful shutdown procedure"""
        self._running = False
        if self._consumer:
            await self._commit_offsets()
            self._consumer.close()
            self.metrics.consumer_stopped.inc()

class MessageSchema(BaseModel):
    """Base message schema for consumption"""
    event_id: str
    timestamp: float
    source: str
    correlation_id: Optional[str] = None
    trace_id: Optional[str] = None
    payload: Dict[str, Any]

# Example Usage
async def message_processor(msg: MessageSchema) -> None:
    """Sample message processing logic"""
    print(f"Processing event {msg.event_id} from {msg.source}")
    await asyncio.sleep(0.1)

async def main():
    config = ConsumerConfig(
        group_id="order-processors",
        dead_letter_topic="failed-orders"
    )
    
    async with KafkaConsumerGroup(config, message_processor) as consumer:
        await consumer.run_consumption_loop()

if __name__ == "__main__":
    asyncio.run(main())
