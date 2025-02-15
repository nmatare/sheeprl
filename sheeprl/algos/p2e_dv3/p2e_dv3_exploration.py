import copy
import os
import warnings
from typing import Any, Dict, Sequence

import gymnasium as gym
import hydra
import numpy as np
import torch
import torch.nn.functional as F
from lightning.fabric import Fabric
from lightning.fabric.wrappers import _FabricModule, _FabricOptimizer
from lightning.pytorch.utilities.seed import isolate_rng
from omegaconf import DictConfig
from tensordict import TensorDict
from tensordict.tensordict import TensorDictBase
from torch import Tensor, nn
from torch.distributions import Bernoulli, Distribution, Independent
from torch.utils.data import BatchSampler
from torchmetrics import SumMetric

from sheeprl.algos.dreamer_v3.agent import PlayerDV3, WorldModel
from sheeprl.algos.dreamer_v3.loss import reconstruction_loss
from sheeprl.algos.dreamer_v3.utils import Moments, compute_lambda_values, init_weights, test
from sheeprl.algos.p2e_dv3.agent import build_models
from sheeprl.data.buffers import AsyncReplayBuffer
from sheeprl.models.models import MLP
from sheeprl.utils.distribution import (
    MSEDistribution,
    OneHotCategoricalValidateArgs,
    SymlogDistribution,
    TwoHotEncodingDistribution,
)
from sheeprl.utils.env import make_env
from sheeprl.utils.logger import create_tensorboard_logger, get_log_dir
from sheeprl.utils.metric import MetricAggregator
from sheeprl.utils.registry import register_algorithm
from sheeprl.utils.timer import timer
from sheeprl.utils.utils import polynomial_decay

# Decomment the following line if you are using MineDojo on an headless machine
# os.environ["MINEDOJO_HEADLESS"] = "1"


