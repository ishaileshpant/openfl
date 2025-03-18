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
    @staticmethod
    def create_tls_session(
        use_tls=True,
        root_certificate: str = None,
        client_certificate: str = None,
        client_key: str = None,
        cert_chain: str = None,
        insecure: bool = False
    ) -> requests.Session:
        """
        Create and return a requests.Session configured for TLS, optionally with client authentication.
        
        Args:
            use_tls (bool): Whether to use TLS. If False, creates an insecure session.
            root_certificate (str): Path to the CA bundle for server verification.
            client_certificate (str): Path to the client certificate file.
            client_key (str): Path to the client private key file.
            cert_chain (str): Path to the certificate chain file for client auth.
            insecure (bool): If True and use_tls is True but no CA is provided, disables certificate verification.
        
        Returns:
            requests.Session: A configured session for making REST calls.
        """
        session = requests.Session()
        
        if not use_tls:
            logger.warning("Using insecure connection: TLS is disabled.")
            session.verify = False
        else:
            # Set the CA bundle if provided; otherwise, optionally skip verification
            if root_certificate:
                session.verify = root_certificate
                logger.info(f"Using root certificate for TLS: {root_certificate}")
            else:
                if insecure:
                    logger.warning("No root certificate provided, skipping server verification.")
                    session.verify = False
                else:
                    logger.error("Root certificate is required for a secure TLS connection.")
                    raise ValueError("Root certificate must be provided for TLS connection.")
            
            # If both client certificate and key are provided, enable mTLS
            if client_certificate and client_key:
                # If cert_chain is provided, combine client cert with chain
                if cert_chain:
                    try:
                        with open(client_certificate, 'rb') as f:
                            cert_data = f.read()
                        with open(cert_chain, 'rb') as f:
                            chain_data = f.read()
                        # Create a temporary combined cert file
                        import tempfile
                        with tempfile.NamedTemporaryFile(delete=False) as temp_cert:
                            temp_cert.write(cert_data + chain_data)
                            temp_cert_path = temp_cert.name
                        session.cert = (temp_cert_path, client_key)
                        logger.info("Configured mutual TLS with client certificates and chain")
                    except Exception as e:
                        logger.error(f"Failed to combine certificate with chain: {e}")
                        # Fallback to using just the client certificate
                        session.cert = (client_certificate, client_key)
                        logger.warning("Falling back to client certificate without chain")
                else:
                    session.cert = (client_certificate, client_key)
                    logger.info("Configured mutual TLS with client certificates")
            else:
                logger.info("Client certificate and key not provided; proceeding without client authentication.")

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
            max_retries=retry_strategy,
            pool_connections=10,
            pool_maxsize=10,
            pool_block=False
        )

        # Mount the adapter for both HTTP and HTTPS
        session.mount("http://", adapter)
        session.mount("https://", adapter)

        # Set default headers
        session.headers.update({
            "Connection": "keep-alive",
            "Keep-Alive": "timeout=300",
            "Accept": "application/x-protobuf"
        })

        return session

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
        cert_chain=None,
        **kwargs,
    ):
        """
        Initialize the AggregatorRESTClient.
        This client uses a openfl-grpc-gateway service to convert HTTP/JSON
        requests into gRPC calls.
        """
        self.use_tls = use_tls
        self.require_client_auth = require_client_auth
        self.root_certificate = root_certificate
        self.certificate = certificate
        self.private_key = private_key
        self.cert_chain = cert_chain
        self.header = None
        self.aggregator_uuid = aggregator_uuid
        self.federation_uuid = federation_uuid
        self.single_col_cert_common_name = single_col_cert_common_name
        self.refetch_server_cert_callback = refetch_server_cert_callback

        # Log TLS configuration
        logger.info("TLS Configuration:")
        logger.info(f"  use_tls: {self.use_tls}")
        logger.info(f"  require_client_auth: {self.require_client_auth}")
        logger.info(f"  root_certificate: {self.root_certificate}")
        logger.info(f"  certificate: {self.certificate}")
        logger.info(f"  private_key: {self.private_key}")
        logger.info(f"  cert_chain: {self.cert_chain}")

        # Create TLS session with appropriate configuration
        self.session = self.create_tls_session(
            use_tls=self.use_tls,
            root_certificate=self.root_certificate if self.use_tls else None,
            client_certificate=self.certificate if (self.use_tls and self.require_client_auth) else None,
            client_key=self.private_key if (self.use_tls and self.require_client_auth) else None,
            cert_chain=self.cert_chain if (self.use_tls and self.require_client_auth) else None,
            insecure=not self.use_tls
        )

        # Configure timeouts with longer duration for large payloads
        self.timeout = (30, 300)  # (connect timeout, read timeout) in seconds

        # Determine scheme based on TLS configuration
        scheme = "https" if self.use_tls else "http"
        self.verify = self.root_certificate if self.use_tls else False
        self.cert = (self.certificate, self.private_key) if (self.use_tls and self.require_client_auth) else None

        # Build the base URL
        self.base_url = f"{scheme}://{agg_addr}:{agg_rest_port}/openfl.aggregator.Aggregator"
        logger.info(f"Initialized REST Aggregator Client with base URL: {self.base_url}")

        # Only test connection if debug logging is enabled
        if logger.getEffectiveLevel() <= logging.DEBUG:
            logger.debug("Debug mode enabled - testing connection...")
            self._test_connection()
        else:
            logger.info("Debug mode disabled - skipping connection test")

    def _test_connection(self):
        """Test the connection to the server. Only called when debug logging is enabled."""
        try:
            # Try to connect to the server's health endpoint or root path
            test_url = f"{self.base_url}/GetTasks"
            logger.debug(f"Testing connection to {test_url}")
            
            # Use the proper collaborator name for testing
            test_collaborator = self.single_col_cert_common_name or "unknown"
            logger.debug(f"Testing connection with collaborator name: {test_collaborator}")
            
            # Create a test payload with proper collaborator name
            test_payload = {
                "header": {
                    "sender": test_collaborator,
                    "receiver": self.aggregator_uuid,
                    "federation_uuid": self.federation_uuid,
                    "single_col_cert_common_name": self.single_col_cert_common_name
                }
            }
            
            # First try to connect to the gRPC-gateway endpoint
            try:
                logger.debug("Testing gRPC-gateway endpoint...")
                response = self.session.request(
                    method="POST",
                    url=test_url,
                    data=json.dumps(test_payload),
                    headers={
                        "Content-Type": "application/json",
                        "Sender": test_collaborator
                    },
                    verify=self.verify,
                    cert=self.cert,
                    timeout=(5, 10)  # Shorter timeout for connection test
                )
                
                if response.status_code == 200:
                    logger.debug("Connection test successful")
                    return
                elif response.status_code == 404:
                    logger.debug("Endpoint not found. Please check if:")
                    logger.debug("1. The gRPC-gateway service is running")
                    logger.debug("2. The endpoint path is correct")
                    logger.debug("3. The gRPC server is properly configured")
                    raise requests.exceptions.RequestException("Endpoint not found")
                elif response.status_code == 499:
                    logger.debug("gRPC server resolution failed. Please check if:")
                    logger.debug("1. The gRPC server is running")
                    logger.debug("2. The gRPC server address is correct")
                    logger.debug("3. The gRPC server is accessible from the gateway")
                    raise requests.exceptions.RequestException("gRPC server resolution failed")
                else:
                    logger.debug(f"Connection test returned status code: {response.status_code}")
                    logger.debug(f"Response: {response.text}")
                    
            except requests.exceptions.ConnectionError as e:
                logger.debug(f"Connection error: {str(e)}")
                logger.debug("Please check if:")
                logger.debug("1. The server is running")
                logger.debug("2. The server address and port are correct")
                logger.debug("3. There are no firewall rules blocking the connection")
                logger.debug("4. The TLS certificates are properly configured")
                raise
            except requests.exceptions.Timeout:
                logger.debug("Connection test timed out")
                logger.debug("Please check if:")
                logger.debug("1. The server is responsive")
                logger.debug("2. The gRPC server is running and accessible")
                logger.debug("3. The network connection is stable")
                raise
                
        except Exception as e:
            logger.debug(f"Unexpected error during connection test: {str(e)}")
            raise

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
            headers (dict, optional): Additional request headers to merge with session headers
            stream (bool, optional): Whether to stream the response
            timeout (Union[float, tuple], optional): Client side timeout.

        Returns:
            requests.Response: The response from the server

        Raises:
            requests.exceptions.RequestException: If the request fails
        """
        start_time = time.time()
        try:
            logger.info(f"Making {method} request to {url}")
            
            # Merge any additional headers with session headers
            request_headers = {}
            if headers:
                request_headers.update(headers)
            
            # Make the request using the configured session
            response = self.session.request(
                method=method,
                url=url,
                data=data,
                headers=request_headers if request_headers else None,
                stream=stream,
                timeout=timeout or self.timeout
            )
            
            # Handle specific status codes
            if response.status_code == 404:
                logger.error("Endpoint not found. Please check if:")
                logger.error("1. The gRPC-gateway service is running")
                logger.error("2. The endpoint path is correct")
                logger.error("3. The gRPC server is properly configured")
                raise requests.exceptions.RequestException("Endpoint not found")
            elif response.status_code == 499:
                logger.error("gRPC server resolution failed. Please check if:")
                logger.error("1. The gRPC server is running")
                logger.error("2. The gRPC server address is correct")
                logger.error("3. The gRPC server is accessible from the gateway")
                raise requests.exceptions.RequestException("gRPC server resolution failed")
            
            response.raise_for_status()
            logger.info(f"Request completed successfully in {time.time() - start_time:.2f} seconds")
            return response
            
        except requests.exceptions.Timeout:
            logger.error(f"Request timed out after {time.time() - start_time:.2f} seconds")
            logger.error("Please check if the server is responsive and the network connection is stable")
            raise
        except requests.exceptions.ConnectionError as e:
            logger.error(f"Connection error: {str(e)}")
            logger.error(f"Connection error type: {type(e).__name__}")
            logger.error("Please check if:")
            logger.error("1. The server is running")
            logger.error("2. The server address and port are correct")
            logger.error("3. There are no firewall rules blocking the connection")
            logger.error("4. The TLS certificates are properly configured")
            logger.error("5. The gRPC server is properly configured and running")
            raise
        except requests.exceptions.RequestException as e:
            logger.error(f"Request failed: {str(e)}")
            logger.error(f"Request error type: {type(e).__name__}")
            if hasattr(e.response, 'text'):
                logger.error(f"Response content: {e.response.text}")
            raise

    def __del__(self):
        """
        Cleanup when the client is destroyed
        """
        self.session.close()
