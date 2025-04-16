"""
KUVA AI Reward Calculation Engine
"""
from __future__ import annotations

import abc
import logging
import math
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple, Union

import numpy as np
import torch
import torch.distributed as dist
from pydantic import BaseModel, ValidationError, validator
from pydantic.types import confloat

from kuva.common.metrics import RewardMetricsCollector
from kuva.common.types import AgentID, RewardConfig, RewardType
from kuva.common.utils import validate_config
from kuva.reinforcement.types import EnvironmentStep

logger = logging.getLogger(__name__)

class RewardRule(BaseModel):
    """Validation model for reward rule configuration"""
    name: str
    weight: confloat(ge=0.0, le=1.0) = 1.0
    decay_rate: Optional[float] = None
    requires: Optional[List[str]] = None
    params: Dict[str, Any] = {}

    @validator('requires')
    def validate_requirements(cls, v):
        return v or []

class BaseRewardCalculator(abc.ABC):
    """Abstract base class for reward calculation strategies"""
    
    _registry = defaultdict(dict)
    
    def __init__(
        self,
        agent_ids: List[AgentID],
        config: RewardConfig,
        distributed: bool = False,
        **kwargs,
    ) -> None:
        self.agent_ids = agent_ids
        self.config = validate_config(config, RewardConfig)
        self.distributed = distributed
        self.timestep = 0
        
        # Initialize components
        self._init_rules()
        self._init_metrics()
        self._init_distributed()
        
    def _init_rules(self) -> None:
        """Load and validate reward rules from config"""
        self.active_rules = {}
        for rule_config in self.config.rules:
            try:
                rule = RewardRule(**rule_config)
                self.active_rules[rule.name] = rule
            except ValidationError as e:
                logger.error(f"Invalid reward rule config: {e}")
                raise

    def _init_metrics(self) -> None:
        """Initialize metrics collection system"""
        self.metrics = RewardMetricsCollector()
        self.rule_weights = {
            rule.name: rule.weight for rule in self.active_rules.values()
        }

    def _init_distributed(self) -> None:
        """Set up distributed computation backend"""
        if self.distributed:
            self.comm = dist.Backend.NCCL if torch.cuda.is_available() else dist.Backend.GLOO
            self.group = dist.group.WORLD
            
    @abc.abstractmethod
    def compute(
        self,
        step_data: EnvironmentStep,
        **kwargs,
    ) -> Dict[AgentID, float]:
        """Main interface for reward computation"""
        raise NotImplementedError
        
    def apply_decay(self) -> None:
        """Apply temporal decay to rule weights"""
        self.timestep += 1
        for rule in self.active_rules.values():
            if rule.decay_rate:
                decay_factor = math.exp(-rule.decay_rate * self.timestep)
                self.rule_weights[rule.name] = rule.weight * decay_factor
                
    def synchronize_weights(self) -> None:
        """Sync rule weights across distributed nodes"""
        if self.distributed:
            weight_tensor = torch.tensor(
                list(self.rule_weights.values()),
                dtype=torch.float32,
            ).to(self._device)
            dist.all_reduce(weight_tensor, op=dist.ReduceOp.AVG, group=self.group)
            self.rule_weights = dict(zip(self.rule_weights.keys(), weight_tensor.tolist()))
                
    def record_metrics(
        self,
        rewards: Dict[AgentID, float],
        step_data: EnvironmentStep,
    ) -> None:
        """Capture detailed reward metrics"""
        self.metrics.record({
            "timestep": self.timestep,
            **{f"reward_{aid}": rwd for aid, rwd in rewards.items()},
            **self._extract_rule_metrics(step_data),
        })
        
    def _extract_rule_metrics(
        self,
        step_data: EnvironmentStep,
    ) -> Dict[str, float]:
        """Extract rule-specific metrics from environment data"""
        metrics = {}
        for rule in self.active_rules.values():
            if hasattr(step_data.metadata, rule.name):
                metrics[f"rule_{rule.name}"] = getattr(
                    step_data.metadata,
                    rule.name,
                )
        return metrics

    @classmethod
    def register_rule(
        cls,
        rule_type: str,
        implementation: Any,
    ) -> None:
        """Decorator for registering custom reward rules"""
        def decorator(func):
            cls._registry[rule_type][func.__name__] = func
            return func
        return decorator

