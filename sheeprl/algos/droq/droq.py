from __future__ import annotations

import copy
import os
import warnings
from math import prod
from typing import Any, Dict

import gymnasium as gym
import hydra
import numpy as np
import torch
import torch.nn.functional as F
from lightning.fabric import Fabric
from tensordict import TensorDict, make_tensordict
from torch.optim import Optimizer
from torch.utils.data.distributed import DistributedSampler
from torch.utils.data.sampler import BatchSampler
from torchmetrics import SumMetric

from sheeprl.algos.droq.agent import DROQAgent, DROQCritic
from sheeprl.algos.sac.agent import SACActor
from sheeprl.algos.sac.loss import entropy_loss, policy_loss
from sheeprl.algos.sac.sac import test
from sheeprl.data.buffers import ReplayBuffer
from sheeprl.utils.env import make_env
from sheeprl.utils.logger import create_tensorboard_logger, get_log_dir
from sheeprl.utils.metric import MetricAggregator
from sheeprl.utils.registry import register_algorithm
from sheeprl.utils.timer import timer


def train(
    fabric: Fabric,
    agent: DROQAgent,
    actor_optimizer: Optimizer,
    qf_optimizer: Optimizer,
    alpha_optimizer: Optimizer,
    rb: ReplayBuffer,
    aggregator: MetricAggregator | None,
    cfg: Dict[str, Any],
):
    # Sample a minibatch in a distributed way: Line 5 - Algorithm 2
    # We sample one time to reduce the communications between processes
    sample = rb.sample(
        cfg.algo.per_rank_gradient_steps * cfg.per_rank_batch_size, sample_next_obs=cfg.buffer.sample_next_obs
    )
    critic_data = fabric.all_gather(sample.to_dict())
    critic_data = make_tensordict(critic_data).view(-1)
    if fabric.world_size > 1:
        dist_sampler: DistributedSampler = DistributedSampler(
            range(len(critic_data)),
            num_replicas=fabric.world_size,
            rank=fabric.global_rank,
            shuffle=True,
            seed=cfg.seed,
            drop_last=False,
        )
        critic_sampler: BatchSampler = BatchSampler(
            sampler=dist_sampler, batch_size=cfg.per_rank_batch_size, drop_last=False
        )
    else:
        critic_sampler = BatchSampler(
            sampler=range(len(critic_data)), batch_size=cfg.per_rank_batch_size, drop_last=False
        )

    # Sample a different minibatch in a distributed way to update actor and alpha parameter
    sample = rb.sample(cfg.per_rank_batch_size)
    actor_data = fabric.all_gather(sample.to_dict())
    actor_data = make_tensordict(actor_data).view(-1)
    if fabric.world_size > 1:
        actor_sampler: DistributedSampler = DistributedSampler(
            range(len(actor_data)),
            num_replicas=fabric.world_size,
            rank=fabric.global_rank,
            shuffle=True,
            seed=cfg.seed,
            drop_last=False,
        )
        actor_data = actor_data[next(iter(actor_sampler))]

    with timer("Time/train_time", SumMetric(sync_on_compute=cfg.metric.sync_on_compute)):
        # Update the soft-critic
        for batch_idxes in critic_sampler:
            critic_batch_data = critic_data[batch_idxes]
            next_target_qf_value = agent.get_next_target_q_values(
                critic_batch_data["next_observations"],
                critic_batch_data["rewards"],
                critic_batch_data["dones"],
                cfg.algo.gamma,
            )
            for qf_value_idx in range(agent.num_critics):
                # Line 8 - Algorithm 2
                qf_loss = F.mse_loss(
                    agent.get_ith_q_value(
                        critic_batch_data["observations"], critic_batch_data["actions"], qf_value_idx
                    ),
                    next_target_qf_value,
                )
                qf_optimizer.zero_grad(set_to_none=True)
                fabric.backward(qf_loss)
                qf_optimizer.step()
                if aggregator and not aggregator.disabled:
                    aggregator.update("Loss/value_loss", qf_loss)

                # Update the target networks with EMA
                agent.qfs_target_ema(critic_idx=qf_value_idx)

        # Update the actor
        actions, logprobs = agent.get_actions_and_log_probs(actor_data["observations"])
        qf_values = agent.get_q_values(actor_data["observations"], actions)
        min_qf_values = torch.mean(qf_values, dim=-1, keepdim=True)
        actor_loss = policy_loss(agent.alpha, logprobs, min_qf_values)
        actor_optimizer.zero_grad(set_to_none=True)
        fabric.backward(actor_loss)
        actor_optimizer.step()

        # Update the entropy value
        alpha_loss = entropy_loss(agent.log_alpha, logprobs.detach(), agent.target_entropy)
        alpha_optimizer.zero_grad(set_to_none=True)
        fabric.backward(alpha_loss)
        agent.log_alpha.grad = fabric.all_reduce(agent.log_alpha.grad)
        alpha_optimizer.step()

        if aggregator and not aggregator.disabled:
            aggregator.update("Loss/policy_loss", actor_loss)
            aggregator.update("Loss/alpha_loss", alpha_loss)