def train(
    fabric: Fabric,
    world_model: WorldModel,
    actor_task: _FabricModule,
    critic_task: _FabricModule,
    target_critic_task: nn.Module,
    world_optimizer: _FabricOptimizer,
    actor_task_optimizer: _FabricOptimizer,
    critic_task_optimizer: _FabricOptimizer,
    data: TensorDictBase,
    aggregator: MetricAggregator,
    cfg: DictConfig,
    ensembles: _FabricModule,
    ensemble_optimizer: _FabricOptimizer,
    actor_exploration: _FabricModule,
    critics_exploration: Dict[str, Dict[str, Any]],
    actor_exploration_optimizer: _FabricOptimizer,
    moments_exploration: Dict[str, Moments],
    moments_task: Moments,
    is_continuous: bool,
    actions_dim: Sequence[int],
) -> None:
    """Runs one-step update of the agent.

    In particular, it updates the agent as specified by Algorithm 1 in
    [Planning to Explore via Self-Supervised World Models](https://arxiv.org/abs/2005.05960).

    The algorithm is made by different phases:
    1. Dynamic Learning: see Algorithm 1 in
        [Dream to Control: Learning Behaviors by Latent Imagination](https://arxiv.org/abs/1912.01603)
    2. Ensemble Learning: learn the ensemble models as described in
        [Planning to Explore via Self-Supervised World Models](https://arxiv.org/abs/2005.05960).
        The ensemble models give the novelty of the state visited by the agent.
    3. Behaviour Learning Exploration: the agent learns to explore the environment,
        having as reward only the intrinsic reward, computed from the ensembles.
    4. Behaviour Learning Task (zero-shot): the agent learns to solve the task,
        the experiences it uses to learn it are the ones collected during the exploration:
        - Imagine trajectories in the latent space from each latent state
        s_t up to the horizon H: s'_(t+1), ..., s'_(t+H).
        - Predict rewards and values in the imagined trajectories.
        - Compute lambda targets (Eq. 6 in [https://arxiv.org/abs/1912.01603](https://arxiv.org/abs/1912.01603))
        - Update the actor and the critic

    This method is based on [sheeprl.algos.dreamer_v3.dreamer_v3](sheeprl.algos.dreamer_v3.dreamer_v3) algorithm,
    extending it to implement the
    [Planning to Explore via Self-Supervised World Models](https://arxiv.org/abs/2005.05960).

    Args:
        fabric (Fabric): the fabric instance.
        world_model (WorldModel): the world model wrapped with Fabric.
        actor_task (_FabricModule): the actor for solving the task.
        critic_task (_FabricModule): the critic for solving the task.
        target_critic_task (nn.Module): the target critic for solving the task.
        world_optimizer (_FabricOptimizer): the world optimizer.
        actor_task_optimizer (_FabricOptimizer): the actor optimizer for solving the task.
        critic_task_optimizer (_FabricOptimizer): the critic optimizer for solving the task.
        data (TensorDictBase): the batch of data to use for training.
        aggregator (MetricAggregator): the aggregator to print the metrics.
        cfg (DictConfig): the configs.
        ensembles (_FabricModule): the ensemble models.
        ensemble_optimizer (_FabricOptimizer): the optimizer of the ensemble models.
        actor_exploration (_FabricModule): the actor for exploration.
        critics_exploration (Dict[str, Dict[str, Any]]): the critic for exploration.
        actor_exploration_optimizer (_FabricOptimizer): the optimizer of the actor for exploration.
        is_continuous (bool): whether or not are continuous actions.
        actions_dim (Sequence[int]): the actions dimension.
    """
    batch_size = cfg.per_rank_batch_size
    sequence_length = cfg.per_rank_sequence_length
    validate_args = cfg.distribution.validate_args
    recurrent_state_size = cfg.algo.world_model.recurrent_model.recurrent_state_size
    stochastic_size = cfg.algo.world_model.stochastic_size
    discrete_size = cfg.algo.world_model.discrete_size
    device = fabric.device
    batch_obs = {k: data[k] / 255.0 for k in cfg.cnn_keys.encoder}
    batch_obs.update({k: data[k] for k in cfg.mlp_keys.encoder})
    data["is_first"][0, :] = torch.tensor([1.0], device=fabric.device).expand_as(data["is_first"][0, :])

    # Given how the environment interaction works, we remove the last actions
    # and add the first one as the zero action
    batch_actions = torch.cat((torch.zeros_like(data["actions"][:1]), data["actions"][:-1]), dim=0)

    # Dynamic Learning
    stoch_state_size = stochastic_size * discrete_size
    recurrent_state = torch.zeros(1, batch_size, recurrent_state_size, device=device)
    posterior = torch.zeros(1, batch_size, stochastic_size, discrete_size, device=device)
    recurrent_states = torch.empty(sequence_length, batch_size, recurrent_state_size, device=device)
    priors_logits = torch.empty(sequence_length, batch_size, stoch_state_size, device=device)
    posteriors = torch.empty(sequence_length, batch_size, stochastic_size, discrete_size, device=device)
    posteriors_logits = torch.empty(sequence_length, batch_size, stoch_state_size, device=device)

    # embedded observations from the environment
    embedded_obs = world_model.encoder(batch_obs)

    for i in range(0, sequence_length):
        recurrent_state, posterior, _, posterior_logits, prior_logits = world_model.rssm.dynamic(
            posterior, recurrent_state, batch_actions[i : i + 1], embedded_obs[i : i + 1], data["is_first"][i : i + 1]
        )
        recurrent_states[i] = recurrent_state
        priors_logits[i] = prior_logits
        posteriors[i] = posterior
        posteriors_logits[i] = posterior_logits
    latent_states = torch.cat((posteriors.view(*posteriors.shape[:-2], -1), recurrent_states), -1)

    # compute predictions for the observations
    reconstructed_obs: Dict[str, torch.Tensor] = world_model.observation_model(latent_states)

    # compute the distribution over the reconstructed observations
    po = {
        k: MSEDistribution(reconstructed_obs[k], dims=len(reconstructed_obs[k].shape[2:])) for k in cfg.cnn_keys.decoder
    }
    po.update(
        {
            k: SymlogDistribution(reconstructed_obs[k], dims=len(reconstructed_obs[k].shape[2:]))
            for k in cfg.mlp_keys.decoder
        }
    )
    # Compute the distribution over the rewards
    pr = TwoHotEncodingDistribution(world_model.reward_model(latent_states.detach()), dims=1)

    # Compute the distribution over the terminal steps, if required
    pc = Independent(
        Bernoulli(logits=world_model.continue_model(latent_states.detach()), validate_args=validate_args),
        1,
        validate_args=validate_args,
    )
    continue_targets = 1 - data["dones"]

    # Reshape posterior and prior logits to shape [B, T, 32, 32]
    priors_logits = priors_logits.view(*priors_logits.shape[:-1], stochastic_size, discrete_size)
    posteriors_logits = posteriors_logits.view(*posteriors_logits.shape[:-1], stochastic_size, discrete_size)

    # world model optimization step
    world_optimizer.zero_grad(set_to_none=True)
    rec_loss, kl, state_loss, reward_loss, observation_loss, continue_loss = reconstruction_loss(
        po,
        batch_obs,
        pr,
        data["rewards"],
        priors_logits,
        posteriors_logits,
        cfg.algo.world_model.kl_dynamic,
        cfg.algo.world_model.kl_representation,
        cfg.algo.world_model.kl_free_nats,
        cfg.algo.world_model.kl_regularizer,
        pc,
        continue_targets,
        cfg.algo.world_model.continue_scale_factor,
    )
    fabric.backward(rec_loss)
    world_model_grads = None
    if cfg.algo.world_model.clip_gradients is not None and cfg.algo.world_model.clip_gradients > 0:
        world_model_grads = fabric.clip_gradients(
            module=world_model,
            optimizer=world_optimizer,
            max_norm=cfg.algo.world_model.clip_gradients,
            error_if_nonfinite=False,
        )
    world_optimizer.step()
    if aggregator and not aggregator.disabled:
        if world_model_grads:
            aggregator.update("Grads/world_model", world_model_grads.mean().detach())
        aggregator.update("Loss/world_model_loss", rec_loss.detach())
        aggregator.update("Loss/observation_loss", observation_loss.detach())
        aggregator.update("Loss/reward_loss", reward_loss.detach())
        aggregator.update("Loss/state_loss", state_loss.detach())
        aggregator.update("Loss/continue_loss", continue_loss.detach())
        aggregator.update("State/kl", kl.mean().detach())
        aggregator.update(
            "State/post_entropy",
            Independent(
                OneHotCategoricalValidateArgs(logits=posteriors_logits.detach(), validate_args=validate_args),
                1,
                validate_args=validate_args,
            )
            .entropy()
            .mean()
            .detach(),
        )
        aggregator.update(
            "State/prior_entropy",
            Independent(
                OneHotCategoricalValidateArgs(logits=priors_logits.detach(), validate_args=validate_args),
                1,
                validate_args=validate_args,
            )
            .entropy()
            .mean()
            .detach(),
        )

    # Free up space
    del posterior
    del prior_logits
    del priors_logits
    del recurrent_state
    del posterior_logits
    del posteriors_logits
    world_optimizer.zero_grad(set_to_none=True)

    # Ensemble Learning
    loss = 0.0
    ensemble_optimizer.zero_grad(set_to_none=True)
    for ens in ensembles:
        out = ens(
            torch.cat(
                (
                    posteriors.view(*posteriors.shape[:-2], -1).detach(),
                    recurrent_states.detach(),
                    data["actions"].detach(),
                ),
                -1,
            )
        )[:-1]
        next_state_embedding_dist = MSEDistribution(out, 1)
        loss -= next_state_embedding_dist.log_prob(posteriors.view(sequence_length, batch_size, -1).detach()[1:]).mean()
    loss.backward()
    ensemble_grad = None
    if cfg.algo.ensembles.clip_gradients is not None and cfg.algo.ensembles.clip_gradients > 0:
        ensemble_grad = fabric.clip_gradients(
            module=ens,
            optimizer=ensemble_optimizer,
            max_norm=cfg.algo.ensembles.clip_gradients,
            error_if_nonfinite=False,
        )
    ensemble_optimizer.step()
    if aggregator and not aggregator.disabled:
        if ensemble_grad:
            aggregator.update("Grads/ensemble", ensemble_grad.detach())
        aggregator.update("Loss/ensemble_loss", loss.detach().cpu())

    # Behaviour Learning Exploration
    imagined_prior = posteriors.detach().reshape(1, -1, stoch_state_size)
    recurrent_state = recurrent_states.detach().reshape(1, -1, recurrent_state_size)
    imagined_latent_state = torch.cat((imagined_prior, recurrent_state), -1)
    imagined_trajectories = torch.empty(
        cfg.algo.horizon + 1,
        batch_size * sequence_length,
        stoch_state_size + recurrent_state_size,
        device=device,
    )
    imagined_trajectories[0] = imagined_latent_state
    imagined_actions = torch.empty(
        cfg.algo.horizon + 1,
        batch_size * sequence_length,
        data["actions"].shape[-1],
        device=device,
    )
    actions = torch.cat(actor_exploration(imagined_latent_state.detach())[0], dim=-1)
    imagined_actions[0] = actions

    # imagine trajectories in the latent space
    for i in range(1, cfg.algo.horizon + 1):
        imagined_prior, recurrent_state = world_model.rssm.imagination(imagined_prior, recurrent_state, actions)
        imagined_prior = imagined_prior.view(1, -1, stoch_state_size)
        imagined_latent_state = torch.cat((imagined_prior, recurrent_state), -1)
        imagined_trajectories[i] = imagined_latent_state
        actions = torch.cat(actor_exploration(imagined_latent_state.detach())[0], dim=-1)
        imagined_actions[i] = actions

    advantages = []
    weights_sum = sum([c["weight"] for c in critics_exploration.values()])
    for k, critic in critics_exploration.items():
        # Predict values and continues
        predicted_values = TwoHotEncodingDistribution(critic["module"](imagined_trajectories), dims=1).mean
        continues = Independent(
            Bernoulli(logits=world_model.continue_model(imagined_trajectories), validate_args=validate_args),
            1,
            validate_args=validate_args,
        ).mode
        true_done = (1 - data["dones"]).flatten().reshape(1, -1, 1)
        continues = torch.cat((true_done, continues[1:]))

        if critic["reward_type"] == "intrinsic":
            # Predict intrinsic reward
            next_state_embedding = torch.empty(
                len(ensembles),
                cfg.algo.horizon + 1,
                batch_size * sequence_length,
                stochastic_size * discrete_size,
                device=device,
            )
            for i, ens in enumerate(ensembles):
                next_state_embedding[i] = ens(
                    torch.cat((imagined_trajectories.detach(), imagined_actions.detach()), -1)
                )

            # next_state_embedding -> N_ensemble x Horizon x Batch_size*Seq_len x Obs_embedding_size
            reward = next_state_embedding.var(0).mean(-1, keepdim=True) * cfg.algo.intrinsic_reward_multiplier
            if aggregator and not aggregator.disabled:
                aggregator.update(f"Rewards/intrinsic_{k}", reward.detach().cpu().mean())
        else:
            reward = TwoHotEncodingDistribution(world_model.reward_model(imagined_trajectories), dims=1).mean

        lambda_values = compute_lambda_values(
            reward[1:],
            predicted_values[1:],
            continues[1:] * cfg.algo.gamma,
            lmbda=cfg.algo.lmbda,
        )
        critic["lambda_values"] = lambda_values
        baseline = predicted_values[:-1]
        offset, invscale = moments_exploration[k](lambda_values)
        normed_lambda_values = (lambda_values - offset) / invscale
        normed_baseline = (baseline - offset) / invscale
        advantages.append((normed_lambda_values - normed_baseline) * critic["weight"] / weights_sum)

        if aggregator and not aggregator.disabled:
            aggregator.update(f"Values_exploration/predicted_values_{k}", predicted_values.detach().cpu().mean())
            aggregator.update(f"Values_exploration/lambda_values_{k}", lambda_values.detach().cpu().mean())

    advantage = torch.stack(advantages, dim=0).sum(dim=0)
    with torch.no_grad():
        discount = torch.cumprod(continues * cfg.algo.gamma, dim=0) / cfg.algo.gamma

    actor_exploration_optimizer.zero_grad(set_to_none=True)
    policies: Sequence[Distribution] = actor_exploration(imagined_trajectories.detach())[1]
    if is_continuous:
        objective = advantage
    else:
        objective = (
            torch.stack(
                [
                    p.log_prob(imgnd_act.detach()).unsqueeze(-1)[:-1]
                    for p, imgnd_act in zip(policies, torch.split(imagined_actions, actions_dim, dim=-1))
                ],
                dim=-1,
            ).sum(dim=-1)
            * advantage.detach()
        )
    try:
        entropy = cfg.algo.actor.ent_coef * torch.stack([p.entropy() for p in policies], -1).sum(dim=-1)
    except NotImplementedError:
        entropy = torch.zeros_like(objective)

    policy_loss_exploration = -torch.mean(discount[:-1].detach() * (objective + entropy.unsqueeze(dim=-1)[:-1]))
    fabric.backward(policy_loss_exploration)
    actor_grads_exploration = None
    if cfg.algo.actor.clip_gradients is not None and cfg.algo.actor.clip_gradients > 0:
        actor_grads_exploration = fabric.clip_gradients(
            module=actor_exploration,
            optimizer=actor_exploration_optimizer,
            max_norm=cfg.algo.actor.clip_gradients,
            error_if_nonfinite=False,
        )
    actor_exploration_optimizer.step()
    if aggregator and not aggregator.disabled:
        if actor_grads_exploration:
            aggregator.update("Grads/actor_exploration", actor_grads_exploration.mean().detach())
        aggregator.update("Loss/policy_loss_exploration", policy_loss_exploration.detach())

    for k, critic in critics_exploration.items():
        qv = TwoHotEncodingDistribution(critic["module"](imagined_trajectories.detach()[:-1]), dims=1)
        with torch.no_grad():
            predicted_target_values_expl = TwoHotEncodingDistribution(
                critic["target_module"](imagined_trajectories.detach()[:-1]), dims=1
            ).mean
        # Critic optimization. Eq. 10 in the paper
        critic["optimizer"].zero_grad(set_to_none=True)
        value_loss = -qv.log_prob(critic["lambda_values"].detach())
        value_loss = value_loss - qv.log_prob(predicted_target_values_expl.detach())
        value_loss = torch.mean(value_loss * discount[:-1].squeeze(-1))

        fabric.backward(value_loss)
        critic_grads_exploration = None
        if cfg.algo.critic.clip_gradients is not None and cfg.algo.critic.clip_gradients > 0:
            critic_grads_exploration = fabric.clip_gradients(
                module=critic["module"],
                optimizer=critic["optimizer"],
                max_norm=cfg.algo.critic.clip_gradients,
                error_if_nonfinite=False,
            )
        critic["optimizer"].step()
        if aggregator and not aggregator.disabled:
            if critic_grads_exploration:
                aggregator.update(f"Grads/critic_exploration_{k}", critic_grads_exploration.mean().detach())
            aggregator.update(f"Loss/value_loss_exploration_{k}", value_loss.detach())

    # reset the world_model gradients, to avoid interferences with task learning
    world_optimizer.zero_grad(set_to_none=True)

    # Behaviour Learning Task
    imagined_prior = posteriors.detach().reshape(1, -1, stoch_state_size)
    recurrent_state = recurrent_states.detach().reshape(1, -1, recurrent_state_size)
    imagined_latent_state = torch.cat((imagined_prior, recurrent_state), -1)
    imagined_trajectories = torch.empty(
        cfg.algo.horizon + 1,
        batch_size * sequence_length,
        stoch_state_size + recurrent_state_size,
        device=device,
    )
    imagined_trajectories[0] = imagined_latent_state
    imagined_actions = torch.empty(
        cfg.algo.horizon + 1,
        batch_size * sequence_length,
        data["actions"].shape[-1],
        device=device,
    )
    actions = torch.cat(actor_task(imagined_latent_state.detach())[0], dim=-1)
    imagined_actions[0] = actions

    # imagine trajectories in the latent space
    for i in range(1, cfg.algo.horizon + 1):
        imagined_prior, recurrent_state = world_model.rssm.imagination(imagined_prior, recurrent_state, actions)
        imagined_prior = imagined_prior.view(1, -1, stoch_state_size)
        imagined_latent_state = torch.cat((imagined_prior, recurrent_state), -1)
        imagined_trajectories[i] = imagined_latent_state
        actions = torch.cat(actor_task(imagined_latent_state.detach())[0], dim=-1)
        imagined_actions[i] = actions

    # Predict values, rewards and continues
    predicted_values = TwoHotEncodingDistribution(critic_task(imagined_trajectories), dims=1).mean
    predicted_rewards = TwoHotEncodingDistribution(world_model.reward_model(imagined_trajectories), dims=1).mean
    continues = Independent(
        Bernoulli(logits=world_model.continue_model(imagined_trajectories), validate_args=validate_args),
        1,
        validate_args=validate_args,
    ).mode
    true_done = (1 - data["dones"]).flatten().reshape(1, -1, 1)
    continues = torch.cat((true_done, continues[1:]))

    lambda_values = compute_lambda_values(
        predicted_rewards[1:],
        predicted_values[1:],
        continues[1:] * cfg.algo.gamma,
        lmbda=cfg.algo.lmbda,
    )

    # Compute the discounts to multiply the lambda values to
    with torch.no_grad():
        discount = torch.cumprod(continues * cfg.algo.gamma, dim=0) / cfg.algo.gamma

    actor_task_optimizer.zero_grad(set_to_none=True)
    policies: Sequence[Distribution] = actor_task(imagined_trajectories.detach())[1]

    baseline = predicted_values[:-1]
    offset, invscale = moments_task(lambda_values)
    normed_lambda_values = (lambda_values - offset) / invscale
    normed_baseline = (baseline - offset) / invscale
    advantage = normed_lambda_values - normed_baseline
    if is_continuous:
        objective = advantage
    else:
        objective = (
            torch.stack(
                [
                    p.log_prob(imgnd_act.detach()).unsqueeze(-1)[:-1]
                    for p, imgnd_act in zip(policies, torch.split(imagined_actions, actions_dim, dim=-1))
                ],
                dim=-1,
            ).sum(dim=-1)
            * advantage.detach()
        )
    try:
        entropy = cfg.algo.actor.ent_coef * torch.stack([p.entropy() for p in policies], -1).sum(dim=-1)
    except NotImplementedError:
        entropy = torch.zeros_like(objective)
    policy_loss_task = -torch.mean(discount[:-1].detach() * (objective + entropy.unsqueeze(dim=-1)[:-1]))
    fabric.backward(policy_loss_task)
    actor_grads_task = None
    if cfg.algo.actor.clip_gradients is not None and cfg.algo.actor.clip_gradients > 0:
        actor_grads_task = fabric.clip_gradients(
            module=actor_task,
            optimizer=actor_task_optimizer,
            max_norm=cfg.algo.actor.clip_gradients,
            error_if_nonfinite=False,
        )
    actor_task_optimizer.step()
    if aggregator and not aggregator.disabled:
        if actor_grads_task:
            aggregator.update("Grads/actor_task", actor_grads_task.mean().detach())
        aggregator.update("Loss/policy_loss_task", policy_loss_task.detach())

    # Predict the values
    qv = TwoHotEncodingDistribution(critic_task(imagined_trajectories.detach()[:-1]), dims=1)
    with torch.no_grad():
        predicted_target_values_tsk = TwoHotEncodingDistribution(
            target_critic_task(imagined_trajectories.detach()[:-1]), dims=1
        ).mean

    # Critic optimization. Eq. 10 in the paper
    critic_task_optimizer.zero_grad(set_to_none=True)
    value_loss_task = -qv.log_prob(lambda_values.detach())
    value_loss_task = value_loss_task - qv.log_prob(predicted_target_values_tsk.detach())
    value_loss_task = torch.mean(value_loss_task * discount[:-1].squeeze(-1))

    fabric.backward(value_loss_task)
    critic_grads_task = None
    if cfg.algo.critic.clip_gradients is not None and cfg.algo.critic.clip_gradients > 0:
        critic_grads_task = fabric.clip_gradients(
            module=critic_task,
            optimizer=critic_task_optimizer,
            max_norm=cfg.algo.critic.clip_gradients,
            error_if_nonfinite=False,
        )
    critic_task_optimizer.step()
    if aggregator and not aggregator.disabled:
        if critic_grads_task:
            aggregator.update("Grads/critic_task", critic_grads_task.mean().detach())
        aggregator.update("Loss/value_loss_task", value_loss_task.detach())

    # Reset everything
    actor_exploration_optimizer.zero_grad(set_to_none=True)
    actor_task_optimizer.zero_grad(set_to_none=True)
    critic_task_optimizer.zero_grad(set_to_none=True)
    world_optimizer.zero_grad(set_to_none=True)
    ensemble_optimizer.zero_grad(set_to_none=True)
    for c in critics_exploration.values():
        c["optimizer"].zero_grad(set_to_none=True)


