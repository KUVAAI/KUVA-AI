"""
KUVA AI gRPC Client Connection Pool (Enterprise Edition)
"""
import asyncio
import logging
import random
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional, Tuple

import grpc
from grpc import ChannelConnectivity, aio

from kuva.logging import StructuredLogger
from kuva.monitoring import (
    ClientMetrics,
    TraceContext,
    trace_method,
)
from proto.healthcheck.v1 import health_pb2, health_pb2_grpc

class ClientConfig:
    """Client pool runtime configuration"""
    
    def __init__(self):
        self.max_conn_per_endpoint = int(os.getenv("GRPC_MAX_CONN", "10"))
        self.max_idle_conn = int(os.getenv("GRPC_MAX_IDLE_CONN", "5"))
        self.reconnect_timeout = float(os.getenv("GRPC_RECONNECT_TIMEOUT", "5.0"))
        self.retry_count = int(os.getenv("GRPC_RETRY_COUNT", "3"))
        self.health_check_interval = float(os.getenv("GRPC_HEALTH_CHECK_INTERVAL", "30.0"))
        self.load_balancing_strategy = os.getenv("GRPC_LB_STRATEGY", "round_robin")
        self.tls_ca_path = os.getenv("GRPC_TLS_CA_PATH", "/certs/ca.pem")
        self.tls_client_cert_path = os.getenv("GRPC_TLS_CLIENT_CERT_PATH", "/certs/client.pem")
        self.tls_client_key_path = os.getenv("GRPC_TLS_CLIENT_KEY_PATH", "/certs/client.key")

class ChannelWrapper:
    """Managed gRPC channel with health monitoring"""
    
    def __init__(self, endpoint: str, config: ClientConfig):
        self.endpoint = endpoint
        self.config = config
        self.channel = self._create_channel()
        self.last_used = time.monotonic()
        self.health_status = health_pb2.HealthCheckResponse.SERVING_STATUS_UNKNOWN
        self.failure_count = 0
        self._lock = asyncio.Lock()
        
    def _create_channel(self) -> aio.Channel:
        """Create secure channel with TLS"""
        with open(self.config.tls_ca_path, 'rb') as f:
            root_certs = f.read()
            
        with open(self.config.tls_client_cert_path, 'rb') as f:
            cert_chain = f.read()
            
        with open(self.config.tls_client_key_path, 'rb') as f:
            private_key = f.read()
            
        credentials = grpc.ssl_channel_credentials(
            root_certificates=root_certs,
            private_key=private_key,
            certificate_chain=cert_chain,
        )
        
        return aio.secure_channel(
            self.endpoint,
            credentials=credentials,
            options=[
                ('grpc.ssl_target_name_override', 'kuva.ai'),
                ('grpc.keepalive_time_ms', 10000),
                ('grpc.keepalive_timeout_ms', 5000),
            ]
        )
    
    async def check_health(self):
        """Perform health check and update status"""
        try:
            stub = health_pb2_grpc.HealthStub(self.channel)
            response = await stub.Check(
                health_pb2.HealthCheckRequest(),
                timeout=10.0
            )
            self.health_status = response.status
            self.failure_count = 0
        except Exception as e:
            self.failure_count += 1
            self.health_status = health_pb2.HealthCheckResponse.NOT_SERVING
    
    def is_healthy(self) -> bool:
        """Determine if channel is usable"""
        return self.health_status == health_pb2.HealthCheckResponse.SERVING
    
    def close(self):
        """Gracefully close the channel"""
        if self.channel:
            self.channel.close()

