import time

import numpy as np
import tensorflow as tf

from baselines import logger
from baselines.common import BaseRLModel
from baselines.common.runners import AbstractEnvRunner
from baselines.acer.buffer import Buffer
from baselines.a2c.utils import batch_to_seq, seq_to_batch, Scheduler, find_trainable_variables, calc_entropy_softmax, \
    EpisodeStats, get_by_index, check_shape, avg_norm, gradient_add, q_explained_variance


def strip(var, n_envs, n_steps, flat=False):
    """
    Removes the last step in the batch

    :param var: (TensorFlow Tensor) The input Tensor
    :param n_envs: (int) The number of environments
    :param n_steps: (int) The number of steps to run for each environment
    :param flat: (bool) If the input Tensor is flat
    :return: (TensorFlow Tensor) the input tensor, without the last step in the batch
    """
    out_vars = batch_to_seq(var, n_envs, n_steps + 1, flat)
    return seq_to_batch(out_vars[:-1], flat)


def q_retrace(rewards, dones, q_i, values, rho_i, n_envs, n_steps, gamma):
    """
    Calculates the target Q-retrace

    :param rewards: ([TensorFlow Tensor]) The rewards
    :param dones: ([TensorFlow Tensor])
    :param q_i: ([TensorFlow Tensor]) The Q values for actions taken
    :param values: ([TensorFlow Tensor]) The output of the value functions
    :param rho_i: ([TensorFlow Tensor]) The importance weight for each action
    :param n_envs: (int) The number of environments
    :param n_steps: (int) The number of steps to run for each environment
    :param gamma: (float) The discount value
    :return: ([TensorFlow Tensor]) the target Q-retrace
    """
    rho_bar = batch_to_seq(tf.minimum(1.0, rho_i), n_envs, n_steps, True)  # list of len steps, shape [n_envs]
    reward_seq = batch_to_seq(rewards, n_envs, n_steps, True)  # list of len steps, shape [n_envs]
    done_seq = batch_to_seq(dones, n_envs, n_steps, True)  # list of len steps, shape [n_envs]
    q_is = batch_to_seq(q_i, n_envs, n_steps, True)
    value_sequence = batch_to_seq(values, n_envs, n_steps + 1, True)
    final_value = value_sequence[-1]
    qret = final_value
    qrets = []
    for i in range(n_steps - 1, -1, -1):
        check_shape([qret, done_seq[i], reward_seq[i], rho_bar[i], q_is[i], value_sequence[i]], [[n_envs]] * 6)
        qret = reward_seq[i] + gamma * qret * (1.0 - done_seq[i])
        qrets.append(qret)
        qret = (rho_bar[i] * (qret - q_is[i])) + value_sequence[i]
    qrets = qrets[::-1]
    qret = seq_to_batch(qrets, flat=True)
    return qret


