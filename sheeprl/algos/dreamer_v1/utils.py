from typing import Tuple

import torch
import torch.nn.functional as F
from torch import Tensor
from torch.distributions import Distribution, Independent, Normal

AGGREGATOR_KEYS = {
    "Rewards/rew_avg",
    "Game/ep_len_avg",
    "Loss/world_model_loss",
    "Loss/value_loss",
    "Loss/policy_loss",
    "Loss/observation_loss",
    "Loss/reward_loss",
    "Loss/state_loss",
    "Loss/continue_loss",
    "State/post_entropy",
    "State/prior_entropy",
    "State/kl",
    "Params/exploration_amount",
    "Grads/world_model",
    "Grads/actor",
    "Grads/critic",
}


def compute_lambda_values(
    rewards: Tensor,
    values: Tensor,
    done_mask: Tensor,
    last_values: Tensor,
    horizon: int = 15,
    lmbda: float = 0.95,
) -> Tensor:
    """
    Compute the lambda values by keeping the gradients of the variables.

    Args:
        rewards (Tensor): the estimated rewards in the latent space.
        values (Tensor): the estimated values in the latent space.
        done_mask (Tensor): 1s for the entries that are relative to a terminal step, 0s otherwise.
        last_values (Tensor): the next values for the last state in the horzon.
        horizon: (int, optional): the horizon of imagination.
            Default to 15.
        lmbda (float, optional): the discout lmbda factor for the lambda values computation.
            Default to 0.95.

    Returns:
        The tensor of the computed lambda values.
    """
    last_values = torch.clone(last_values)
    last_lambda_values = 0
    lambda_targets = []
    for step in reversed(range(horizon - 1)):
        if step == horizon - 2:
            next_values = last_values
        else:
            next_values = values[step + 1] * (1 - lmbda)
        delta = rewards[step] + next_values * done_mask[step]
        last_lambda_values = delta + lmbda * done_mask[step] * last_lambda_values
        lambda_targets.append(last_lambda_values)
    return torch.stack(list(reversed(lambda_targets)), dim=0)


def compute_stochastic_state(
    state_information: Tensor, event_shape: int = 1, min_std: float = 0.1, validate_args: bool = False
) -> Tuple[Tuple[Tensor, Tensor], Tensor]:
    """
    Compute the stochastic state from the information of the distribution of the stochastic state.

    Args:
        state_information (Tensor): information about the distribution of the stochastic state,
            it is the output of either the representation model or the transition model.
        event_shape (int): how many batch dimensions have to be reinterpreted as event dims.
            Default to 1.
        min_std (float): the minimum value for the standard deviation.
            Default to 0.1.

    Returns:
        The mean and the standard deviation of the distribution of the stochastic state.
        The sampled stochastic state.
    """
    mean, std = torch.chunk(state_information, 2, -1)
    std = F.softplus(std) + min_std
    state_distribution: Distribution = Normal(mean, std, validate_args=validate_args)
    if event_shape:
        # it is necessary an Independent distribution because
        # it is necessary to create (batch_size * sequence_length) independent distributions,
        # each producing a sample of size equal to the stochastic size
        state_distribution = Independent(state_distribution, event_shape, validate_args=validate_args)
    stochastic_state = state_distribution.rsample()
    return (mean, std), stochastic_state