@register_algorithm()
def main(fabric: Fabric, cfg: Dict[str, Any]):
    device = fabric.device
    rank = fabric.global_rank
    world_size = fabric.world_size
    fabric.seed_everything(cfg.seed)
    torch.backends.cudnn.deterministic = cfg.torch_deterministic

    if cfg.checkpoint.resume_from:
        state = fabric.load(cfg.checkpoint.resume_from)
        cfg.per_rank_batch_size = state["batch_size"] // world_size

    # These arguments cannot be changed
    cfg.env.frame_stack = 1
    cfg.algo.player.actor_type = "exploration"

    # Create TensorBoardLogger. This will create the logger only on the
    # rank-0 process
    logger = create_tensorboard_logger(fabric, cfg)
    if logger and fabric.is_global_zero:
        fabric._loggers = [logger]
        fabric.logger.log_hyperparams(cfg)
    log_dir = get_log_dir(fabric, cfg.root_dir, cfg.run_name)

    # Environment setup
    vectorized_env = gym.vector.SyncVectorEnv if cfg.env.sync_env else gym.vector.AsyncVectorEnv
    envs = vectorized_env(
        [
            make_env(
                cfg,
                cfg.seed + rank * cfg.env.num_envs + i,
                rank * cfg.env.num_envs,
                log_dir if rank == 0 else None,
                "train",
                vector_env_idx=i,
            )
            for i in range(cfg.env.num_envs)
        ]
    )
    action_space = envs.single_action_space
    observation_space = envs.single_observation_space

    is_continuous = isinstance(action_space, gym.spaces.Box)
    is_multidiscrete = isinstance(action_space, gym.spaces.MultiDiscrete)
    actions_dim = (
        action_space.shape if is_continuous else (action_space.nvec.tolist() if is_multidiscrete else [action_space.n])
    )
    clip_rewards_fn = lambda r: torch.tanh(r) if cfg.env.clip_rewards else r
    if not isinstance(observation_space, gym.spaces.Dict):
        raise RuntimeError(f"Unexpected observation type, should be of type Dict, got: {observation_space}")
    if cfg.cnn_keys.encoder == [] and cfg.mlp_keys.encoder == []:
        raise RuntimeError(
            "You should specify at least one CNN keys or MLP keys from the cli: "
            "`cnn_keys.encoder=[rgb]` or `mlp_keys.encoder=[state]`"
        )
    if (
        len(set(cfg.cnn_keys.encoder).intersection(set(cfg.cnn_keys.decoder))) == 0
        and len(set(cfg.mlp_keys.encoder).intersection(set(cfg.mlp_keys.decoder))) == 0
    ):
        raise RuntimeError("The CNN keys or the MLP keys of the encoder and decoder must not be disjointed")
    if len(set(cfg.cnn_keys.decoder) - set(cfg.cnn_keys.encoder)) > 0:
        raise RuntimeError(
            "The CNN keys of the decoder must be contained in the encoder ones. "
            f"Those keys are decoded without being encoded: {list(set(cfg.cnn_keys.decoder))}"
        )
    if len(set(cfg.mlp_keys.decoder) - set(cfg.mlp_keys.encoder)) > 0:
        raise RuntimeError(
            "The MLP keys of the decoder must be contained in the encoder ones. "
            f"Those keys are decoded without being encoded: {list(set(cfg.mlp_keys.decoder))}"
        )
    if cfg.metric.log_level > 0:
        fabric.print("Encoder CNN keys:", cfg.cnn_keys.encoder)
        fabric.print("Encoder MLP keys:", cfg.mlp_keys.encoder)
        fabric.print("Decoder CNN keys:", cfg.cnn_keys.decoder)
        fabric.print("Decoder MLP keys:", cfg.mlp_keys.decoder)
    obs_keys = cfg.cnn_keys.encoder + cfg.mlp_keys.encoder

    (
        world_model,
        actor_task,
        critic_task,
        target_critic_task,
        actor_exploration,
        critics_exploration,
    ) = build_models(
        fabric,
        actions_dim,
        is_continuous,
        cfg,
        observation_space,
        state["world_model"] if cfg.checkpoint.resume_from else None,
        state["actor_task"] if cfg.checkpoint.resume_from else None,
        state["critic_task"] if cfg.checkpoint.resume_from else None,
        state["target_critic_task"] if cfg.checkpoint.resume_from else None,
        state["actor_exploration"] if cfg.checkpoint.resume_from else None,
        state["critics_exploration"] if cfg.checkpoint.resume_from else None,
    )

    # initialize the ensembles with different seeds to be sure they have different weights
    ens_list = []
    cfg_ensembles = cfg.algo.ensembles
    with isolate_rng():
        for i in range(cfg_ensembles.n):
            fabric.seed_everything(cfg.seed + i)
            ens_list.append(
                MLP(
                    input_dims=int(
                        sum(actions_dim)
                        + cfg.algo.world_model.recurrent_model.recurrent_state_size
                        + cfg.algo.world_model.stochastic_size * cfg.algo.world_model.discrete_size
                    ),
                    output_dim=cfg.algo.world_model.stochastic_size * cfg.algo.world_model.discrete_size,
                    hidden_sizes=[cfg_ensembles.dense_units] * cfg_ensembles.mlp_layers,
                    activation=eval(cfg_ensembles.dense_act),
                    flatten_dim=None,
                    layer_args={"bias": not cfg.algo.ensembles.layer_norm},
                    norm_layer=(
                        [nn.LayerNorm for _ in range(cfg_ensembles.mlp_layers)] if cfg_ensembles.layer_norm else None
                    ),
                    norm_args=(
                        [{"normalized_shape": cfg_ensembles.dense_units} for _ in range(cfg_ensembles.mlp_layers)]
                        if cfg_ensembles.layer_norm
                        else None
                    ),
                ).apply(init_weights)
            )
    ensembles = nn.ModuleList(ens_list)
    if cfg.checkpoint.resume_from:
        ensembles.load_state_dict(state["ensembles"])
    fabric.setup_module(ensembles)
    player = PlayerDV3(
        world_model.encoder.module,
        world_model.rssm,
        actor_exploration.module,
        actions_dim,
        cfg.env.num_envs,
        cfg.algo.world_model.stochastic_size,
        cfg.algo.world_model.recurrent_model.recurrent_state_size,
        fabric.device,
        discrete_size=cfg.algo.world_model.discrete_size,
        actor_type=cfg.algo.player.actor_type,
    )

    # Optimizers
    world_optimizer = hydra.utils.instantiate(cfg.algo.world_model.optimizer, params=world_model.parameters())
    actor_exploration_optimizer = hydra.utils.instantiate(
        cfg.algo.actor.optimizer, params=actor_exploration.parameters()
    )
    for k, critic in critics_exploration.items():
        critic["optimizer"] = hydra.utils.instantiate(cfg.algo.critic.optimizer, params=critic["module"].parameters())
    actor_task_optimizer = hydra.utils.instantiate(cfg.algo.actor.optimizer, params=actor_task.parameters())
    critic_task_optimizer = hydra.utils.instantiate(cfg.algo.critic.optimizer, params=critic_task.parameters())
    ensemble_optimizer = hydra.utils.instantiate(cfg.algo.critic.optimizer, params=ensembles.parameters())
    if cfg.checkpoint.resume_from:
        world_optimizer.load_state_dict(state["world_optimizer"])
        actor_task_optimizer.load_state_dict(state["actor_task_optimizer"])
        critic_task_optimizer.load_state_dict(state["critic_task_optimizer"])
        ensemble_optimizer.load_state_dict(state["ensemble_optimizer"])
        actor_exploration_optimizer.load_state_dict(state["actor_exploration_optimizer"])
        for k, c in critics_exploration.items():
            c["optimizer"].load_state_dict(state[f"critic_exploration_optimizer_{k}"])
    (
        world_optimizer,
        actor_task_optimizer,
        critic_task_optimizer,
        ensemble_optimizer,
        actor_exploration_optimizer,
    ) = fabric.setup_optimizers(
        world_optimizer,
        actor_task_optimizer,
        critic_task_optimizer,
        ensemble_optimizer,
        actor_exploration_optimizer,
    )
    for k, critic in critics_exploration.items():
        critic["optimizer"] = fabric.setup_optimizers(critic["optimizer"])

    moments_exploration = {
        k: Moments(
            fabric,
            cfg.algo.actor.moments.decay,
            cfg.algo.actor.moments.max,
            cfg.algo.actor.moments.percentile.low,
            cfg.algo.actor.moments.percentile.high,
        )
        for k in critics_exploration.keys()
    }
    moments_task = Moments(
        fabric,
        cfg.algo.actor.moments.decay,
        cfg.algo.actor.moments.max,
        cfg.algo.actor.moments.percentile.low,
        cfg.algo.actor.moments.percentile.high,
    )
    if cfg.checkpoint.resume_from:
        for k, m in moments_exploration.items():
            m.load_state_dict(state[f"moments_exploration_{k}"])
        moments_task.load_state_dict(state["moments_task"])

    # Metrics
    # Since there could be more exploration critics, the key of the critic is added
    # to the metrics that the user has selected.
    for k, c in critics_exploration.items():
        if "Loss/value_loss_exploration" in cfg.metric.aggregator.metrics:
            cfg.metric.aggregator.metrics[f"Loss/value_loss_exploration_{k}"] = cfg.metric.aggregator.metrics[
                "Loss/value_loss_exploration"
            ]
        if "Values_exploration/predicted_values" in cfg.metric.aggregator.metrics:
            cfg.metric.aggregator.metrics[f"Values_exploration/predicted_values_{k}"] = cfg.metric.aggregator.metrics[
                "Values_exploration/predicted_values"
            ]
        if "Values_exploration/lambda_values" in cfg.metric.aggregator.metrics:
            cfg.metric.aggregator.metrics[f"Values_exploration/lambda_values_{k}"] = cfg.metric.aggregator.metrics[
                "Values_exploration/lambda_values"
            ]
        if "Grads/critic_exploration" in cfg.metric.aggregator.metrics:
            cfg.metric.aggregator.metrics[f"Grads/critic_exploration_{k}"] = cfg.metric.aggregator.metrics[
                "Grads/critic_exploration"
            ]
        if c["reward_type"] == "intrinsic" and "Rewards/intrinsic" in cfg.metric.aggregator.metrics:
            cfg.metric.aggregator.metrics[f"Rewards/intrinsic_{k}"] = cfg.metric.aggregator.metrics["Rewards/intrinsic"]
    # Remove general log keys from the aggregator
    cfg.metric.aggregator.metrics.pop("Loss/value_loss_exploration", None)
    cfg.metric.aggregator.metrics.pop("Values_exploration/predicted_values", None)
    cfg.metric.aggregator.metrics.pop("Values_exploration/lambda_values", None)
    cfg.metric.aggregator.metrics.pop("Grads/critic_exploration", None)
    cfg.metric.aggregator.metrics.pop("Rewards/intrinsic", None)
    aggregator = None
    if not MetricAggregator.disabled:
        aggregator: MetricAggregator = hydra.utils.instantiate(cfg.metric.aggregator).to(device)

    # Local data
    buffer_size = cfg.buffer.size // int(cfg.env.num_envs * world_size) if not cfg.dry_run else 4
    rb = AsyncReplayBuffer(
        buffer_size,
        cfg.env.num_envs,
        device="cpu",
        memmap=cfg.buffer.memmap,
        memmap_dir=os.path.join(log_dir, "memmap_buffer", f"rank_{fabric.global_rank}"),
        sequential=True,
    )
    if cfg.checkpoint.resume_from and cfg.buffer.checkpoint:
        if isinstance(state["rb"], list) and world_size == len(state["rb"]):
            rb = state["rb"][fabric.global_rank]
        elif isinstance(state["rb"], AsyncReplayBuffer):
            rb = state["rb"]
        else:
            raise RuntimeError(f"Given {len(state['rb'])}, but {world_size} processes are instantiated")
    step_data = TensorDict({}, batch_size=[cfg.env.num_envs], device="cpu")
    expl_decay_steps = state["expl_decay_steps"] if cfg.checkpoint.resume_from else 0

    # Global variables
    train_step = 0
    last_train = 0
    start_step = state["update"] // fabric.world_size if cfg.checkpoint.resume_from else 1
    policy_step = state["update"] * cfg.env.num_envs if cfg.checkpoint.resume_from else 0
    last_log = state["last_log"] if cfg.checkpoint.resume_from else 0
    last_checkpoint = state["last_checkpoint"] if cfg.checkpoint.resume_from else 0
    policy_steps_per_update = int(cfg.env.num_envs * fabric.world_size)
    updates_before_training = cfg.algo.train_every // policy_steps_per_update
    num_updates = int(cfg.total_steps // policy_steps_per_update) if not cfg.dry_run else 1
    learning_starts = cfg.algo.learning_starts // policy_steps_per_update if not cfg.dry_run else 0
    if cfg.checkpoint.resume_from and not cfg.buffer.checkpoint:
        learning_starts += start_step
    max_step_expl_decay = cfg.algo.actor.max_step_expl_decay // (cfg.algo.per_rank_gradient_steps * fabric.world_size)
    if cfg.checkpoint.resume_from:
        actor_task.expl_amount = polynomial_decay(
            expl_decay_steps,
            initial=cfg.algo.actor.expl_amount,
            final=cfg.algo.actor.expl_min,
            max_decay_steps=max_step_expl_decay,
        )
        actor_exploration.expl_amount = polynomial_decay(
            expl_decay_steps,
            initial=cfg.algo.actor.expl_amount,
            final=cfg.algo.actor.expl_min,
            max_decay_steps=max_step_expl_decay,
        )

    # Warning for log and checkpoint every
    if cfg.metric.log_level > 0 and cfg.metric.log_every % policy_steps_per_update != 0:
        warnings.warn(
            f"The metric.log_every parameter ({cfg.metric.log_every}) is not a multiple of the "
            f"policy_steps_per_update value ({policy_steps_per_update}), so "
            "the metrics will be logged at the nearest greater multiple of the "
            "policy_steps_per_update value."
        )
    if cfg.checkpoint.every % policy_steps_per_update != 0:
        warnings.warn(
            f"The checkpoint.every parameter ({cfg.checkpoint.every}) is not a multiple of the "
            f"policy_steps_per_update value ({policy_steps_per_update}), so "
            "the checkpoint will be saved at the nearest greater multiple of the "
            "policy_steps_per_update value."
        )

    # Get the first environment observation and start the optimization
    o = envs.reset(seed=cfg.seed)[0]
    obs = {k: torch.from_numpy(v).view(cfg.env.num_envs, *v.shape[1:]) for k, v in o.items() if k.startswith("mask")}
    for k in obs_keys:
        torch_obs = torch.from_numpy(o[k]).view(cfg.env.num_envs, *o[k].shape[1:])
        if k in cfg.mlp_keys.encoder:
            # Images stay uint8 to save space
            torch_obs = torch_obs.float()
        step_data[k] = torch_obs
        obs[k] = torch_obs
    step_data["dones"] = torch.zeros(cfg.env.num_envs, 1).float()
    step_data["rewards"] = torch.zeros(cfg.env.num_envs, 1).float()
    step_data["is_first"] = torch.ones_like(step_data["dones"]).float()
    player.init_states()

    per_rank_gradient_steps = 0
    for update in range(start_step, num_updates + 1):
        policy_step += cfg.env.num_envs * world_size

        # Measure environment interaction time: this considers both the model forward
        # to get the action given the observation and the time taken into the environment
        with timer("Time/env_interaction_time", SumMetric(sync_on_compute=False)):
            # Sample an action given the observation received by the environment
            if (
                update <= learning_starts
                and cfg.checkpoint.resume_from is None
                and "minedojo" not in cfg.algo.actor.cls.lower()
            ):
                real_actions = actions = np.array(envs.action_space.sample())
                if not is_continuous:
                    actions = np.concatenate(
                        [
                            F.one_hot(torch.tensor(act), act_dim).numpy()
                            for act, act_dim in zip(actions.reshape(len(actions_dim), -1), actions_dim)
                        ],
                        axis=-1,
                    )
            else:
                with torch.no_grad():
                    preprocessed_obs = {}
                    for k, v in obs.items():
                        if k in cfg.cnn_keys.encoder:
                            preprocessed_obs[k] = v[None, ...].to(device) / 255.0
                        else:
                            preprocessed_obs[k] = v[None, ...].to(device)
                    mask = {k: v for k, v in preprocessed_obs.items() if k.startswith("mask")}
                    if len(mask) == 0:
                        mask = None
                    real_actions = actions = player.get_exploration_action(preprocessed_obs, mask)
                    actions = torch.cat(actions, -1).cpu().numpy()
                    if is_continuous:
                        real_actions = torch.cat(real_actions, dim=-1).cpu().numpy()
                    else:
                        real_actions = np.array([real_act.cpu().argmax(dim=-1).numpy() for real_act in real_actions])

            step_data["actions"] = torch.from_numpy(actions).view(cfg.env.num_envs, -1).float()
            rb.add(step_data[None, ...])

            o, rewards, dones, truncated, infos = envs.step(real_actions.reshape(envs.action_space.shape))
            dones = np.logical_or(dones, truncated)

        step_data["is_first"] = torch.zeros_like(step_data["dones"])
        if "restart_on_exception" in infos:
            for i, agent_roe in enumerate(infos["restart_on_exception"]):
                if agent_roe and not dones[i]:
                    last_inserted_idx = (rb.buffer[i]._pos - 1) % rb.buffer[i].buffer_size
                    rb.buffer[i]["dones"][last_inserted_idx] = torch.ones_like(rb.buffer[i]["dones"][last_inserted_idx])
                    rb.buffer[i]["is_first"][last_inserted_idx] = torch.zeros_like(
                        rb.buffer[i]["is_first"][last_inserted_idx]
                    )
                    step_data["is_first"][i] = torch.ones_like(step_data["is_first"][i])

        if cfg.metric.log_level > 0 and "final_info" in infos:
            for i, agent_ep_info in enumerate(infos["final_info"]):
                if agent_ep_info is not None:
                    ep_rew = agent_ep_info["episode"]["r"]
                    ep_len = agent_ep_info["episode"]["l"]
                    if aggregator and not aggregator.disabled:
                        aggregator.update("Rewards/rew_avg", ep_rew)
                        aggregator.update("Game/ep_len_avg", ep_len)
                    fabric.print(f"Rank-0: policy_step={policy_step}, reward_env_{i}={ep_rew[-1]}")

        # Save the real next observation
        real_next_obs = copy.deepcopy(o)
        if "final_observation" in infos:
            for idx, final_obs in enumerate(infos["final_observation"]):
                if final_obs is not None:
                    for k, v in final_obs.items():
                        real_next_obs[k][idx] = v

        next_obs: Dict[str, Tensor] = {
            k: torch.from_numpy(v).view(cfg.env.num_envs, *v.shape[1:]) for k, v in o.items() if k.startswith("mask")
        }
        for k in real_next_obs.keys():  # [N_envs, N_obs]
            if k in obs_keys:
                next_obs[k] = torch.from_numpy(o[k]).view(cfg.env.num_envs, *o[k].shape[1:])
                step_data[k] = next_obs[k]
                if k in cfg.mlp_keys.encoder:
                    next_obs[k] = next_obs[k].float()
                    step_data[k] = step_data[k].float()

        # next_obs becomes the new obs
        obs = next_obs

        rewards = torch.from_numpy(rewards).view(cfg.env.num_envs, -1).float()
        dones = torch.from_numpy(dones).view(cfg.env.num_envs, -1).float()
        step_data["dones"] = dones
        step_data["rewards"] = clip_rewards_fn(rewards)

        dones_idxes = dones.nonzero(as_tuple=True)[0].tolist()
        reset_envs = len(dones_idxes)
        if reset_envs > 0:
            reset_data = TensorDict({}, batch_size=[reset_envs], device="cpu")
            for k in real_next_obs.keys():
                if k in obs_keys:
                    reset_data[k] = real_next_obs[k][dones_idxes]
                    if k in cfg.mlp_keys.encoder:
                        reset_data[k] = reset_data[k].float()
            reset_data["dones"] = torch.ones(reset_envs, 1).float()
            reset_data["actions"] = torch.zeros(reset_envs, np.sum(actions_dim)).float()
            reset_data["rewards"] = step_data["rewards"][dones_idxes].float()
            reset_data["is_first"] = torch.zeros_like(reset_data["dones"]).float()
            rb.add(reset_data[None, ...], dones_idxes)

            # Reset already inserted step data
            step_data["rewards"][dones_idxes] = torch.zeros_like(reset_data["rewards"]).float()
            step_data["dones"][dones_idxes] = torch.zeros_like(step_data["dones"][dones_idxes]).float()
            step_data["is_first"][dones_idxes] = torch.ones_like(step_data["is_first"][dones_idxes]).float()
            player.init_states(dones_idxes)

        updates_before_training -= 1

        # Train the agent
        if update >= learning_starts and updates_before_training <= 0:
            local_data = rb.sample(
                cfg.per_rank_batch_size,
                sequence_length=cfg.per_rank_sequence_length,
                n_samples=cfg.algo.per_rank_pretrain_steps
                if update == learning_starts
                else cfg.algo.per_rank_gradient_steps,
            ).to(device)
            distributed_sampler = BatchSampler(range(local_data.shape[0]), batch_size=1, drop_last=False)
            # Start training
            with timer("Time/train_time", SumMetric(sync_on_compute=cfg.metric.sync_on_compute)):
                for i in distributed_sampler:
                    if per_rank_gradient_steps % cfg.algo.critic.target_network_update_freq == 0:
                        tau = 1 if per_rank_gradient_steps == 0 else cfg.algo.critic.tau
                        for cp, tcp in zip(critic_task.module.parameters(), target_critic_task.parameters()):
                            tcp.data.copy_(tau * cp.data + (1 - tau) * tcp.data)
                        for k in critics_exploration.keys():
                            for cp, tcp in zip(
                                critics_exploration[k]["module"].module.parameters(),
                                critics_exploration[k]["target_module"].parameters(),
                            ):
                                tcp.data.copy_(tau * cp.data + (1 - tau) * tcp.data)
                    train(
                        fabric,
                        world_model,
                        actor_task,
                        critic_task,
                        target_critic_task,
                        world_optimizer,
                        actor_task_optimizer,
                        critic_task_optimizer,
                        local_data[i].view(cfg.per_rank_sequence_length, cfg.per_rank_batch_size),
                        aggregator,
                        cfg,
                        ensembles=ensembles,
                        ensemble_optimizer=ensemble_optimizer,
                        actor_exploration=actor_exploration,
                        critics_exploration=critics_exploration,
                        actor_exploration_optimizer=actor_exploration_optimizer,
                        is_continuous=is_continuous,
                        actions_dim=actions_dim,
                        moments_exploration=moments_exploration,
                        moments_task=moments_task,
                    )
                train_step += world_size
            updates_before_training = cfg.algo.train_every // policy_steps_per_update
            if cfg.algo.actor.expl_decay:
                expl_decay_steps += 1
                actor_task.expl_amount = polynomial_decay(
                    expl_decay_steps,
                    initial=cfg.algo.actor.expl_amount,
                    final=cfg.algo.actor.expl_min,
                    max_decay_steps=max_step_expl_decay,
                )
                actor_exploration.expl_amount = polynomial_decay(
                    expl_decay_steps,
                    initial=cfg.algo.actor.expl_amount,
                    final=cfg.algo.actor.expl_min,
                    max_decay_steps=max_step_expl_decay,
                )
            if aggregator and not aggregator.disabled:
                aggregator.update("Params/exploration_amount_task", actor_task.expl_amount)
                aggregator.update("Params/exploration_amount_exploration", actor_exploration.expl_amount)

        # Log metrics
        if cfg.metric.log_level > 0 and (policy_step - last_log >= cfg.metric.log_every or update == num_updates):
            # Sync distributed metrics
            if aggregator and not aggregator.disabled:
                metrics_dict = aggregator.compute()
                fabric.log_dict(metrics_dict, policy_step)
                aggregator.reset()

            # Sync distributed timers
            if not timer.disabled:
                timer_metrics = timer.compute()
                if "Time/train_time" in timer_metrics:
                    fabric.log(
                        "Time/sps_train",
                        (train_step - last_train) / timer_metrics["Time/train_time"],
                        policy_step,
                    )
                if "Time/env_interaction_time" in timer_metrics:
                    fabric.log(
                        "Time/sps_env_interaction",
                        ((policy_step - last_log) / world_size * cfg.env.action_repeat)
                        / timer_metrics["Time/env_interaction_time"],
                        policy_step,
                    )
                timer.reset()

            # Reset counters
            last_log = policy_step
            last_train = train_step

        # Checkpoint Model
        if (cfg.checkpoint.every > 0 and policy_step - last_checkpoint >= cfg.checkpoint.every) or (
            update == num_updates and cfg.checkpoint.save_last
        ):
            last_checkpoint = policy_step
            critics_exploration_state = {"critics_exploration": {}}
            for k, c in critics_exploration.items():
                critics_exploration_state["critics_exploration"][k] = {
                    "module": c["module"].state_dict(),
                    "target_module": c["target_module"].state_dict(),
                }
                critics_exploration_state[f"critic_exploration_optimizer_{k}"] = c["optimizer"].state_dict()
                critics_exploration_state[f"moments_exploration_{k}"] = moments_exploration[k].state_dict()
            state = {
                "world_model": world_model.state_dict(),
                "actor_task": actor_task.state_dict(),
                "critic_task": critic_task.state_dict(),
                "target_critic_task": target_critic_task.state_dict(),
                "ensembles": ensembles.state_dict(),
                "world_optimizer": world_optimizer.state_dict(),
                "actor_task_optimizer": actor_task_optimizer.state_dict(),
                "critic_task_optimizer": critic_task_optimizer.state_dict(),
                "ensemble_optimizer": ensemble_optimizer.state_dict(),
                "expl_decay_steps": expl_decay_steps,
                "update": update * world_size,
                "batch_size": cfg.per_rank_batch_size * world_size,
                "actor_exploration": actor_exploration.state_dict(),
                "actor_exploration_optimizer": actor_exploration_optimizer.state_dict(),
                "last_log": last_log,
                "last_checkpoint": last_checkpoint,
                "moments_task": moments_task.state_dict(),
                **critics_exploration_state,
            }
            ckpt_path = log_dir + f"/checkpoint/ckpt_{policy_step}_{fabric.global_rank}.ckpt"
            fabric.call(
                "on_checkpoint_coupled",
                fabric=fabric,
                ckpt_path=ckpt_path,
                state=state,
                replay_buffer=rb if cfg.buffer.checkpoint else None,
            )

    envs.close()
    # task test zero-shot
    if fabric.is_global_zero:
        player.actor = actor_task.module
        player.actor_type = "task"
        test(player, fabric, cfg, log_dir, "zero-shot")