class ACER(BaseRLModel):
    def __init__(self, policy, env, gamma=0.99, n_steps=20, nstack=4, num_procs=1, q_coef=0.5, ent_coef=0.01,
                 max_grad_norm=10, learning_rate=7e-4, lr_schedule='linear', rprop_alpha=0.99, rprop_epsilon=1e-5,
                 buffer_size=5000, replay_ratio=4, replay_start=1000, correction_term=10.0, trust_region=True,
                 alpha=0.99, delta=1, verbose=0, _init_setup_model=True):
        """
        The ACER (Actor-Critic with Experience Replay) model class, https://arxiv.org/abs/1611.01224

        :param policy: (ACERPolicy) The policy model to use (MLP, CNN, LSTM, ...)
        :param env: (Gym environment) The environment to learn from
        :param gamma: (float) The discount value
        :param n_steps: (int) The number of steps to run for each environment
        :param nstack: (int) The number of stacked frames
        :param num_procs: (int) The number of threads for TensorFlow operations
        :param q_coef: (float) The weight for the loss on the Q value
        :param ent_coef: (float) The weight for the entropic loss
        :param max_grad_norm: (float) The clipping value for the maximum gradient
        :param learning_rate: (float) The initial learning rate for the RMS prop optimizer
         :param lr_schedule: (str) The type of scheduler for the learning rate update ('linear', 'constant',
                                 'double_linear_con', 'middle_drop' or 'double_middle_drop')
        :param rprop_epsilon: (float) RMS prop optimizer epsilon
        :param rprop_alpha: (float) RMS prop optimizer decay
        :param buffer_size: (int) The buffer size in number of steps
        :param replay_ratio: (float) The number of replay learning per on policy learning on average,
                                     using a poisson distribution
        :param replay_start: (int) The minimum number of steps in the buffer, before learning replay
        :param correction_term: (float) The correction term for the weights
        :param trust_region: (bool) Enable Trust region policy optimization loss
        :param alpha: (float) The decay rate for the Exponential moving average of the parameters
        :param delta: (float) trust region delta value
        :param verbose: (int) the verbosity level: 0 none, 1 training information, 2 tensorflow debug
        :param _init_setup_model: (bool) Whether or not to build the network at the creation of the instance
        """
        super(ACER, self).__init__(env=env, requires_vec_env=True, verbose=verbose)

        self.n_steps = n_steps
        self.replay_ratio = replay_ratio
        self.nstack = nstack
        self.buffer_size = buffer_size
        self.replay_start = replay_start
        self.policy = policy
        self.gamma = gamma
        self.alpha = alpha
        self.correction_term = correction_term
        self.q_coef = q_coef
        self.ent_coef = ent_coef
        self.trust_region = trust_region
        self.delta = delta
        self.max_grad_norm = max_grad_norm
        self.rprop_alpha = rprop_alpha
        self.rprop_epsilon = rprop_epsilon
        self.learning_rate = learning_rate
        self.lr_schedule = lr_schedule
        self.num_procs = num_procs

        self.sess = None
        self.action_ph = None
        self.done_ph = None
        self.reward_ph = None
        self.mu_ph = None
        self.learning_rate_ph = None
        self.params = None
        self.polyak_model = None
        self.learning_rate_schedule = None
        self.run_ops = None
        self.names_ops = None
        self.train_model = None
        self.step_model = None
        self.step = None
        self.initial_state = None
        self.n_act = None
        self.n_batch = None

        if _init_setup_model:
            self.setup_model()

    def setup_model(self):
        super().setup_model()

        self.n_act = self.action_space.n
        self.n_batch = self.n_envs * self.n_steps

        config = tf.ConfigProto(allow_soft_placement=True,
                                intra_op_parallelism_threads=self.num_procs,
                                inter_op_parallelism_threads=self.num_procs)
        self.sess = tf.Session(config=config)

        self.action_ph = tf.placeholder(tf.int32, [self.n_batch])  # actions
        self.done_ph = tf.placeholder(tf.float32, [self.n_batch])  # dones
        self.reward_ph = tf.placeholder(tf.float32, [self.n_batch])  # rewards, not returns
        self.mu_ph = tf.placeholder(tf.float32, [self.n_batch, self.n_act])  # mu's
        self.learning_rate_ph = tf.placeholder(tf.float32, [])
        eps = 1e-6

        step_model = self.policy(self.sess, self.observation_space, self.action_space, self.n_envs, 1, self.nstack,
                                 reuse=False)
        train_model = self.policy(self.sess, self.observation_space, self.action_space, self.n_envs, self.n_steps + 1,
                                  self.nstack, reuse=True)

        self.params = find_trainable_variables("model")
        print("Params {}".format(len(self.params)))
        for var in self.params:
            print(var)

        # create polyak averaged model
        ema = tf.train.ExponentialMovingAverage(self.alpha)
        ema_apply_op = ema.apply(self.params)

        def custom_getter(getter, *args, **kwargs):
            val = ema.average(getter(*args, **kwargs))
            print(val.name)
            return val

        with tf.variable_scope("", custom_getter=custom_getter, reuse=True):
            self.polyak_model = polyak_model = self.policy(self.sess, self.observation_space, self.action_space,
                                                           self.n_envs, self.n_steps + 1, self.nstack, reuse=True)

        # Notation: (var) = batch variable, (var)s = sequence variable, (var)_i = variable index by action at step i
        value = tf.reduce_sum(train_model.policy * train_model.q_value, axis=-1)  # shape is [n_envs * (n_steps + 1)]

        # strip off last step
        # f is a distribution, chosen to be Gaussian distributions
        # with fixed diagonal covariance and mean \phi(x)
        # in the paper
        distribution_f, f_polyak, q_value = map(lambda variables: strip(variables, self.n_envs, self.n_steps),
                                                [train_model.policy, polyak_model.policy, train_model.q_value])
        # Get pi and q values for actions taken
        f_i = get_by_index(distribution_f, self.action_ph)
        q_i = get_by_index(q_value, self.action_ph)

        # Compute ratios for importance truncation
        rho = distribution_f / (self.mu_ph + eps)
        rho_i = get_by_index(rho, self.action_ph)

        # Calculate Q_retrace targets
        qret = q_retrace(self.reward_ph, self.done_ph, q_i, value, rho_i, self.n_envs, self.n_steps, self.gamma)

        # Calculate losses
        # Entropy
        entropy = tf.reduce_mean(calc_entropy_softmax(distribution_f))

        # Policy Gradient loss, with truncated importance sampling & bias correction
        value = strip(value, self.n_envs, self.n_steps, True)
        check_shape([qret, value, rho_i, f_i], [[self.n_envs * self.n_steps]] * 4)
        check_shape([rho, distribution_f, q_value], [[self.n_envs * self.n_steps, self.n_act]] * 2)

        # Truncated importance sampling
        adv = qret - value
        log_f = tf.log(f_i + eps)
        gain_f = log_f * tf.stop_gradient(adv * tf.minimum(self.correction_term, rho_i))  # [n_envs * n_steps]
        loss_f = -tf.reduce_mean(gain_f)

        # Bias correction for the truncation
        adv_bc = (q_value - tf.reshape(value, [self.n_envs * self.n_steps, 1]))  # [n_envs * n_steps, n_act]
        log_f_bc = tf.log(distribution_f + eps)  # / (f_old + eps)
        check_shape([adv_bc, log_f_bc], [[self.n_envs * self.n_steps, self.n_act]] * 2)
        gain_bc = tf.reduce_sum(log_f_bc *
                                tf.stop_gradient(
                                    adv_bc * tf.nn.relu(1.0 - (self.correction_term / (rho + eps))) * distribution_f),
                                axis=1)
        # IMP: This is sum, as expectation wrt f
        loss_bc = -tf.reduce_mean(gain_bc)

        loss_policy = loss_f + loss_bc

        # Value/Q function loss, and explained variance
        check_shape([qret, q_i], [[self.n_envs * self.n_steps]] * 2)
        explained_variance = q_explained_variance(tf.reshape(q_i, [self.n_envs, self.n_steps]),
                                                  tf.reshape(qret, [self.n_envs, self.n_steps]))
        loss_q = tf.reduce_mean(tf.square(tf.stop_gradient(qret) - q_i) * 0.5)

        # Net loss
        check_shape([loss_policy, loss_q, entropy], [[]] * 3)
        loss = loss_policy + self.q_coef * loss_q - self.ent_coef * entropy

        norm_grads_q, norm_grads_policy, avg_norm_grads_f, avg_norm_k, avg_norm_g, avg_norm_k_dot_g, avg_norm_adj = \
            None, None, None, None, None, None, None
        if self.trust_region:
            # [n_envs * n_steps, n_act]
            grad = tf.gradients(- (loss_policy - self.ent_coef * entropy) * self.n_steps * self.n_envs, distribution_f)
            # [n_envs * n_steps, n_act] # Directly computed gradient of KL divergence wrt f
            kl_grad = - f_polyak / (distribution_f + eps)
            k_dot_g = tf.reduce_sum(kl_grad * grad, axis=-1)
            adj = tf.maximum(0.0, (tf.reduce_sum(kl_grad * grad, axis=-1) - self.delta) / (
                    tf.reduce_sum(tf.square(kl_grad), axis=-1) + eps))  # [n_envs * n_steps]

            # Calculate stats (before doing adjustment) for logging.
            avg_norm_k = avg_norm(kl_grad)
            avg_norm_g = avg_norm(grad)
            avg_norm_k_dot_g = tf.reduce_mean(tf.abs(k_dot_g))
            avg_norm_adj = tf.reduce_mean(tf.abs(adj))

            grad = grad - tf.reshape(adj, [self.n_envs * self.n_steps, 1]) * kl_grad
            # These are turst region adjusted gradients wrt f ie statistics of policy pi
            grads_f = -grad / (self.n_envs * self.n_steps)
            grads_policy = tf.gradients(distribution_f, self.params, grads_f)
            grads_q = tf.gradients(loss_q * self.q_coef, self.params)
            grads = [gradient_add(g1, g2, param) for (g1, g2, param) in zip(grads_policy, grads_q, self.params)]

            avg_norm_grads_f = avg_norm(grads_f) * (self.n_steps * self.n_envs)
            norm_grads_q = tf.global_norm(grads_q)
            norm_grads_policy = tf.global_norm(grads_policy)
        else:
            grads = tf.gradients(loss, self.params)

        norm_grads = None
        if self.max_grad_norm is not None:
            grads, norm_grads = tf.clip_by_global_norm(grads, self.max_grad_norm)
        grads = list(zip(grads, self.params))
        trainer = tf.train.RMSPropOptimizer(learning_rate=self.learning_rate_ph, decay=self.rprop_alpha,
                                            epsilon=self.rprop_epsilon)
        _opt_op = trainer.apply_gradients(grads)

        # so when you call _train, you first do the gradient step, then you apply ema
        with tf.control_dependencies([_opt_op]):
            _train = tf.group(ema_apply_op)

        # Ops/Summaries to run, and their names for logging
        assert norm_grads is not None
        run_ops = [_train, loss, loss_q, entropy, loss_policy, loss_f, loss_bc, explained_variance, norm_grads]
        names_ops = ['loss', 'loss_q', 'entropy', 'loss_policy', 'loss_f', 'loss_bc', 'explained_variance',
                     'norm_grads']
        if self.trust_region:
            self.run_ops = run_ops + [norm_grads_q, norm_grads_policy, avg_norm_grads_f, avg_norm_k, avg_norm_g,
                                      avg_norm_k_dot_g, avg_norm_adj]
            self.names_ops = names_ops + ['norm_grads_q', 'norm_grads_policy', 'avg_norm_grads_f', 'avg_norm_k',
                                          'avg_norm_g', 'avg_norm_k_dot_g', 'avg_norm_adj']

        self.train_model = train_model
        self.step_model = step_model
        self.step = step_model.step
        self.initial_state = step_model.initial_state

        tf.global_variables_initializer().run(session=self.sess)

    def _train_step(self, obs, actions, rewards, dones, mus, states, masks, steps):
        """
        applies a training step to the model

        :param obs: ([float]) The input observations
        :param actions: ([float]) The actions taken
        :param rewards: ([float]) The rewards from the environment
        :param dones: ([bool]) Whether or not the episode is over (aligned with reward, used for reward calculation)
        :param mus: ([float]) The logits values
        :param states: ([float]) The states (used for reccurent policies)
        :param masks: ([bool]) Whether or not the episode is over (used for reccurent policies)
        :param steps: (int) the number of steps done so far
        :return: ([str], [float]) the list of update operation name, and the list of the results of the operations
        """
        cur_lr = self.learning_rate_schedule.value_steps(steps)
        td_map = {self.train_model.obs_ph: obs, self.polyak_model.obs_ph: obs, self.action_ph: actions,
                  self.reward_ph: rewards, self.done_ph: dones, self.mu_ph: mus, self.learning_rate_ph: cur_lr}

        if len(states) != 0:
            td_map[self.train_model.states_ph] = states
            td_map[self.train_model.masks_ph] = masks
            td_map[self.polyak_model.states_ph] = states
            td_map[self.polyak_model.masks_ph] = masks

        return self.names_ops, self.sess.run(self.run_ops, td_map)[1:]  # strip off _train

    def learn(self, total_timesteps, callback=None, seed=None, log_interval=100):
        self._setup_learn(seed)

        self.learning_rate_schedule = Scheduler(initial_value=self.learning_rate, n_values=total_timesteps,
                                                schedule=self.lr_schedule)

        episode_stats = EpisodeStats(self.n_steps, self.n_envs)

        runner = _Runner(env=self.env, model=self, n_steps=self.n_steps, nstack=self.nstack)
        if self.replay_ratio > 0:
            buffer = Buffer(env=self.env, n_steps=self.n_steps, nstack=self.nstack, size=self.buffer_size)
        else:
            buffer = None

        t_start = time.time()

        # n_batch samples, 1 on_policy call and multiple off-policy calls
        for steps in range(0, total_timesteps, self.n_batch):
            enc_obs, obs, actions, rewards, mus, dones, masks = runner.run()
            episode_stats.feed(rewards, dones)

            if buffer is not None:
                buffer.put(enc_obs, actions, rewards, mus, dones, masks)

            # reshape stuff correctly
            obs = obs.reshape(runner.batch_ob_shape)
            actions = actions.reshape([runner.n_batch])
            rewards = rewards.reshape([runner.n_batch])
            mus = mus.reshape([runner.n_batch, runner.n_act])
            dones = dones.reshape([runner.n_batch])
            masks = masks.reshape([runner.batch_ob_shape[0]])

            names_ops, values_ops = self._train_step(obs, actions, rewards, dones, mus, self.initial_state, masks,
                                                     steps)

            if callback is not None:
                callback(locals(), globals())

            if self.verbose >= 1 and (int(steps / runner.n_batch) % log_interval == 0):
                logger.record_tabular("total_timesteps", steps)
                logger.record_tabular("fps", int(steps / (time.time() - t_start)))
                # IMP: In EpisodicLife env, during training, we get done=True at each loss of life,
                # not just at the terminal state. Thus, this is mean until end of life, not end of episode.
                # For true episode rewards, see the monitor files in the log folder.
                logger.record_tabular("mean_episode_length", episode_stats.mean_length())
                logger.record_tabular("mean_episode_reward", episode_stats.mean_reward())
                for name, val in zip(names_ops, values_ops):
                    logger.record_tabular(name, float(val))
                logger.dump_tabular()

            if self.replay_ratio > 0 and buffer.has_atleast(self.replay_start):
                samples_number = np.random.poisson(self.replay_ratio)
                for _ in range(samples_number):
                    # get obs, actions, rewards, mus, dones from buffer.
                    obs, actions, rewards, mus, dones, masks = buffer.get()

                    # reshape stuff correctly
                    obs = obs.reshape(runner.batch_ob_shape)
                    actions = actions.reshape([runner.n_batch])
                    rewards = rewards.reshape([runner.n_batch])
                    mus = mus.reshape([runner.n_batch, runner.n_act])
                    dones = dones.reshape([runner.n_batch])
                    masks = masks.reshape([runner.batch_ob_shape[0]])

                    self._train_step(obs, actions, rewards, dones, mus, self.initial_state, masks, steps)

        return self

    def predict(self, observation, state=None, mask=None):
        if state is None:
            state = self.initial_state
        if mask is None:
            mask = [False for _ in range(self.n_envs)]
        observation = np.array(observation).reshape(self.observation_space.shape)

        actions, _, states = self.policy.step(observation, state, mask)
        return actions, states

    def action_probability(self, observation, state=None, mask=None):
        if state is None:
            state = self.initial_state
        if mask is None:
            mask = [False for _ in range(self.n_envs)]
        observation = np.array(observation).reshape(self.observation_space.shape)

        _, proba, _ = self.policy.step(observation, state, mask)
        return proba

    def save(self, save_path):
        data = {
            "gamma": self.gamma,
            "n_steps": self.n_steps,
            "nstack": self.nstack,
            "q_coef": self.q_coef,
            "ent_coef": self.ent_coef,
            "max_grad_norm": self.max_grad_norm,
            "learning_rate": self.learning_rate,
            "lr_schedule": self.lr_schedule,
            "rprop_alpha": self.rprop_alpha,
            "rprop_epsilon": self.rprop_epsilon,
            "replay_ratio": self.replay_ratio,
            "replay_start": self.replay_start,
            "verbose": self.verbose,
            "policy": self.policy,
            "observation_space": self.observation_space,
            "action_space": self.action_space,
            "n_envs": self.n_envs
        }

        params = self.sess.run(self.params)

        self._save_to_file(save_path, data=data, params=params)

    @classmethod
    def load(cls, load_path, env=None, **kwargs):
        data, params = cls._load_from_file(load_path)

        model = cls(policy=data["policy"], env=env, _init_setup_model=False)
        model.__dict__.update(data)
        model.__dict__.update(kwargs)
        model.set_env(env)
        model.setup_model()

        restores = []
        for param, loaded_p in zip(model.params, params):
            restores.append(param.assign(loaded_p))
        model.sess.run(restores)

        return model


