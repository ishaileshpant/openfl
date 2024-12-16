# Copyright (C) 2020-2023 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
"""ModeBasedAssigner tests."""

import pytest
from openfl.component.assigner.mode_based_assigner import ModeBasedAssigner


@pytest.fixture
def sample_tasks():
    return ["task1", "task2", "task3", "task4"]

@pytest.fixture
def sample_authorized_cols():
    return ["col1", "col2", "col3"]

@pytest.fixture
def sample_task_groups():
    return [
        {"name": "train", "tasks": ["task1", "task2"], "percentage": 1.0},
        {"name": "evaluate", "tasks": ["task3"], "percentage": 1.0},
        {"name": "train_and_validate", "tasks": ["task4"], "percentage": 1.0}
    ]

def test_init_with_valid_mode(sample_tasks, sample_authorized_cols):
    """Test initialization with valid mode and task groups."""
    training_task_groups = [{"name": "train", "tasks": ["task1"], "percentage": 1.0}]
    assigner = ModeBasedAssigner(
        task_groups=training_task_groups,
        mode="train",
        tasks=sample_tasks,
        authorized_cols=sample_authorized_cols,
        rounds_to_train=3
    )
    assert assigner.mode == "train"
    assert assigner.task_groups == training_task_groups

def test_define_task_assignments_train_mode(sample_task_groups, sample_tasks, sample_authorized_cols):
    """Test task assignments filtering for train mode."""
    assigner = ModeBasedAssigner(
        task_groups=sample_task_groups,
        mode="train",
        tasks=sample_tasks,
        authorized_cols=sample_authorized_cols,
        rounds_to_train=3
    )
    assigner.define_task_assignments()
    assert len(assigner.task_groups) == 1
    assert assigner.task_groups[0]["name"] == "train"

def test_define_task_assignments_evaluate_mode(sample_task_groups, sample_tasks, sample_authorized_cols):
    """Test task assignments filtering for evaluate mode with rounds=1."""
    assigner = ModeBasedAssigner(
        task_groups=sample_task_groups,
        mode="evaluate",
        tasks=sample_tasks,
        authorized_cols=sample_authorized_cols,
        rounds_to_train=1
    )
    assigner.define_task_assignments()
    assert len(assigner.task_groups) == 1
    assert assigner.task_groups[0]["name"] == "evaluate"

def test_init_with_invalid_task_group_format(sample_tasks, sample_authorized_cols):
    """Test initialization with task groups missing required fields."""
    invalid_task_groups = [{"name": "train"}]  # Missing 'tasks' and 'percentage'
    with pytest.raises(KeyError):
        ModeBasedAssigner(
            task_groups=invalid_task_groups,
            mode="train",
            tasks=sample_tasks,
            authorized_cols=sample_authorized_cols,
            rounds_to_train=1
        )
