import math
import warnings

import numpy as np
import pandas as pd
from nilmtk import DataSet
from tensorflow.keras.utils import Sequence

warnings.simplefilter(action='ignore', category=FutureWarning)


class TimeSeries:
    """
    Encapsulates UK Dale dataset handling intelligence with a focus on loading the data from a single HDF5 format file.
    """

    def __init__(self, dataset: DataSet, training_buildings: list, test_buildings: list, wandb_config):
        self.training_buildings = training_buildings
        self.test_buildings = test_buildings
        self.dataset = dataset
        self.wandb_config = wandb_config
        self.window_size = wandb_config.window_size
        self.batch_size = wandb_config.batch_size
        self.appliance = wandb_config.appliance
        self.mean_power = None
        self.std_power = None
        self._compute_normalization_params()

    def _compute_normalization_params(self):
        """
        Computes normalization parameters (mean and standard deviation) using the training data.
        """
        all_train_mains_power = []

        for building in self.training_buildings:
            train_elec = self.dataset.buildings[building].elec
            train_mains = train_elec.mains()
            mains_data_frame = train_mains.load()

            for train_mains_df in mains_data_frame:
                mains_power = train_mains_df[('power', 'apparent')]
                all_train_mains_power.append(mains_power)

        if all_train_mains_power:
            combined_mains_power = pd.concat(all_train_mains_power, axis=0)
            self.mean_power = combined_mains_power.mean()
            self.std_power = combined_mains_power.std()
            print(f"Mean power: {self.mean_power}")
            print(f"Std power: {self.std_power}")

            if math.isnan(self.mean_power) or math.isnan(self.std_power):
                raise ValueError("Normalization parameters contain NaN values. Check your data preprocessing steps.")
        else:
            raise ValueError("No training data available for normalization.")

    def getTrainingDataGenerator(self):
        return TimeSeriesDataGenerator(
            self.dataset, self.training_buildings, self.appliance, self.mean_power,
            self.std_power, self.wandb_config, is_training=True
        )

    def getTestDataGenerator(self):
        return TimeSeriesDataGenerator(
            self.dataset, self.test_buildings, self.appliance, self.mean_power,
            self.std_power, self.wandb_config, is_training=False
        )


class TimeSeriesDataGenerator(Sequence):
    def __init__(self, dataset, buildings, appliance, mean_power, std_power, wandb_config, is_training=True):
        self.dataset = dataset
        self.buildings = buildings
        self.appliance = appliance
        self.mean_power = mean_power
        self.std_power = std_power
        self.window_size = wandb_config.window_size
        self.batch_size = wandb_config.batch_size
        self.max_power = wandb_config.max_power
        self.on_threshold = wandb_config.on_threshold
        self.min_on_duration = wandb_config.min_on_duration
        self.min_off_duration = wandb_config.min_off_duration
        self.is_training = is_training
        self.data_generator = self._data_generator()
        self.total_samples = self._count_samples()

    def _data_generator(self):
        """
        Generates data chunks for each building and processes it into usable batches.
        """
        chunk_size = 1000000
        for building in self.buildings:
            elec = self.dataset.buildings[building].elec
            mains = elec.mains()
            appliance = elec[self.appliance]
            for mains_df, appliance_df in zip(mains.load(chunksize=chunk_size), appliance.load(chunksize=chunk_size)):
                mains_power_apparent = mains_df[('power', 'apparent')]
                appliance_power_active = appliance_df[('power', 'active')]
                mains_power, appliance_power = self._process_data(mains_power_apparent, appliance_power_active)
                for i in range(0, len(mains_power) - self.window_size + 1, self.window_size):
                    yield mains_power[i:i + self.window_size], appliance_power[i:i + self.window_size]

    def _count_samples(self):
        """
        Adjusted total sample count to avoid mismatch with sequence length.
        """
        total_samples = 0
        for building in self.buildings:
            elec = self.dataset.buildings[building].elec
            mains = elec.mains()
            mains_length, _ = next(mains.load()).shape
            total_samples += mains_length
        return total_samples // self.window_size

    def __len__(self):
        """
        Defines the number of batches in one epoch.
        """
        return self.total_samples // self.batch_size

    def __getitem__(self, index):
        batch_X, batch_y = [], []
        for _ in range(self.batch_size):
            try:
                X, y = next(self.data_generator)
                batch_X.append(X)
                batch_y.append(y)
            except StopIteration:
                self.data_generator = self._data_generator()  # Reset generator
                X, y = next(self.data_generator)
                batch_X.append(X)
                batch_y.append(y)

        return np.array(batch_X), np.array(batch_y)

    def _process_data(self, mains_power, appliance_power):
        # ClampPING the appliance power between on_threshold and max_power
        appliance_power = appliance_power.clip(lower=self.on_threshold, upper=self.max_power)

        # Remove any possible duplicated indices
        mains_power = mains_power[~mains_power.index.duplicated(keep='first')]

        # Down sampling mains power to 6s intervals and handling missing values
        mains_power = mains_power.resample('6s').nearest(limit=1)
        mains_power, appliance_power = mains_power.align(appliance_power, join='inner', axis=0)
        mains_power = mains_power.ffill().bfill()

        # Normalizing only the mains power
        mains_power = (mains_power - self.mean_power) / (self.std_power + 1e-8)

        # Converting appliance power to binary "on/off" states, for convenience
        appliance_status = appliance_power > self.on_threshold

        # Applying minimum on/off duration constraints
        appliance_status = self._apply_min_durations(appliance_status, self.min_on_duration, self.min_off_duration)

        # Basically turning off the appliance when it was not ON for the minimum duration
        appliance_power = appliance_power * appliance_status

        # Convert to numpy arrays
        mains_power = mains_power.values.reshape(-1, 1)
        appliance_power = appliance_power.values.reshape(-1, 1)

        return mains_power, appliance_power

    def _apply_min_durations(self, appliance_status, min_on_duration, min_off_duration):
        """
        Apply minimum on and off durations to the binary appliance status.

        Parameters:
        - appliance_status: Pandas Series of binary on/off states.
        - min_on_duration: Minimum duration (in seconds) an appliance must stay "on".
        - min_off_duration: Minimum duration (in seconds) an appliance must stay "off".

        Returns:
        - Filtered binary appliance status.
        """
        appliance_status = appliance_status.astype(int)  # Ensure binary states are integers
        status_changes = appliance_status.diff().fillna(0)  # Identify state changes

        # Filters short "on" durations
        on_groups = (status_changes == 1).cumsum() * appliance_status
        on_durations = on_groups.groupby(on_groups).transform('count') * 6  # Convert to seconds (6s intervals)
        appliance_status[on_durations < min_on_duration] = 0

        # Filters short "off" durations
        off_groups = (status_changes == -1).cumsum() * (1 - appliance_status)
        off_durations = off_groups.groupby(off_groups).transform('count') * 6  # Convert to seconds (6s intervals)
        appliance_status[off_durations < min_off_duration] = 1

        return appliance_status
