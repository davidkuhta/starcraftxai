import os.path as osp
import time
import joblib
import numpy as np
import tensorflow as tf
from baselines import logger

from baselines.common import set_global_seeds, explained_variance

from baselines.acktr.utils import discount_with_dones
from baselines.acktr.utils import Scheduler, find_trainable_variables
from baselines.acktr.utils import cat_entropy, mse
from baselines.acktr import kfac

from pysc2.env import environment
from pysc2.lib import actions as sc2_actions

class Model(object):

  def __init__(self, policy, ob_space, ac_space,
               nenvs,total_timesteps, nprocs=32, nsteps=20,
               nstack=4, ent_coef=0.01, vf_coef=0.5, vf_fisher_coef=1.0,
               lr=0.25, max_grad_norm=0.5,
               kfac_clip=0.001, lrschedule='linear'):
    config = tf.ConfigProto(allow_soft_placement=True,
                            intra_op_parallelism_threads=nprocs,
                            inter_op_parallelism_threads=nprocs)
    config.gpu_options.allow_growth = True
    self.sess = sess = tf.Session(config=config)
    #nact = ac_space.n
    nbatch = nenvs * nsteps
    A = tf.placeholder(tf.int32, [nbatch])
    SUB1 = tf.placeholder(tf.int32, [nbatch])
    SUB2 = tf.placeholder(tf.int32, [nbatch])
    SUB3 = tf.placeholder(tf.int32, [nbatch])
    X1 = tf.placeholder(tf.int32, [nbatch])
    Y1 = tf.placeholder(tf.int32, [nbatch])
    X2 = tf.placeholder(tf.int32, [nbatch])
    Y2 = tf.placeholder(tf.int32, [nbatch])

    ADV = tf.placeholder(tf.float32, [nbatch])
    R = tf.placeholder(tf.float32, [nbatch])
    PG_LR = tf.placeholder(tf.float32, [])
    VF_LR = tf.placeholder(tf.float32, [])

    self.model = step_model = policy(sess, ob_space, ac_space, nenvs, 1, nstack, reuse=False)
    self.model2 = train_model = policy(sess, ob_space, ac_space, nenvs, nsteps, nstack, reuse=True)

    logpac = tf.nn.sparse_softmax_cross_entropy_with_logits(logits=train_model.pi, labels=A) \
             * tf.nn.sparse_softmax_cross_entropy_with_logits(logits=train_model.pi_sub1, labels=SUB1) \
             * tf.nn.sparse_softmax_cross_entropy_with_logits(logits=train_model.pi_sub2, labels=SUB2) \
             * tf.nn.sparse_softmax_cross_entropy_with_logits(logits=train_model.pi_sub3, labels=SUB3) \
             * tf.nn.sparse_softmax_cross_entropy_with_logits(logits=train_model.pi_x1, labels=X1) \
             * tf.nn.sparse_softmax_cross_entropy_with_logits(logits=train_model.pi_y1, labels=Y1) \
             * tf.nn.sparse_softmax_cross_entropy_with_logits(logits=train_model.pi_x2, labels=X2) \
             * tf.nn.sparse_softmax_cross_entropy_with_logits(logits=train_model.pi_y2, labels=Y2)

    self.logits = logits = train_model.pi

    ##training loss
    pg_loss = tf.reduce_mean(ADV*logpac) * tf.reduce_mean(ADV)
    entropy = tf.reduce_mean(cat_entropy(train_model.pi))
    pg_loss = pg_loss - ent_coef * entropy
    vf_loss = tf.reduce_mean(mse(tf.squeeze(train_model.vf), R))
    train_loss = pg_loss + vf_coef * vf_loss

    ##Fisher loss construction
    self.pg_fisher = pg_fisher_loss = -tf.reduce_mean(logpac)
    sample_net = train_model.vf + tf.random_normal(tf.shape(train_model.vf))
    self.vf_fisher = vf_fisher_loss = - vf_fisher_coef*tf.reduce_mean(tf.pow(train_model.vf - tf.stop_gradient(sample_net), 2))
    self.joint_fisher = joint_fisher_loss = pg_fisher_loss + vf_fisher_loss

    self.params=params = find_trainable_variables("model")

    self.grads_check = grads = tf.gradients(train_loss,params)

    with tf.device('/gpu:0'):
      self.optim = optim = kfac.KfacOptimizer(learning_rate=PG_LR, clip_kl=kfac_clip, \
                                              momentum=0.9, kfac_update=1, epsilon=0.01, \
                                              stats_decay=0.99, async=1, cold_iter=10, max_grad_norm=max_grad_norm)

      update_stats_op = optim.compute_and_apply_stats(joint_fisher_loss, var_list=params)
      train_op, q_runner = optim.apply_gradients(list(zip(grads,params)))
    self.q_runner = q_runner
    self.lr = Scheduler(v=lr, nvalues=total_timesteps, schedule=lrschedule)

    def train(obs, states, rewards, masks, actions, sub1, sub2, sub3, x1, y1, x2, y2, values):
      advs = rewards - values
      for step in range(len(obs)):
        cur_lr = self.lr.value()

      td_map = {train_model.X:obs, A:actions, SUB1:sub1, SUB2:sub2, SUB3:sub3, X1:x1, Y1:y1, X2:x2, Y2:y2, ADV:advs, R:rewards, PG_LR:cur_lr}
      if states != []:
        td_map[train_model.S] = states
        td_map[train_model.M] = masks

      policy_loss, value_loss, policy_entropy, _ = sess.run(
        [pg_loss, vf_loss, entropy, train_op],
        td_map
      )
      return policy_loss, value_loss, policy_entropy

    def save(save_path):
      ps = sess.run(params)
      joblib.dump(ps, save_path)

    def load(load_path):
      loaded_params = joblib.load(load_path)
      restores = []
      for p, loaded_p in zip(params, loaded_params):
        restores.append(p.assign(loaded_p))
      sess.run(restores)

    self.train = train
    self.save = save
    self.load = load
    self.train_model = train_model
    self.step_model = step_model
    self.step = step_model.step
    self.value = step_model.value
    self.initial_state = step_model.initial_state
    print("global_variables_initializer start")
    tf.global_variables_initializer().run(session=sess)
    print("global_variables_initializer complete")

