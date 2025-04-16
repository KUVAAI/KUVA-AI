"""
KUVA AI Asynchronous gRPC Server (Enterprise Edition)
"""
import asyncio
import logging
import signal
import ssl
from concurrent import futures
from typing import Any, List, Optional

import grpc
from grpc import aio
from prometheus_client import start_http_server as start_metrics_server

from kuva.logging import StructuredLogger
from kuva.monitoring import (
    ServerMetrics,
    init_tracing,
    instrument_grpc_server,
)
from proto.healthcheck.v1 import health_pb2_grpc
from proto.agent.v1 import agent_pb2_grpc
from kuva.core.services import (
    HealthServicer,
    AgentManagerServicer,
    AccessControlInterceptor,
    RateLimitingInterceptor,
)

class ServerConfig:
    """Runtime configuration loaded from environment"""
    
    def __init__(self):
        self.host = os.getenv("GRPC_HOST", "[::]")
        self.port = int(os.getenv("GRPC_PORT", 50051))
        self.max_workers = int(os.getenv("GRPC_MAX_WORKERS", 100))
        self.tls_enabled = os.getenv("GRPC_TLS_ENABLED", "true").lower() == "true"
        self.tls_cert = os.getenv("GRPC_TLS_CERT_PATH", "/certs/server.pem")
        self.tls_key = os.getenv("GRPC_TLS_KEY_PATH", "/certs/server.key")
        self.ca_cert = os.getenv("GRPC_CA_CERT_PATH", "/certs/ca.pem")
        self.metrics_port = int(os.getenv("METRICS_PORT", 9090))
        self.jaeger_endpoint = os.getenv("JAEGER_ENDPOINT", "http://jaeger:14268/api/traces")

class GracefulExit:
    """Context manager for graceful shutdown"""
    
    def __init__(self, server: aio.Server):
        self.server = server
        self.signals = (signal.SIGTERM, signal.SIGINT)
        self.loop = asyncio.get_event_loop()
        
    async def _shutdown(self):
        await self.server.stop(5)
        logging.getLogger("shutdown").info("gRPC server stopped")

    def _signal_handler(self, sig):
        self.loop.create_task(self._shutdown())

    def __enter__(self):
        for sig in self.signals:
            self.loop.add_signal_handler(sig, self._signal_handler, sig)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.loop.remove_signal_handler(signal.SIGTERM)
        self.loop.remove_signal_handler(signal.SIGINT)

class AsyncGRPCServer:
    """Production-grade gRPC server implementation"""
    
    def __init__(self, config: ServerConfig):
        self.config = config
        self.logger = StructuredLogger(__name__)
        self.metrics = ServerMetrics()
        self.server = None
        self.credentials = None
        self._prepare_ssl()
        self._prepare_interceptors()
        init_tracing(config.jaeger_endpoint)

    def _prepare_ssl(self):
        if self.config.tls_enabled:
            with open(self.config.ca_cert, 'rb') as f:
                ca_data = f.read()
                
            self.credentials = grpc.ssl_server_credentials(
                private_key_certificate_chain_pairs=[(
                    open(self.config.tls_key, 'rb').read(),
                    open(self.config.tls_cert, 'rb').read(),
                )],
                root_certificates=ca_data,
                require_client_auth=True,
            )

    def _prepare_interceptors(self):
        self.interceptors = [
            AccessControlInterceptor(),
            RateLimitingInterceptor(),
            instrument_grpc_server(self.metrics),
        ]

    async def _load_services(self):
        """Dynamically register all gRPC services"""
        self.server = aio.server(
            futures.ThreadPoolExecutor(max_workers=self.config.max_workers),
            interceptors=self.interceptors,
            options=[
                ('grpc.max_send_message_length', 100 * 1024 * 1024),
                ('grpc.max_receive_message_length', 100 * 1024 * 1024),
                ('grpc.http2.max_pings_without_data', 0),
            ]
        )
        
        # Register services
        health_pb2_grpc.add_HealthServicer_to_server(
            HealthServicer(), self.server)
        agent_pb2_grpc.add_AgentManagerServicer_to_server(
            AgentManagerServicer(), self.server)

    async def start(self):
        """Start server with full observability"""
        await self._load_services()
        
        # Start metrics exporter
        start_metrics_server(self.config.metrics_port)
        self.logger.info(
            "Starting metrics server", 
            port=self.config.metrics_port
        )

        # Start gRPC server
        listen_addr = f"{self.config.host}:{self.config.port}"
        if self.credentials:
            self.server.add_secure_port(listen_addr, self.credentials)
        else:
            self.server.add_insecure_port(listen_addr)
            
        await self.server.start()
        self.logger.info(
            "gRPC server started", 
            host=self.config.host,
            port=self.config.port,
            tls=self.config.tls_enabled
        )

        # Async wait for termination
        with GracefulExit(self.server):
            await self.server.wait_for_termination()

if __name__ == "__main__":
    config = ServerConfig()
    logging.basicConfig(level=logging.INFO)
    
    loop = asyncio.get_event_loop()
    server = AsyncGRPCServer(config)
    
    try:
        loop.run_until_complete(server.start())
    except KeyboardInterrupt:
        loop.run_until_complete(server.server.stop(5))
    finally:
        loop.close()
