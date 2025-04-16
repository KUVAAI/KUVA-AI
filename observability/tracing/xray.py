"""
KUVA AI AWS X-Ray Integration (Enterprise Security Edition)
"""
import asyncio
import contextlib
import logging
import os
import socket
import time
from contextvars import ContextVar
from typing import Any, Callable, Dict, Generator, Optional, TypeVar

from aws_xray_sdk.core import xray_recorder
from aws_xray_sdk.core.models import http
from aws_xray_sdk.ext.aiohttp.middleware import AioHttpXRayMiddleware
from aws_xray_sdk.ext.flask.middleware import XRayMiddleware as FlaskXRayMiddleware
from fastapi import FastAPI, Request
from pydantic import BaseModel, validator

# ======================
# Configuration Models
# ======================
class XRayConfig(BaseModel):
    """Enterprise X-Ray configuration"""
    
    service_name: str = "kuva-ai"
    service_version: str = "1.0.0"
    sampling_rules: Optional[Dict] = None
    daemon_address: str = "127.0.0.1:2000"
    context_missing: str = "LOG_ERROR"
    plugins: tuple = ("ECSPlugin", "EC2Plugin")
    streaming_threshold: int = 100
    max_trace_back: int = 10
    collect_sql_queries: bool = False
    collect_aws_metadata: bool = True
    enable_propagation: bool = True
    enable_plugin_override: bool = False

    @validator("daemon_address")
    def validate_daemon_address(cls, value):
        if not all(s.isdigit() for s in value.split(":")[-1]):
            raise ValueError("Invalid daemon port format")
        return value

# ======================
# Core X-Ray Client
# ======================
class XRayEnterpriseClient:
    """Enterprise-grade X-Ray instrumentation client"""
    
    def __init__(self, config: XRayConfig):
        self.config = config
        self._current_segment = ContextVar("current_segment", default=None)
        self._initialize_recorder()

    def _initialize_recorder(self):
        """Configure X-Ray recorder with enterprise settings"""
        
        xray_recorder.configure(
            service=self.config.service_name,
            sampling_rules=self.config.sampling_rules,
            daemon_address=self.config.daemon_address,
            context_missing=self.config.context_missing,
            plugins=self.config.plugins,
            streaming_threshold=self.config.streaming_threshold,
            max_trace_back=self.config.max_trace_back,
            collect_sql_queries=self.config.collect_sql_queries,
            collect_aws_metadata=self.config.collect_aws_metadata
        )

    # ======================
    # Context Management
    # ======================
    @contextlib.contextmanager
    def capture(self, name: str) -> Generator[Any, None, None]:
        """Context manager for segment capture"""
        segment = xray_recorder.begin_segment(name)
        token = self._current_segment.set(segment)
        try:
            yield segment
        except Exception as e:
            self._record_exception(e)
            raise
        finally:
            xray_recorder.end_segment()
            self._current_segment.reset(token)

    def _record_exception(self, exc: Exception):
        """Record exception details in current segment"""
        if segment := self._current_segment.get():
            xray_recorder.current_segment().add_exception(exc)

    # ======================
    # Framework Integration
    # ======================
    def instrument_fastapi(self, app: FastAPI):
        """Instrument FastAPI application with X-Ray"""
        
        @app.middleware("http")
        async def middleware(request: Request, call_next):
            with xray_recorder.capture(f"{request.method} {request.url.path}"):
                host = request.client.host if request.client else "unknown"
                xray_recorder.current_segment().put_http_meta(http.REMOTE_ADDR, host)
                xray_recorder.current_segment().put_http_meta(http.USER_AGENT, request.headers.get("User-Agent"))
                
                response = await call_next(request)
                
                xray_recorder.current_segment().put_http_meta(http.STATUS, response.status_code)
                return response

    def instrument_flask(self, app):
        """Instrument Flask application with X-Ray"""
        XRayMiddleware(app, xray_recorder)

    def instrument_aiohttp(self, app):
        """Instrument aiohttp application with X-Ray"""
        app.middlewares.append(AioHttpXRayMiddleware(xray_recorder))

    # ======================
    # Decorators
    # ======================
    F = TypeVar("F", bound=Callable[..., Any])

    def trace(self, name: str = None) -> Callable[[F], F]:
        """Decorator for X-Ray segment capture"""
        def decorator(func: F) -> F:
            nonlocal name
            name = name or f"{func.__module__}.{func.__name__}"
            
            if asyncio.iscoroutinefunction(func):
                async def async_wrapper(*args, **kwargs):
                    with xray_recorder.capture(name):
                        return await func(*args, **kwargs)
                return async_wrapper  # type: ignore
            else:
                def sync_wrapper(*args, **kwargs):
                    with xray_recorder.capture(name):
                        return func(*args, **kwargs)
                return sync_wrapper  # type: ignore
        
        return decorator

    # ======================
    # Metadata Management
    # ======================
    def add_metadata(self, key: str, value: Any):
        """Add custom metadata to current segment"""
        if segment := self._current_segment.get():
            segment.put_metadata(key, value)

    def add_annotation(self, key: str, value: Any):
        """Add annotation to current segment"""
        if segment := self._current_segment.get():
            segment.put_annotation(key, value)

    # ======================
    # Security Configuration
    # ======================
    @staticmethod
    def configure_tls(
        cert_file: str,
        key_file: str,
        ca_bundle: str = None,
        validate_hostname: bool = True
    ):
        """Enable TLS for X-Ray daemon communication"""
        from aws_xray_sdk.core.sampling.local.sampler import LocalSampler
        from aws_xray_sdk.core.daemon_config import DaemonConfig
        
        DaemonConfig.tls_cert = cert_file
        DaemonConfig.tls_key = key_file
        Daaconfig.tls_ca = ca_bundle
        DaemonConfig.tls_verify = validate_hostname

    # ======================
    # Lifecycle Management
    # ======================
    def shutdown(self):
        """Cleanly shutdown X-Ray recorder"""
        xray_recorder.clear_trace_entities()
        xray_recorder.emitter.shutdown()

