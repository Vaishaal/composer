# Copyright 2022 MosaicML Composer authors
# SPDX-License-Identifier: Apache-2.0

import json
from concurrent.futures import Future
from typing import Type
from unittest.mock import MagicMock

import mcli
import pytest
import torch
from torch.utils.data import DataLoader

from composer.core import Callback, Time, TimeUnit
from composer.loggers import WandBLogger
from composer.loggers.mosaicml_logger import (
    MOSAICML_ACCESS_TOKEN_ENV_VAR,
    MOSAICML_PLATFORM_ENV_VAR,
    MosaicMLLogger,
    format_data_to_json_serializable,
)
from composer.trainer import Trainer
from composer.utils import dist, get_composer_env_dict
from tests.callbacks.callback_settings import get_cb_kwargs, get_cb_model_and_datasets, get_cbs_and_marks
from tests.common import RandomClassificationDataset, SimpleModel
from tests.common.markers import world_size


class MockMAPI:

    def __init__(self, simulate_exception: bool = False):
        self.run_metadata = {}
        self.simulate_exception = simulate_exception

    def update_run_metadata(self, run_name, new_metadata, future=False, protect=True):
        if future:
            # Simulate asynchronous behavior using Future
            future_obj = Future()
            try:
                self._update_metadata(run_name, new_metadata)
                future_obj.set_result(None)  # Set a result to indicate completion
            except Exception as e:
                future_obj.set_exception(e)  # Set an exception if something goes wrong
            return future_obj
        else:
            self._update_metadata(run_name, new_metadata)

    def _update_metadata(self, run_name, new_metadata):
        if self.simulate_exception:
            raise RuntimeError('Simulated exception')
        if run_name not in self.run_metadata:
            self.run_metadata[run_name] = {}
        for k, v in new_metadata.items():
            self.run_metadata[run_name][k] = v
        # Serialize the data to ensure it is json serializable
        json.dumps(self.run_metadata[run_name])


def test_format_data_to_json_serializable():
    data = {
        'key1': 'value1',
        'key2': 42,
        'key3': 3.14,
        'key4': True,
        'key5': torch.tensor([1, 2, 3]),
        'key6': torch.tensor([42]),
        'key7': {
            'inner_key': 'inner_value',
        },
        'key8': [1, 2, 3],
    }
    formatted_data = format_data_to_json_serializable(data)

    expected_formatted_data = {
        'key1': 'value1',
        'key2': 42,
        'key3': 3.14,
        'key4': True,
        'key5': 'Tensor of shape torch.Size([3])',
        'key6': 42,
        'key7': {
            'inner_key': 'inner_value',
        },
        'key8': [1, 2, 3],
    }

    assert formatted_data == expected_formatted_data


@pytest.mark.parametrize('callback_cls', get_cbs_and_marks(callbacks=True))
@world_size(1, 2)
@pytest.mark.filterwarnings('ignore::UserWarning')
def test_logged_data_is_json_serializable(monkeypatch, callback_cls: Type[Callback], world_size):
    """Test that all logged data is json serializable, which is a requirement to use MAPI."""

    mock_mapi = MockMAPI()
    monkeypatch.setenv('MOSAICML_PLATFORM', 'True')
    monkeypatch.setattr(mcli, 'update_run_metadata', mock_mapi.update_run_metadata)
    run_name = 'small_chungus'
    monkeypatch.setenv('RUN_NAME', run_name)

    callback_kwargs = get_cb_kwargs(callback_cls)
    callback = callback_cls(**callback_kwargs)
    train_dataset = RandomClassificationDataset()
    model, train_dataloader, _ = get_cb_model_and_datasets(callback, sampler=dist.get_sampler(train_dataset))
    trainer = Trainer(
        model=model,
        train_dataloader=train_dataloader,
        train_subset_num_batches=1,
        max_duration='1ep',
        callbacks=callback,
        loggers=MosaicMLLogger(),
    )
    trainer.fit()

    if dist.get_global_rank() == 0:
        assert len(mock_mapi.run_metadata[run_name].keys()) > 0
    else:
        assert len(mock_mapi.run_metadata.keys()) == 0


@world_size(1, 2)
@pytest.mark.parametrize('ignore_exceptions', [True, False])
def test_logged_data_exception_handling(monkeypatch, world_size: int, ignore_exceptions: bool):
    """Test that exceptions in MAPI are raised properly."""
    mock_mapi = MockMAPI(simulate_exception=True)
    monkeypatch.setenv('MOSAICML_PLATFORM', 'True')
    monkeypatch.setattr(mcli, 'update_run_metadata', mock_mapi.update_run_metadata)
    run_name = 'small_chungus'
    monkeypatch.setenv('RUN_NAME', run_name)

    logger = MosaicMLLogger(ignore_exceptions=ignore_exceptions)
    logger.buffered_metadata = {'key': 'value'}  # Add dummy data so logging runs
    if dist.get_global_rank() != 0:
        assert logger._enabled is False
        logger._flush_metadata(force_flush=True)
        assert logger._enabled is False
    elif ignore_exceptions:
        assert logger._enabled is True
        logger._flush_metadata(force_flush=True)
        assert logger._enabled is False
    else:
        with pytest.raises(RuntimeError, match='Simulated exception'):
            assert logger._enabled is True
            logger._flush_metadata(force_flush=True)


