"""
Classic cart-pole system implemented by Rich Sutton et al.
Copied from https://webdocs.cs.ualberta.ca/~sutton/book/code/pole.c
"""
import os, inspect
currentdir = os.path.dirname(os.path.abspath(inspect.getfile(inspect.currentframe())))
parentdir = os.path.dirname(os.path.dirname(currentdir))
os.sys.path.insert(0, parentdir)

import logging
import math
import gym
from gym import spaces
from gym.utils import seeding
import numpy as np
import time
import pybullet_data
from pkg_resources import parse_version
from pybullet_envs.deep_mimic.env.pybullet_deep_mimic_env import PyBulletDeepMimicEnv, InitializationStrategy
from pybullet_utils.arg_parser import ArgParser
from pybullet_utils.logger import Logger


logger = logging.getLogger(__name__)


class HumanoidDeepBulletEnv(gym.Env):
  """Base Gym environment for the DeepMimic motion imitation tasks."""
  metadata = {'render.modes': ['human', 'rgb_array'], 'video.frames_per_second': 50}

  def __init__(self,
               renders=False,
               arg_file='',
               test_mode=False,
               time_step=1./240,
               rescale_actions=True,
               rescale_observations=True,
               use_com_reward=False):
    """Instantiate a DeepMimic motion imitation environment.
    
    Args:
      test_mode (bool): in test mode, the `reset()` method will always set the mocap clip time to 0
        at the beginning of every episode.
      time_step (float): physics time step.
      rescale_actions (bool): rescale the actions using the bounds on the action space.
      rescale_observations (bool): rescale the observations using the bounds on the observation space.
      use_com_reward (bool): whether to use the center-of-mass reward.
    """
    self._arg_parser = ArgParser()
    Logger.print2("===========================================================")
    succ = False
    if (arg_file != ''):
      path = pybullet_data.getDataPath() + "/args/" + arg_file
      succ = self._arg_parser.load_file(path)
      Logger.print2(arg_file)
    assert succ, Logger.print2('Failed to load args from: ' + arg_file)

    self._p = None
    self._time_step = time_step
    self._internal_env = None
    self._renders = renders
    self._discrete_actions = False
    self._arg_file = arg_file
    self._render_height = 480
    self._render_width = 854
    self._rescale_actions = rescale_actions
    self._rescale_observations = rescale_observations
    self._use_com_reward = use_com_reward
    self.agent_id = -1

    self._numSteps = None
    self.test_mode = test_mode
    if self.test_mode:
      print("Environment running in TEST mode")

    # cam options
    self._cam_dist = 3
    self._cam_pitch = 0.3
    self._cam_yaw = 0.1
    self._cam_roll = 0

    self.reset()

    # Query the policy at 30Hz
    self.policy_query_30 = True
    if self.policy_query_30:
        self._policy_step = 1./30
    else:
        self._policy_step = 1./240
    self._num_env_steps = int(self._policy_step / self._time_step)
    
    self.theta_threshold_radians = 12 * 2 * math.pi / 360
    self.x_threshold = 0.4  #2.4
    high = np.array([
        self.x_threshold * 2,
        np.finfo(np.float32).max, self.theta_threshold_radians * 2,
        np.finfo(np.float32).max
    ])

    ctrl_size = 43  #numDof
    root_size = 7 # root
    action_dim = ctrl_size - root_size
    
    action_bound_min = np.array(self._internal_env.build_action_bound_min(-1), dtype=np.float32)
    action_bound_max = np.array(self._internal_env.build_action_bound_max(-1), dtype=np.float32)
    if self._rescale_actions:
      action_bound_min = self.scale_action(action_bound_min)
      action_bound_max = self.scale_action(action_bound_max)
    self.action_space = spaces.Box(action_bound_min, action_bound_max)

    observation_min = np.array([0.0]+[-100.0]+[-4.0]*105+[-500.0]*90, dtype=np.float32)
    observation_max = np.array([1.0]+[ 100.0]+[ 4.0]*105+[ 500.0]*90, dtype=np.float32)
    if self._rescale_observations:
      observation_min = self.scale_observation(observation_min)
      observation_max = self.scale_observation(observation_max)
    state_size = 197
    self.observation_space = spaces.Box(observation_min, observation_max)

    self.seed()
    
    self.viewer = None
    self._configure()
    
  def scale_action(self, action):
    """Offset the action and scale it."""
    mean = -self._action_offset
    std = 1./self._action_scale
    return (action - mean) / std

  def scale_observation(self, state):
    mean = -self._state_offset
    std = 1./self._state_scale
    return (state - mean) / (std + 1e-8)

  def unscale_action(self, scaled_action):
    mean = -self._action_offset
    std = 1./self._action_scale
    return scaled_action * std + mean

  def _configure(self, display=None):
    self.display = display

  def seed(self, seed=None):
    self.np_random, seed = seeding.np_random(seed)
    return [seed]

  def step(self, action):
    agent_id = self.agent_id

    if self._rescale_actions:
      # Rescale the action
      action = self.unscale_action(action)

    # Record reward
    reward = self._internal_env.calc_reward(agent_id)
    # print(f"mean {action.mean():<5.3f} | std {action.std():<5.3f} | min {action.min():.3f} max {action.max():.3f}")

    # Apply control action
    self._internal_env.set_action(agent_id, action)

    start_time = self._internal_env.t

    # step sim
    for i in range(self._num_env_steps):
      self._internal_env.update(self._time_step)

    elapsed_time = self._internal_env.t - start_time

    self._numSteps += 1

    # Record state
    self.state = self._internal_env.record_state(agent_id)
    state = self.state
    
    if self._rescale_observations:
      state = self.scale_observation(state)

    # Record done
    done = self._internal_env.is_episode_end()

    self.camera_update()
    
    # get the reward info
    info = {
      'reward': self._internal_env._humanoid._info_rew,
      'error': self._internal_env._humanoid._info_err
    }
    
    return state, reward, done, info

  def reset(self):
    # use the initialization strategy
    if self._internal_env is None:
      if self.test_mode:
        init_strat = InitializationStrategy.START
      else:
        init_strat = InitializationStrategy.RANDOM
      self._internal_env = PyBulletDeepMimicEnv(self._arg_parser, self._renders,
                                                time_step=self._time_step,
                                                init_strategy=init_strat,
                                                use_com_reward=self._use_com_reward)

    self._internal_env.reset()
    self._p = self._internal_env._pybullet_client
    agent_id = self.agent_id  # unused here
    self._state_offset = self._internal_env.build_state_offset(self.agent_id)
    self._state_scale = self._internal_env.build_state_scale(self.agent_id)
    self._action_offset = self._internal_env.build_action_offset(self.agent_id)
    self._action_scale = self._internal_env.build_action_scale(self.agent_id)
    self._numSteps = 0
    # Record state
    self.state = self._internal_env.record_state(agent_id)

    self.camera_update()

    # return state as ndarray
    state = np.array(self.state)
    if self._rescale_observations:
      state = self.scale_observation(state)
    return state

  def render(self, mode='human', close=False, use_dual_view=False):
    if mode == "human":
      self._renders = True
    if mode != "rgb_array":
      return np.array([])
    human = self._internal_env._humanoid
    base_pos, orn = self._p.getBasePositionAndOrientation(human._sim_model)
    base_pos = np.asarray(base_pos)
    # track the position
    base_pos[1] += 0.3
    rpy = self._p.getEulerFromQuaternion(orn)  # rpy, in radians
    rpy = 180 / np.pi * np.asarray(rpy)  # convert rpy in degrees

    if (not self._p == None):
      view_matrix = self._p.computeViewMatrixFromYawPitchRoll(
        cameraTargetPosition=base_pos,
        distance=self._cam_dist,
        yaw=self._cam_yaw,
        pitch=self._cam_pitch,
        roll=self._cam_roll,
        upAxisIndex=1)
      width = self._render_width
      height = self._render_height
      if use_dual_view:
        width = width // 2
      proj_matrix = self._p.computeProjectionMatrixFOV(fov=60,
             aspect=float(width) / height,
             nearVal=0.1,
             farVal=100.0)
      (_, _, px, _, _) = self._p.getCameraImage(
          width=width,
          height=self._render_height,
          viewMatrix=view_matrix,
          projectionMatrix=proj_matrix,
          renderer=self._p.ER_BULLET_HARDWARE_OPENGL,
          shadow=1)
      if use_dual_view:
        px2 = self._get_camera_kin_char(width, height, proj_matrix)
        px = np.concatenate([px, px2], axis=1)
    else:
      px = np.array([[[255,255,255,255]]*self._render_width]*self._render_height, dtype=np.uint8)
    rgb_array = np.array(px, dtype=np.uint8)
    rgb_array = np.reshape(np.array(px), (self._render_height, self._render_width, -1))
    rgb_array = rgb_array[:, :, :3]
    return rgb_array

  def _get_camera_kin_char(self, width, height, proj_matrix):
    """Define a view matrix focusing on the kinematic character, and get camera image."""
    human = self._internal_env._humanoid
    kin_char_pos = self._p.getBasePositionAndOrientation(human._kin_model)[0]
    kin_char_pos = np.asarray(kin_char_pos)
    kin_char_pos[1] += 0.3
    view_matrix = self._p.computeViewMatrixFromYawPitchRoll(
      cameraTargetPosition=kin_char_pos,
      distance=self._cam_dist,
      yaw=self._cam_yaw,
      pitch=self._cam_pitch,
      roll=self._cam_roll,
      upAxisIndex=1)
    _, _, px, _, _ = self._p.getCameraImage(
      width=width,
      height=height,
      viewMatrix=view_matrix,
      projectionMatrix=proj_matrix,
      renderer=self._p.ER_BULLET_HARDWARE_OPENGL,
      shadow=1
      )
    return px

  def configure(self, args):
    pass
    
  def close(self):
    
    pass

  def camera_update(self):
    """Update the debug visualizer camera."""
    # update camera
    human = self._internal_env._humanoid
    base_pos, base_orn = self._p.getBasePositionAndOrientation(
        human._sim_model)
    debug_caminfo = self._p.getDebugVisualizerCamera()
    (dbg_yaw, dbg_pitch, cur_dist) = debug_caminfo[8:11]
    w, h = debug_caminfo[:2]
    self._cam_dist = cur_dist
    if w > 0 and h > 0:
      self._cam_yaw = dbg_yaw
      self._cam_pitch = dbg_pitch
    self._p.resetDebugVisualizerCamera(
        cameraDistance=self._cam_dist,
        cameraYaw=dbg_yaw,
        cameraPitch=dbg_pitch,
        cameraTargetPosition=base_pos)

class HumanoidDeepMimicBackflipBulletEnv(HumanoidDeepBulletEnv):
  metadata = {'render.modes': ['human', 'rgb_array'], 'video.frames_per_second': 50}

  def __init__(self, renders=False, test_mode=False, use_com_reward=False):
    # start the bullet physics server
    HumanoidDeepBulletEnv.__init__(self, renders, arg_file="run_humanoid3d_backflip_args.txt",
                                   test_mode=test_mode,
                                   use_com_reward=use_com_reward)


class HumanoidDeepMimicWalkBulletEnv(HumanoidDeepBulletEnv):
  metadata = {'render.modes': ['human', 'rgb_array'], 'video.frames_per_second': 50}

  def __init__(self, renders=False, test_mode=False, use_com_reward=False):
    # start the bullet physics server
    HumanoidDeepBulletEnv.__init__(self, renders, arg_file="run_humanoid3d_walk_args.txt",
                                   test_mode=test_mode,
                                   use_com_reward=use_com_reward)
