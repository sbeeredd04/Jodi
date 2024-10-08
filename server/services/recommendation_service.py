import pandas as pd
from sklearn.preprocessing import MinMaxScaler
from sklearn.metrics.pairwise import cosine_similarity
import torch
import torch.nn as nn
import torch.optim as optim
import random
from collections import deque
import numpy as np
from sqlalchemy import select
from models.song_model import Song, UserHistory, UserMood
from utils.db import get_session
import threading
import logging

# Set up logging
logging.basicConfig(level=logging.DEBUG)

# Define the global list of user songs
user_songs = [67016, 91000, 81004, 17000, 20414, 81000, 81074, 81109, 20652, 
              91016, 91017, 91018, 51150, 51503, 56064, 33012, 57162, 53050, 
              67351, 51450, 94632, 51500, 53055]


# DQN Model Definition
class DQN(nn.Module):
    def __init__(self, state_size, action_size):
        super(DQN, self).__init__()
        self.fc1 = nn.Linear(state_size, 128)
        self.fc2 = nn.Linear(128, 128)
        self.fc3 = nn.Linear(128, action_size)

    def forward(self, x):
        x = torch.relu(self.fc1(x))
        x = torch.relu(self.fc2(x))
        return self.fc3(x)

# Data Preprocessing Helper Function
def preprocess_data(df):
    logging.debug("Preprocessing data...")
    
    df = df.dropna()
    df = df.drop(['duration_ms', 'explicit', 'mode', 'liveness', 'loudness', 'time_signature', 'key'], axis=1)
    df.rename(columns={'Unnamed: 0': 'song_id'}, inplace=True)

    features_to_scale = ['popularity', 'danceability', 'energy', 'acousticness', 'valence', 'tempo', 'speechiness', 'instrumentalness']
    scaler = MinMaxScaler()
    scaled_features = scaler.fit_transform(df[features_to_scale])
    df_scaled = pd.DataFrame(scaled_features, columns=features_to_scale)

    df_scaled['song_id'] = df['song_id']
    df_scaled['track_id'] = df['track_id']
    df_scaled['artist_name'] = df['artists'].fillna('')
    df_scaled['track_name'] = df['track_name']
    df_scaled['album_name'] = df['album_name'].fillna('')
    df_scaled['track_genre'] = df['track_genre'].fillna('')

    logging.debug(f"Data preprocessing complete. Shape of scaled data: {df_scaled.shape}")
    return df_scaled.drop_duplicates()

# Helper Function to Get User's Songs and Initialize Playlist
def get_user_playlist_from_db(user_id, df_scaled):
    logging.debug(f"Fetching user {user_id}'s playlist from predefined global song list...")
    
    # Use the predefined global list of user songs
    user_playlist = []
    for song_id in user_songs:
        song_record = df_scaled[df_scaled['song_id'] == song_id]
        if not song_record.empty:
            user_playlist.append(song_record['song_id'].values[0])
    
    logging.debug(f"User {user_id}'s playlist (from global song list): {user_playlist}")
    return user_playlist, len(user_playlist)



def update_user_feedback(user_id, feedback):
    logging.debug(f"Updating feedback for user {user_id}...")
    
    session = get_session()
    with session:
        for item in feedback:
            song_id = item['song_id']
            reward = item['reward']  # Use 'reward' instead of 'liked'
            mood = item['mood']

            # Check if the song already exists in the history
            existing_record = session.query(UserHistory).filter_by(user_id=user_id, song_id=song_id).first()

            if existing_record:
                existing_record.reward = reward  # Update 'reward'
                existing_record.mood = mood
            else:
                new_history = UserHistory(user_id=user_id, song_id=song_id, reward=reward, mood=mood)
                session.add(new_history)
        
        session.commit()

    logging.debug(f"Feedback update complete for user {user_id}")