def test_metric_partial_filtering(monkeypatch):
    mock_mapi = MockMAPI()
    monkeypatch.setenv('MOSAICML_PLATFORM', 'True')
    monkeypatch.setattr(mcli, 'update_run_metadata', mock_mapi.update_run_metadata)
    run_name = 'small_chungus'
    monkeypatch.setenv('RUN_NAME', run_name)

    trainer = Trainer(
        model=SimpleModel(),
        train_dataloader=DataLoader(RandomClassificationDataset()),
        train_subset_num_batches=2,
        max_duration='1ep',
        loggers=MosaicMLLogger(ignore_keys=['loss', 'accuracy']),
    )
    trainer.fit()

    assert 'mosaicml/num_nodes' in mock_mapi.run_metadata[run_name]
    assert 'mosaicml/loss' not in mock_mapi.run_metadata[run_name]


def test_logged_composer_version(monkeypatch):
    mock_mapi = MockMAPI()
    monkeypatch.setenv('MOSAICML_PLATFORM', 'True')
    monkeypatch.setattr(mcli, 'update_run_metadata', mock_mapi.update_run_metadata)
    run_name = 'small_chungus'
    monkeypatch.setenv('RUN_NAME', run_name)

    Trainer(
        model=SimpleModel(),
        train_dataloader=DataLoader(RandomClassificationDataset()),
        train_subset_num_batches=2,
        max_duration='1ep',
        loggers=MosaicMLLogger(ignore_keys=['loss', 'accuracy']),
    )
    composer_env_dict = get_composer_env_dict()
    composer_version = composer_env_dict['composer_version']
    composer_commit_hash = str(composer_env_dict['composer_commit_hash'])
    assert composer_version == mock_mapi.run_metadata[run_name]['mosaicml/composer_version']
    assert composer_commit_hash == mock_mapi.run_metadata[run_name]['mosaicml/composer_commit_hash']


def test_metric_full_filtering(monkeypatch):
    mock_mapi = MockMAPI()
    monkeypatch.setenv('MOSAICML_PLATFORM', 'True')
    monkeypatch.setattr(mcli, 'update_run_metadata', mock_mapi.update_run_metadata)
    run_name = 'small_chungus'
    monkeypatch.setenv('RUN_NAME', run_name)

    trainer = Trainer(
        model=SimpleModel(),
        train_dataloader=DataLoader(RandomClassificationDataset()),
        train_subset_num_batches=2,
        max_duration='1ep',
        loggers=MosaicMLLogger(ignore_keys=['*']),
    )
    trainer.fit()

    assert run_name not in mock_mapi.run_metadata


class SetWandBRunURL(Callback):
    """Sets run_url attribute on WandB for offline unit testing."""

    def __init__(self, run_url) -> None:
        self.run_url = run_url

    def init(self, state, event) -> None:
        for callback in state.callbacks:
            if isinstance(callback, WandBLogger):
                callback.run_url = self.run_url


def test_wandb_run_url(monkeypatch):
    mock_mapi = MockMAPI()
    monkeypatch.setenv('MOSAICML_PLATFORM', 'True')
    monkeypatch.setattr(mcli, 'update_run_metadata', mock_mapi.update_run_metadata)
    run_name = 'small_chungus'
    monkeypatch.setenv('RUN_NAME', run_name)

    run_url = 'my_run_url'
    monkeypatch.setenv('WANDB_MODE', 'offline')

    Trainer(
        model=SimpleModel(),
        loggers=[
            MosaicMLLogger(),
            WandBLogger(),
        ],
        callbacks=[
            SetWandBRunURL(run_url),
        ],
    )

    assert mock_mapi.run_metadata[run_name]['mosaicml/wandb/run_url'] == run_url


