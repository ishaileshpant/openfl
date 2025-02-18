# Copyright 2020-2024 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""AggregatorRESTClient module."""

# Standard library imports
import json
import logging
import struct
import time
from typing import Any, Iterator, List, Tuple

import requests

# Third-party libraries
from google.protobuf import json_format
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Internal modules
from openfl.protocols import aggregator_pb2, base_pb2
from openfl.protocols.aggregator_client_interface import AggregatorClientInterface

logger = logging.getLogger(__name__)


class AggregatorRESTClient(AggregatorClientInterface):
    def __init__(
        self,
        agg_addr,
        agg_rest_port,
        root_certificate,
        certificate,
        private_key,
        use_tls=True,
        require_client_auth=True,
        aggregator_uuid=None,
        federation_uuid=None,
        single_col_cert_common_name=None,
        refetch_server_cert_callback=None,
        **kwargs,
    ):
        """
        Initialize the AggregatorRESTClient.
        This client uses a openfl-grpc-gateway service to convert HTTP/JSON
        requests into gRPC calls.

        If ca_cert is provided, it is used for HTTPS verification.
        If not provided, verification defaults (which may be insecure if using self-signed certs).
        """

        self.use_tls = use_tls
        self.require_client_auth = require_client_auth
        self.root_certificate = root_certificate
        self.certificate = certificate
        self.private_key = private_key
        self.header = None
        self.aggregator_uuid = aggregator_uuid
        self.federation_uuid = federation_uuid
        self.single_col_cert_common_name = single_col_cert_common_name
        self.refetch_server_cert_callback = refetch_server_cert_callback

        # Determine scheme and TLS verification.
        scheme = "https" if self.use_tls else "http"
        self.verify = self.root_certificate if self.use_tls else False
        self.cert = (
            (self.certificate, self.private_key)
            if (self.use_tls and self.require_client_auth and self.certificate and self.private_key)
            else None
        )

        self.session = requests.Session()
        # Request protobuf responses as configured in the gateway
        self.session.headers.update({"Accept": "application/x-protobuf"})

        # Configure timeouts with longer duration for large payloads
        self.timeout = (30, 300)  # (connect timeout, read timeout) in seconds

        # Build the base URL
        self.base_url = f"{scheme}://{agg_addr}:{agg_rest_port}/openfl.aggregator.Aggregator"
        logger.info("Initialized REST Aggregator Client with base URL: %s", self.base_url)

        # Configure retries with backoff
        retry_strategy = Retry(
            total=3,  # number of retries
            backoff_factor=1,  # wait 1, 2, 4 seconds between retries
            status_forcelist=[408, 429, 500, 502, 503, 504],  # retry on these status codes
            allowed_methods=["HEAD", "GET", "PUT", "DELETE", "OPTIONS", "TRACE", "POST"],
            raise_on_status=True,
        )

        # Configure the adapter with the retry strategy
        adapter = HTTPAdapter(
            max_retries=retry_strategy, pool_connections=10, pool_maxsize=10, pool_block=False
        )

        # Mount the adapter for both HTTP and HTTPS
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)

        # Set default headers
        self.session.headers.update({"Connection": "keep-alive", "Keep-Alive": "timeout=300"})

    def _build_header(self, collaborator_name: str) -> dict:
        """
        Build and return a header dictionary.
        """
        return {
            "sender": collaborator_name,
            "receiver": self.aggregator_uuid,
            "federation_uuid": self.federation_uuid,
            "single_col_cert_common_name": self.single_col_cert_common_name,
        }

    def get_tasks(self, collaborator_name: str) -> Tuple[List[Any], int, int, bool]:
        payload = {"header": self._build_header(collaborator_name)}
        url = f"{self.base_url}/GetTasks"
        headers = {"Accept": "application/json", "Sender": collaborator_name}
        response = self._make_request("POST", url, data=json.dumps(payload), headers=headers)
        data = response.json()
        resp = aggregator_pb2.GetTasksResponse()
        json_format.ParseDict(data, resp, ignore_unknown_fields=True)
        return resp.tasks, resp.round_number, resp.sleep_time, resp.quit

    def get_aggregated_tensor(
        self,
        collaborator_name: str,
        tensor_name: str,
        round_number: int,
        report: bool,
        tags: List[str],
        require_lossless: bool,
    ) -> Any:
        payload = {
            "header": self._build_header(collaborator_name),
            "tensor_name": tensor_name,
            "round_number": round_number,
            "report": report,
            "tags": tags,
            "require_lossless": require_lossless,
        }
        headers = {"Accept": "application/json", "Sender": collaborator_name}
        url = f"{self.base_url}/GetAggregatedTensor"
        response = self._make_request("POST", url, data=json.dumps(payload), headers=headers)
        data = response.json()
        resp = aggregator_pb2.GetAggregatedTensorResponse()
        json_format.ParseDict(data, resp, ignore_unknown_fields=True)
        return resp.tensor

    def send_local_task_results(
        self,
        collaborator_name: str,
        round_number: int,
        task_name: str,
        data_size: int,
        named_tensors: List[Any],
    ) -> bool:
        """Send local task results to the aggregator.

        Returns:
            bool: True if the request was successful (status code 2xx)
        """
        logger.info(f"Starting to send task results for round {round_number}, task {task_name}")
        logger.info(f"Data size: {data_size}, Number of tensors: {len(named_tensors)}")

        # Create the TaskResults message
        task_results = aggregator_pb2.TaskResults(
            header=self._build_header(collaborator_name),
            round_number=round_number,
            task_name=task_name,
            data_size=data_size,
            tensors=named_tensors,
        )

        # Serialize the TaskResults first
        task_results_bytes = task_results.SerializeToString()
        logger.info(f"TaskResults serialized size: {len(task_results_bytes)} bytes")

        # Create a DataStream message containing the TaskResults bytes
        data_stream = base_pb2.DataStream(size=len(task_results_bytes), npbytes=task_results_bytes)

        # Create an empty DataStream to signal end of stream
        end_stream = base_pb2.DataStream(size=0, npbytes=b"")

        # Serialize both messages
        data_bytes = data_stream.SerializeToString()
        end_bytes = end_stream.SerializeToString()

        # Create length-prefixed stream format
        stream_data = (
            struct.pack(">I", len(data_bytes))  # Length prefix for first message
            + data_bytes  # First message
            + struct.pack(">I", len(end_bytes))  # Length prefix for second message
            + end_bytes  # Second message (empty message signals end)
        )

        url = f"{self.base_url}/SendLocalTaskResults"
        headers = {
            "Content-Type": "application/x-protobuf-stream",
            "Content-Length": str(len(stream_data)),
            "Sender": collaborator_name,
        }
        logger.info(f"Sending request to {url}")
        logger.info(f"Request headers: {headers}")
        logger.info(f"Sending {len(stream_data)} bytes of length-prefixed protobuf stream")

        try:
            response = self._make_request(
                "POST",
                url,
                data=stream_data,
                headers=headers,
                timeout=(30, 60),  # Keep shorter timeout since we're sending all data at once
            )
            response.raise_for_status()
            logger.info("Successfully sent task results")
            return True
        except Exception as e:
            logger.error(f"Failed to send local task results: {str(e)}")
            logger.error(f"Error type: {type(e).__name__}")
            logger.error(f"Request headers were: {headers}")
            raise

    def get_metric_stream(self, experiment_name: str) -> Iterator[Any]:
        payload = {"experiment_name": experiment_name}
        headers = {"Sender": "metric_stream_client"}  # Generic sender for metrics
        url = f"{self.base_url}/GetMetricStream"
        response = self._make_request(
            "POST", url, data=json.dumps(payload), headers=headers, stream=True
        )

        def stream_generator():
            for line in response.iter_lines():
                if line:
                    metric_resp = aggregator_pb2.GetMetricStreamResponse()
                    json_format.ParseDict(
                        json.loads(line.decode("utf-8")), metric_resp, ignore_unknown_fields=True
                    )
                    yield metric_resp

        return stream_generator()

    def get_trained_model(self, experiment_name: str, model_type: int) -> Any:
        payload = {"experiment_name": experiment_name, "model_type": model_type}
        headers = {
            "Accept": "application/json",
            "Sender": "model_client",  # Generic sender for model requests
        }
        url = f"{self.base_url}/GetTrainedModel"
        response = self._make_request("POST", url, data=json.dumps(payload), headers=headers)
        data = response.json()
        resp = aggregator_pb2.TrainedModelResponse()
        json_format.ParseDict(data, resp, ignore_unknown_fields=True)
        return resp

    def get_experiment_description(self, name: str) -> Any:
        payload = {"name": name}
        headers = {
            "Accept": "application/json",
            "Sender": "experiment_client",  # Generic sender for experiment requests
        }
        url = f"{self.base_url}/GetExperimentDescription"
        response = self._make_request("POST", url, data=json.dumps(payload), headers=headers)
        data = response.json()
        resp = aggregator_pb2.GetExperimentDescriptionResponse()
        json_format.ParseDict(data, resp, ignore_unknown_fields=True)
        return resp

    def _make_request(self, method, url, data=None, headers=None, stream=False, timeout=None):
        """
        Make a request with retry logic and proper error handling

        Args:
            method (str): HTTP method (GET, POST, etc.)
            url (str): The URL to make the request to
            data (Union[str, bytes, None]): The request body
            headers (dict, optional): Request headers
            stream (bool, optional): Whether to stream the response
            timeout (Union[float, tuple], optional): Client side timeout.

        Returns:
            requests.Response: The response from the server

        Raises:
            requests.exceptions.RequestException: If the request fails
        """
        start_time = time.time()
        try:
            logger.debug(f"Making {method} request to {url}")

            # Set base headers
            request_headers = {"Connection": "keep-alive", "Keep-Alive": "timeout=300"}
            if headers:
                request_headers.update(headers)

            response = self.session.request(
                method=method,
                url=url,
                data=data,
                headers=request_headers,
                verify=self.verify,
                cert=self.cert,
                timeout=timeout or self.timeout,
                stream=stream,
            )
            response.raise_for_status()
            logger.debug(f"Request completed in {time.time() - start_time:.2f} seconds")
            return response
        except requests.exceptions.Timeout:
            logger.error(f"Request timed out after {time.time() - start_time:.2f} seconds")
            raise
        except requests.exceptions.ConnectionError as e:
            logger.error(f"Connection error: {e}")
            raise
        except requests.exceptions.RequestException as e:
            logger.error(f"Request failed: {e}")
            raise

    def __del__(self):
        """
        Cleanup when the client is destroyed
        """
        self.session.close()
