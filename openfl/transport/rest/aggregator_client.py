# Copyright 2025 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""AggregatorRESTClient module."""

# Standard library imports
import logging
import random
import ssl
import struct
import time
from typing import Any, List, Tuple

# Third-party libraries
import requests
from google.protobuf import json_format
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Internal modules
from openfl.protocols import aggregator_pb2, base_pb2
from openfl.protocols.aggregator_client_interface import AggregatorClientInterface

logger = logging.getLogger(__name__)


class SecurityError(Exception):
    """Security-related error."""

    pass


class AggregatorRESTClient(AggregatorClientInterface):
    def __init__(
        self,
        agg_addr,
        agg_port,
        aggregator_uuid: str,
        federation_uuid: str,
        collaborator_name: str,
        use_tls=True,
        require_client_auth=True,
        root_certificate=None,
        certificate=None,
        private_key=None,
        single_col_cert_common_name=None,
        refetch_server_cert_callback=None,
        **kwargs,
    ):
        """
        Initialize the AggregatorRESTClient with proper security settings.

        Args:
            agg_addr: Aggregator address
            agg_port: Aggregator port
            aggregator_uuid: UUID of the aggregator
            federation_uuid: UUID of the federation
            collaborator_name: Name of the collaborator
            use_tls: Whether to use TLS
            require_client_auth: Whether to require client authentication
            root_certificate: Path to root certificate
            certificate: Path to client certificate
            private_key: Path to client private key
            single_col_cert_common_name: Common name for single collaborator certificate
            refetch_server_cert_callback: Callback to refetch server certificate
        """
        self.use_tls = use_tls
        self.require_client_auth = require_client_auth
        self.root_certificate = root_certificate
        self.certificate = certificate
        self.private_key = private_key
        self.aggregator_uuid = aggregator_uuid
        self.federation_uuid = federation_uuid
        self.collaborator_name = collaborator_name
        self.single_col_cert_common_name = single_col_cert_common_name
        self.refetch_server_cert_callback = refetch_server_cert_callback

        # New: Store server connection details for reconnection
        self.agg_addr = agg_addr
        self.agg_port = agg_port

        # Determine scheme and TLS verification
        scheme = "https" if self.use_tls else "http"

        # Configure certificate verification
        self.cert_verification = self._configure_cert_verification(
            self.use_tls, self.root_certificate
        )

        # Configure client certificates if required
        if self.use_tls and self.require_client_auth:
            if not self.certificate or not self.private_key:
                raise ValueError(
                    "Both certificate and private key are required for mTLS "
                    "(client authentication). "
                    "Please provide both certificate and private key paths."
                )
            self.cert = (self.certificate, self.private_key)
        else:
            self.cert = None

        # Configure session with proper settings
        self.session = self._create_session()

        # Build the base URL
        self.base_url = f"{scheme}://{agg_addr}:{agg_port}/experimental/v1"

        # Log warning about experimental API
        logger.warning(
            "Initializing Aggregator REST Client (EXPERIMENTAL API - Not for production use)"
        )

        # Verify certificates if TLS is enabled
        if self.use_tls:
            try:
                self._verify_certificates()
            except Exception as e:
                logger.error(f"Certificate verification failed: {e}")
                raise

        # New: Connection state tracking
        self._last_successful_connection = 0
        self._connection_failures = 0
        self._server_restarting = False

    @classmethod
    def _configure_cert_verification(
        cls, use_tls: bool, root_certificate: str = None
    ) -> bool | str:
        """
        Configure certificate verification settings for requests.

        Args:
            use_tls: Whether TLS is enabled
            root_certificate: Optional path to root certificate file

        Returns:
            Union[bool, str]: Either True for system CA bundle, False for no verification,
                             or path to root certificate file
        """
        if not use_tls:
            return False

        if root_certificate:
            return root_certificate

        return True  # Use system's default CA bundle

    def _verify_certificates(self):
        """Verify SSL certificates and configuration."""
        import socket
        import ssl

        # Try to establish a test connection
        try:
            hostname = self.base_url.split("://")[1].split(":")[0]
            port = int(self.base_url.split(":")[2].split("/")[0])

            # Create SSL context with specific options
            context = ssl.create_default_context()
            context.verify_mode = ssl.CERT_REQUIRED
            context.check_hostname = True

            # Set secure cipher suites
            context.set_ciphers("ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-GCM-SHA384")

            # Disable older TLS versions
            context.options |= (
                ssl.OP_NO_TLSv1
                | ssl.OP_NO_TLSv1_1
                | ssl.OP_NO_TLSv1_2
                | ssl.OP_NO_COMPRESSION
                | ssl.OP_NO_TICKET
            )

            if self.root_certificate:
                context.load_verify_locations(cafile=self.root_certificate)

            if self.certificate and self.private_key:
                context.load_cert_chain(certfile=self.certificate, keyfile=self.private_key)

            # Use context managers for proper resource cleanup
            with socket.create_connection((hostname, port)) as sock:
                with context.wrap_socket(sock, server_hostname=hostname) as _:
                    pass  # Connection successful if we get here

        except ssl.SSLError as e:
            if "CERTIFICATE_UNKNOWN" in str(e):
                logger.error(
                    "Certificate unknown error - this usually means the "
                    "server's certificate is not trusted"
                )
                logger.error("Please verify that:")
                logger.error(
                    "1. The root certificate contains all necessary intermediate certificates"
                )
                logger.error("2. The server's certificate is properly signed by a trusted CA")
                logger.error("3. The hostname matches the certificate's subject")
            raise
        except Exception as e:
            logger.error(f"Connection verification failed: {e}")
            raise

    def _build_header(self) -> dict:
        """Build and return a header dictionary with security headers."""
        headers = {
            "Receiver": self.aggregator_uuid,
            "Federation-UUID": self.federation_uuid,
            "Single-Col-Cert-CN": self.single_col_cert_common_name or "",
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
            "X-XSS-Protection": "1; mode=block",
            "Sender": self.collaborator_name,
        }
        if self.use_tls:
            headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return headers

    def _make_request(
        self, method, url, data=None, params=None, headers=None, stream=False, timeout=None
    ):
        """Make a request with proper security settings."""
        start_time = time.time()
        try:
            self._validate_url_scheme(url)
            request_headers = self._prepare_headers(headers)
            response = self._execute_request(
                method, url, request_headers, data, params, stream, timeout
            )
            self._validate_response(response)
            logger.debug(f"Request completed in {time.time() - start_time:.2f} seconds")
            return response

        except (
            requests.exceptions.ConnectionError,
            requests.exceptions.Timeout,
            requests.exceptions.RequestException,
        ):
            # Just raise the exception - _execute_request already handles retries
            raise

    def _validate_url_scheme(self, url):
        """Validate URL scheme matches TLS setting."""
        if self.use_tls and not url.startswith("https://"):
            raise ValueError("TLS required but URL is not HTTPS")
        elif not url.startswith("http://") and not url.startswith("https://"):
            raise ValueError("URL must use either HTTP or HTTPS scheme")

    def _prepare_headers(self, headers):
        """Prepare request headers with security settings."""
        request_headers = self._build_header()
        if headers:
            request_headers.update(headers)
        return request_headers

    def _create_session(self):
        """Create and configure a requests session with improved retry and connection settings."""
        session = requests.Session()

        # Set default headers
        session.headers.update(
            {
                "Connection": "keep-alive",
                "Keep-Alive": "timeout=300",
                "Accept": "application/json",
                "X-Content-Type-Options": "nosniff",
                "X-Frame-Options": "DENY",
                "X-XSS-Protection": "1; mode=block",
            }
        )

        # Configure timeouts with longer duration for large payloads
        self.timeout = (30, 300)  # (connect timeout, read timeout) in seconds

        # Enhanced retry strategy with better backoff
        retry_strategy = Retry(
            total=5,  # Increased from 3 to 5 retries
            backoff_factor=2,  # Increased from 1 to 2 for more aggressive backoff
            status_forcelist=[408, 429, 500, 502, 503, 504],
            allowed_methods=["GET", "POST"],
            raise_on_status=True,
        )

        # Configure the adapter with the retry strategy and better pooling
        adapter = HTTPAdapter(
            max_retries=retry_strategy,
            pool_connections=20,  # Increased from 10
            pool_maxsize=20,  # Increased from 10
            pool_block=False,
        )

        # Mount the adapter for both HTTP and HTTPS
        session.mount("http://", adapter)
        session.mount("https://", adapter)

        return session

    def _execute_request(self, method, url, headers, data, params, stream, timeout):
        """Execute the HTTP request with enhanced retry logic and server restart detection."""
        # Enhanced retry mechanism for server restarts
        max_high_level_retries = (
            12  # Allow up to 12 retries at high level (about 1 minute with backoff)
        )
        base_sleep_time = 0.5  # Start with half a second

        for high_level_attempt in range(max_high_level_retries):
            try:
                response = self._try_request(method, url, headers, data, params, stream, timeout)

                # Successfully connected - reset failure tracking
                self._last_successful_connection = time.time()
                self._connection_failures = 0
                self._server_restarting = False

                return response

            except requests.exceptions.SSLError as e:
                if not self._handle_ssl_error(
                    e, high_level_attempt, max_high_level_retries, base_sleep_time
                ):
                    raise

            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
                if not self._handle_connection_error(
                    e, high_level_attempt, max_high_level_retries, base_sleep_time
                ):
                    raise

            except requests.exceptions.RequestException as e:
                if not self._handle_request_exception(e, high_level_attempt, base_sleep_time):
                    raise

    def _try_request(self, method, url, headers, data, params, stream, timeout):
        """Attempt to make a request with the current session."""
        session = self.session
        if self.use_tls:
            # Extract hostname from URL for verification
            hostname = url.split("://")[1].split(":")[0].split("/")[0]

            # Create a custom SSL context for this request
            context = ssl.create_default_context(
                cafile=self.root_certificate if self.root_certificate else None
            )
            context.verify_mode = ssl.CERT_REQUIRED

            # Configure session with SSL context and hostname verification
            session.verify = self.cert_verification
            session.cert = self.cert

            # Build the complete headers with security information
            base_headers = self._build_header()
            if headers:
                # Merge user-provided headers with base headers
                base_headers.update(headers)
            headers = base_headers
            headers["Host"] = hostname

            # Add certificate info to request kwargs
            request_kwargs = {
                "method": method,
                "url": url,
                "headers": headers,
                "data": data,
                "params": params,
                "stream": stream,
                "verify": self.cert_verification,
                "timeout": timeout or self.timeout,
            }

            # Add client certificate if mTLS is enabled
            if self.require_client_auth:
                if not self.certificate or not self.private_key:
                    raise ValueError(
                        "Both certificate and private key are required for mTLS "
                        "(client authentication). "
                        "Please provide both certificate and private key paths."
                    )
                # Use proper cert format
                request_kwargs["cert"] = (self.certificate, self.private_key)

            return session.request(**request_kwargs)
        else:
            # For non-TLS requests, still use the security headers
            base_headers = self._build_header()
            if headers:
                base_headers.update(headers)

            return session.request(
                method=method,
                url=url,
                headers=base_headers,
                data=data,
                params=params,
                stream=stream,
                timeout=timeout or self.timeout,
            )

    def _handle_ssl_error(self, e, attempt, max_retries, base_sleep_time):
        """Handle SSL errors with specialized retry logic.

        Returns:
            bool: True if retry should continue, False if error should be raised
        """
        # Handle SSL errors with specialized retry logic
        self._connection_failures += 1

        if "CERTIFICATE_UNKNOWN" in str(e):
            logger.error(
                "Certificate unknown error - this usually means the "
                "server's certificate is not trusted"
            )
            logger.error("Please verify that:")
            logger.error("1. The root certificate contains all necessary intermediate certificates")
            logger.error("2. The server's certificate is properly signed by a trusted CA")
            logger.error("3. The hostname matches the certificate's subject")

            # If we have a certificate callback, try to refetch
            if attempt < max_retries - 1 and self.refetch_server_cert_callback:
                logger.debug("Attempting to refetch server certificate")
                self.root_certificate = self.refetch_server_cert_callback()
                # Update the cert_verification with the new root certificate
                self.cert_verification = self._configure_cert_verification(
                    self.use_tls, self.root_certificate
                )
                # Re-verify certificates
                try:
                    self._verify_certificates()
                    logger.info("Successfully refetched and verified server certificate")
                    # Reset the session to use the new certificate
                    self.session = self._create_session()
                    # Wait before retrying
                    time.sleep(base_sleep_time * (2**attempt))
                    return True
                except Exception as verify_error:
                    logger.error(f"Certificate re-verification failed: {verify_error}")

            if attempt < max_retries - 1:
                sleep_time = min(60, base_sleep_time * (2**attempt))
                jitter = random.uniform(0.75, 1.25)
                sleep_time = sleep_time * jitter
                logger.warning(
                    f"Certificate error (attempt {attempt + 1}/{max_retries}). "
                    f"Retrying in {sleep_time:.2f} seconds: {str(e)}"
                )
                time.sleep(sleep_time)
                return True
            else:
                logger.error(f"SSL certificate validation failed after {max_retries} attempts")
                return False
        else:
            # Other SSL errors
            if attempt < max_retries - 1:
                sleep_time = min(60, base_sleep_time * (2**attempt))
                jitter = random.uniform(0.75, 1.25)
                sleep_time = sleep_time * jitter
                logger.warning(
                    f"SSL error (attempt {attempt + 1}/{max_retries}). "
                    f"Retrying in {sleep_time:.2f} seconds: {str(e)}"
                )
                time.sleep(sleep_time)
                return True
            else:
                logger.error(f"SSL error after {max_retries} attempts: {str(e)}")
                return False

    def _handle_connection_error(self, e, attempt, max_retries, base_sleep_time):
        """Handle connection errors with exponential backoff.

        Returns:
            bool: True if retry should continue, False if error should be raised
        """
        # Connection or timeout errors may indicate server restart
        self._connection_failures += 1
        current_time = time.time()

        # If we've had multiple failures and had a successful connection before,
        # we might be experiencing a server restart
        if (
            self._connection_failures >= 2
            and self._last_successful_connection > 0
            and current_time - self._last_successful_connection < 300
        ):  # Within last 5 minutes
            self._server_restarting = True
            logger.warning(f"Detected possible server restart. Attempt {attempt + 1}/{max_retries}")

        # Calculate backoff time - exponential with jitter
        sleep_time = min(60, base_sleep_time * (2**attempt))  # Cap at 60 seconds
        jitter = random.uniform(0.75, 1.25)  # Add 25% jitter
        sleep_time = sleep_time * jitter

        if attempt < max_retries - 1:
            logger.warning(
                f"Connection failed (attempt {attempt + 1}/{max_retries}). "
                f"Retrying in {sleep_time:.2f} seconds: {str(e)}"
            )
            time.sleep(sleep_time)

            # If we think the server is restarting, try to re-create the session
            if self._server_restarting and attempt % 2 == 1:
                logger.info("Recreating session due to potential server restart")
                self.session = self._create_session()
            return True
        else:
            logger.error(f"Connection failed after {max_retries} attempts: {str(e)}")
            return False

    def _handle_request_exception(self, e, attempt, base_sleep_time):
        """Handle other request exceptions.

        Returns:
            bool: True if retry should continue, False if error should be raised
        """
        # For other request exceptions, raise after a few retries
        if attempt < 2:  # Only retry non-connection errors a couple times
            logger.warning(f"Request error (attempt {attempt + 1}/3): {str(e)}")
            time.sleep(base_sleep_time * (2**attempt))
            return True
        else:
            logger.error(f"Request failed after 3 attempts: {str(e)}")
            return False

    def _validate_response(self, response):
        """Validate response headers and security settings."""
        security_headers = {
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
            "X-XSS-Protection": "1; mode=block",
        }
        for header, expected_value in security_headers.items():
            if header in response.headers and response.headers[header] != expected_value:
                logger.warning(f"Missing or incorrect security header: {header}")

        response.raise_for_status()

    def get_tasks(self) -> Tuple[List[Any], int, int, bool]:
        """Get tasks from the aggregator with proper security settings."""
        # Check connection health before getting tasks
        self.check_connection_health()

        headers = {"Accept": "application/json", "Sender": self.collaborator_name}
        params = {
            "collaborator_id": self.collaborator_name,
            "federation_uuid": self.federation_uuid,
        }
        url = f"{self.base_url}/tasks"
        response = self._make_request("GET", url, headers=headers, params=params)
        response.raise_for_status()
        data = response.json()
        tasks_resp = aggregator_pb2.GetTasksResponse()
        json_format.ParseDict(data, tasks_resp)

        logger.debug(
            f"Received tasks response - Round: {tasks_resp.round_number}, "
            f"Tasks: {[t.name for t in tasks_resp.tasks]}, "
            f"Sleep: {tasks_resp.sleep_time}, Quit: {tasks_resp.quit}"
        )
        return tasks_resp.tasks, tasks_resp.round_number, tasks_resp.sleep_time, tasks_resp.quit

    def get_aggregated_tensor(
        self,
        tensor_name: str,
        round_number: int,
        report: bool,
        tags: List[str],
        require_lossless: bool,
    ) -> Any:
        """Get aggregated tensor with proper security settings."""
        # Check connection health before getting tensor
        self.check_connection_health()

        params = {
            "sender": self.collaborator_name,
            "receiver": self.aggregator_uuid,
            "federation_uuid": self.federation_uuid,
            "tensor_name": tensor_name,
            "round_number": round_number,
            "report": report,
            "tags": tags,
            "require_lossless": require_lossless,
            "collaborator_id": self.collaborator_name,
        }
        headers = {"Accept": "application/json", "Sender": self.collaborator_name}
        url = f"{self.base_url}/tensors/aggregated"
        extended_timeout = (30, 600)  # 30 seconds connect, 10 minutes read timeout
        try:
            logger.debug(f"Requesting aggregated tensor {tensor_name} for round {round_number}")
            response = self._make_request(
                "GET", url, params=params, headers=headers, timeout=extended_timeout
            )
            data = response.json()
            resp = aggregator_pb2.GetAggregatedTensorResponse()
            json_format.ParseDict(data, resp, ignore_unknown_fields=True)
            logger.debug(f"Successfully retrieved tensor {tensor_name} for round {round_number}")
            return resp.tensor
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 404:
                # This is expected during round 0 or when tensor hasn't been aggregated yet
                logger.debug(
                    f"No aggregated tensor found for {tensor_name} at round {round_number}"
                )
                return None
            raise

    def send_local_task_results(
        self,
        round_number: int,
        task_name: str,
        data_size: int,
        named_tensors: List[Any],
    ) -> bool:
        """Send local task results with proper security settings and enhanced resiliency."""
        # Check connection health before sending tasks
        self.check_connection_health()

        logger.debug(f"Sending task results for round {round_number}, task {task_name}")

        # Prepare the request data
        stream_data, request_headers = self._prepare_task_results_data(
            round_number, task_name, data_size, named_tensors
        )

        url = f"{self.base_url}/tasks/results"

        # Enhanced retry mechanism for task results specifically
        max_task_retries = 40  # More retries for large and important task result submission
        initial_sleep = 1.0  # Start with a 1-second delay
        max_sleep = 180.0  # Maximum delay of 3 minutes between retries
        server_restart_time = 15.0  # Expected time for server to restart

        # Reset the server restarting flag if needed - we'll detect it again if necessary
        if self._server_restarting:
            logger.info("Resetting server restart detection state before sending task results")
            self._server_restarting = False

        return self._send_task_results_with_retry(
            url,
            stream_data,
            request_headers,
            max_task_retries,
            initial_sleep,
            max_sleep,
            server_restart_time,
            round_number,
        )

    def _prepare_task_results_data(
        self, round_number: int, task_name: str, data_size: int, named_tensors: List[Any]
    ):
        """Prepare serialized task results data and headers for sending."""
        # Create the TaskResults message
        task_results = aggregator_pb2.TaskResults(
            header=aggregator_pb2.MessageHeader(
                sender=self.collaborator_name,
                receiver=self.aggregator_uuid,
                federation_uuid=self.federation_uuid,
                single_col_cert_common_name=self.single_col_cert_common_name or "",
            ),
            round_number=round_number,
            task_name=task_name,
            data_size=data_size,
            tensors=named_tensors,
        )

        # Serialize the TaskResults first
        task_results_bytes = task_results.SerializeToString()
        logger.debug(f"TaskResults serialized size: {len(task_results_bytes)} bytes")

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

        # Prepare headers
        request_headers = self._build_header()
        request_headers["Sender"] = self.collaborator_name
        request_headers["Content-Type"] = "application/x-protobuf-stream"
        request_headers["Content-Length"] = str(len(stream_data))

        return stream_data, request_headers

    def _send_task_results_with_retry(
        self,
        url,
        stream_data,
        request_headers,
        max_retries,
        initial_sleep,
        max_sleep,
        server_restart_time,
        round_number,
    ):
        """Send task results with retry logic for handling server restarts."""
        consecutive_failures = 0
        for attempt in range(max_retries):
            try:
                # Keep track of attempt start time to measure actual delay
                start_time = time.time()

                # Use our enhanced _execute_request method which has better retry logic
                response = self._make_request(
                    "POST",
                    url,
                    data=stream_data,
                    headers=request_headers,
                    timeout=(30, 600),  # 30s connect, 10min read (increased for large models)
                )
                response.raise_for_status()

                # Successfully sent task results
                logger.debug(f"Successfully sent task results for round {round_number}")
                self._last_successful_connection = time.time()
                self._connection_failures = 0
                self._server_restarting = False
                consecutive_failures = 0
                return True

            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
                # Handle connection errors with specialized backoff strategy
                if not self._handle_connection_failure_during_task_submission(
                    e,
                    attempt,
                    consecutive_failures,
                    max_retries,
                    initial_sleep,
                    max_sleep,
                    server_restart_time,
                    start_time,
                ):
                    logger.error(f"Failed to send task results: {str(e)}")
                    logger.error(f"Error type: {type(e).__name__}")
                    logger.error(f"Request headers were: {request_headers}")
                    raise
                consecutive_failures += 1

            except Exception as e:
                # For other errors, we'll retry fewer times
                if attempt < 5:  # Only retry non-connection errors a few times
                    sleep_time = initial_sleep * (2**attempt)
                    logger.warning(
                        f"Error sending task results (attempt {attempt + 1}/5): {str(e)}"
                    )
                    time.sleep(sleep_time)
                else:
                    logger.error(f"Failed to send task results: {str(e)}")
                    logger.error(f"Error type: {type(e).__name__}")
                    logger.error(f"Request headers were: {request_headers}")
                    raise

        return False

    def _handle_connection_failure_during_task_submission(
        self,
        e,
        attempt,
        consecutive_failures,
        max_retries,
        initial_sleep,
        max_sleep,
        server_restart_time,
        start_time,
    ):
        """Handle connection failures during task submission with smart backoff.

        Returns:
            bool: True to continue retrying, False to raise the error
        """
        # Detect server restart by pattern of connection failures
        if consecutive_failures >= 2:
            # We've had multiple consecutive failures, likely a server restart
            self._server_restarting = True
            logger.warning(
                f"Detected potential server restart during task submission "
                f"(consecutive failures: {consecutive_failures})"
            )

        # Calculate backoff with different strategies for normal vs. restart cases
        sleep_time = self._calculate_backoff_time(
            attempt, consecutive_failures, initial_sleep, max_sleep, server_restart_time
        )

        if attempt < max_retries - 1:
            # Log with appropriate message based on restart detection
            self._log_retry_attempt(attempt, max_retries, sleep_time, str(e))

            # If server is restarting, recreate the session before retry more aggressively
            if self._should_recreate_session(consecutive_failures):
                logger.info("Recreating session for task result submission due to server restart")
                self.session = self._create_session()

            # Implement the backoff
            time.sleep(sleep_time)

            # Check if server is back after enough time has passed
            if self._should_check_server_status(start_time, server_restart_time):
                self._try_ping_server()

            return True
        else:
            logger.error(f"Failed to send task results after {max_retries} attempts: {str(e)}")
            return False

    def _calculate_backoff_time(
        self, attempt, consecutive_failures, initial_sleep, max_sleep, server_restart_time
    ):
        """Calculate appropriate backoff time based on restart detection."""
        # Calculate different backoff strategies based on server restart status
        if self._server_restarting:
            # If server is restarting, use a longer base delay to allow time for restart
            backoff_base = server_restart_time
            # For first few attempts during restart, wait longer
            if consecutive_failures <= 3:
                backoff_multiplier = 1.0  # Wait at least server_restart_time on first failures
            else:
                # More gradual backoff after initial restart period
                backoff_multiplier = 1.0 + (0.5 * (consecutive_failures - 3))
        else:
            # Normal exponential backoff for intermittent failures
            backoff_base = initial_sleep
            backoff_multiplier = 2 ** min(attempt, 10)  # Cap the exponent to avoid huge values

        # Calculate sleep time with the backoff strategy
        sleep_time = min(max_sleep, backoff_base * backoff_multiplier)

        # Add jitter to prevent synchronized retries
        jitter = random.uniform(0.8, 1.2)  # Add 20% jitter
        sleep_time = sleep_time * jitter

        return sleep_time

    def _log_retry_attempt(self, attempt, max_retries, sleep_time, error_msg):
        """Log retry attempt with appropriate message based on restart detection."""
        if self._server_restarting:
            logger.warning(
                f"Server restart detected. Connection failed "
                f"(attempt {attempt + 1}/{max_retries}). "
                f"Waiting {sleep_time:.2f} seconds for server to come back online: "
                f"{error_msg}"
            )
        else:
            logger.warning(
                f"Connection failed (attempt {attempt + 1}/{max_retries}). "
                f"Retrying in {sleep_time:.2f} seconds: {error_msg}"
            )

    def _should_recreate_session(self, consecutive_failures):
        """Determine if session should be recreated based on failure pattern."""
        return self._server_restarting and (
            consecutive_failures % 2 == 1 or consecutive_failures < 3
        )

    def _should_check_server_status(self, start_time, server_restart_time):
        """Determine if we should check server status based on elapsed time."""
        retry_duration = time.time() - start_time
        return retry_duration > server_restart_time and self._server_restarting

    def _try_ping_server(self):
        """Try to ping server to check if it's back online."""
        try:
            logger.info("Attempting to ping server to check if it's back online")
            self.ping()
            logger.info("Server is back online!")
            # Reset flags as server is back
            self._server_restarting = False
            return True
        except Exception as ping_error:
            logger.warning(f"Server ping failed, will continue retrying: {str(ping_error)}")
            return False

    def ping(self):
        """Ping the aggregator to check connectivity."""
        logger.info("Aggregator ping...")
        headers = {"Accept": "application/json", "Sender": self.collaborator_name}
        params = {
            "collaborator_id": self.collaborator_name,
            "federation_uuid": self.federation_uuid,
        }
        url = f"{self.base_url}/ping"
        response = self._make_request("GET", url, headers=headers, params=params)
        response.raise_for_status()
        data = response.json()

        # Validate response header like GRPC client
        header = data.get("header", {})
        assert header.get("receiver") == self.collaborator_name, (
            f"Receiver in response header does not match collaborator name. "
            f"Expected: {self.collaborator_name}, Actual: {header.get('receiver')}"
        )
        assert header.get("sender") == self.aggregator_uuid, (
            f"Sender in response header does not match aggregator UUID. "
            f"Expected: {self.aggregator_uuid}, Actual: {header.get('sender')}"
        )
        assert header.get("federationUuid") == self.federation_uuid, (
            f"Federation UUID in response header does not match. "
            f"Expected: {self.federation_uuid}, Actual: {header.get('federationUuid')}"
        )
        assert header.get("singleColCertCommonName", "") == (
            self.single_col_cert_common_name or ""
        ), (
            f"Single collaborator certificate common name in response header does not match. "
            f"Expected: {self.single_col_cert_common_name}, "
            f"Actual: {header.get('singleColCertCommonName')}"
        )

        logger.info("Aggregator pong!")

        # Reset connection status after successful ping
        self._last_successful_connection = time.time()
        self._connection_failures = 0
        self._server_restarting = False

        return True

    def check_connection_health(self):
        """
        Check the health of the connection to the aggregator.

        If no successful connection in a while or we've detected server restarts,
        attempt to refresh the connection by recreating the session and pinging.

        Returns:
            bool: True if connection is healthy, False otherwise
        """
        current_time = time.time()
        connection_age = current_time - self._last_successful_connection

        # If connection is too old (over 120 seconds) or we suspect server has restarted
        if (
            connection_age > 120 or self._server_restarting
        ) and self._last_successful_connection > 0:
            logger.info(
                f"Connection health check: connection age is {connection_age:.1f}s, "
                f"server_restarting={self._server_restarting}"
            )

            # Recreate session to refresh connection state
            logger.info("Recreating session to refresh connection state")
            self.session = self._create_session()

            # Try to ping
            try:
                self.ping()
                logger.info("Connection refreshed successfully")
                return True
            except Exception as e:
                logger.warning(f"Failed to refresh connection: {str(e)}")
                # We'll increase the failure count but not consider it fatal
                self._connection_failures += 1
                return False

        return True

    def send_message_to_server(self, openfl_message: Any, collaborator_name: str) -> Any:
        """
        Forwards a converted message from the local REST client to the OpenFL server and returns
        the response.

        Args:
            openfl_message: The InteropMessage proto to be sent to the OpenFL server.
            collaborator_name: The name of the collaborator.

        Returns:
            The response from the OpenFL server (InteropMessage proto).
        """
        # Set the header fields
        header = aggregator_pb2.MessageHeader(
            sender=collaborator_name,
            receiver=self.aggregator_uuid,
            federation_uuid=self.federation_uuid,
            single_col_cert_common_name=self.single_col_cert_common_name or "",
        )
        openfl_message.header.CopyFrom(header)

        # Serialize to JSON
        json_payload = json_format.MessageToJson(openfl_message)
        url = f"{self.base_url}/interop/relay"
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Sender": collaborator_name,
        }
        response = self._make_request(
            "POST",
            url,
            data=json_payload,
            headers=headers,
            timeout=(30, 300),
        )
        response.raise_for_status()
        response_json = response.json()
        openfl_response = aggregator_pb2.InteropMessage()
        json_format.ParseDict(response_json, openfl_response, ignore_unknown_fields=True)
        return openfl_response

    def __del__(self):
        """Cleanup when the client is destroyed."""
        self.session.close()
