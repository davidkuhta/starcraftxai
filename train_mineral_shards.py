import sys

import gflags as flags
from baselines import deepq
from pysc2.env import sc2_env
from pysc2.lib import actions
import os
from baselines import logger
from baselines.common import set_global_seeds

import deepq_mineral_shards
import datetime

from baselines import bench
from common.vec_env.subproc_vec_env import SubprocVecEnv
from acktr.policies import CnnPolicy
from acktr import acktr_disc
from baselines.logger import Logger, TensorBoardOutputFormat, HumanOutputFormat

import threading
import time

_MOVE_SCREEN = actions.FUNCTIONS.Move_screen.id
_SELECT_ARMY = actions.FUNCTIONS.select_army.id
_SELECT_ALL = [0]
_NOT_QUEUED = [0]

step_mul = 8

FLAGS = flags.FLAGS
flags.DEFINE_string("map", "CollectMineralShards", "Name of a map to use to play.")
start_time = datetime.datetime.now().strftime("%Y%m%d%H%M")
flags.DEFINE_string("log", "tensorboard", "logging type(stdout, tensorboard)")
flags.DEFINE_string("algorithm", "deepq", "RL algorithm to use.")
flags.DEFINE_integer("timesteps", 2000000, "Steps to train")
flags.DEFINE_float("exploration_fraction", 0.5, "Exploration Fraction")
flags.DEFINE_boolean("prioritized", True, "prioritized_replay")
flags.DEFINE_boolean("dueling", True, "dueling")
flags.DEFINE_float("lr", 0.0005, "Learning rate")
flags.DEFINE_integer("num_cpu", 4, "number of cpus")

def main():
  FLAGS(sys.argv)

  print("algorithm : %s" % FLAGS.algorithm)
  print("timesteps : %s" % FLAGS.timesteps)
  print("exploration_fraction : %s" % FLAGS.exploration_fraction)
  print("prioritized : %s" % FLAGS.prioritized)
  print("dueling : %s" % FLAGS.dueling)
  print("num_cpu : %s" % FLAGS.num_cpu)
  print("lr : %s" % FLAGS.lr)

  logdir = "tensorboard"
  if(FLAGS.algorithm == "deepq"):
    logdir = "tensorboard/%s/%s_%s_prio%s_duel%s_lr%s/%s" % (
      FLAGS.algorithm,
      FLAGS.timesteps,
      FLAGS.exploration_fraction,
      FLAGS.prioritized,
      FLAGS.dueling,
      FLAGS.lr,
      start_time
    )
  elif(FLAGS.algorithm == "acktr"):
    logdir = "tensorboard/%s/%s_num%s_lr%s/%s" % (
      FLAGS.algorithm,
      FLAGS.timesteps,
      FLAGS.num_cpu,
      FLAGS.lr,
      start_time
    )

  if(FLAGS.log == "tensorboard"):
    Logger.DEFAULT \
      = Logger.CURRENT \
      = Logger(dir=None,
               output_formats=[TensorBoardOutputFormat(logdir)])

  elif(FLAGS.log == "stdout"):
    Logger.DEFAULT \
      = Logger.CURRENT \
      = Logger(dir=None,
               output_formats=[HumanOutputFormat(sys.stdout)])

  if(FLAGS.algorithm == "deepq"):

    with sc2_env.SC2Env(
        "CollectMineralShards",
        step_mul=step_mul,
        visualize=True) as env:

      model = deepq.models.cnn_to_mlp(
        convs=[(16, 8, 4), (32, 4, 2)],
        hiddens=[256],
        dueling=True
      )

      act = deepq_mineral_shards.learn(
        env,
        q_func=model,
        num_actions=64,
        lr=1e-3,
        max_timesteps=20000000,
        buffer_size=10000,
        exploration_fraction=0.5,
        exploration_final_eps=0.01,
        train_freq=4,
        learning_starts=10000,
        target_network_update_freq=1000,
        gamma=0.99,
        prioritized_replay=True
      )
      act.save("mineral_shards.pkl")

  elif(FLAGS.algorithm == "acktr"):

    num_timesteps=int(40e6)

    num_timesteps //= 4

    seed=0
    num_cpu=2

    # def make_env(rank):
    #   # env = sc2_env.SC2Env(
    #   #   "CollectMineralShards",
    #   #   step_mul=step_mul)
    #   # return env
    #   #env.seed(seed + rank)
    #   def _thunk():
    #     env = sc2_env.SC2Env(
    #         map_name=FLAGS.map,
    #         step_mul=step_mul,
    #         visualize=True)
    #     #env.seed(seed + rank)
    #     if logger.get_dir():
    #      env = bench.Monitor(env, os.path.join(logger.get_dir(), "{}.monitor.json".format(rank)))
    #     return env
    #   return _thunk

    # agents = [Agent()
    #           for _ in range(num_cpu)]
    #
    # for agent in agents:
    #   time.sleep(1)
    #   agent.daemon = True
    #   agent.start()

    # agent_controller = AgentController(agents)

    #set_global_seeds(seed)
    env = SubprocVecEnv(num_cpu, FLAGS.map)

    policy_fn = CnnPolicy
    acktr_disc.learn(policy_fn, env, seed, total_timesteps=num_timesteps, nprocs=num_cpu)

from pysc2.env import environment
import numpy as np

class Agent(threading.Thread):
  def __init__(self):
    threading.Thread.__init__(self)
    self.env = sc2_env.SC2Env(
      map_name=FLAGS.map,
      step_mul=step_mul)

    def run(self):
      print(threading.currentThread().getName(), self.receive_messages)

    def do_thing_with_message(self, message):
      if self.receive_messages:
        print(threading.currentThread().getName(), "Received %s".format(message))

class AgentController(object):
  def __init__(self, agents):
    self.agents = agents
    self.observation_space = (64,64,13)

  def step(self, actions):
    obs, rewards, dones, infos = [], [], [], []
    for idx, agent in enumerate(self.agents):
      result = agent.env.step(actions=actions[idx])
      ob = result[0].observation["screen"]
      reward = result[0].reward
      done = result[0].step_type == environment.StepType.LAST
      info = result[0].observation["available_actions"]
      obs.append(ob)
      rewards.append(reward)
      dones.append(done)
      infos.append(info)
    return np.stack(obs), np.stack(rewards), np.stack(dones), np.stack(infos)

  def close(self, actions):
    for idx, agent in enumerate(self.agents):
      agent.env.close()

  def reset(self):
    obs, rewards, dones, infos = [], [], [], []
    for idx, agent in enumerate(self.agents):
      result = agent.env.reset()
      ob = result[0].observation["screen"]
      reward = result[0].reward
      done = result[0].step_type == environment.StepType.LAST
      info = result[0].observation["available_actions"]
      obs.append(ob)
      rewards.append(reward)
      dones.append(done)
      infos.append(info)
    return np.stack(obs), np.stack(rewards), np.stack(dones), np.stack(infos)

if __name__ == '__main__':
  main()
