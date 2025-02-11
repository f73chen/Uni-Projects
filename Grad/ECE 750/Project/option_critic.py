import torch
import torch.nn as nn
from torch.distributions import Categorical, Bernoulli
import numpy as np
from math import exp
from tqdm import tqdm
import time

from replay_buffer import ReplayBuffer
from utils import actor_loss_oc, critic_loss_oc
from logger import OCLogger

# Flattens observations
# Outputs a discrete action index
class OptionCriticFeatures(nn.Module):
    def __init__(self,
                 env,
                 num_options,
                 device="cpu",
                 
                 temperature=1.0,
                 epsilon_start=1.0,
                 epsilon_min=0.1,
                 epsilon_decay=int(1e6),
                 gamma=0.95,
                 tau=1.0,
                 
                 termination_reg=0.01,
                 entropy_reg=0.01,
                 
                 hidden_size=32,
                 state_size=64,
                 hidden_size_2=32,
                 hidden_size_Q=32,
                 hidden_size_termination=32,
                 hidden_size_policy=32,
                 use_hidden_size=True,
                 use_hidden_size_2=False,
                 use_hidden_size_Q=False,
                 use_hidden_size_termination=False,
                 use_hidden_size_policy=False,
                 
                 learning_rate=1e-4,
                 batch_size=64,
                 critic_freq=10,
                 target_update_freq=50,
                 buffer_size=10000,
                 
                 tensorboard_log=None,
                 verbose=1,
                 is_policy_network=True) -> None:
        super(OptionCriticFeatures, self).__init__()
        
        self.env = env
        obs_shape = env.observation_space.shape
        flattened_obs = np.prod(obs_shape)
        self.in_features = flattened_obs
        self.num_actions = env.action_space.n
        self.num_options = num_options
        self.device = device
        
        self.temperature = temperature
        self.epsilon_start = epsilon_start
        self.epsilon_min = epsilon_min
        self.epsilon_decay = epsilon_decay
        self.gamma = gamma
        self.tau = tau
        
        self.termination_reg = termination_reg
        self.entropy_reg = entropy_reg
        
        self.learning_rate = learning_rate
        self.batch_size = batch_size
        self.critic_freq = critic_freq
        self.target_update_freq = target_update_freq
        self.buffer_size = buffer_size
        
        self.tensorboard_log = tensorboard_log
        self.verbose = verbose
        
        # Shared network
        if use_hidden_size_2:
            self.features = nn.Sequential(
                nn.Linear(self.in_features, hidden_size),
                nn.ReLU(),
                nn.Linear(hidden_size, hidden_size_2),
                nn.ReLU(),
                nn.Linear(hidden_size_2, state_size),
                nn.ReLU()
            )
        elif use_hidden_size:
            self.features = nn.Sequential(
                nn.Linear(self.in_features, hidden_size),
                nn.ReLU(),
                nn.Linear(hidden_size, state_size),
                nn.ReLU()
            )
        else:
            self.features = nn.Sequential(
                nn.Linear(self.in_features, state_size),
                nn.ReLU()
            )
        
        # Q_Omega head
        if use_hidden_size_Q:
            self.Q = nn.Sequential(
                nn.Linear(state_size, hidden_size_Q),
                nn.ReLU(),
                nn.Linear(hidden_size_Q, num_options)
            )
        else:
            self.Q = nn.Linear(state_size, num_options)
        
        # beta_w head
        if use_hidden_size_termination:
            self.terminations = nn.Sequential(
                nn.Linear(state_size, hidden_size_termination),
                nn.ReLU(),
                nn.Linear(hidden_size_termination, num_options)
            )
        else:
            self.terminations = nn.Linear(state_size, num_options)
        
        # Intra-option policy (pi_w) weights and biases        
        if use_hidden_size_policy:
            self.policies = nn.ModuleList([
                nn.Sequential(
                    nn.Linear(state_size, hidden_size_policy),
                    nn.ReLU(),
                    nn.Linear(hidden_size_policy, self.num_actions)
                ) for _ in range(num_options)
            ])
        else:
            self.policies = nn.ModuleList([
                nn.Linear(state_size, self.num_actions) for _ in range(num_options)
            ])
        
        # Initialize target network and copy over the parameters
        # Only create the target network if self is policy network to avoid infinite recursion
        if is_policy_network:
            self.target_network = OptionCriticFeatures(env, num_options, device,
                                                       temperature, epsilon_start, epsilon_min, epsilon_decay, gamma, tau,
                                                       termination_reg, entropy_reg,
                                                       hidden_size, state_size, hidden_size_2, hidden_size_Q, hidden_size_termination, hidden_size_policy,
                                                       use_hidden_size, use_hidden_size_2, use_hidden_size_Q, use_hidden_size_termination, use_hidden_size_policy,
                                                       learning_rate, batch_size, critic_freq, target_update_freq, buffer_size,
                                                       tensorboard_log, verbose, is_policy_network=False)
            self.update_target_network()
        else:
            self.target_network = None
        
        self.to(device)
        
    # Tau = 1.0 --> Hard copy, completely replace target network with policy network
    # Tau = 0.01 --> Soft update, gradually shift target network in the direction of policy network
    def update_target_network(self):
        with torch.no_grad():
            for target_param, param in zip(self.target_network.parameters(), self.parameters()):
                target_param.data.copy_(self.tau * param.data + (1.0 - self.tau) * target_param.data)
                
    # Calculate whether the current option should terminate
    def get_option_termination(self, obs, option):
        obs_tensor = torch.tensor(obs, dtype=torch.float32).reshape(1, -1).to(self.device)
        state = self.features(obs_tensor)
        termination = self.terminations(state)[:, option].sigmoid()
        termination = Bernoulli(termination).sample()
        return bool(termination.item())
        
    # Learn by interacting with the environment
    def learn(self, total_timesteps) -> None:
        obs, _ = self.env.reset()
        episode_idx = 0
        episode_reward = 0
        episode_length = 0
        option_lengths = {opt: [] for opt in range(self.num_options)}
        option = None
        curr_option_length = 0
        option_termination = True
        replay_buffer = ReplayBuffer(capacity=self.buffer_size)
        optimizer = torch.optim.AdamW(self.parameters(), lr=self.learning_rate)
        
        if self.tensorboard_log is not None:
            logger = OCLogger(logdir=self.tensorboard_log, run_name=f"OC-{time.time()}")
        
        for step in tqdm(range(total_timesteps)):
            # Choose an option and action using epsilon-greedy
            epsilon = self.epsilon_min + (self.epsilon_start - self.epsilon_min) * exp(-step / self.epsilon_decay)
            option, action, logp, entropy = self.predict(obs, option, option_termination, epsilon)
            
            # Take a step in the environment
            next_obs, reward, done, truncated, _ = self.env.step(action)
            replay_buffer.add(obs, option, reward, next_obs, done)
            
            # Sample whether the current option terminates in the next step
            option_termination = self.get_option_termination(next_obs, option)
            
            # Iterate episodic variables
            obs = next_obs
            episode_reward += reward
            episode_length += 1
            curr_option_length += 1
            
            # Record option lengths
            if option_termination:
                option_lengths[option].append(curr_option_length)
                curr_option_length = 0
            
            # Reset environment if done or truncated
            if done or truncated:
                if self.verbose == 1:
                    print(f"Episode {episode_idx} finished with reward: {episode_reward}, epsilon:{epsilon}")
                
                # Terminate the option if the episode ends
                if not option_termination:
                    option_lengths[option].append(curr_option_length)
                    curr_option_length = 0
                
                # Log and reset episodic variables
                if self.tensorboard_log is not None:
                    logger.log_episode(episode_idx, episode_reward, episode_length, option_lengths)
                obs, _ = self.env.reset()
                episode_idx += 1
                episode_reward = 0
                episode_length = 0
                option_lengths = {opt: [] for opt in range(self.num_options)}
                option_termination = True
                
            # Sample from buffer and backprop the loss
            actor_loss_value = None
            critic_loss_value = None
            if len(replay_buffer) >= self.batch_size:
                # Update the actor loss every step
                actor_loss_value = actor_loss_oc(self, self.target_network, obs, option, reward, next_obs, done, logp, entropy)
                loss = actor_loss_value
                
                # Update the critic loss every critic_freq steps
                if step % self.critic_freq == 0:
                    batch = replay_buffer.sample(self.batch_size)
                    critic_loss_value = critic_loss_oc(self, self.target_network, batch)
                    loss += critic_loss_value
                
                # Optimization step
                optimizer.zero_grad()
                loss.backward()
                optimizer.step()
                
            # Update the target network every few steps
            if step % self.target_update_freq == 0:
                self.update_target_network()
                
            if self.tensorboard_log is not None:
                logger.log_step(step, actor_loss_value, critic_loss_value, entropy.item(), epsilon)
    
    # Helper: Return the option index with the highest Q_Omega value
    def greedy_option(self, state):
        Q_Omegas = self.Q(state)
        return Q_Omegas.argmax(dim=-1).item()
    
    # Helper: Selects an action based on the state and option
    def get_action(self, state, option):
        # logits = state * weights + biases (unnormalized action scores for that option)
        logits = self.policies[option](state)
        
        # Convert logits to softmax probabilities (sums to 1)
        action_dist = (logits / self.temperature).softmax(dim=-1)
        
        # Probability wrapper (adds functionality)
        action_dist = Categorical(action_dist)
        
        # Chooses an action based on the probabilites
        action = action_dist.sample()
        logp = action_dist.log_prob(action)
        entropy = action_dist.entropy()
        
        return action.item(), logp, entropy
    
    # Selects an action based on the observations
    def predict(self, obs, option, option_termination, epsilon=1.0, testing=False) -> int:
        # "We used an ε-greedy policy over options with ε = 0.05 during the test phase"
        if testing:
            epsilon = 0.05
        
        # Flatten the observations to 1D and add a batch dimension
        obs_tensor = torch.tensor(obs, dtype=torch.float32).reshape(1, -1).to(self.device)
        
        # Get the output of the shared network
        state = self.features(obs_tensor)
        
        # Choose a new option if the previous one terminates
        if option_termination:
            if np.random.random() > epsilon:
                option = self.greedy_option(state)
            else:
                option = np.random.randint(0, self.num_options)
        
        # Choose an action based on the chosen option
        action, logp, entropy = self.get_action(state, option)
        return option, action, logp, entropy
    
    # Saves the model
    def save(self, filepath) -> None:
        torch.save(self.state_dict(), filepath)
    
    # Loads the model
    def load(self, filepath, device='cpu'):
        device = torch.device(device)
        self.load_state_dict(torch.load(filepath, map_location=device))
        self.to(device)

