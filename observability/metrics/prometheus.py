"""
KUVA AI Prometheus Custom Metrics Collector (Enterprise Edition)
"""
import inspect
import logging
import time
from collections import defaultdict
from datetime import datetime, timedelta
from functools import wraps
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from prometheus_client import (
    REGISTRY,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    Summary,
    generate_latest,
    start_http_server,
)
from prometheus_client.core import GaugeMetricFamily, InfoMetricFamily
from pydantic import BaseModel, Field, ValidationError, validator
from typing_extensions import Literal

# ======================
# Configuration Models
# ======================
class MetricConfig(BaseModel):
    """Base configuration for custom metrics"""
    name: str = Field(..., regex=r'^[a-zA-Z_:][a-zA-Z0-9_:]*$')
    description: str = Field("KUVA AI Custom Metric")
    labels: List[str] = Field([])
    namespace: str = "kuva_ai"
    subsystem: Optional[str] = None
    ttl: Optional[float] = Field(
        None, 
        description="Time-to-live in seconds for metric values"
    )

class HistogramConfig(MetricConfig):
    """Histogram-specific configuration"""
    buckets: List[Union[float, str]] = Field(["0.1", "0.5", "1", "5", "10"])
    type: Literal["histogram"] = "histogram"

class SummaryConfig(MetricConfig):
    """Summary-specific configuration"""
    quantiles: List[Tuple[float, float]] = Field([(0.5, 0.05), (0.95, 0.01)])
    type: Literal["summary"] = "summary"

# ======================
# Core Collector Classes
# ======================
class DynamicMetric:
    """Base class for dynamically managed metrics"""
    _metrics_lock = threading.RLock()
    _metrics_cache: Dict[str, Any] = {}
    _last_updated: Dict[str, float] = {}
    
    def __init__(self, config: MetricConfig):
        self.config = config
        self.full_name = f"{config.namespace}_{config.subsystem}_{config.name}" \
            if config.subsystem else f"{config.namespace}_{config.name}"
        self.label_names = config.labels.copy()
        self._values: Dict[Tuple, Any] = {}

    def _check_ttl(self):
        """Remove expired metric values based on TTL"""
        current_time = time.time()
        expired_keys = [
            k for k, v in self._last_updated.items()
            if self.config.ttl and (current_time - v) > self.config.ttl
        ]
        for key in expired_keys:
            del self._values[key]
            del self._last_updated[key]

    def update(self, value: float, labels: Optional[Dict[str, str]] = None):
        """Thread-safe metric update with label validation"""
        label_values = self._validate_labels(labels or {})
        
        with self._metrics_lock:
            self._values[tuple(label_values)] = value
            self._last_updated[self._cache_key(label_values)] = time.time()
            self._check_ttl()

    def _validate_labels(self, labels: Dict[str, str]) -> List[str]:
        """Validate and order label values"""
        if set(labels.keys()) != set(self.label_names):
            raise ValueError(f"Label mismatch. Expected {self.label_names}, got {list(labels.keys())}")
        return [labels[name] for name in self.label_names]

    def _cache_key(self, label_values: List[str]) -> str:
        """Generate unique cache key for label set"""
        return f"{self.full_name}:{':'.join(label_values)}"

class DynamicGauge(DynamicMetric):
    """Dynamic gauge implementation with TTL support"""
    
    def collect(self):
        """Generate Prometheus metric samples"""
        fm = GaugeMetricFamily(
            self.full_name,
            self.config.description,
            labels=self.label_names
        )
        
        with self._metrics_lock:
            for label_values, value in self._values.items():
                fm.add_metric(list(label_values), value)
        
        yield fm

