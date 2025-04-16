"""
KUVA AI Reinforcement Learning Policy Networks
"""
from __future__ import annotations

import abc
import logging
import os
from typing import Any, Dict, List, Optional, Tuple, Type, Union

import numpy as np
import torch
import torch.distributed as dist
import torch.nn as nn
import torch.optim as optim
from torch.distributed.rpc import RRef, remote
from torch.nn.parallel import DistributedDataParallel as DDP
from torch.utils.tensorboard import SummaryWriter

from kuva.common.metrics import RLMetricsCollector
from kuva.common.registry import ComponentRegistry
from kuva.common.types import ActionSpace, ObservationSpace
from kuva.reinforcement.buffers import PrioritizedExperienceReplay
from kuva.reinforcement.types import AgentID, Batch, Device, PolicyState

logger = logging.getLogger(__name__)

class BasePolicyNetwork(abc.ABC, nn.Module):
    """Abstract base class for all policy networks"""
    
    _registry = ComponentRegistry("policy_networks")
    
    def __init__(
        self,
        observation_space: ObservationSpace,
        action_space: ActionSpace,
        config: Dict[str, Any],
        device: Device = "cpu",
        distributed: bool = False,
        **kwargs,
    ) -> None:
        super().__init__()
        self.observation_space = observation_space
        self.action_space = action_space
        self.config = config
        self.device = torch.device(device)
        self.distributed = distributed
        self.iter = 0
        
        # Initialize core components
        self._build_network()
        self._configure_optimizer()
        self._configure_loss()
        
        # Distributed training setup
        if self.distributed:
            self._init_distributed()
            
        # Monitoring
        self.writer = SummaryWriter(log_dir=os.getenv("KUVA_LOG_DIR", "./logs"))
        self.metrics = RLMetricsCollector()
        
        # Move to device
        self.to(self.device)
    
    @abc.abstractmethod
    def _build_network(self) -> None:
        """Construct network architecture"""
        raise NotImplementedError
        
    @abc.abstractmethod
    def forward(
        self,
        observations: torch.Tensor,
        hidden_state: Optional[torch.Tensor] = None,
    ) -> Tuple[torch.Tensor, ...]:
        """Forward pass with optional hidden state"""
        raise NotImplementedError
        
    @abc.abstractmethod
    def compute_loss(
        self,
        batch: Batch,
        **kwargs,
    ) -> Tuple[torch.Tensor, Dict[str, float]]:
        """Calculate policy loss and metrics"""
        raise NotImplementedError
        
    def _configure_optimizer(self) -> None:
        """Initialize optimizer with configurable parameters"""
        optimizer_cls = getattr(optim, self.config.get("optimizer", "Adam"))
        self.optimizer = optimizer_cls(
            self.parameters(),
            lr=self.config.get("learning_rate", 3e-4),
            **self.config.get("optimizer_kwargs", {}),
        )
        
    def _configure_loss(self) -> None:
        """Configure loss functions with automatic scaling"""
        self.value_loss_fn = nn.MSELoss()
        self.entropy_coeff = self.config.get("entropy_coeff", 0.01)
        
    def _init_distributed(self) -> None:
        """Initialize distributed training components"""
        if not dist.is_initialized():
            raise RuntimeError("Distributed training not initialized")
            
        self.model = DDP(self)
        self.param_server = RRef(self)
        
    def sync_parameters(self) -> None:
        """Synchronize parameters across distributed nodes"""
        if self.distributed:
            for param in self.parameters():
                dist.broadcast(param.data, src=0)
                
    def save_checkpoint(self, path: str) -> None:
        """Save policy state to checkpoint"""
        state_dict = {
            "model": self.state_dict(),
            "optimizer": self.optimizer.state_dict(),
            "iter": self.iter,
        }
        torch.save(state_dict, path)
        logger.info(f"Checkpoint saved to {path}")
        
    def load_checkpoint(self, path: str) -> None:
        """Load policy state from checkpoint"""
        state_dict = torch.load(path, map_location=self.device)
        self.load_state_dict(state_dict["model"])
        self.optimizer.load_state_dict(state_dict["optimizer"])
        self.iter = state_dict.get("iter", 0)
        logger.info(f"Checkpoint loaded from {path}")
        
    def update(
        self,
        replay_buffer: PrioritizedExperienceReplay,
        **kwargs,
    ) -> Dict[str, float]:
        """Perform policy update from experience"""
        self.train()
        batch = replay_buffer.sample(
            self.config["batch_size"],
            beta=self.config.get("per_beta", 0.4),
        )
        
        # Compute loss and metrics
        total_loss, metrics = self.compute_loss(batch)
        
        # Backpropagation
        self.optimizer.zero_grad()
        total_loss.backward()
        
        # Gradient clipping
        if self.config.get("grad_clip"):
            nn.utils.clip_grad_norm_(
                self.parameters(),
                self.config["grad_clip"],
            )
            
        self.optimizer.step()
        self.iter += 1
        
        # Update priorities
        if replay_buffer.prioritized:
            replay_buffer.update_priorities(
                batch.indices,
                metrics["td_errors"].detach().cpu().numpy(),
            )
            
        # Log metrics
        self._log_metrics(metrics)
        return metrics
        
    def _log_metrics(self, metrics: Dict[str, float]) -> None:
        """Log training metrics to multiple backends"""
        step = self.iter
        
        # TensorBoard logging
        for key, value in metrics.items():
            self.writer.add_scalar(f"train/{key}", value, step)
            
        # Metric collector
        self.metrics.record(metrics)
        
        # Distributed logging
        if self.distributed and dist.get_rank() == 0:
            logger.info(
                f"Iter {step} | "
                f"Loss: {metrics['total_loss']:.3f} | "
                f"Return: {metrics['mean_return']:.1f}"
            )
            
    def get_state(self) -> PolicyState:
        """Get current policy state for deployment"""
        return PolicyState(
            model_state=self.state_dict(),
            config=self.config,
            metrics=self.metrics.summary(),
        )
        
    @classmethod
    def create(
        cls,
        policy_type: str,
        **kwargs,
    ) -> BasePolicyNetwork:
        """Factory method for policy network creation"""
        return cls._registry.create(policy_type, **kwargs)

