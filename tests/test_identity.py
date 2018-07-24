import random

import pytest
import tensorflow as tf
import numpy as np
from gym.spaces.prng import np_random

from baselines.a2c import A2C
from baselines.ppo2 import PPO2
from baselines.common.identity_env import IdentityEnv
from baselines.common.vec_env.dummy_vec_env import DummyVecEnv
from baselines.a2c.policies import MlpPolicy


learn_func_list = [
    lambda e: A2C(policy=MlpPolicy, env=e).learn(total_timesteps=50000, seed=0),
    lambda e: PPO2(policy=MlpPolicy, env=e, learning_rate=1e-3, n_steps=128, ent_coef=0.01).learn(
        total_timesteps=50000, seed=0)
]


@pytest.mark.slow
@pytest.mark.parametrize("learn_func", learn_func_list)
def test_identity(learn_func):
    """
    Test if the algorithm (with a given policy) 
    can learn an identity transformation (i.e. return observation as an action)

    :param learn_func: (lambda (Gym Environment): A2CPolicy) the policy generator
    """
    tf.reset_default_graph()
    np.random.seed(0)
    np_random.seed(0)
    random.seed(0)

    env = DummyVecEnv([lambda: IdentityEnv(10)])

    tf.set_random_seed(0)
    model = learn_func(env)

    n_trials = 1000
    reward_sum = 0
    obs = env.reset()
    for _ in range(n_trials):
        action, _ = model.predict(obs)
        obs, reward, _, _ = env.step(action)
        reward_sum += reward

    assert reward_sum > 0.9 * n_trials
