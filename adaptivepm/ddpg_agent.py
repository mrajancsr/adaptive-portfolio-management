from dataclasses import dataclass, field
from typing import List, Optional, Tuple

import torch
import torch.nn as nn
import torch_snippets as ts
from torch.utils.data import DataLoader

from adaptivepm.dataset import (
    KrakenDataSet,
    SlidingWindowBatchSampler,
    get_current_and_next_batch,
)
from adaptivepm.memory import ExperienceReplayMemory, PortfolioVectorMemory
from adaptivepm.models import Actor, Critic
from adaptivepm.portfolio import Portfolio

torch.set_default_device("mps")


@dataclass
class DDPGAgent:
    """Implementation of Deep Deterministic Policy Gradient for the Agent"""

    portfolio: Portfolio
    batch_size: int
    window_size: int
    step_size: int
    n_epochs: int
    learning_rate: Optional[float] = 3e-5
    betas: Optional[Tuple[float, float]] = (0.0, 0.9)
    device: Optional[str] = "mps"
    actor: Actor = field(init=False)
    critic: Critic = field(init=False)
    target_actor: nn.Module = field(init=False)
    target_critic: nn.Module = field(init=False)
    actor_optimizer: torch.optim = field(init=False)
    critic_optimizer: torch.optim = field(init=False)
    loss_fn: nn.modules.loss.MSELoss = field(init=False)
    dataloader: DataLoader = field(init=False)
    pvm: PortfolioVectorMemory = field(init=False)
    replay_memory: ExperienceReplayMemory = field(init=False)
    gamma: float = 0.99
    tau: float = 0.005

    def __post_init__(self):
        kraken_ds = KrakenDataSet(self.portfolio, self.window_size, self.step_size)
        batch_sampler = SlidingWindowBatchSampler(
            kraken_ds, batch_size=self.batch_size, step_size=self.step_size
        )
        self.dataloader = DataLoader(
            kraken_ds,
            batch_size=1,
            batch_sampler=batch_sampler,
            pin_memory=True,
            generator=torch.Generator(device=self.device),
        )
        m_noncash_assets = self.portfolio.m_noncash_assets
        self.actor = Actor(3, m_noncash_assets)
        self.critic = Critic(3, m_noncash_assets)
        self.target_actor = self.clone_network(self.actor)
        self.target_critic = self.clone_network(self.critic)
        self.actor_optimizer = torch.optim.Adam(
            self.actor.parameters(), lr=self.learning_rate, betas=self.betas
        )
        self.critic_optimizer = torch.optim.Adam(
            self.critic.parameters(), lr=self.learning_rate, betas=self.betas
        )
        self.actor.to(self.device)
        self.critic.to(self.device)
        self.target_actor.to(self.device)
        self.target_critic.to(self.device)
        self.loss_fn = nn.MSELoss()
        self.pvm = PortfolioVectorMemory(
            self.portfolio.n_samples, self.portfolio.m_noncash_assets
        )
        self.replay_memory = ExperienceReplayMemory()
        self.update_target_networks()

    def clone_network(self, network):
        cloned_network = type(network)()
        cloned_network.load_state_dict(network.state_dict())
        return cloned_network

    def select_action(self, state):
        """Select action using the actor's policy (deterministic action)"""
        self.actor.eval()  # Ensure the actor is in evaluation mode
        with torch.no_grad():
            action = self.actor(state)
        self.actor.train()
        return action

    def update_target_networks(self):
        self.soft_update(self.target_actor, self.actor, self.tau)
        self.soft_update(self.target_critic, self.critic, self.tau)

    def soft_update(
        self, target_network: nn.Module, main_network: nn.Module, tau: float
    ):
        """_summary_

        Parameters
        ----------
        target_network : nn.Module
            _description_
        main_network : nn.Module
            _description_
        tau : float
            _description_
        """
        for target_param, main_param in zip(
            target_network.parameters(), main_network.parameters()
        ):
            target_param.data.copy_(
                tau * main_param.data + (1.0 - tau) * target_param.data
            )

    def train_actor(self, price_tensor: torch.tensor, prev_index: torch.tensor):
        previous_weights = self.pvm.get_memory_stack(prev_index)
        state = (price_tensor, previous_weights)
        actions = self.actor(state)

        # compute the actor loss using deterministic policy gradient
        actor_loss = -self.critic(state, actions).mean()

        self.pvm.update_memory_stack(actions.detach(), prev_index + 1)

        # perform backprop
        self.actor_optimizer.zero_grad()
        actor_loss.backward()
        self.actor_optimizer.step()
        return actor_loss.item()

    def train_critic(self, state, action, reward, next_state):
        """Train the critic network by minimizing the loss based on TD Error

        Parameters
        ----------
        state : _type_
            _description_
        action : _type_
            _description_
        reward : _type_
            _description_
        next_state : _type_
            _description_
        done : function
            _description_
        """
        # get predicted q values from current batch
        predicted_q_values = self.critic(state, action)

        with torch.no_grad():
            # get the next q values using the target critic and next state (from target network)
            next_action = self.target_actor(next_state)
            next_q_values = self.target_critic(next_state, next_action)

            # calculate target q values using bellman equation
            target_q_values = reward + self.gamma * next_q_values

        # compute the critic loss using MSE between predicted Q-values and target Q-values
        # Hence we are minimizing the TD Error
        critic_loss = self.loss_fn(predicted_q_values, target_q_values)
        self.critic_optimizer.zero_grad()
        critic_loss.backward()
        self.critic_optimizer.step()
        return critic_loss.item()

    def train(self):
        log = ts.Report(self.n_epochs)
        n = len(self.dataloader)

        for epoch in range(self.n_epochs):
            for idx, (xt, xt_next, prev_index) in enumerate(
                get_current_and_next_batch(self.dataloader)
            ):
                # get the previous weights from portfolio vector memory
                previous_action = self.pvm.get_memory_stack(prev_index)
                state = (xt, previous_action)

                # get current weight from actor network given s = (Xt, wt_prev)
                action = self.select_action(state)
                # store the current action back into pvm
                self.pvm.update_memory_stack(action.detach(), prev_index + 1)
                # get the relative price vector from price tensor
                yt = 1 / xt[:, 0, :, -2]
                reward = self.portfolio.get_reward(action, yt, previous_action)
                next_state = (xt_next, action)
                self.replay_memory.add(
                    state, action, reward, next_state, self.batch_size
                )
                state_batch, action_batch, reward_batch, next_state_batch = (
                    self.replay_memory.sample(self.batch_size)
                )
                # train the critic
                critic_loss = self.train_critic(
                    state_batch, action_batch, reward_batch, next_state_batch
                )

                # train the actor
                actor_loss = self.train_actor(
                    state_batch, action_batch, reward_batch, next_state_batch
                )
                self.update_target_networks()
                pos = epoch + (1 + idx) / n
                log.record(
                    pos,
                    critic_loss=critic_loss.detach(),
                    actor_loss=actor_loss.detach(),
                    end="\r",
                )
            log.report_avgs(epoch + 1)
        log.plot_epochs(["critic_loss", "actor_loss"])