# Filter Songs Based on Similarity and Append User Playlist Songs
def recommend_songs_filtered(user_songs, df, features, feature_weights, top_n=0):
    logging.debug("Filtering and recommending songs based on similarity...")
    
    df_copy = df.copy()
    for feature in features:
        df_copy[feature] = df_copy[feature] * feature_weights.get(feature, 1.0)

    user_songs_df = df_copy[df_copy['song_id'].isin(user_songs)]
    filtered_df = df_copy[df_copy['track_genre'].isin(user_songs_df['track_genre'].unique())]

    similarity_matrix = cosine_similarity(filtered_df[features], user_songs_df[features])
    aggregated_similarities = similarity_matrix.mean(axis=1)
    filtered_df['similarity'] = aggregated_similarities
    
    recommendations = filtered_df[~filtered_df['song_id'].isin(user_songs)].sort_values(by=['similarity', 'popularity'], ascending=[False, False])

    if top_n > 0:
        recommendations = recommendations.head(top_n // 2)  # 50% new songs
        user_songs_df = user_songs_df.sample(min(len(user_songs_df), top_n // 2))  # 50% user playlist songs
    
    final_recommendations = pd.concat([user_songs_df, recommendations]).drop_duplicates()

    logging.debug(f"Generated {len(final_recommendations)} song recommendations.")
    logging.debug(f"Recommendations: \n{final_recommendations[['song_id', 'track_name', 'similarity']].head(10)}")
    
    return final_recommendations

# Function to Fetch User Interaction History and Generate Immediate Recommendations
def fetch_user_history_and_recommend(user_id, df_scaled, features, feature_weights, default_mood='Calm'):
    logging.debug(f"Fetching user {user_id}'s history and generating recommendations...")
    
    user_mood = get_user_mood(user_id) or default_mood
    user_songs, history_length = get_user_playlist_from_db(user_id, df_scaled)

    if not user_songs:
        user_songs = df_scaled['song_id'].sample(10).tolist()  # Pick 10 random songs for initial recommendation

    if history_length >= 5:
        logging.debug(f"User {user_id} has sufficient history for model training. Initiating background training.")
        run_background_training(user_id, df_scaled, features, feature_weights)
    
    recommended_songs_df = recommend_songs_filtered(user_songs, df_scaled, features, feature_weights, top_n=20)
    return recommended_songs_df

# DQN Initialization Function
def init_dqn_model(state_size, action_size):
    logging.debug("Initializing DQN model...")
    
    policy_net = DQN(state_size, action_size)
    target_net = DQN(state_size, action_size)
    target_net.load_state_dict(policy_net.state_dict())
    optimizer = optim.Adam(policy_net.parameters())
    memory = deque(maxlen=10000)

    logging.debug("DQN model initialized successfully.")
    return policy_net, target_net, optimizer, memory

# Background Training Function
def background_train_dqn(user_id, df_scaled, features, feature_weights):
    logging.debug(f"Starting background training for user {user_id}...")

    user_songs, _ = get_user_playlist_from_db(user_id, df_scaled)
    recommended_songs_df = recommend_songs_filtered(user_songs, df_scaled, features, feature_weights, top_n=200)
    action_size = len(recommended_songs_df)

    policy_net, target_net, optimizer, memory = init_dqn_model(1, action_size)  # State size is now 1 (integer state)

    eps_start = 1.0
    eps_end = 0.1
    eps_decay = 0.995
    batch_size = 32
    gamma = 0.99
    target_update = 10
    eps_threshold = eps_start
    num_episodes = 10

    # Initialize the state based on the user's mood (integer representation)
    user_mood = get_user_mood(user_id)
    state = convert_mood_to_state(user_mood)  # Integer state

    for episode in range(num_episodes):
        for t in range(10):
            # Pass policy_net into select_action function
            action = select_action([state], eps_threshold, action_size, policy_net)

            # Simulate next state and reward based on the action
            next_state, reward = get_next_state_and_reward(action, recommended_songs_df)

            # Append to memory for DQN training
            memory.append(([state], action, reward, [next_state]))  # Store states as lists for torch compatibility
            
            # Optimize the DQN model using the memory
            optimize_model(policy_net, target_net, memory, optimizer, batch_size, gamma)
            
            # Update the state to the next one
            state = next_state  # Update with the integer next_state

        eps_threshold = max(eps_end, eps_threshold * eps_decay)

        if episode % target_update == 0:
            target_net.load_state_dict(policy_net.state_dict())

    logging.debug(f"Background training completed for user {user_id}.")



# Function to run training in the background
def run_background_training(user_id, df_scaled, features, feature_weights):
    logging.debug(f"Running background training for user {user_id} in a separate thread...")
    
    training_thread = threading.Thread(target=background_train_dqn, args=(user_id, df_scaled, features, feature_weights))
    training_thread.start()

# DQN Model Optimization Function
def optimize_model(policy_net, target_net, memory, optimizer, batch_size, gamma):
    if len(memory) < batch_size:
        return
    transitions = random.sample(memory, batch_size)
    batch_state, batch_action, batch_reward, batch_next_state = zip(*transitions)

    # Convert the batch to tensors (now using scalar states)
    batch_state = torch.tensor(batch_state).float()  # Ensure states are 1D tensors
    batch_action = torch.tensor(batch_action).long()
    batch_reward = torch.tensor(batch_reward).float()
    batch_next_state = torch.tensor(batch_next_state).float()

    # Compute Q-values
    q_values = policy_net(batch_state).gather(1, batch_action.unsqueeze(1)).squeeze(1)
    next_q_values = target_net(batch_next_state).max(1)[0]
    expected_q_values = batch_reward + (gamma * next_q_values)

    # Optimize the model
    loss = nn.functional.mse_loss(q_values, expected_q_values)
    optimizer.zero_grad()
    loss.backward()
    optimizer.step()



def select_action(state, eps_threshold, action_size, policy_net):
    if random.random() > eps_threshold:
        with torch.no_grad():
            return policy_net(torch.tensor(state).float()).argmax().item()  # Use policy_net to predict action
    else:
        return random.randrange(action_size)  # Exploration: return random action


# Function to update user mood in the database
def update_user_mood(user_id, mood):
    logging.debug(f"Updating mood for user {user_id}...")
    
    session = get_session()
    with session:
        existing_mood = session.query(UserMood).filter_by(user_id=user_id).first()

        if existing_mood:
            existing_mood.mood = mood
        else:
            new_mood = UserMood(user_id=user_id, mood=mood)
            session.add(new_mood)
        
        session.commit()

    logging.debug(f"Mood update complete for user {user_id}")

# Function to get user mood from the database
def get_user_mood(user_id):
    session = get_session()
    with session:
        existing_mood = session.query(UserMood).filter_by(user_id=user_id).first()
        if existing_mood:
            return existing_mood.mood
        else:
            return 'Calm'
        

def convert_mood_to_state(mood):
    """
    Convert mood to an integer state.
    Moods: Angry, Content, Happy, Delighted, Calm, Sleepy, Sad, Depressed, Excited
    """
    mood_mapping = {
        'Angry': 1,
        'Content': 2,
        'Happy': 3,
        'Delighted': 4,
        'Calm': 5,
        'Sleepy': 6,
        'Sad': 7,
        'Depressed': 8,
        'Excited': 9
    }
    return mood_mapping.get(mood, 5)  # Default to 'Calm' if mood not found

import numpy as np

def get_initial_state(mood_state):
    """
    Return a one-hot encoded vector representing the user's mood state.
    Mood state is expected to be an integer in the range [1, 9].
    """
    state_size = 9
    state = np.zeros(state_size)
    state[mood_state - 1] = 1  # Set the corresponding index for the mood state to 1
    return state  # Return the one-hot encoded state

def get_next_state_and_reward(action, recommended_songs_df):
    """
    Simulate the next state and reward based on the action.
    Here, the action corresponds to a recommended song, and the reward is based on user feedback or random values.
    """
    song = recommended_songs_df.iloc[action]  # Get the song from the recommended songs
    
    # For now, let's assume reward is randomly assigned for simulation purposes
    reward = random.choice([-1, 0, 1])  # -1: dislike, 0: neutral, 1: like
    
    # For the next state, return the current song's features (to simulate mood state transition)
    next_state = convert_mood_to_state('Calm')  # Replace with actual logic if necessary

    return next_state, reward
