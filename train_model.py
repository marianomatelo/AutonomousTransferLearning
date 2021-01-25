import time
import math
import numpy as np
import json
import os
import sys
import requests
import copy
import datetime

from airsim_client import msgpackrpc, CarClient, CarControls, Pose, Vector3r, ImageRequest, AirSimImageType, AirSimClientBase
from rl_model import RlModel


# A class that represents the agent that will drive the vehicle, train the model, and send the gradient updates to the trainer.
class DistributedAgent(object):
    def __init__(self, batch_update_frequency, max_epoch_runtime_sec, per_iter_epsilon_reduction, min_epsilon, batch_size
                             , replay_memory_size, weights_path, train_conv_layers, airsim_path, experiment_name):


        print('Starting time: {0}'.format(datetime.datetime.utcnow()), file=sys.stderr)
        self.__model_buffer = None
        self.__model = None
        self.__airsim_started = False
        self.__per_iter_epsilon_reduction = float(per_iter_epsilon_reduction)
        self.__min_epsilon = float(min_epsilon)
        self.__max_epoch_runtime_sec = float(max_epoch_runtime_sec)
        self.__replay_memory_size = int(replay_memory_size)
        self.__batch_size = int(batch_size)
        self.__experiment_name = experiment_name
        self.__train_conv_layers = train_conv_layers
        self.__epsilon = 1
        self.__num_batches_run = 0
        self.__last_checkpoint_batch_count = 0

        self.__batch_update_frequency = batch_update_frequency

        if weights_path:
            self.__weights_path = weights_path

        else:
            self.__weights_path = None

        self.__airsim_path = airsim_path

        self.__car_client = None
        self.__car_controls = None

        self.__minibatch_dir = os.path.join('minibatches')
        self.__output_model_dir = os.path.join('models')

        self.__make_dir_if_not_exist(self.__minibatch_dir)
        self.__make_dir_if_not_exist(self.__output_model_dir)
        self.__last_model_file = ''

        self.__possible_ip_addresses = []
        self.__trainer_ip_address = None

        self.__experiences = {}

        self.__init_road_points()
        self.__init_reward_points()

    # Starts the agent
    def start(self):
        self.__run_function()

    # The function that will be run during training.
    # It will initialize the connection to the trainer, start AirSim, and continuously run training iterations.
    def __run_function(self):

        self.__model = RlModel(self.__weights_path, self.__train_conv_layers)

        # Connect to the AirSim exe
        self.__connect_to_airsim()

        # Fill the replay memory by driving randomly.
        while True:
            print('Running Airsim Epoch.')
            try:
                self.__run_airsim_epoch(True)
                percent_full = 100.0 * len(self.__experiences['actions'])/self.__replay_memory_size
                print('Replay memory now contains {0} members. ({1}% full)'.format(len(self.__experiences['actions']), percent_full))

                if (percent_full >= 100.0):
                    break
            except msgpackrpc.error.TimeoutError:
                print('Lost connection to AirSim while fillling replay memory. Attempting to reconnect.')
                self.__connect_to_airsim()

        while True:
            try:
                if (self.__model is not None):

                    #Generate a series of training examples by driving the vehicle in AirSim
                    print('Running Airsim Epoch.')
                    experiences, frame_count = self.__run_airsim_epoch(False)

                    # If we didn't immediately crash, train on the gathered experiences
                    if (frame_count > 0):
                        print('Generating {0} minibatches...'.format(frame_count))

                        print('Sampling Experiences.')
                        # Sample experiences from the replay memory
                        sampled_experiences = self.__sample_experiences(experiences, frame_count, True)

                        self.__num_batches_run += frame_count
                        
                        # If we successfully sampled, train on the collected minibatches and send the gradients to the trainer node
                        if (len(sampled_experiences) > 0):
                            print('Publishing AirSim Epoch.')
                            self.__publish_batch_and_update_model(frame_count)

            except msgpackrpc.error.TimeoutError:
                print('Lost connection to AirSim. Attempting to reconnect.')
                self.__connect_to_airsim()

    def __connect_to_airsim(self):
        self.__car_client = CarClient()
        self.__car_client.confirmConnection()
        self.__car_client.enableApiControl(True)
        self.__car_controls = CarControls()
        print('Connected!')

    # Appends a sample to a ring buffer.
    # If the appended example takes the size of the buffer over buffer_size, the example at the front will be removed.
    def __append_to_ring_buffer(self, item, buffer, buffer_size):
        if (len(buffer) >= buffer_size):
            buffer = buffer[1:]
        buffer.append(item)
        return buffer
    
    # Runs an interation of data generation from AirSim.
    # Data will be saved in the replay memory.
    def __run_airsim_epoch(self, always_random):
        print('Running AirSim epoch.')
        
        # Pick a random starting point on the roads
        starting_points, starting_direction = self.__get_next_starting_point()
        
        # Initialize the state buffer.
        # For now, save 4 images at 0.01 second intervals.
        state_buffer_len = 4
        state_buffer = []
        wait_delta_sec = 0.01

        print('Getting Pose')
        self.__car_client.simSetPose(Pose(Vector3r(starting_points[0], starting_points[1], starting_points[2]), AirSimClientBase.toQuaternion(starting_direction[0], starting_direction[1], starting_direction[2])), True)

        # Currently, simSetPose does not allow us to set the velocity. 
        # So, if we crash and call simSetPose, the car will be still moving at its previous velocity.
        # We need the car to stop moving, so push the brake and wait for a few seconds.
        print('Waiting for momentum to die')
        self.__car_controls.steering = 0
        self.__car_controls.throttle = 0
        self.__car_controls.brake = 1
        self.__car_client.setCarControls(self.__car_controls)
        time.sleep(4)
        
        print('Resetting')
        self.__car_client.simSetPose(Pose(Vector3r(starting_points[0], starting_points[1], starting_points[2]), AirSimClientBase.toQuaternion(starting_direction[0], starting_direction[1], starting_direction[2])), True)

        #Start the car rolling so it doesn't get stuck
        print('Running car for a few seconds...')
        self.__car_controls.steering = 0
        self.__car_controls.throttle = 1
        self.__car_controls.brake = 0
        self.__car_client.setCarControls(self.__car_controls)
        
        # While the car is rolling, start initializing the state buffer
        stop_run_time =datetime.datetime.now() + datetime.timedelta(seconds=2)
        while(datetime.datetime.now() < stop_run_time):
            time.sleep(wait_delta_sec)
            state_buffer = self.__append_to_ring_buffer(self.__get_image(), state_buffer, state_buffer_len)
        done = False

        # records the state we go to
        actions = []
        pre_states = []
        post_states = []
        rewards = []
        predicted_rewards = []
        car_state = self.__car_client.getCarState()

        start_time = datetime.datetime.utcnow()
        end_time = start_time + datetime.timedelta(seconds=self.__max_epoch_runtime_sec)
        
        num_random = 0
        far_off = False
        
        # Main data collection loop
        while not done:
            collision_info = self.__car_client.getCollisionInfo()
            utc_now = datetime.datetime.utcnow()
            
            # Check for terminal conditions:
            # 1) Car has collided
            # 2) Car is stopped
            # 3) The run has been running for longer than max_epoch_runtime_sec. 
            #       This constraint is so the model doesn't end up having to churn through huge chunks of data, slowing down training
            # 4) The car has run off the road
            if (collision_info.has_collided or car_state.speed < 2 or utc_now > end_time or far_off):
                print('Start time: {0}, end time: {1}'.format(start_time, utc_now), file=sys.stderr)
                if (utc_now > end_time):
                    print('timed out.')
                    print('Full autonomous run finished at {0}'.format(utc_now), file=sys.stderr)
                done = True
                sys.stderr.flush()
            else:

                # The Agent should occasionally pick random action instead of best action
                do_greedy = np.random.random_sample()
                pre_state = copy.deepcopy(state_buffer)
                if (do_greedy < self.__epsilon or always_random):
                    num_random += 1
                    next_state = self.__model.get_random_state()
                    predicted_reward = 0
                    
                else:
                    next_state, predicted_reward = self.__model.predict_state(pre_state)
                    print('Model predicts {0}'.format(next_state))

                # Convert the selected state to a control signal
                next_control_signals = self.__model.state_to_control_signals(next_state, self.__car_client.getCarState())

                # Take the action
                self.__car_controls.steering = next_control_signals[0]
                self.__car_controls.throttle = next_control_signals[1]
                self.__car_controls.brake = next_control_signals[2]
                self.__car_client.setCarControls(self.__car_controls)
                
                # Wait for a short period of time to see outcome
                time.sleep(wait_delta_sec)

                # Observe outcome and compute reward from action
                state_buffer = self.__append_to_ring_buffer(self.__get_image(), state_buffer, state_buffer_len)
                car_state = self.__car_client.getCarState()
                collision_info = self.__car_client.getCollisionInfo()
                reward, far_off = self.__compute_reward(collision_info, car_state)
                
                # Add the experience to the set of examples from this iteration
                pre_states.append(pre_state)
                post_states.append(state_buffer)
                rewards.append(reward)
                predicted_rewards.append(predicted_reward)
                actions.append(next_state)

        # Only the last state is a terminal state.
        is_not_terminal = [1 for i in range(0, len(actions)-1, 1)]
        is_not_terminal.append(0)
        
        # Add all of the states from this iteration to the replay memory
        self.__add_to_replay_memory('pre_states', pre_states)
        self.__add_to_replay_memory('post_states', post_states)
        self.__add_to_replay_memory('actions', actions)
        self.__add_to_replay_memory('rewards', rewards)
        self.__add_to_replay_memory('predicted_rewards', predicted_rewards)
        self.__add_to_replay_memory('is_not_terminal', is_not_terminal)

        print('Percent random actions: {0}'.format(num_random / max(1, len(actions))))
        print('Num total actions: {0}'.format(len(actions)))
        
        # If we are in the main loop, reduce the epsilon parameter so that the model will be called more often
        # Note: this will be overwritten by the trainer's epsilon if running in distributed mode
        if not always_random:
            self.__epsilon -= self.__per_iter_epsilon_reduction
            self.__epsilon = max(self.__epsilon, self.__min_epsilon)
        
        return self.__experiences, len(actions)

    # Adds a set of examples to the replay memory
    def __add_to_replay_memory(self, field_name, data):
        if field_name not in self.__experiences:
            self.__experiences[field_name] = data
        else:
            self.__experiences[field_name] += data
            start_index = max(0, len(self.__experiences[field_name]) - self.__replay_memory_size)
            self.__experiences[field_name] = self.__experiences[field_name][start_index:]

    # Sample experiences from the replay memory
    def __sample_experiences(self, experiences, frame_count, sample_randomly):
        sampled_experiences = {}
        sampled_experiences['pre_states'] = []
        sampled_experiences['post_states'] = []
        sampled_experiences['actions'] = []
        sampled_experiences['rewards'] = []
        sampled_experiences['predicted_rewards'] = []
        sampled_experiences['is_not_terminal'] = []

        # Compute the surprise factor, which is the difference between the predicted an the actual Q value for each state.
        # We can use that to weight examples so that we are more likely to train on examples that the model got wrong.
        suprise_factor = np.abs(np.array(experiences['rewards'], dtype=np.dtype(float)) - np.array(experiences['predicted_rewards'], dtype=np.dtype(float)))
        suprise_factor_normalizer = np.sum(suprise_factor)
        suprise_factor /= float(suprise_factor_normalizer)

        # Generate one minibatch for each frame of the run
        for _ in range(0, frame_count, 1):
            if sample_randomly:
                idx_set = set(np.random.choice(list(range(0, suprise_factor.shape[0], 1)), size=(self.__batch_size), replace=False))
            else:
                idx_set = set(np.random.choice(list(range(0, suprise_factor.shape[0], 1)), size=(self.__batch_size), replace=False, p=suprise_factor))
        
            sampled_experiences['pre_states'] += [experiences['pre_states'][i] for i in idx_set]
            sampled_experiences['post_states'] += [experiences['post_states'][i] for i in idx_set]
            sampled_experiences['actions'] += [experiences['actions'][i] for i in idx_set]
            sampled_experiences['rewards'] += [experiences['rewards'][i] for i in idx_set]
            sampled_experiences['predicted_rewards'] += [experiences['predicted_rewards'][i] for i in idx_set]
            sampled_experiences['is_not_terminal'] += [experiences['is_not_terminal'][i] for i in idx_set]
            
        return sampled_experiences
        
     
    # Train the model on minibatches and post to the trainer node.
    # The trainer node will respond with the latest version of the model that will be used in further data generation iterations.
    def __publish_batch_and_update_model(self, batches_count):
        # Train and get the gradients

        if (self.__num_batches_run > self.__batch_update_frequency + self.__last_checkpoint_batch_count):
            self.__model.update_critic()

            checkpoint = {}
            checkpoint['model'] = self.__model.to_packet(get_target=True)
            checkpoint['batch_count'] = batches_count
            checkpoint_str = json.dumps(checkpoint)

            checkpoint_dir = os.path.join('checkpoint', self.__experiment_name)

            if not os.path.isdir(checkpoint_dir):
                os.makedirs(checkpoint_dir)

            file_name = os.path.join(checkpoint_dir,'{0}.json'.format(self.__num_batches_run))
            with open(file_name, 'w') as f:
                print('Checkpointing to {0}'.format(file_name))
                f.write(checkpoint_str)

            self.__last_checkpoint_batch_count = self.__num_batches_run
                
    # Gets the latest model from the trainer node
    def __get_latest_model(self):
        print('Getting latest model from parameter server...')
        response = requests.get('http://{0}:80/latest'.format(self.__trainer_ip_address)).json()
        self.__model.from_packet(response)

    # Gets an image from AirSim
    def __get_image(self):
        image_response = self.__car_client.simGetImages([ImageRequest(0, AirSimImageType.Scene, False, False)])[0]
        image1d = np.fromstring(image_response.image_data_uint8, dtype=np.uint8)
        image_rgba = image1d.reshape(image_response.height, image_response.width, 4)

        return image_rgba[76:135,0:255,0:3].astype(float)

    # Computes the reward functinon based on the car position.
    def __compute_reward(self, collision_info, car_state):
        #Define some constant parameters for the reward function
        THRESH_DIST = 3.5                # The maximum distance from the center of the road to compute the reward function
        DISTANCE_DECAY_RATE = 1.2        # The rate at which the reward decays for the distance function
        CENTER_SPEED_MULTIPLIER = 2.0    # The ratio at which we prefer the distance reward to the speed reward

        # If the car has collided, the reward is always zero
        if (collision_info.has_collided):
            return 0.0, True
        
        # If the car is stopped, the reward is always zero
        speed = car_state.speed
        if (speed < 2):
            return 0.0, True
        
        #Get the car position
        position_key = bytes('position', encoding='utf8')
        x_val_key = bytes('x_val', encoding='utf8')
        y_val_key = bytes('y_val', encoding='utf8')

        car_point = np.array([car_state.kinematics_true[position_key][x_val_key], car_state.kinematics_true[position_key][y_val_key], 0])
        
        # Distance component is exponential distance to nearest line
        distance = 999
        
        #Compute the distance to the nearest center line
        for line in self.__reward_points:
            local_distance = 0
            length_squared = ((line[0][0]-line[1][0])**2) + ((line[0][1]-line[1][1])**2)
            if (length_squared != 0):
                t = max(0, min(1, np.dot(car_point-line[0], line[1]-line[0]) / length_squared))
                proj = line[0] + (t * (line[1]-line[0]))
                local_distance = np.linalg.norm(proj - car_point)
            
            distance = min(local_distance, distance)
            
        distance_reward = math.exp(-(distance * DISTANCE_DECAY_RATE))
        
        return distance_reward, distance > THRESH_DIST

    # Initializes the points used for determining the starting point of the vehicle
    def __init_road_points(self):
        self.__road_points = []
        car_start_coords = [12961.722656, 6660.329102, 0]

        with open('road_lines.txt', 'r') as f:
            for line in f:
                points = line.split('\t')
                first_point = np.array([float(p) for p in points[0].split(',')] + [0])
                second_point = np.array([float(p) for p in points[1].split(',')] + [0])
                self.__road_points.append(tuple((first_point, second_point)))

        # Points in road_points.txt are in unreal coordinates
        # But car start coordinates are not the same as unreal coordinates
        for point_pair in self.__road_points:
            for point in point_pair:
                point[0] -= car_start_coords[0]
                point[1] -= car_start_coords[1]
                point[0] /= 100
                point[1] /= 100
              
    # Initializes the points used for determining the optimal position of the vehicle during the reward function
    def __init_reward_points(self):
        self.__reward_points = []
        with open('reward_points.txt', 'r') as f:
            for line in f:
                point_values = line.split('\t')
                first_point = np.array([float(point_values[0]), float(point_values[1]), 0])
                second_point = np.array([float(point_values[2]), float(point_values[3]), 0])
                self.__reward_points.append(tuple((first_point, second_point)))

    # Randomly selects a starting point on the road
    # Used for initializing an iteration of data generation from AirSim
    def __get_next_starting_point(self):

        # Pick a random road.
        random_line_index = np.random.randint(0, high=len(self.__road_points))
        
        # Pick a random position on the road. 
        # Do not start too close to either end, as the car may crash during the initial run.
        random_interp = (np.random.random_sample() * 0.4) + 0.3
        
        # Pick a random direction to face
        random_direction_interp = np.random.random_sample()

        # Compute the starting point of the car
        random_line = self.__road_points[random_line_index]
        random_start_point = list(random_line[0])
        random_start_point[0] += (random_line[1][0] - random_line[0][0])*random_interp
        random_start_point[1] += (random_line[1][1] - random_line[0][1])*random_interp

        # Compute the direction that the vehicle will face
        # Vertical line
        if (np.isclose(random_line[0][1], random_line[1][1])):
            if (random_direction_interp > 0.5):
                random_direction = (0,0,0)
            else:
                random_direction = (0, 0, math.pi)
        # Horizontal line
        elif (np.isclose(random_line[0][0], random_line[1][0])):
            if (random_direction_interp > 0.5):
                random_direction = (0,0,math.pi/2)
            else:
                random_direction = (0,0,-1.0 * math.pi/2)

        # The z coordinate is always zero
        random_start_point[2] = -0
        return (random_start_point, random_direction)

    # A helper function to make a directory if it does not exist
    def __make_dir_if_not_exist(self, directory):
        if not (os.path.exists(directory)):
            os.makedirs(directory)




if __name__ == '__main__':

    batch_update_frequency = 10
    max_epoch_runtime_sec = 30
    per_iter_epsilon_reduction = 0.003
    min_epsilon = 0.1
    batch_size = 32
    replay_memory_size = 50
    weights_path = "pretrain_model_weights.h5"
    train_conv_layers = False
    airsim_path = "/AD_Cookbook_AirSim"
    experiment_name = "rl_run_local"

    # Start the training
    agent = DistributedAgent(batch_update_frequency, max_epoch_runtime_sec, per_iter_epsilon_reduction, min_epsilon, batch_size
                             , replay_memory_size, weights_path, train_conv_layers, airsim_path, experiment_name)
    agent.start()