class _Runner(AbstractEnvRunner):
    def __init__(self, env, model, n_steps, nstack):
        """
        A runner to learn the policy of an environment for a model

        :param env: (Gym environment) The environment to learn from
        :param model: (Model) The model to learn
        :param n_steps: (int) The number of steps to run for each environment
        :param nstack: (int) The number of stacked frames
        """
        super(_Runner, self).__init__(env=env, model=model, n_steps=n_steps)
        self.nstack = nstack
        obs_height, obs_width, obs_num_channels = env.observation_space.shape
        self.num_channels = obs_num_channels  # obs_num_channels = 1 for atari, but just in case
        self.n_env = n_env = env.num_envs
        self.n_act = env.action_space.n
        self.n_batch = n_env * n_steps
        self.batch_ob_shape = (n_env * (n_steps + 1), obs_height, obs_width, obs_num_channels * nstack)
        self.obs = np.zeros((n_env, obs_height, obs_width, obs_num_channels * nstack), dtype=np.uint8)
        obs = env.reset()
        self.update_obs(obs)

    def update_obs(self, obs, dones=None):
        """
        Update the observation for rolling observation with stacking

        :param obs: ([int] or [float]) The input observation
        :param dones: ([bool])
        """
        if dones is not None:
            self.obs *= (1 - dones.astype(np.uint8))[:, None, None, None]
        self.obs = np.roll(self.obs, shift=-self.num_channels, axis=3)
        self.obs[:, :, :, -self.num_channels:] = obs[:, :, :, :]

    def run(self):
        """
        Run a step leaning of the model

        :return: ([float], [float], [float], [float], [float], [bool], [float])
                 encoded observation, observations, actions, rewards, mus, dones, masks
        """
        enc_obs = np.split(self.obs, self.nstack, axis=3)  # so now list of obs steps
        mb_obs, mb_actions, mb_mus, mb_dones, mb_rewards = [], [], [], [], []
        for _ in range(self.n_steps):
            actions, mus, states = self.model.step(self.obs, state=self.states, mask=self.dones)
            mb_obs.append(np.copy(self.obs))
            mb_actions.append(actions)
            mb_mus.append(mus)
            mb_dones.append(self.dones)
            obs, rewards, dones, _ = self.env.step(actions)
            # states information for statefull models like LSTM
            self.states = states
            self.dones = dones
            self.update_obs(obs, dones)
            mb_rewards.append(rewards)
            enc_obs.append(obs)
        mb_obs.append(np.copy(self.obs))
        mb_dones.append(self.dones)

        enc_obs = np.asarray(enc_obs, dtype=np.uint8).swapaxes(1, 0)
        mb_obs = np.asarray(mb_obs, dtype=np.uint8).swapaxes(1, 0)
        mb_actions = np.asarray(mb_actions, dtype=np.int32).swapaxes(1, 0)
        mb_rewards = np.asarray(mb_rewards, dtype=np.float32).swapaxes(1, 0)
        mb_mus = np.asarray(mb_mus, dtype=np.float32).swapaxes(1, 0)

        mb_dones = np.asarray(mb_dones, dtype=np.bool).swapaxes(1, 0)

        mb_masks = mb_dones  # Used for statefull models like LSTM's to mask state when done
        mb_dones = mb_dones[:, 1:]  # Used for calculating returns. The dones array is now aligned with rewards

        # shapes are now [n_env, n_steps, []]
        # When pulling from buffer, arrays will now be reshaped in place, preventing a deep copy.

        return enc_obs, mb_obs, mb_actions, mb_rewards, mb_mus, mb_dones, mb_masks
