"""
KUVA AI Enterprise Syslog Handler (RFC 5424/RFC 3164 Compliant)
"""
from __future__ import annotations

import asyncio
import datetime
import logging
import socket
import ssl
import time
from enum import IntEnum
from queue import Full, Queue
from threading import Lock, Thread
from typing import Any, Dict, Optional, Tuple

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from opentelemetry import metrics, trace
from pydantic import BaseModel, Field, ValidationError, validator

# ----- Observability Setup -----
tracer = trace.get_tracer("syslog.tracer")
meter = metrics.get_meter("syslog.meter")

syslog_sent_counter = meter.create_counter(
    "syslog.messages.sent",
    description="Total syslog messages sent"
)
syslog_errors_counter = meter.create_counter(
    "syslog.errors",
    description="Total syslog transmission errors"
)
syslog_latency_histogram = meter.create_histogram(
    "syslog.latency",
    description="Syslog transmission latency in milliseconds"
)

# ----- Data Models -----
class SyslogFacility(IntEnum):
    """RFC 5424 Facility Codes"""
    KERN = 0
    USER = 1
    MAIL = 2
    DAEMON = 3
    AUTH = 4
    SYSLOG = 5
    LPR = 6
    NEWS = 7
    UUCP = 8
    CRON = 9
    AUTHPRIV = 10
    FTP = 11
    NTP = 12
    AUDIT = 13
    ALERT = 14
    CLOCK = 15
    LOCAL0 = 16
    LOCAL1 = 17
    LOCAL2 = 18
    LOCAL3 = 19
    LOCAL4 = 20
    LOCAL5 = 21
    LOCAL6 = 22
    LOCAL7 = 23

class SyslogSeverity(IntEnum):
    """RFC 5424 Severity Levels"""
    EMERGENCY = 0
    ALERT = 1
    CRITICAL = 2
    ERROR = 3
    WARNING = 4
    NOTICE = 5
    INFO = 6
    DEBUG = 7

class SyslogConfig(BaseModel):
    """Validated syslog configuration"""
    host: str = Field(..., min_length=1)
    port: int = Field(514, ge=1, le=65535)
    protocol: str = Field("udp", regex=r"^(udp|tcp|tls)$")
    facility: SyslogFacility = SyslogFacility.LOCAL0
    timeout: float = Field(5.0, gt=0)
    queue_size: int = Field(10000, ge=100)
    retries: int = Field(3, ge=0)
    tls_context: Optional[ssl.SSLContext] = None
    structured_data: Optional[Dict[str, Dict[str, str]]] = None

    @validator('protocol')
    def validate_tls_context(cls, v, values):
        if v == "tls" and not values.get("tls_context"):
            raise ValueError("TLS protocol requires SSLContext")
        return v