class Runner(object):

  def __init__(self, env, model, nsteps, nstack, gamma):
    self.env = env
    self.model = model
    nh, nw, nc = (64, 64, 13)
    self.nsteps = nsteps
    self.nenv = nenv = env.num_envs
    self.batch_ob_shape = (nenv*nsteps, nc*nstack, nh, nw)
    self.batch_coord_shape = (nenv*nsteps, 64)
    self.obs = np.zeros((nenv, nc*nstack, nh, nw), dtype=np.uint8)
    self.available_actions = None
    self.base_act_mask = np.full((self.nenv, 524), 0, dtype=np.uint8)
    obs, rewards, dones, available_actions = env.reset()
    self.update_obs(obs) # (2,13,64,64)
    self.update_available(available_actions)
    self.gamma = gamma
    self.states = model.initial_state
    self.dones = [False for _ in range(nenv)]

  def update_obs(self, obs):
    self.obs = np.roll(self.obs, shift=-13*self.nsteps, axis=1)
    self.obs[:, -13:, :, :] = obs[:, :, :, :]

  def update_available(self, _available_actions):
    self.available_actions = _available_actions
    # avail = np.array([[0,1,2,3,4,7], [0,1,2,3,4,7]])
    self.base_act_mask = np.full((self.nenv, 524), 0, dtype=np.uint8)
    for env_num, list in enumerate(_available_actions):
      for action_num in list:
        self.base_act_mask[env_num][action_num] = 1

  def valid_base_action(self, base_actions):
    for env_num, list in enumerate(self.available_actions):
      if base_actions[env_num] not in list:
        base_actions[env_num] = np.random.choice(list)
    return base_actions

  def get_sub_act_mask(self, base_action_spec):
    sub1_act_mask = np.zeros((self.nenv, 2))
    sub2_act_mask = np.zeros((self.nenv, 10))
    sub3_act_mask = np.zeros((self.nenv, 500))
    for env_num, spec in enumerate(base_action_spec):
      for arg_idx, arg in enumerate(spec.args):
        if(len(arg.sizes) == 1 and arg.sizes[0] == 2):
          sub_act_len = spec.args[arg_idx].sizes[0]
          sub1_act_mask[env_num][0:sub_act_len] = 1
        elif(len(arg.sizes) == 1 and arg.sizes[0] == 500):
          sub_act_len = spec.args[arg_idx].sizes[0]
          sub3_act_mask[env_num][0:sub_act_len] = 1
        elif(len(arg.sizes) == 1):
          sub_act_len = spec.args[arg_idx].sizes[0]
          sub2_act_mask[env_num][0:sub_act_len] = 1

    return sub1_act_mask, sub2_act_mask, sub3_act_mask

  def construct_action(self, base_actions, base_action_spec, sub1, sub2, sub3, x1, y1, x2, y2):
    actions = []
    for env_num, spec in enumerate(base_action_spec):
      #print("spec", spec.args)
      args = []
      for arg_idx, arg in enumerate(spec.args):
        if(len(arg.sizes) == 1 and arg.sizes[0] == 2): # size : 2
          args.append([sub1[env_num]])
        elif(len(arg.sizes) == 1 and arg.sizes[0] == 500): # size : 500
          args.append([sub3[env_num]])
        elif(len(arg.sizes) == 1): # size : 3 ~ 10
          args.append([sub2[env_num]])
        elif(len(arg.sizes) == 2 and arg_idx in (0, 1)):
          args.append([x1[env_num], y1[env_num]])
        elif(len(arg.sizes) == 2):
          args.append([x2[env_num], y2[env_num]])
        else:
          raise NotImplementedError("cannot construct this arg", spec.args)

      action = sc2_actions.FunctionCall(base_actions[env_num], args)
      actions.append(action)

    return actions

  def run(self):
    mb_obs, mb_rewards, mb_base_actions, \
    mb_sub1_actions, mb_sub2_actions, mb_sub3_actions,\
      mb_x1, mb_y1, mb_x2, mb_y2, mb_values, mb_dones \
      = [],[],[],[],[],[],[],[],[],[],[],[]

    mb_states = self.states
    for n in range(self.nsteps):
      #pi, pi2, x1, y1, x2, y2, v0
      pi1, pi_sub1, pi_sub2, pi_sub3, x1, y1, x2, y2, values, states = self.model.step(self.obs, self.states, self.dones)
      #avail = self.env.available_actions()

      base_actions = np.argmax(pi1 * self.base_act_mask, axis=1) # pi (2?, 524) * (2?, 524) masking
      base_actions = self.valid_base_action(base_actions)
      #print("base_actions : ", base_actions)
      base_action_spec = self.env.action_spec(base_actions)
      #print("base_action_spec : ", base_action_spec)
      sub1_act_mask, sub2_act_mask, sub3_act_mask = self.get_sub_act_mask(base_action_spec)
      sub1_actions = np.argmax(pi_sub1 * sub1_act_mask, axis=1) # pi (2?, 524) * (2?, 524) masking
      sub2_actions = np.argmax(pi_sub2 * sub2_act_mask, axis=1) # pi (2?, 524) * (2?, 524) masking
      sub3_actions = np.argmax(pi_sub3 * sub3_act_mask, axis=1) # pi (2?, 524) * (2?, 524) masking
      actions = self.construct_action(base_actions, base_action_spec, sub1_actions, sub2_actions, sub3_actions, x1*2, y1*2, x2*2, y2*2)
      #sc2_actions.FUNCTIONS[base_action]
      #sub_action = pi2 * avail2 #pi2 (2?, 500) * (2?, 500) masking

      mb_obs.append(np.copy(self.obs))
      mb_base_actions.append(base_actions)
      mb_sub1_actions.append(sub1_actions)
      mb_sub2_actions.append(sub2_actions)
      mb_sub3_actions.append(sub3_actions)

      mb_x1.append(x1)
      mb_y1.append(y1)
      mb_x2.append(x2)
      mb_y2.append(y2)
      mb_values.append(values)
      mb_dones.append(self.dones)

      #print("final acitons : ", actions)
      obs, rewards, dones, available_actions = self.env.step(actions=actions)
      self.update_available(available_actions)

      self.states = states
      self.dones = dones
      for n, done in enumerate(dones):
        if done:
          self.obs[n] = self.obs[n]*0
      self.update_obs(obs)
      mb_rewards.append(rewards)
    mb_dones.append(self.dones)
    #batch of steps to batch of rollouts
    mb_obs = np.asarray(mb_obs, dtype=np.uint8).swapaxes(1, 0).reshape(self.batch_ob_shape)
    mb_rewards = np.asarray(mb_rewards, dtype=np.float32).swapaxes(1, 0)
    mb_base_actions = np.asarray(mb_base_actions, dtype=np.int32).swapaxes(1, 0)
    mb_sub1_actions = np.asarray(mb_sub1_actions, dtype=np.int32).swapaxes(1, 0)
    mb_sub2_actions = np.asarray(mb_sub2_actions, dtype=np.int32).swapaxes(1, 0)
    mb_sub3_actions = np.asarray(mb_sub3_actions, dtype=np.int32).swapaxes(1, 0)

    mb_x1 = np.asarray(mb_x1, dtype=np.int32).swapaxes(1, 0)
    mb_y1 = np.asarray(mb_y1, dtype=np.int32).swapaxes(1, 0)
    mb_x2 = np.asarray(mb_x2, dtype=np.int32).swapaxes(1, 0)
    mb_y2 = np.asarray(mb_y2, dtype=np.int32).swapaxes(1, 0)

    mb_values = np.asarray(mb_values, dtype=np.float32).swapaxes(1, 0)
    mb_dones = np.asarray(mb_dones, dtype=np.bool).swapaxes(1, 0)
    mb_masks = mb_dones[:, :-1]
    mb_dones = mb_dones[:, 1:]
    last_values = self.model.value(self.obs, self.states, self.dones).tolist()
    #discount/bootstrap off value fn
    for n, (rewards, dones, value) in enumerate(zip(mb_rewards, mb_dones, last_values)):
      rewards = rewards.tolist()
      dones = dones.tolist()
      if dones[-1] == 0:
        rewards = discount_with_dones(rewards+[value], dones+[0], self.gamma)[:-1]
      else:
        rewards = discount_with_dones(rewards, dones, self.gamma)
      mb_rewards[n] = rewards
    mb_rewards = mb_rewards.flatten()
    mb_base_actions = mb_base_actions.flatten()
    mb_sub1_actions = mb_sub1_actions.flatten()
    mb_sub2_actions = mb_sub2_actions.flatten()
    mb_sub3_actions = mb_sub3_actions.flatten()
    mb_x1 = mb_x1.flatten()
    mb_y1 = mb_y1.flatten()
    mb_x2 = mb_x2.flatten()
    mb_y2 = mb_y2.flatten()

    mb_values = mb_values.flatten()
    mb_masks = mb_masks.flatten()
    return mb_obs, mb_states, mb_rewards, mb_masks, \
           mb_base_actions, mb_sub1_actions, mb_sub2_actions, mb_sub3_actions,\
           mb_x1, mb_y1, mb_x2, mb_y2, mb_values

