"""
KUVA AI Operator Framework (Kubernetes Operator Pattern Inspired)
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from functools import partial
from typing import Any, AsyncGenerator, Dict, List, Optional

import aiohttp
from pydantic import BaseModel, Field, ValidationError
from tenacity import (
    AsyncRetrying,
    RetryError,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from kuva.common import (
    CircuitBreaker,
    ExponentialBackoff,
    ObservabilityMixin,
    SecurityContext,
)
from kuva.core.agent import AgentSpec, AgentStatus, LifecycleManager

# ========================
# Data Models
# ========================
class OperatorSpec(BaseModel):
    reconciliation_interval: float = Field(5.0, ge=1.0)
    max_concurrent_reconciles: int = Field(5, ge=1)
    failure_threshold: int = Field(5, ge=1)
    leader_election: bool = True
    feature_gates: Dict[str, bool] = {
        "autoScaling": True,
        "selfHealing": True,
        "gracefulShutdown": True,
    }


class ResourceState(BaseModel):
    desired: Dict[str, Any] = {}
    current: Dict[str, Any] = {}
    last_operation: Optional[str] = None


# ========================
# Core Operator
# ========================
class Operator(ObservabilityMixin):
    def __init__(
        self,
        namespace: str,
        spec: OperatorSpec,
        security_ctx: SecurityContext,
    ):
        super().__init__()
        self.namespace = namespace
        self.spec = spec
        self.ctx = security_ctx
        self.lifecycle_mgr = LifecycleManager(security_ctx)
        self._state_store: Dict[str, ResourceState] = {}
        self._circuit_breaker = CircuitBreaker(
            failure_threshold=spec.failure_threshold,
            recovery_timeout=60,
        )
        self._backoff = ExponentialBackoff(
            initial=1,
            maximum=60,
            factor=2,
        )
        self._leader_lock = asyncio.Lock()
        self._reconcile_sem = asyncio.Semaphore(
            spec.max_concurrent_reconciles
        )

    @asynccontextmanager
    async def _leader_election(self) -> AsyncGenerator[None, None]:
        if not self.spec.leader_election:
            yield
            return

        async with self._leader_lock:
            try:
                async with aiohttp.ClientSession() as session:
                    # Implement distributed lock using Kubernetes API/etcd
                    async with session.post(
                        f"http://leader-election/namespaces/{self.namespace}/leases",
                        headers=self.ctx.auth_headers,
                        json={"ttl": 30},
                    ) as resp:
                        if resp.status == 201:
                            yield
                        else:
                            self.logger.warning("Leader election failed")
            except Exception as e:
                self.logger.error(f"Leadership acquisition error: {str(e)}")
                raise

    async def reconcile(self, resource_id: str) -> None:
        async with self._reconcile_sem:
            try:
                async for attempt in AsyncRetrying(
                    stop=stop_after_attempt(3),
                    wait=wait_exponential(),
                    retry=retry_if_exception_type(aiohttp.ClientError),
                ):
                    with attempt:
                        await self._perform_reconciliation(resource_id)
            except RetryError as e:
                self.logger.error(f"Reconcile failed for {resource_id}: {str(e)}")
                await self._handle_reconciliation_failure(resource_id)

    async def _perform_reconciliation(self, resource_id: str) -> None:
        state = self._state_store.get(resource_id, ResourceState())
        desired_spec = state.desired

        try:
            current_status = await self.lifecycle_mgr.get_status(resource_id)
            state.current = current_status.dict()

            if current_status.state != desired_spec.get("targetState"):
                await self._apply_state_change(resource_id, desired_spec)

            await self._update_resource_state(resource_id, state)
            await self.record_metric("reconcile_success_total", tags={"resource": resource_id})
        except ValidationError as e:
            self.logger.error(f"State validation failed: {str(e)}")
            await self.record_metric("reconcile_validation_errors_total")
        except aiohttp.ClientError as e:
            self.logger.warning(f"Network error during reconcile: {str(e)}")
            raise
        except Exception as e:
            self.logger.exception("Critical reconciliation failure")
            await self._circuit_breaker.trigger()
            raise

    async def _apply_state_change(self, resource_id: str, spec: Dict[str, Any]) -> None:
        async with self.lifecycle_mgr.managed_operation(resource_id):
            target_state = spec["targetState"]
            agent_spec = AgentSpec.parse_obj(spec)

            if target_state == "RUNNING":
                await self.lifecycle_mgr.start_agent(resource_id, agent_spec)
            elif target_state == "STOPPED":
                await self.lifecycle_mgr.stop_agent(resource_id)
            elif target_state == "TERMINATED":
                await self.lifecycle_mgr.terminate_agent(resource_id)
            else:
                raise ValueError(f"Invalid target state: {target_state}")

    async def _update_resource_state(self, resource_id: str, state: ResourceState) -> None:
        self._state_store[resource_id] = state
        await self.record_metric(
            "resource_state_version",
            value=time.monotonic(),
            tags={"resource": resource_id},
        )

    async def _handle_reconciliation_failure(self, resource_id: str) -> None:
        await self.record_metric("reconcile_failure_total", tags={"resource": resource_id})
        if self.spec.feature_gates["selfHealing"]:
            await self.lifecycle_mgr.restart_agent(resource_id)

    async def run_forever(self) -> None:
        async with self._leader_election():
            self.logger.info("Starting operator control loop")
            while True:
                try:
                    async with self._circuit_breaker.protect():
                        await self._reconciliation_loop()
                        await asyncio.sleep(self.spec.reconciliation_interval)
                except Exception as e:
                    self.logger.critical(f"Control loop failure: {str(e)}")
                    await self._backoff.wait()
                    continue

    async def _reconciliation_loop(self) -> None:
        resource_ids = await self._list_managed_resources()
        tasks = [self.reconcile(rid) for rid in resource_ids]
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _list_managed_resources(self) -> List[str]:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"http://registry/namespaces/{self.namespace}/agents",
                    headers=self.ctx.auth_headers,
                ) as resp:
                    data = await resp.json()
                    return [item["id"] for item in data["items"]]
        except Exception as e:
            self.logger.error(f"Resource discovery failed: {str(e)}")
            return []

# ========================
# Enterprise Extensions
# ========================
class OperatorMetrics:
    RECONCILE_SUCCESS_TOTAL = "reconcile_success_total"
    RECONCILE_FAILURE_TOTAL = "reconcile_failure_total"
    RESOURCE_STATE_VERSION = "resource_state_version"

class OperatorAPI:
    def __init__(self, operator: Operator):
        self.operator = operator

    async def get_resource_state(self, resource_id: str) -> ResourceState:
        return self.operator._state_store.get(resource_id, ResourceState())

    async def trigger_reconciliation(self, resource_id: str) -> None:
        await self.operator.reconcile(resource_id)

    async def get_operator_health(self) -> Dict[str, Any]:
        return {
            "circuit_breaker": self.operator._circuit_breaker.state,
            "active_reconciles": self.operator._reconcile_sem._value,
            "leader_status": "Leader" if self.operator.spec.leader_election else "Standalone",
        }