@pytest.mark.parametrize('platform_env_var', ['True', 'None'])
@pytest.mark.parametrize('access_token_env_var', ['my-token', 'None'])
@pytest.mark.parametrize('logger_set', [True, False])
def test_auto_add_logger(monkeypatch, platform_env_var, access_token_env_var, logger_set):
    mock_mapi = MockMAPI()
    monkeypatch.setenv('MOSAICML_PLATFORM', 'True')
    monkeypatch.setattr(mcli, 'update_run_metadata', mock_mapi.update_run_metadata)
    run_name = 'small_chungus'
    monkeypatch.setenv('RUN_NAME', run_name)

    monkeypatch.setenv(MOSAICML_PLATFORM_ENV_VAR, platform_env_var)
    monkeypatch.setenv(MOSAICML_ACCESS_TOKEN_ENV_VAR, access_token_env_var)

    trainer = Trainer(
        model=SimpleModel(),
        train_dataloader=DataLoader(RandomClassificationDataset()),
        train_subset_num_batches=2,
        max_duration='1ep',
        loggers=MosaicMLLogger() if logger_set else None,
    )

    logger_count = 0
    for callback in trainer.state.callbacks:
        if isinstance(callback, MosaicMLLogger):
            logger_count += 1
    # If logger is specified manually, ensure only 1
    if logger_set:
        assert logger_count == 1
    # Otherwise, auto-add only if platform and access token are set
    elif platform_env_var.lower() == 'true' and access_token_env_var is not None:
        assert logger_count == 1
    # Otherwise, no logger
    else:
        assert logger_count == 0


def test_run_events_logged(monkeypatch):
    ''''
    Current run events include:
    1. model initialization time
    2. training progress (i.e. [batch=x/xx] at batch end)
    '''
    mock_mapi = MockMAPI()
    monkeypatch.setenv('MOSAICML_PLATFORM', 'True')
    monkeypatch.setattr(mcli, 'update_run_metadata', mock_mapi.update_run_metadata)
    run_name = 'test-run-name'
    monkeypatch.setenv('RUN_NAME', run_name)
    trainer = Trainer(
        model=SimpleModel(),
        train_dataloader=DataLoader(RandomClassificationDataset()),
        train_subset_num_batches=1,
        max_duration='4ba',
        loggers=[MosaicMLLogger()],
    )
    trainer.fit()
    metadata = mock_mapi.run_metadata[run_name]
    assert isinstance(metadata['mosaicml/model_initialized_time'], float)
    assert 'mosaicml/training_progress' in metadata
    assert metadata['mosaicml/training_progress'] == '[batch=4/4]'
    assert 'mosaicml/training_sub_progress' not in metadata
    assert isinstance(metadata['mosaicml/train_finished_time'], float)


def test_token_training_progress_metrics():
    logger = MosaicMLLogger()
    logger._enabled = True
    state = MagicMock()
    state.max_duration.unit = TimeUnit.TOKEN
    state.max_duration.value = 64
    state.timestamp.token.value = 50
    training_progress = logger._get_training_progress_metrics(state)
    assert 'training_progress' in training_progress
    assert training_progress['training_progress'] == '[token=50/64]'
    assert 'training_sub_progress' not in training_progress


def test_epoch_training_progress_metrics():
    logger = MosaicMLLogger()
    logger._enabled = True
    state = MagicMock()
    state.max_duration.unit = TimeUnit.EPOCH
    state.max_duration = Time(3, TimeUnit.EPOCH)
    state.timestamp.epoch = Time(2, TimeUnit.EPOCH)
    state.timestamp.batch = Time(11, TimeUnit.BATCH)
    state.timestamp.batch_in_epoch = Time(1, TimeUnit.BATCH)
    training_progress = logger._get_training_progress_metrics(state)
    assert 'training_progress' in training_progress
    assert training_progress['training_progress'] == '[epoch=3/3]'
    assert 'training_sub_progress' in training_progress
    assert training_progress['training_sub_progress'] == '[batch=1/5]'


def test_epoch_zero_progress_metrics():
    logger = MosaicMLLogger()
    logger._enabled = True
    state = MagicMock()
    logger.train_dataloader_len = 5
    state.max_duration.unit = TimeUnit.EPOCH
    state.max_duration = Time(3, TimeUnit.EPOCH)
    state.timestamp.epoch = Time(0, TimeUnit.EPOCH)
    state.timestamp.batch = Time(0, TimeUnit.BATCH)
    state.timestamp.batch_in_epoch = Time(0, TimeUnit.BATCH)
    training_progress = logger._get_training_progress_metrics(state)
    assert 'training_progress' in training_progress
    assert training_progress['training_progress'] == '[epoch=1/3]'
    assert 'training_sub_progress' in training_progress
    assert training_progress['training_sub_progress'] == '[batch=0/5]'


def test_epoch_zero_no_dataloader_progress_metrics():
    logger = MosaicMLLogger()
    logger._enabled = True
    state = MagicMock()
    state.dataloader_len = None
    state.max_duration.unit = TimeUnit.EPOCH
    state.max_duration = Time(3, TimeUnit.EPOCH)
    state.timestamp.epoch = Time(0, TimeUnit.EPOCH)
    state.timestamp.batch = Time(1, TimeUnit.BATCH)
    state.timestamp.batch_in_epoch = Time(1, TimeUnit.BATCH)
    training_progress = logger._get_training_progress_metrics(state)
    assert 'training_progress' in training_progress
    assert training_progress['training_progress'] == '[epoch=1/3]'
    assert 'training_sub_progress' in training_progress
    assert training_progress['training_sub_progress'] == '[batch=1]'