def learn(policy, env, seed, total_timesteps=int(40e6),
          gamma=0.99, log_interval=1, nprocs=32, nsteps=2,
          nstack=4, ent_coef=0.01, vf_coef=0.5, vf_fisher_coef=1.0,
          lr=0.25, max_grad_norm=0.5,
          kfac_clip=0.001, save_interval=None, lrschedule='linear'):
  tf.reset_default_graph()
  set_global_seeds(seed)

  nenvs = nprocs
  ob_space = (64, 64, 1) # env.observation_space
  ac_space = (64, 64)
  make_model = lambda : Model(policy, ob_space, ac_space, nenvs,
                              total_timesteps,
                              nprocs=nprocs,
                              nsteps=nsteps,
                              nstack=nstack,
                              ent_coef=ent_coef,
                              vf_coef=vf_coef,
                              vf_fisher_coef=vf_fisher_coef,
                              lr=lr,
                              max_grad_norm=max_grad_norm,
                              kfac_clip=kfac_clip,
                              lrschedule=lrschedule)

  if save_interval and logger.get_dir():
    import cloudpickle
    with open(osp.join(logger.get_dir(), 'make_model.pkl'), 'wb') as fh:
      fh.write(cloudpickle.dumps(make_model))
  model = make_model()
  print("make_model complete!")
  runner = Runner(env, model, nsteps=nsteps, nstack=nstack, gamma=gamma)
  nbatch = nenvs*nsteps
  tstart = time.time()
  enqueue_threads = model.q_runner.create_threads(model.sess, coord=tf.train.Coordinator(), start=True)
  for update in range(1, total_timesteps//nbatch+1):
    obs, states, rewards, masks, actions, sub1_actions, sub2_actions, sub3_actions, x1, y1, x2, y2, values = runner.run()
    # (obs, states, rewards, masks, actions, actions2, x1, y1, x2, y2, values)
    policy_loss, value_loss, policy_entropy \
      = model.train(obs, states, rewards, masks, actions, sub1_actions, sub2_actions, sub3_actions, x1, y1, x2, y2, values)
    model.old_obs = obs
    nseconds = time.time()-tstart
    fps = int((update*nbatch)/nseconds)
    if update % log_interval == 0 or update == 1:
      ev = explained_variance(values, rewards)
      logger.record_tabular("nupdates", update)
      logger.record_tabular("total_timesteps", update*nbatch)
      logger.record_tabular("fps", fps)
      logger.record_tabular("policy_entropy", float(policy_entropy))
      logger.record_tabular("policy_loss", float(policy_loss))
      logger.record_tabular("value_loss", float(value_loss))
      logger.record_tabular("explained_variance", float(ev))
      logger.dump_tabular()

    if save_interval and (update % save_interval == 0 or update == 1) and logger.get_dir():
      savepath = osp.join(logger.get_dir(), 'checkpoint%.5i'%update)
      print('Saving to', savepath)
      model.save(savepath)

  env.close()