class ClientPool:
    """Enterprise-grade gRPC client connection pool"""
    
    def __init__(self, endpoints: List[str], config: ClientConfig):
        self.config = config
        self.logger = StructuredLogger(__name__)
        self.metrics = ClientMetrics()
        self._endpoints = endpoints
        self._pools: Dict[str, Deque[ChannelWrapper]] = {}
        self._semaphore = asyncio.Semaphore(config.max_conn_per_endpoint * len(endpoints))
        self._health_check_task: Optional[asyncio.Task] = None
        self._load_balancer = self._create_load_balancer()
        
        # Initialize connection pools
        for endpoint in endpoints:
            self._pools[endpoint] = deque(maxlen=config.max_conn_per_endpoint)

    def _create_load_balancer(self):
        """Initialize load balancing strategy"""
        strategies = {
            "round_robin": RoundRobinStrategy(),
            "random": RandomStrategy(),
            "least_connections": LeastConnectionsStrategy(),
            "latency_aware": LatencyAwareStrategy(),
        }
        return strategies.get(self.config.load_balancing_strategy, strategies["round_robin"])
    
    async def _health_check_loop(self):
        """Background health monitoring task"""
        while True:
            try:
                await asyncio.gather(*[
                    self._check_endpoint_health(endpoint)
                    for endpoint in self._endpoints
                ])
            except Exception as e:
                self.logger.error("Health check failed", error=str(e))
            await asyncio.sleep(self.config.health_check_interval)
    
    async def _check_endpoint_health(self, endpoint: str):
        """Validate and maintain endpoint connections"""
        try:
            async with self._semaphore:
                # Prune unhealthy connections
                await self._evict_unhealthy_connections(endpoint)
                
                # Maintain minimum connections
                await self._warm_pool(endpoint)
        except Exception as e:
            self.logger.error("Endpoint health check failed", endpoint=endpoint, error=str(e))
    
    async def _evict_unhealthy_connections(self, endpoint: str):
        """Remove unhealthy channels from the pool"""
        valid = []
        while self._pools[endpoint]:
            channel = self._pools[endpoint].popleft()
            if channel.is_healthy():
                valid.append(channel)
            else:
                channel.close()
                self.metrics.connection_closed.inc()
        self._pools[endpoint].extend(valid)
    
    async def _warm_pool(self, endpoint: str):
        """Maintain minimum healthy connections"""
        while len(self._pools[endpoint]) < self.config.max_conn_per_endpoint:
            channel = ChannelWrapper(endpoint, self.config)
            await channel.check_health()
            if channel.is_healthy():
                self._pools[endpoint].append(channel)
                self.metrics.connection_created.inc()
            else:
                channel.close()
                self.metrics.connection_failed.inc()
    
    @trace_method("client_pool.acquire")
    async def acquire(self, service: Any) -> Tuple[ChannelWrapper, Any]:
        """Acquire a healthy channel and service stub"""
        for attempt in range(self.config.retry_count):
            endpoint = self._load_balancer.select_endpoint(self._endpoints)
            async with self._semaphore:
                if self._pools[endpoint]:
                    channel = self._pools[endpoint].popleft()
                    if channel.is_healthy():
                        channel.last_used = time.monotonic()
                        stub = service(channel.channel)
                        return channel, stub
                    else:
                        channel.close()
                        self.metrics.connection_closed.inc()
            
            # Attempt to create new connection if pool exhausted
            try:
                channel = ChannelWrapper(endpoint, self.config)
                await channel.check_health()
                if channel.is_healthy():
                    stub = service(channel.channel)
                    self.metrics.connection_created.inc()
                    return channel, stub
            except Exception as e:
                self.logger.error("Connection failed", endpoint=endpoint, error=str(e))
                self.metrics.connection_failed.inc()
                await asyncio.sleep(self.config.reconnect_timeout)
        
        raise grpc.RpcError(f"Failed to acquire connection after {self.config.retry_count} attempts")
    
    async def release(self, channel: ChannelWrapper):
        """Return a channel back to the pool"""
        async with self._semaphore:
            if len(self._pools[channel.endpoint]) < self.config.max_idle_conn:
                self._pools[channel.endpoint].append(channel)
                self.metrics.connection_reused.inc()
            else:
                channel.close()
                self.metrics.connection_closed.inc()
    
    async def __aenter__(self):
        self._health_check_task = asyncio.create_task(self._health_check_loop())
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        self._health_check_task.cancel()
        try:
            await self._health_check_task
        except asyncio.CancelledError:
            pass
        
        # Cleanup all connections
        for endpoint in self._endpoints:
            while self._pools[endpoint]:
                channel = self._pools[endpoint].popleft()
                channel.close()
                self.metrics.connection_closed.inc()

# Load Balancing Strategies
class RoundRobinStrategy:
    def __init__(self):
        self._index = 0
    
    def select_endpoint(self, endpoints: List[str]) -> str:
        self._index = (self._index + 1) % len(endpoints)
        return endpoints[self._index]

class RandomStrategy:
    def select_endpoint(self, endpoints: List[str]) -> str:
        return random.choice(endpoints)

class LeastConnectionsStrategy:
    def select_endpoint(self, endpoints: List[str]) -> str:
        # Implementation requires tracking active connections
        pass

class LatencyAwareStrategy:
    def select_endpoint(self, endpoints: List[str]) -> str:
        # Implementation requires historical latency metrics
        pass
