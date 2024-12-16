# Copyright 2020-2024 Intel Corporation
# SPDX-License-Identifier: Apache-2.0


"""Assigner module."""

from functools import wraps

pass


def mode_based_filtering(func):
    """Decorator to filter task groups based on mode.

    This decorator should be applied to define_task_assignments() method
    in Assigner subclasses to handle mode-based filtering.
    """

    @wraps(func)
    def wrapper(self, *args, **kwargs):
        # Filter task groups based on mode before assignment
        if hasattr(self, "mode"):
            self.task_groups = [
                group for group in self.task_groups if group["name"].startswith(self.mode)
            ]

            if not self.task_groups:
                raise ValueError(f"No task groups found for mode: {self.mode}")

            # Mode-specific validations
            if self.mode == "evaluate":
                assert self.rounds == 1, "Number of rounds should be 1 for evaluate mode"

        # Call the original method
        return func(self, *args, **kwargs)

    return wrapper


class Assigner:
    r"""
    The task assigner maintains a list of tasks.

    Also it decides the policy for which collaborator should run those tasks.
    There may be many types of policies implemented, but a natural place to start
    is with a:

        - RandomGroupedTaskAssigner :
            Given a set of task groups, and a percentage,
            assign that task group to that percentage of collaborators in the federation.
            After assigning the tasks to collaborator, those tasks should be carried
            out each round (no reassignment between rounds).
        - GroupedTaskAssigner :
            Given task groups and a list of collaborators that belong to that task group,
            carry out tasks for each round of experiment.

    Attributes:
        tasks* (list of object): List of tasks to assign.
        authorized_cols (list of str): Collaborators.
        rounds (int): Number of rounds to train.
        all_tasks_in_groups (list): All tasks in groups.
        task_group_collaborators (dict): Task group collaborators.
        collaborators_for_task (dict): Collaborators for each task.
        collaborator_tasks (dict): Tasks for each collaborator.

    .. note::
        \* - ``tasks`` argument is taken from ``tasks`` section of FL plan YAML file.
    """

    def __init__(
        self, tasks, authorized_cols, rounds_to_train, mode: str = "train_and_validate", **kwargs
    ):
        """Initializes the Assigner.

        Args:
            tasks (list of object): List of tasks to assign.
            authorized_cols (list of str): Collaborators.
            rounds_to_train (int): Number of training rounds.
            mode (str, optional): Federation mode for filtering tasks. Defaults to "train_and_validate".
            **kwargs: Additional keyword arguments.
        """
        self.tasks = tasks
        self.authorized_cols = authorized_cols
        self.rounds = rounds_to_train
        self.mode = mode
        self.all_tasks_in_groups = []

        self.task_group_collaborators = {}
        self.collaborators_for_task = {}
        self.collaborator_tasks = {}

        self.define_task_assignments()

    def define_task_assignments(self):
        """Abstract method."""
        raise NotImplementedError

    def get_tasks_for_collaborator(self, collaborator_name, round_number):
        """Abstract method."""
        raise NotImplementedError

    def get_collaborators_for_task(self, task_name, round_number):
        """Abstract method."""
        raise NotImplementedError

    def get_all_tasks_for_round(self, round_number):
        """Return tasks for the current round.

        Currently all tasks are performed on each round,
        But there may be a reason to change this.

        Args:
            round_number (int): Round number.

        Returns:
            list: List of tasks for the current round.
        """
        return self.all_tasks_in_groups

    def get_aggregation_type_for_task(self, task_name):
        """Extract aggregation type from self.tasks.

        Args:
            task_name (str): Name of the task.

        Returns:
            str: Aggregation type for the task.
        """
        if "aggregation_type" not in self.tasks[task_name]:
            return None
        return self.tasks[task_name]["aggregation_type"]