@register_algorithm()
def main(fabric: Fabric, cfg: Dict[str, Any]):
    if "minedojo" in cfg.env.wrapper._target_.lower():
        raise ValueError(
            "MineDojo is not currently supported by DroQ agent, since it does not take "
            "into consideration the action masks provided by the environment, but needed "
            "in order to play correctly the game. "
            "As an alternative you can use one of the Dreamers' agents."
        )

    device = fabric.device
    rank = fabric.global_rank
    world_size = fabric.world_size
    fabric.seed_everything(cfg.seed)
    torch.backends.cudnn.deterministic = cfg.torch_deterministic

    # Resume from checkpoint
    if cfg.checkpoint.resume_from:
        state = fabric.load(cfg.checkpoint.resume_from)
        cfg.per_rank_batch_size = state["batch_size"] // fabric.world_size

    if len(cfg.cnn_keys.encoder) > 0:
        warnings.warn("DroQ algorithm cannot allow to use images as observations, the CNN keys will be ignored")
        cfg.cnn_keys.encoder = []

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
    if not isinstance(action_space, gym.spaces.Box):
        raise ValueError("Only continuous action space is supported for the DroQ agent")
    if not isinstance(observation_space, gym.spaces.Dict):
        raise RuntimeError(f"Unexpected observation type, should be of type Dict, got: {observation_space}")
    if len(cfg.mlp_keys.encoder) == 0:
        raise RuntimeError("You should specify at least one MLP key for the encoder: `mlp_keys.encoder=[state]`")
    for k in cfg.mlp_keys.encoder:
        if len(observation_space[k].shape) > 1:
            raise ValueError(
                "Only environments with vector-only observations are supported by the DroQ agent. "
                f"Provided environment: {cfg.env.id}"
            )
    if cfg.metric.log_level > 0:
        fabric.print("Encoder MLP keys:", cfg.mlp_keys.encoder)

    # Define the agent and the optimizer and setup them with Fabric
    act_dim = prod(action_space.shape)
    obs_dim = sum([prod(observation_space[k].shape) for k in cfg.mlp_keys.encoder])
    actor = SACActor(
        observation_dim=obs_dim,
        action_dim=act_dim,
        distribution_cfg=cfg.distribution,
        hidden_size=cfg.algo.actor.hidden_size,
        action_low=action_space.low,
        action_high=action_space.high,
    )
    critics = [
        DROQCritic(
            observation_dim=obs_dim + act_dim,
            hidden_size=cfg.algo.critic.hidden_size,
            num_critics=1,
            dropout=cfg.algo.critic.dropout,
        )
        for _ in range(cfg.algo.critic.n)
    ]
    target_entropy = -act_dim
    agent = DROQAgent(
        actor, critics, target_entropy, alpha=cfg.algo.alpha.alpha, tau=cfg.algo.tau, device=fabric.device
    )
    if cfg.checkpoint.resume_from:
        agent.load_state_dict(state["agent"])
    agent.actor = fabric.setup_module(agent.actor)
    agent.critics = [fabric.setup_module(critic) for critic in agent.critics]

    # Optimizers
    qf_optimizer = hydra.utils.instantiate(cfg.algo.critic.optimizer, params=agent.qfs.parameters())
    actor_optimizer = hydra.utils.instantiate(cfg.algo.actor.optimizer, params=agent.actor.parameters())
    alpha_optimizer = hydra.utils.instantiate(cfg.algo.alpha.optimizer, params=[agent.log_alpha])
    if cfg.checkpoint.resume_from:
        qf_optimizer.load_state_dict(state["qf_optimizer"])
        actor_optimizer.load_state_dict(state["actor_optimizer"])
        alpha_optimizer.load_state_dict(state["alpha_optimizer"])
    qf_optimizer, actor_optimizer, alpha_optimizer = fabric.setup_optimizers(
        qf_optimizer, actor_optimizer, alpha_optimizer
    )

    # Metrics
    aggregator = None
    if not MetricAggregator.disabled:
        aggregator: MetricAggregator = hydra.utils.instantiate(cfg.metric.aggregator).to(device)

    # Local data
    buffer_size = cfg.buffer.size // int(cfg.env.num_envs * fabric.world_size) if not cfg.dry_run else 1
    rb = ReplayBuffer(
        buffer_size,
        cfg.env.num_envs,
        device=device,
        memmap=cfg.buffer.memmap,
        memmap_dir=os.path.join(log_dir, "memmap_buffer", f"rank_{fabric.global_rank}"),
    )
    if cfg.checkpoint.resume_from and cfg.buffer.checkpoint:
        if isinstance(state["rb"], list) and fabric.world_size == len(state["rb"]):
            rb = state["rb"][fabric.global_rank]
        elif isinstance(state["rb"], ReplayBuffer):
            rb = state["rb"]
        else:
            raise RuntimeError(f"Given {len(state['rb'])}, but {fabric.world_size} processes are instantiated")
    step_data = TensorDict({}, batch_size=[cfg.env.num_envs], device=device)

    # Global variables
    last_train = 0
    train_step = 0
    start_step = state["update"] // fabric.world_size if cfg.checkpoint.resume_from else 1
    policy_step = state["update"] * cfg.env.num_envs if cfg.checkpoint.resume_from else 0
    last_log = state["last_log"] if cfg.checkpoint.resume_from else 0
    last_checkpoint = state["last_checkpoint"] if cfg.checkpoint.resume_from else 0
    policy_steps_per_update = int(cfg.env.num_envs * fabric.world_size)
    num_updates = int(cfg.total_steps // policy_steps_per_update) if not cfg.dry_run else 1
    learning_starts = cfg.algo.learning_starts // policy_steps_per_update if not cfg.dry_run else 0
    if cfg.checkpoint.resume_from and not cfg.buffer.checkpoint:
        learning_starts += start_step

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

    with device:
        # Get the first environment observation and start the optimization
        o = envs.reset(seed=cfg.seed)[0]
        obs = torch.cat(
            [torch.tensor(o[k], dtype=torch.float32) for k in cfg.mlp_keys.encoder], dim=-1
        )  # [N_envs, N_obs]

    for update in range(start_step, num_updates + 1):
        policy_step += cfg.env.num_envs * fabric.world_size

        # Measure environment interaction time: this considers both the model forward
        # to get the action given the observation and the time taken into the environment
        with timer("Time/env_interaction_time", SumMetric(sync_on_compute=False)):
            with torch.no_grad():
                # Sample an action given the observation received by the environment
                actions, _ = agent.actor.module(obs)
                actions = actions.cpu().numpy()
            next_obs, rewards, dones, truncated, infos = envs.step(actions)
            dones = np.logical_or(dones, truncated)

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
        real_next_obs = copy.deepcopy(next_obs)
        if "final_observation" in infos:
            for idx, final_obs in enumerate(infos["final_observation"]):
                if final_obs is not None:
                    for k, v in final_obs.items():
                        real_next_obs[k][idx] = v

        with device:
            next_obs = torch.cat(
                [torch.tensor(next_obs[k], dtype=torch.float32) for k in cfg.mlp_keys.encoder], dim=-1
            )  # [N_envs, N_obs]
            real_next_obs = torch.cat(
                [torch.tensor(real_next_obs[k], dtype=torch.float32) for k in cfg.mlp_keys.encoder], dim=-1
            )  # [N_envs, N_obs]
            actions = torch.tensor(actions, dtype=torch.float32).view(cfg.env.num_envs, -1)
            rewards = torch.tensor(rewards, dtype=torch.float32).view(cfg.env.num_envs, -1)  # [N_envs, 1]
            dones = torch.tensor(dones, dtype=torch.float32).view(cfg.env.num_envs, -1)

        step_data["dones"] = dones
        step_data["actions"] = actions
        step_data["observations"] = obs
        if not cfg.buffer.sample_next_obs:
            step_data["next_observations"] = real_next_obs
        step_data["rewards"] = rewards
        rb.add(step_data.unsqueeze(0))

        # next_obs becomes the new obs
        obs = next_obs

        # Train the agent
        if update > learning_starts:
            train(fabric, agent, actor_optimizer, qf_optimizer, alpha_optimizer, rb, aggregator, cfg)
            train_step += world_size

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

        # Checkpoint model
        if (cfg.checkpoint.every > 0 and policy_step - last_checkpoint >= cfg.checkpoint.every) or (
            update == num_updates and cfg.checkpoint.save_last
        ):
            last_checkpoint = policy_step
            state = {
                "agent": agent.state_dict(),
                "qf_optimizer": qf_optimizer.state_dict(),
                "actor_optimizer": actor_optimizer.state_dict(),
                "alpha_optimizer": alpha_optimizer.state_dict(),
                "update": update * fabric.world_size,
                "batch_size": cfg.per_rank_batch_size * fabric.world_size,
                "last_log": last_log,
                "last_checkpoint": last_checkpoint,
            }
            ckpt_path = os.path.join(log_dir, f"checkpoint/ckpt_{policy_step}_{fabric.global_rank}.ckpt")
            fabric.call(
                "on_checkpoint_coupled",
                fabric=fabric,
                ckpt_path=ckpt_path,
                state=state,
                replay_buffer=rb if cfg.buffer.checkpoint else None,
            )

    envs.close()
    if fabric.is_global_zero:
        test(agent.actor.module, fabric, cfg, log_dir)