"""
class OptionCriticConv(OptionCriticFeatures):
    def __init__(self,
                 env,
                 num_options,
                 device="cpu",
                 temperature=1.0,
                 epsilon_start=1.0,
                 epsilon_min=0.1,
                 epsilon_decay=int(1e6),
                 gamma=0.95,
                 termination_reg=0.01,
                 entropy_reg = 0.01,
                 learning_rate=1e-4,
                 batch_size=64,
                 critic_freq=10,
                 target_update_freq=50,
                 buffer_size=10000,
                 tensorboard_log=None,
                 verbose=1,
                 is_policy_network=True) -> None:
        super(OptionCriticFeatures, self).__init__()
        
        self.env = env
        obs_shape = env.observation_space.shape
        flattened_obs = np.prod(obs_shape)
        self.in_features = flattened_obs
        self.num_actions = env.action_space.n
        
        self.num_options = num_options
        self.device = device
        
        self.temperature = temperature
        self.epsilon_start = epsilon_start
        self.epsilon_min = epsilon_min
        self.epsilon_decay = epsilon_decay
        self.gamma = gamma
        self.termination_reg = termination_reg
        self.entropy_reg = entropy_reg
        
        self.learning_rate = learning_rate
        self.batch_size = batch_size
        self.critic_freq = critic_freq
        self.target_update_freq = target_update_freq
        self.buffer_size = buffer_size
        
        self.tensorboard_log = tensorboard_log
        self.verbose = verbose
        
        # Shared network
        self.features = nn.Sequential(
            nn.Linear(self.in_features, 32),
            nn.ReLU(),
            nn.Linear(32, 64),
            nn.ReLU()
        )
        
        # Q_Omega head
        self.Q = nn.Linear(512, num_options)
        
        # beta_w head
        self.terminations = nn.Linear(512, num_options)
        
        # pi_w head weights and biases
        self.options_W = nn.Parameter(torch.empty(num_options, 512, self.num_actions, device=device))
        torch.nn.init.xavier_uniform_(self.options_W)   # Make weights non-zero
        self.options_b = nn.Parameter(torch.zeros(num_options, self.num_actions, device=device))
        
        self.to(device)
        
        # Initialize target network and copy over the parameters
        # Only create the target network if self is policy network to avoid infinite recursion
        if is_policy_network:
            self.target_network = OptionCriticFeatures(env, num_options, is_policy_network=False)
            self.update_target_network()
        else:
            self.target_network = None
"""