@BaseRewardCalculator.register_rule("cooperative")
class CooperativeRewardCalculator(BaseRewardCalculator):
    """Team-based reward distribution with shared incentives"""
    
    def compute(
        self,
        step_data: EnvironmentStep,
        **kwargs,
    ) -> Dict[AgentID, float]:
        team_rewards = defaultdict(float)
        
        # Apply all active reward rules
        for rule_name, rule in self.active_rules.items():
            rule_func = getattr(self, f"_compute_{rule_name}", None)
            if rule_func and self._check_requirements(rule, step_data):
                reward = rule_func(step_data, **rule.params)
                team_rewards[rule_name] += reward * self.rule_weights[rule_name]
                
        # Normalize and distribute
        total_reward = sum(team_rewards.values())
        return self._distribute_rewards(total_reward)
    
    def _distribute_rewards(self, total: float) -> Dict[AgentID, float]:
        """Distribute team reward to individual agents"""
        if self.config.distribution == "equal":
            return {aid: total / len(self.agent_ids) for aid in self.agent_ids}
        elif self.config.distribution == "performance":
            # Implement performance-based distribution logic
            pass
        return {aid: 0.0 for aid in self.agent_ids}
    
    def _check_requirements(
        self,
        rule: RewardRule,
        step_data: EnvironmentStep,
    ) -> bool:
        """Verify rule prerequisites are met"""
        return all(
            hasattr(step_data.metadata, req)
            for req in (rule.requires or [])
        )

@BaseRewardCalculator.register_rule("competitive")
class CompetitiveRewardCalculator(BaseRewardCalculator):
    """Zero-sum reward distribution with ranking support"""
    
    def compute(
        self,
        step_data: EnvironmentStep,
        **kwargs,
    ) -> Dict[AgentID, float]:
        agent_rewards = {aid: 0.0 for aid in self.agent_ids}
        
        for rule_name, rule in self.active_rules.items():
            rule_func = getattr(self, f"_compute_{rule_name}", None)
            if rule_func and self._check_requirements(rule, step_data):
                rewards = rule_func(step_data, **rule.params)
                for aid, rwd in rewards.items():
                    agent_rewards[aid] += rwd * self.rule_weights[rule_name]
                    
        # Apply zero-sum normalization
        if self.config.zero_sum:
            total = sum(agent_rewards.values())
            mean = total / len(agent_rewards)
            return {aid: rwd - mean for aid, rwd in agent_rewards.items()}
        return agent_rewards

# Example Custom Reward Rules ##################################################

@CooperativeRewardCalculator.register_rule("sparse_success")
def _compute_sparse_success(
    self,
    step_data: EnvironmentStep,
    success_key: str = "success",
    bonus: float = 10.0,
    **kwargs,
) -> float:
    """Sparse binary success reward"""
    return bonus if getattr(step_data.metadata, success_key, False) else 0.0

@CooperativeRewardCalculator.register_rule("distance_penalty")
def _compute_distance_penalty(
    self,
    step_data: EnvironmentStep,
    target_key: str,
    position_key: str = "position",
    scale: float = 0.1,
    **kwargs,
) -> float:
    """Continuous penalty based on distance to target"""
    target = np.array(getattr(step_data.metadata, target_key))
    positions = np.array([obs.agent_state[position_key] for obs in step_data.observations])
    distances = np.linalg.norm(positions - target, axis=1)
    return -scale * np.mean(distances)

# Example Usage ################################################################

if __name__ == "__main__":
    # Configuration
    config = {
        "type": "cooperative",
        "rules": [
            {
                "name": "sparse_success",
                "weight": 0.8,
                "params": {"success_key": "goal_reached"},
            },
            {
                "name": "distance_penalty",
                "weight": 0.2,
                "decay_rate": 0.01,
                "requires": ["target_position"],
                "params": {"target_key": "target_position"},
            },
        ],
        "distribution": "equal",
    }
    
    # Initialize calculator
    agents = ["agent1", "agent2", "agent3"]
    calculator = CooperativeRewardCalculator(
        agent_ids=agents,
        config=RewardConfig(**config),
    )
    
    # Simulated environment step
    step_data = EnvironmentStep(
        observations=[...],
        rewards={},
        dones={},
        metadata={
            "goal_reached": True,
            "target_position": np.array([1.0, 2.0]),
        },
    )
    
    # Compute rewards
    rewards = calculator.compute(step_data)
    print(f"Computed rewards: {rewards}")
    
    # Update rule weights
    calculator.apply_decay()
    calculator.synchronize_weights()