class DynamicHistogram(DynamicMetric):
    """Dynamic histogram implementation with configurable buckets"""
    
    def __init__(self, config: HistogramConfig):
        super().__init__(config)
        self.buckets = self._parse_buckets(config.buckets)

    def _parse_buckets(self, buckets: List[Union[float, str]]) -> List[float]:
        """Convert bucket specifications to numeric values"""
        parsed = []
        for b in buckets:
            if isinstance(b, str) and b.endswith("s"):
                parsed.append(time.time() + float(b[:-1]))
            else:
                parsed.append(float(b))
        return sorted(parsed)

    def collect(self):
        """Generate Prometheus histogram samples"""
        fm = HistogramMetricFamily(
            self.full_name,
            self.config.description,
            labels=self.label_names
        )
        
        with self._metrics_lock:
            for label_values, value in self._values.items():
                # Calculate bucket counts
                counts = [0] * (len(self.buckets) + 1)
                sum_value = 0.0
                for v in value:
                    sum_value += v
                    for i, upper_bound in enumerate(self.buckets):
                        if v <= upper_bound:
                            counts[i] += 1
                            break
                    else:
                        counts[-1] += 1
                
                # Add buckets
                buckets = list(zip(self.buckets, counts))
                fm.add_metric(
                    list(label_values),
                    buckets,
                    sum_value=sum_value
                )
        
        yield fm

# ======================
# Collector Registry
# ======================
class CollectorManager:
    """Enterprise-grade metrics collector manager"""
    
    def __init__(self):
        self.registry = CollectorRegistry(auto_describe=True)
        self._collectors: Dict[str, DynamicMetric] = {}
        self._lock = threading.RLock()
        self._health_status = Gauge(
            'kuva_ai_collector_health',
            'Collector health status (1=healthy, 0=unhealthy)',
            registry=self.registry
        )
        self._init_system_metrics()

    def _init_system_metrics(self):
        """Register system-level monitoring metrics"""
        self._scrape_counter = Counter(
            'kuva_ai_scrapes_total',
            'Total number of scrapes',
            registry=self.registry
        )
        self._error_counter = Counter(
            'kuva_ai_scrape_errors_total',
            'Total number of scrape errors',
            ['error_type'],
            registry=self.registry
        )

    def register_collector(self, config: MetricConfig) -> DynamicMetric:
        """Thread-safe collector registration"""
        with self._lock:
            if config.name in self._collectors:
                raise ValueError(f"Collector {config.name} already registered")
            
            collector_class = self._get_collector_class(config.type)
            collector = collector_class(config)
            
            def collect_fn():
                try:
                    self._scrape_counter.inc()
                    return collector.collect()
                except Exception as e:
                    self._error_counter.labels(error_type=type(e).__name__).inc()
                    raise
                
            self.registry.register(collect_fn)
            self._collectors[config.name] = collector
            return collector

    def _get_collector_class(self, metric_type: str) -> type:
        """Map configuration to collector implementation"""
        type_map = {
            'gauge': DynamicGauge,
            'histogram': DynamicHistogram,
            'counter': DynamicCounter,
            'summary': DynamicSummary
        }
        return type_map[metric_type]

    def start_server(self, port: int = 9090, addr: str = '0.0.0.0'):
        """Start metrics HTTP server with health checks"""
        start_http_server(port, addr, registry=self.registry)
        self._health_status.set(1)

    def generate_snapshot(self) -> bytes:
        """Generate metrics snapshot for external consumers"""
        return generate_latest(self.registry)

# ======================
# Decorators & Utilities
# ======================
def timed_metric(config: MetricConfig) -> Callable:
    """Decorator for automatic method timing"""
    def decorator(func: Callable) -> Callable:
        collector = CollectorManager().register_collector(config)
        
        @wraps(func)
        def wrapper(*args, **kwargs):
            start_time = time.time()
            result = func(*args, **kwargs)
            duration = time.time() - start_time
            
            labels = {
                'method': func.__name__,
                'module': inspect.getmodule(func).__name__
            }
            collector.update(duration, labels)
            return result
            
        return wrapper
    return decorator

# ======================
# Example Usage
# ======================
if __name__ == "__main__":
    # Initialize collector manager
    manager = CollectorManager()
    
    # Register custom metrics
    api_requests_config = HistogramConfig(
        name="api_requests_duration_seconds",
        description="API request processing duration",
        labels=["endpoint", "method", "status"],
        buckets=[0.1, 0.5, 1, 5, 10],
        subsystem="http"
    )
    api_histogram = manager.register_collector(api_requests_config)
    
    # Start metrics server
    manager.start_server(port=9090)
    
    # Example metric update
    api_histogram.update(
        0.45,
        labels={
            "endpoint": "/api/v1/agents",
            "method": "GET",
            "status": "200"
        }
    )
    
    # Keep service running
    while True:
        time.sleep(1)