@BasePolicyNetwork._registry.register("ppo")
class PPOPolicy(BasePolicyNetwork):
    """Proximal Policy Optimization implementation"""
    
    def _build_network(self) -> None:
        # Actor-Critic architecture
        self.actor = nn.Sequential(
            nn.Linear(self.observation_space.shape[0], 256),
            nn.ReLU(),
            nn.Linear(256, 256),
            nn.ReLU(),
            nn.Linear(256, self.action_space.n),
        )
        
        self.critic = nn.Sequential(
            nn.Linear(self.observation_space.shape[0], 256),
            nn.ReLU(),
            nn.Linear(256, 256),
            nn.ReLU(),
            nn.Linear(256, 1),
        )
        
        # Policy parameters
        self.clip_epsilon = self.config.get("clip_epsilon", 0.2)
        self.value_coeff = self.config.get("value_coeff", 0.5)
        
    def forward(
        self,
        observations: torch.Tensor,
        hidden_state: Optional[torch.Tensor] = None,
    ) -> Tuple[torch.Tensor, ...]:
        logits = self.actor(observations)
        values = self.critic(observations)
        return logits, values
        
    def compute_loss(
        self,
        batch: Batch,
        **kwargs,
    ) -> Tuple[torch.Tensor, Dict[str, float]]:
        # Unpack batch
        obs, actions, old_log_probs, returns, advantages, masks = batch
        
        # Forward pass
        new_logits, values = self(obs)
        dist = torch.distributions.Categorical(logits=new_logits)
        new_log_probs = dist.log_prob(actions)
        entropy = dist.entropy().mean()
        
        # Policy loss
        ratio = (new_log_probs - old_log_probs).exp()
        clipped_ratio = torch.clamp(ratio, 1 - self.clip_epsilon, 1 + self.clip_epsilon)
        policy_loss = -torch.min(ratio * advantages, clipped_ratio * advantages).mean()
        
        # Value loss
        value_loss = self.value_loss_fn(values.squeeze(-1), returns)
        
        # Entropy bonus
        entropy_loss = -self.entropy_coeff * entropy
        
        # Total loss
        total_loss = policy_loss + self.value_coeff * value_loss + entropy_loss
        
        # Metrics
        metrics = {
            "total_loss": total_loss.item(),
            "policy_loss": policy_loss.item(),
            "value_loss": value_loss.item(),
            "entropy": entropy.item(),
            "approx_kl": (old_log_probs - new_log_probs).mean().item(),
            "clip_frac": ((ratio < (1 - self.clip_epsilon)) | 
                         (ratio > (1 + self.clip_epsilon))).float().mean().item(),
            "mean_return": returns.mean().item(),
            "td_errors": (returns - values.squeeze(-1)).abs(),
        }
        
        return total_loss, metrics

@BasePolicyNetwork._registry.register("a3c")
class A3CPolicy(PPOPolicy):
    """Asynchronous Advantage Actor-Critic implementation"""
    
    def _build_network(self) -> None:
        super()._build_network()
        self.lstm = nn.LSTM(
            input_size=256,
            hidden_size=256,
            batch_first=True,
        )
        
    def forward(
        self,
        observations: torch.Tensor,
        hidden_state: Optional[Tuple[torch.Tensor, torch.Tensor]] = None,
    ) -> Tuple[torch.Tensor, ...]:
        features = self._base_network(observations)
        
        if hidden_state is None:
            lstm_out, new_hidden = self.lstm(features.unsqueeze(1))
        else:
            lstm_out, new_hidden = self.lstm(
                features.unsqueeze(1),
                (hidden_state[0].contiguous(), hidden_state[1].contiguous()),
            )
            
        logits = self.actor(lstm_out.squeeze(1))
        values = self.critic(lstm_out.squeeze(1))
        return logits, values, new_hidden

# Example usage
if __name__ == "__main__":
    # Configuration
    config = {
        "policy_type": "ppo",
        "learning_rate": 3e-4,
        "batch_size": 256,
        "clip_epsilon": 0.2,
        "entropy_coeff": 0.01,
        "grad_clip": 0.5,
    }
    
    # Create policy network
    policy = BasePolicyNetwork.create(
        policy_type="ppo",
        observation_space=ObservationSpace(shape=(8,)),
        action_space=ActionSpace(n=4),
        config=config,
        device="cuda" if torch.cuda.is_available() else "cpu",
    )
    
    # Training loop example
    from kuva.reinforcement.buffers import PrioritizedExperienceReplay
    buffer = PrioritizedExperienceReplay(capacity=10000)
    
    for _ in range(1000):
        metrics = policy.update(buffer)