# ======================
# Production Setup
# ======================
def configure_xray(
    service_name: str = "kuva-ai",
    service_version: str = "1.0.0",
    daemon_endpoint: str = None,
    **kwargs
) -> XRayEnterpriseClient:
    """Production X-Ray configuration factory"""
    
    config = XRayConfig(
        service_name=service_name,
        service_version=service_version,
        daemon_address=daemon_endpoint or os.getenv("AWS_XRAY_DAEMON_ADDRESS", "127.0.0.1:2000"),
        sampling_rules={
            "version": 2,
            "rules": [
                {
                    "description": "Default sampling rule",
                    "host": "*",
                    "http_method": "*",
                    "url_path": "*",
                    "fixed_target": 1,
                    "rate": 0.05
                }
            ],
            "default": {
                "fixed_target": 1,
                "rate": 0.1
            }
        },
        **kwargs
    )
    
    return XRayEnterpriseClient(config)

# ======================
# Usage Example
# ======================
xray = configure_xray(
    service_name="kuva-agent-service",
    service_version=os.getenv("APP_VERSION"),
    collect_sql_queries=True
)

@xray.trace("process_payment")
async def process_payment(transaction):
    xray.add_annotation("transaction_id", transaction.id)
    xray.add_metadata("amount", transaction.amount)
    # Payment processing logic

if __name__ == "__main__":
    # TLS Configuration for production
    XRayEnterpriseClient.configure_tls(
        cert_file="/certs/xray.crt",
        key_file="/certs/xray.key",
        ca_bundle="/certs/ca.pem"
    )
    
    with xray.capture("main_operation"):
        xray.add_metadata("environment", os.getenv("ENV", "production"))
        asyncio.run(process_payment(transaction))