# ----- Core Implementation -----
class SyslogHandler:
    """Enterprise-grade syslog handler with queue management"""
    
    _instance = None
    _lock = Lock()
    
    def __new__(cls, config: SyslogConfig):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialize(config)
            return cls._instance

    def _initialize(self, config: SyslogConfig):
        """Thread-safe initialization"""
        self.config = config
        self._queue = Queue(maxsize=config.queue_size)
        self._worker_thread = Thread(target=self._process_queue, daemon=True)
        self._shutdown_flag = False
        self._stats = {
            'sent': 0,
            'failed': 0,
            'queue_drops': 0
        }
        self._socket = None
        self._connect_lock = Lock()
        self._worker_thread.start()

    def _connect(self) -> socket.socket:
        """Establish protocol-specific connection"""
        with self._connect_lock:
            if self._socket:
                return self._socket

            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM 
                                    if self.config.protocol != "udp" 
                                    else socket.SOCK_DGRAM)
                sock.settimeout(self.config.timeout)

                if self.config.protocol == "tls":
                    wrapped_sock = self.config.tls_context.wrap_socket(
                        sock,
                        server_hostname=self.config.host
                    )
                    wrapped_sock.connect((self.config.host, self.config.port))
                    self._socket = wrapped_sock
                else:
                    sock.connect((self.config.host, self.config.port))
                    self._socket = sock

                return self._socket
            except Exception as e:
                self._handle_error(f"Connection failed: {str(e)}")
                raise

    def _disconnect(self):
        """Close connection gracefully"""
        with self._connect_lock:
            if self._socket:
                try:
                    if self.config.protocol == "tls":
                        self._socket.unwrap()
                    self._socket.close()
                except Exception:
                    pass
                finally:
                    self._socket = None

    def _format_message(
        self,
        message: str,
        severity: SyslogSeverity,
        app_name: Optional[str] = None,
        proc_id: Optional[str] = None,
        msg_id: Optional[str] = None
    ) -> bytes:
        """Format message according to RFC 5424"""
        pri = (self.config.facility.value * 8) + severity.value
        timestamp = datetime.datetime.utcnow().isoformat() + "Z"
        structured_data = self._format_structured_data()
        
        return (
            f"<{pri}>1 {timestamp} {socket.gethostname()} "
            f"{app_name or '-'} {proc_id or '-'} {msg_id or '-'} "
            f"{structured_data} {message}\n"
        ).encode('utf-8')

    def _format_structured_data(self) -> str:
        """Format structured data elements"""
        if not self.config.structured_data:
            return "-"
        return " ".join(
            f"[{sd_id} {' '.join(f'{k}=\"{v}\"' for k, v in params.items())}]"
            for sd_id, params in self.config.structured_data.items()
        )

    def _process_queue(self):
        """Background queue processing"""
        while not self._shutdown_flag:
            try:
                message, severity, start_time = self._queue.get(timeout=1)
                for attempt in range(self.config.retries + 1):
                    try:
                        sock = self._connect()
                        sock.sendall(message)
                        syslog_sent_counter.add(1)
                        latency = (time.time() - start_time) * 1000
                        syslog_latency_histogram.record(latency)
                        self._stats['sent'] += 1
                        break
                    except Exception as e:
                        if attempt == self.config.retries:
                            self._stats['failed'] += 1
                            syslog_errors_counter.add(1)
                            self._handle_error(f"Final send failure: {str(e)}")
                        else:
                            time.sleep(2 ** attempt)
                            self._disconnect()
            except Empty:
                continue
            except Exception as e:
                self._handle_error(f"Queue processing error: {str(e)}")

    def _handle_error(self, error_msg: str):
        """Central error handling"""
        logging.error(error_msg)
        syslog_errors_counter.add(1)
        self._disconnect()

    def emit(
        self,
        message: str,
        severity: SyslogSeverity = SyslogSeverity.INFO,
        **kwargs: Any
    ):
        """Public method to send syslog messages"""
        try:
            formatted_msg = self._format_message(message, severity, **kwargs)
            start_time = time.time()
            try:
                self._queue.put_nowait((formatted_msg, severity, start_time))
            except Full:
                self._stats['queue_drops'] += 1
                syslog_errors_counter.add(1)
                logging.warning("Syslog queue full - message dropped")
        except ValidationError as e:
            logging.error(f"Invalid syslog message: {str(e)}")

    def flush(self, timeout: Optional[float] = None):
        """Block until queue is empty"""
        end_time = time.time() + timeout if timeout else None
        while not self._queue.empty():
            if end_time and time.time() > end_time:
                raise TimeoutError("Flush operation timed out")
            time.sleep(0.1)

    def close(self):
        """Graceful shutdown"""
        self._shutdown_flag = True
        self._worker_thread.join()
        self._disconnect()

# ----- TLS Configuration -----
def create_tls_context(
    certfile: Optional[str] = None,
    keyfile: Optional[str] = None,
    ca_certs: Optional[str] = None,
    verify_mode: int = ssl.CERT_REQUIRED
) -> ssl.SSLContext:
    """Create enterprise-grade TLS context"""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = True
    context.verify_mode = verify_mode
    
    if ca_certs:
        context.load_verify_locations(cafile=ca_certs)
    if certfile and keyfile:
        context.load_cert_chain(certfile, keyfile)
    
    context.set_ciphers("ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-GCM-SHA384")
    context.options |= ssl.OP_NO_SSLv2 | ssl.OP_NO_SSLv3 | ssl.OP_NO_TLSv1 | ssl.OP_NO_TLSv1_1
    context.set_alpn_protocols(["syslog-tls"])
    return context

# ----- Example Usage -----
if __name__ == "__main__":
    tls_ctx = create_tls_context(
        ca_certs="/etc/ssl/certs/syslog-ca.pem",
        certfile="/etc/ssl/certs/client.pem",
        keyfile="/etc/ssl/private/client.key"
    )
    
    config = SyslogConfig(
        host="syslog.kuva.ai",
        port=6514,
        protocol="tls",
        facility=SyslogFacility.LOCAL0,
        tls_context=tls_ctx,
        structured_data={
            "kuva@48577": {
                "app": "kuva-ai",
                "version": "1.0.0"
            }
        }
    )
    
    handler = SyslogHandler(config)
    
    try:
        handler.emit(
            "System initialized",
            severity=SyslogSeverity.INFO,
            app_name="kuva-ai"
        )
    finally:
        handler.flush(timeout=10)
        handler.close()
