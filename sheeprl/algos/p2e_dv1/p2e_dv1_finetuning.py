from __future__ import annotations

import copy
import os
import pathlib
import warnings
from typing import Any, Dict

import gymnasium as gym
import hydra
import numpy as np
import torch
from lightning.fabric import Fabric
from tensordict import TensorDict
from torch.utils.data import BatchSampler
from torchmetrics import SumMetric

from sheeprl.algos.dreamer_v1.agent import PlayerDV1
from sheeprl.algos.dreamer_v1.dreamer_v1 import train
from sheeprl.algos.dreamer_v2.utils import test
from sheeprl.algos.p2e_dv1.agent import build_models
from sheeprl.data.buffers import AsyncReplayBuffer
from sheeprl.utils.env import make_env
from sheeprl.utils.logger import create_tensorboard_logger, get_log_dir
from sheeprl.utils.metric import MetricAggregator
from sheeprl.utils.registry import register_algorithm
from sheeprl.utils.timer import timer
from sheeprl.utils.utils import polynomial_decay

# Decomment the following line if you are using MineDojo on an headless machine
# os.environ["MINEDOJO_HEADLESS"] = "1"


@register_algorithm()
def main(fabric: Fabric, cfg: Dict[str, Any], exploration_cfg: Dict[str, Any]):
    device = fabric.device
    rank = fabric.global_rank
    world_size = fabric.world_size
    fabric.seed_everything(cfg.seed)
    torch.backends.cudnn.deterministic = cfg.torch_deterministic

    ckpt_path = pathlib.Path(cfg.checkpoint.exploration_ckpt_path)
    resume_from_checkpoint = cfg.checkpoint.resume_from is not None
    # Finetuning that was interrupted for some reason
    if resume_from_checkpoint:
        state = fabric.load(pathlib.Path(cfg.checkpoint.resume_from))
        cfg.per_rank_batch_size = state["batch_size"] // world_size
    else:
        state = fabric.load(ckpt_path)

        # All the models must be equal to the ones of the exploration phase
    cfg.algo.gamma = exploration_cfg.algo.gamma
    cfg.algo.lmbda = exploration_cfg.algo.lmbda
    cfg.algo.horizon = exploration_cfg.algo.horizon
    cfg.algo.layer_norm = exploration_cfg.algo.layer_norm
    cfg.algo.dense_units = exploration_cfg.algo.dense_units
    cfg.algo.mlp_layers = exploration_cfg.algo.mlp_layers
    cfg.algo.dense_act = exploration_cfg.algo.dense_act
    cfg.algo.cnn_act = exploration_cfg.algo.cnn_act
    cfg.algo.world_model = exploration_cfg.algo.world_model
    cfg.algo.actor = exploration_cfg.algo.actor
    cfg.algo.critic = exploration_cfg.algo.critic
    # Rewards must be clipped in the same way as during exploration
    cfg.env.clip_rewards = exploration_cfg.env.clip_rewards
    # If the buffer is the same of the exploration, then we have to mantain the same number
    # of environments:
    #   - With less environments, you will replay too old experiences after a certain number of steps.
    #   - With more environments, you will raise an exception when you add new experienves.
    if cfg.buffer.load_from_exploration and exploration_cfg.buffer.checkpoint:
        cfg.env.num_envs = exploration_cfg.env.num_envs
    # There must be the same cnn and mlp keys during exploration and finetuning
    cfg.cnn_keys = exploration_cfg.cnn_keys
    cfg.mlp_keys = exploration_cfg.mlp_keys

    # These arguments cannot be changed
    cfg.env.screen_size = 64
    cfg.env.frame_stack = 1

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

    world_model, actor_task, critic_task, actor_exploration, _ = build_models(
        fabric,
        actions_dim,
        is_continuous,
        cfg,
        observation_space,
        state["world_model"],
        state["actor_task"],
        state["critic_task"],
        state["actor_exploration"],
    )

    player = PlayerDV1(
        world_model.encoder.module,
        world_model.rssm.recurrent_model.module,
        world_model.rssm.representation_model.module,
        actor_exploration.module if cfg.algo.player.actor_type == "exploration" else actor_task.module,
        actions_dim,
        cfg.env.num_envs,
        cfg.algo.world_model.stochastic_size,
        cfg.algo.world_model.recurrent_model.recurrent_state_size,
        fabric.device,
        actor_type=cfg.algo.player.actor_type,
    )

    # Optimizers
    world_optimizer = hydra.utils.instantiate(cfg.algo.world_model.optimizer, params=world_model.parameters())
    actor_task_optimizer = hydra.utils.instantiate(cfg.algo.actor.optimizer, params=actor_task.parameters())
    critic_task_optimizer = hydra.utils.instantiate(cfg.algo.critic.optimizer, params=critic_task.parameters())
    world_optimizer.load_state_dict(state["world_optimizer"])
    actor_task_optimizer.load_state_dict(state["actor_task_optimizer"])
    critic_task_optimizer.load_state_dict(state["critic_task_optimizer"])
    world_optimizer, actor_task_optimizer, critic_task_optimizer = fabric.setup_optimizers(
        world_optimizer, actor_task_optimizer, critic_task_optimizer
    )

    # Metrics
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
    if resume_from_checkpoint or (cfg.buffer.load_from_exploration and exploration_cfg.buffer.checkpoint):
        if isinstance(state["rb"], list) and world_size == len(state["rb"]):
            rb = state["rb"][fabric.global_rank]
        elif isinstance(state["rb"], AsyncReplayBuffer):
            rb = state["rb"]
        else:
            raise RuntimeError(f"Given {len(state['rb'])}, but {world_size} processes are instantiated")
    step_data = TensorDict({}, batch_size=[cfg.env.num_envs], device="cpu")
    expl_decay_steps = state["expl_decay_steps"] if resume_from_checkpoint else 0

    # Global variables
    train_step = 0
    last_train = 0
    start_step = state["update"] // world_size if resume_from_checkpoint else 1
    policy_step = state["update"] * cfg.env.num_envs if resume_from_checkpoint else 0
    last_log = state["last_log"] if resume_from_checkpoint else 0
    last_checkpoint = state["last_checkpoint"] if resume_from_checkpoint else 0
    policy_steps_per_update = int(cfg.env.num_envs * world_size)
    updates_before_training = cfg.algo.train_every // policy_steps_per_update if not cfg.dry_run else 0
    num_updates = int(cfg.total_steps // policy_steps_per_update) if not cfg.dry_run else 1
    learning_starts = (cfg.algo.learning_starts // policy_steps_per_update) if not cfg.dry_run else 0
    if resume_from_checkpoint and not cfg.buffer.checkpoint:
        learning_starts += start_step
    max_step_expl_decay = cfg.algo.actor.max_step_expl_decay // (cfg.algo.per_rank_gradient_steps * world_size)
    if resume_from_checkpoint:
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
            torch_obs = torch_obs.float()
        step_data[k] = torch_obs
        obs[k] = torch_obs
    step_data["dones"] = torch.zeros(cfg.env.num_envs, 1)
    step_data["actions"] = torch.zeros(cfg.env.num_envs, sum(actions_dim))
    step_data["rewards"] = torch.zeros(cfg.env.num_envs, 1)
    rb.add(step_data[None, ...])
    player.init_states()

    for update in range(start_step, num_updates + 1):
        policy_step += cfg.env.num_envs * world_size

        # Measure environment interaction time: this considers both the model forward
        # to get the action given the observation and the time taken into the environment
        with timer("Time/env_interaction_time", SumMetric(sync_on_compute=False)):
            with torch.no_grad():
                preprocessed_obs = {}
                for k, v in obs.items():
                    if k in cfg.cnn_keys.encoder:
                        preprocessed_obs[k] = v[None, ...].to(device) / 255 - 0.5
                    else:
                        preprocessed_obs[k] = v[None, ...].to(device)
                mask = {k: v for k, v in preprocessed_obs.items() if k.startswith("mask")}
                if len(mask) == 0:
                    mask = None
                real_actions = actions = player.get_exploration_action(preprocessed_obs, mask)
                actions = torch.cat(actions, -1).cpu().numpy()
                if is_continuous:
                    real_actions = torch.cat(real_actions, -1).cpu().numpy()
                else:
                    real_actions = np.array([real_act.cpu().argmax(dim=-1).numpy() for real_act in real_actions])

            o, rewards, dones, truncated, infos = envs.step(real_actions.reshape(envs.action_space.shape))
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
        real_next_obs = copy.deepcopy(o)
        if "final_observation" in infos:
            for idx, final_obs in enumerate(infos["final_observation"]):
                if final_obs is not None:
                    for k, v in final_obs.items():
                        if k == "rgb":
                            real_next_obs[idx] = v

        next_obs = {
            k: torch.from_numpy(v).view(cfg.env.num_envs, *v.shape[1:]) for k, v in o.items() if k.startswith("mask")
        }
        for k in obs_keys:  # [N_envs, N_obs]
            next_obs[k] = torch.from_numpy(o[k]).view(cfg.env.num_envs, *o[k].shape[1:])
            step_data[k] = torch.from_numpy(real_next_obs[k]).view(cfg.env.num_envs, *real_next_obs[k].shape[1:])
            if k in cfg.mlp_keys.encoder:
                next_obs[k] = next_obs[k].float()
                step_data[k] = step_data[k].float()
        actions = torch.from_numpy(actions).view(cfg.env.num_envs, -1).float()
        rewards = torch.from_numpy(rewards).view(cfg.env.num_envs, -1).float()
        dones = torch.from_numpy(dones).view(cfg.env.num_envs, -1).float()

        # next_obs becomes the new obs
        obs = next_obs

        step_data["dones"] = dones
        step_data["actions"] = actions
        step_data["rewards"] = clip_rewards_fn(rewards)
        rb.add(step_data[None, ...])

        # Reset and save the observation coming from the automatic reset
        dones_idxes = dones.nonzero(as_tuple=True)[0].tolist()
        reset_envs = len(dones_idxes)
        if reset_envs > 0:
            reset_data = TensorDict({}, batch_size=[reset_envs], device="cpu")
            for k in next_obs.keys():
                reset_data[k] = next_obs[k][dones_idxes]
            reset_data["dones"] = torch.zeros(reset_envs, 1)
            reset_data["actions"] = torch.zeros(reset_envs, np.sum(actions_dim))
            reset_data["rewards"] = torch.zeros(reset_envs, 1)
            rb.add(reset_data[None, ...], dones_idxes)
            # Reset dones so that `is_first` is updated
            for d in dones_idxes:
                step_data["dones"][d] = torch.zeros_like(step_data["dones"][d])
            # Reset internal agent states
            player.init_states(dones_idxes)

        updates_before_training -= 1

        # Train the agent
        if update >= learning_starts and updates_before_training <= 0:
            if player.actor_type == "exploration":
                player.actor = actor_task.module
                player.actor_type = "task"
            local_data = rb.sample(
                cfg.per_rank_batch_size,
                sequence_length=cfg.per_rank_sequence_length,
                n_samples=cfg.algo.per_rank_gradient_steps,
            ).to(device)
            distributed_sampler = BatchSampler(range(local_data.shape[0]), batch_size=1, drop_last=False)
            # Start training
            with timer("Time/train_time", SumMetric(sync_on_compute=cfg.metric.sync_on_compute)):
                for i in distributed_sampler:
                    train(
                        fabric,
                        world_model,
                        actor_task,
                        critic_task,
                        world_optimizer,
                        actor_task_optimizer,
                        critic_task_optimizer,
                        local_data[i].view(cfg.per_rank_sequence_length, cfg.per_rank_batch_size),
                        aggregator,
                        cfg,
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
            state = {
                "world_model": world_model.state_dict(),
                "actor_task": actor_task.state_dict(),
                "critic_task": critic_task.state_dict(),
                "world_optimizer": world_optimizer.state_dict(),
                "actor_task_optimizer": actor_task_optimizer.state_dict(),
                "critic_task_optimizer": critic_task_optimizer.state_dict(),
                "expl_decay_steps": expl_decay_steps,
                "update": update * world_size,
                "batch_size": cfg.per_rank_batch_size * world_size,
                "actor_exploration": actor_exploration.state_dict(),
                "last_log": last_log,
                "last_checkpoint": last_checkpoint,
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
    # task test few-shot
    if fabric.is_global_zero:
        player.actor = actor_task.module
        player.actor_type = "task"
        test(player, fabric, cfg, log_dir, "few-shot")
