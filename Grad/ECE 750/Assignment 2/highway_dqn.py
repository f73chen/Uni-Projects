import gymnasium as gym
from stable_baselines3 import DQN

import highway_env

TRAIN = False

if __name__ == "__main__":
    # Create the environment
    env = gym.make("highway-fast-v0", render_mode="rgb_array")
    obs, info = env.reset()

    # Create the model
    model = DQN(
        "MlpPolicy",
        env,
        policy_kwargs=dict(net_arch=[256, 256]),
        learning_rate=5e-4,
        buffer_size=15000,
        learning_starts=200,
        batch_size=32,
        gamma=0.8,
        train_freq=1,
        gradient_steps=1,
        target_update_interval=50,
        verbose=1,
        tensorboard_log="results/highway_dqn/",
    )

    # Train the model
    if TRAIN:
        model.learn(total_timesteps=int(2e4))
        model.save("results/highway_dqn/model")
        del model

    # Run the trained model
    model = DQN.load("results/highway_dqn/model", env=env)
    env.unwrapped.config["simulation_frequency"] = 15

    for episode in range(10):
        done = truncated = False
        obs, info = env.reset()
        while not (done or truncated):
            action, _states = model.predict(obs, deterministic=True)
            obs, reward, done, truncated, info = env.step(action)
            env.render()
    env.close()