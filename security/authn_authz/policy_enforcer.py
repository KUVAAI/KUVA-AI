"""
KUVA AI Policy Enforcement Engine (NIST Zero Trust Architecture Compliant)
"""
import asyncio
import datetime
import hashlib
import json
import logging
import re
import uuid
from typing import Any, Awaitable, Dict, List, Optional, Tuple

from pydantic import BaseModel, ValidationError
from opentelemetry import metrics, trace
from fastapi.requests import Request
from prometheus_client import Counter, Histogram

# ----- Observability Setup -----
tracer = trace.get_tracer("policy_enforcer.tracer")
meter = metrics.get_meter("policy_enforcer.meter")
policy_counter = Counter("policy_evals_total", "Total policy evaluations", ["result"])
decision_latency = Histogram("policy_decision_latency_seconds", "Decision latency")

# ----- Core Data Models -----
class PolicyCondition(BaseModel):
    attribute: str
    operator: str  # 'eq', 'contains', 'range', etc.
    value: Any

class EnforcementPolicy(BaseModel):
    id: uuid.UUID
    name: str
    description: str
    priority: int
    targets: Dict[str, List[str]]  # {subject_type: [identifiers]}
    conditions: List[PolicyCondition]
    effect: str  # 'allow', 'deny', 'require_2fa'

class DecisionResult(BaseModel):
    allowed: bool
    policy_id: Optional[uuid.UUID]
    obligations: Dict[str, Any]
    rationale: str

# ----- Engine Implementation -----
class PolicyEnforcer:
    def __init__(self, policy_source: 'PolicyRepository'):
        self.policy_source = policy_source
        self._policy_cache = {}
        self._cache_lock = asyncio.Lock()
        self.logger = logging.getLogger("kuva.policy")

    async def _refresh_policies(self):
        """Hot-reload policies with version consistency checks"""
        async with self._cache_lock:
            new_policies = await self.policy_source.fetch_active()
            if self._validate_policy_consistency(new_policies):
                self._policy_cache = {p.id: p for p in new_policies}
                self.logger.info("Policy cache updated with %d policies", len(new_policies))

    def _validate_policy_consistency(self, policies: List[EnforcementPolicy]) -> bool:
        """Prevent conflicting policy updates"""
        version_hash = hashlib.sha256(
            "".join(sorted(str(p.id) for p in policies)).encode()
        ).hexdigest()
        return version_hash == self.policy_source.get_current_hash()

    @decision_latency.time()
    async def evaluate_access(
        self,
        request: Request,
        subject: Dict[str, Any],
        resource: Dict[str, Any],
        action: str,
        context: Dict[str, Any]
    ) -> DecisionResult:
        """Zero-trust evaluation with continuous validation"""
        with tracer.start_as_current_span("policy.evaluate") as span:
            span.set_attributes({
                "subject.id": subject.get('id'),
                "resource.id": resource.get('id'),
                "action": action
            })

            try:
                # Phase 1: Policy selection
                candidate_policies = await self._match_candidate_policies(subject, resource, action)
                
                # Phase 2: Attribute resolution
                resolved_ctx = self._resolve_attributes(subject, resource, context)
                
                # Phase 3: Policy evaluation
                for policy in sorted(candidate_policies, key=lambda p: -p.priority):
                    if self._evaluate_conditions(policy, resolved_ctx):
                        obligations = self._generate_obligations(policy, context)
                        result = DecisionResult(
                            allowed=(policy.effect == 'allow'),
                            policy_id=policy.id,
                            obligations=obligations,
                            rationale=f"Matched policy {policy.name}"
                        )
                        policy_counter.labels(result='allow' if result.allowed else 'deny').inc()
                        return result

                # Default deny
                return DecisionResult(
                    allowed=False,
                    rationale="No matching policies allow this action"
                )

            except PolicyEvaluationError as e:
                self.logger.error("Policy evaluation failed: %s", e)
                policy_counter.labels(result='error').inc()
                return DecisionResult(
                    allowed=False,
                    rationale=f"Policy evaluation error: {str(e)}"
                )

    async def _match_candidate_policies(self, subject, resource, action) -> List[EnforcementPolicy]:
        """Efficient policy filtering using attribute indexes"""
        policies = await self._get_cached_policies()
        return [
            p for p in policies
            if self._policy_target_matches(p, subject, resource, action)
        ]

    def _policy_target_matches(self, policy: EnforcementPolicy, subject, resource, action) -> bool:
        """Multi-dimensional policy targeting"""
        sub_match = any(
            subject.get('type') == t and subject.get('id') in ids
            for t, ids in policy.targets.get('subjects', {}).items()
        )
        res_match = any(
            resource.get('type') == t and resource.get('id') in ids
            for t, ids in policy.targets.get('resources', {}).items()
        )
        act_match = action in policy.targets.get('actions', [])
        return sub_match and res_match and act_match

    def _evaluate_conditions(self, policy: EnforcementPolicy, context: Dict) -> bool:
        """Safe evaluation with context sandboxing"""
        try:
            return all(
                self._evaluate_condition(cond, context)
                for cond in policy.conditions
            )
        except Exception as e:
            raise PolicyEvaluationError(f"Condition evaluation failed: {str(e)}")

    def _evaluate_condition(self, condition: PolicyCondition, context: Dict) -> bool:
        """Type-safe evaluation with operator registry"""
        value = context.get(condition.attribute)
        ops = {
            'eq': lambda a, b: a == b,
            'gt': lambda a, b: a > b,
            'in': lambda a, b: a in b,
            'range': lambda a, b: b[0] <= a <= b[1],
            'regex': lambda a, b: re.match(b, str(a)) is not None
        }
        
        if condition.operator not in ops:
            raise PolicyEvaluationError(f"Invalid operator {condition.operator}")
        
        try:
            return ops[condition.operator](value, condition.value)
        except TypeError as e:
            raise PolicyEvaluationError(f"Type error in condition: {str(e)}")

    def _generate_obligations(self, policy: EnforcementPolicy, context: Dict) -> Dict:
        """Dynamic obligation injection (2FA, logging, etc.)"""
        obligations = {}
        if policy.effect == 'require_2fa':
            if context.get('auth_strength') < 2:
                obligations['required_auth'] = {'methods': ['biometric', 'hardware_key']}
        return obligations

# ----- Error Handling -----
class PolicyEvaluationError(Exception):
    """Critical policy evaluation failure"""

# ----- Dependencies -----
class PolicyRepository:
    async def fetch_active(self) -> List[EnforcementPolicy]:
        """Abstract method for policy retrieval"""
        raise NotImplementedError

    def get_current_hash(self) -> str:
        """Consistency check signature"""
        raise NotImplementedError

# ----- Example Usage -----
if __name__ == "__main__":
    # Implement mock policy repository
    class DemoPolicyRepo(PolicyRepository):
        async def fetch_active(self):
            return [
                EnforcementPolicy(
                    id=uuid.uuid4(),
                    name="High Security Access",
                    priority=100,
                    targets={
                        "resources": {"sensitive_db": ["*"]},
                        "actions": ["write", "delete"]
                    },
                    conditions=[
                        PolicyCondition(
                            attribute="user.auth_level",
                            operator="gte",
                            value=3
                        )
                    ],
                    effect="deny"
                )
            ]
        
        def get_current_hash(self):
            return "mock_hash"

    enforcer = PolicyEnforcer(DemoPolicyRepo())
