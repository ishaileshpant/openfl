from abc import ABC, abstractmethod
from typing import Any, Iterator, List, Tuple


class AggregatorClientInterface(ABC):
    @abstractmethod
    def get_tasks(self, collaborator_name: str) -> Tuple[List[Any], int, int, bool]:
        """
        Retrieves tasks for the given collaborator.
        Returns a tuple: (tasks, round_number, sleep_time, time_to_quit)
        """
        pass

    @abstractmethod
    def get_aggregated_tensor(
        self,
        collaborator_name: str,
        tensor_name: str,
        round_number: int,
        report: bool,
        tags: List[str],
        require_lossless: bool,
    ) -> Any:
        """
        Retrieves the aggregated tensor.
        """
        pass

    @abstractmethod
    def send_local_task_results(
        self,
        collaborator_name: str,
        round_number: int,
        task_name: str,
        data_size: int,
        named_tensors: List[Any],
    ) -> Any:
        """
        Sends local task results.
        Parameters:
          collaborator_name: Name of the collaborator.
          round_number: The current round.
          task_name: Name of the task.
          data_size: Size of the data.
          named_tensors: A list of tensors (or named tensor objects).
        Returns a SendLocalTaskResultsResponse.
        """
        pass

    def get_metric_stream(self, experiment_name: str) -> Iterator[Any]:
        """
        Initiates a metric stream for the given experiment.
        Returns an iterator over GetMetricStreamResponse messages.
        """
        raise NotImplementedError

    def get_trained_model(self, experiment_name: str, model_type: int) -> Any:
        """
        Retrieves the trained model.
        """
        raise NotImplementedError

    def get_experiment_description(self, name: str) -> Any:
        """
        Retrieves the experiment description.
        """
        raise NotImplementedError
