import unittest
import time
import ssl
import pytest
import mock
import requests
from unittest.mock import MagicMock, patch, call

from openfl.transport.rest.aggregator_client import AggregatorRESTClient
from openfl.protocols import aggregator_pb2, base_pb2


class TestRestResiliency(unittest.TestCase):
    """Test cases for REST client resiliency mechanisms."""

    def setUp(self):
        """Set up test fixtures."""
        self.mock_session = MagicMock()
        self.mock_response = MagicMock()
        self.mock_response.status_code = 200
        self.mock_response.json.return_value = {
            "header": {
                "sender": "test-uuid",
                "receiver": "test-collaborator",
                "federationUuid": "fed-uuid",
                "singleColCertCommonName": ""
            }
        }
        self.mock_session.request.return_value = self.mock_response
        
        # Patch the requests.Session to return our mock
        self.session_patcher = patch('requests.Session', return_value=self.mock_session)
        self.mock_session_class = self.session_patcher.start()
        
        # Create a REST client with mocked SSL verification
        with patch('ssl.create_default_context'), \
             patch('openfl.transport.rest.aggregator_client.AggregatorRESTClient._verify_certificates'):
            self.client = AggregatorRESTClient(
                agg_addr="localhost",
                agg_port=8080,
                aggregator_uuid="test-uuid",
                federation_uuid="fed-uuid",
                collaborator_name="test-collaborator",
                use_tls=True,
                require_client_auth=False,
                root_certificate=None,
                certificate=None,
                private_key=None,
            )
            # Replace the session with our mock
            self.client.session = self.mock_session
    
    def tearDown(self):
        """Tear down test fixtures."""
        self.session_patcher.stop()
    
    def test_connection_failure_retry(self):
        """Test that connection failures trigger retries."""
        # Mock the session.request to raise ConnectionError first and then succeed
        side_effects = [
            requests.exceptions.ConnectionError("Connection refused"),
            requests.exceptions.ConnectionError("Connection refused"),
            self.mock_response
        ]
        self.mock_session.request.side_effect = side_effects
        
        # Call a method that uses _make_request
        with patch('time.sleep'):  # Patch sleep to avoid waiting
            self.client.ping()
        
        # Verify the request was called exactly 3 times
        assert self.mock_session.request.call_count == 3
        
        # Verify connection state was updated
        assert self.client._server_restarting == False
        assert self.client._connection_failures == 0
        assert self.client._last_successful_connection > 0
    
    def test_server_restart_detection(self):
        """Test that server restarts are detected properly."""
        # Set initial connection state to simulate a previous connection
        self.client._last_successful_connection = time.time() - 10
        self.client._connection_failures = 0
        self.client._server_restarting = False
        
        # Directly call _handle_connection_error multiple times to simulate connection failures
        error = requests.exceptions.ConnectionError("Connection refused")
        
        # First call increases connection_failures to 1
        self.client._handle_connection_error(error, 0, 12, 0.5)
        assert self.client._connection_failures == 1
        assert self.client._server_restarting == False
        
        # Second call should trigger server restart detection
        self.client._handle_connection_error(error, 1, 12, 0.5)
        assert self.client._connection_failures == 2
        assert self.client._server_restarting == True
        
        # Verify that the detected state persists
        assert self.client._server_restarting == True
    
    def test_send_task_results_resiliency(self):
        """Test that send_local_task_results is resilient to server restarts."""
        # Create mock task results
        task_results = aggregator_pb2.TaskResults()
        task_results.task_name = "test_task"
        task_results.round_number = 1
        
        # Mock the _make_request method to simulate server restart
        original_make_request = self.client._make_request
        call_count = [0]
        
        def mock_make_request(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] <= 3:
                raise requests.exceptions.ConnectionError("Connection refused")
            elif call_count[0] == 4:
                # On 4th attempt, recreate session to simulate recovery
                return self.mock_response
            else:
                return original_make_request(*args, **kwargs)
        
        # Replace _make_request with our mock
        self.client._make_request = mock_make_request
        
        # Call send_local_task_results with retries
        with patch('time.sleep'), patch('openfl.transport.rest.aggregator_client.AggregatorRESTClient.check_connection_health'):
            result = self.client.send_local_task_results(1, "test_task", 100, [])
        
        # Verify results
        assert result == True
        assert call_count[0] == 4  # Should have made 4 attempts
    
    def test_connection_health_check(self):
        """Test that connection health is checked and refreshed."""
        # Set up initial state to trigger health check
        self.client._last_successful_connection = time.time() - 150  # Connection is old
        self.client._server_restarting = False
        
        # Mock ping to verify it's called
        with patch.object(self.client, 'ping') as mock_ping:
            mock_ping.return_value = True
            
            # Call health check
            result = self.client.check_connection_health()
            
            # Verify ping was called and session was recreated
            assert mock_ping.call_count == 1
            assert result == True
    
    def test_exponential_backoff(self):
        """Test exponential backoff during connection failures."""
        # Mock the session.request to raise ConnectionError
        self.mock_session.request.side_effect = requests.exceptions.ConnectionError("Connection refused")
        
        # Mock time.sleep to capture sleep times
        sleep_times = []
        
        def mock_sleep(duration):
            sleep_times.append(duration)
        
        # Create a patched _execute_request that always fails but records sleep times
        original_execute = self.client._execute_request
        
        def patched_execute(*args, **kwargs):
            # Simulate connection error that triggers backoff
            e = requests.exceptions.ConnectionError("Connection refused")
            # Call _handle_connection_error directly to test backoff
            self.client._handle_connection_error(e, 0, 12, 0.5)  # attempt=0, max_retries=12, base_sleep=0.5
            self.client._handle_connection_error(e, 1, 12, 0.5)  # attempt=1
            self.client._handle_connection_error(e, 2, 12, 0.5)  # attempt=2
            # Raise the exception to simulate failure
            raise e
            
        # Replace the execute method
        self.client._execute_request = patched_execute
        
        # Replace sleep with our mock
        with patch('time.sleep', mock_sleep):
            try:
                # Call ping which will fail and trigger retries
                self.client.ping()
            except requests.exceptions.ConnectionError:
                pass  # Expected to fail
            
            # Verify sleep times follow exponential pattern (accounting for jitter)
            assert len(sleep_times) >= 3  # At least 3 backoff attempts
            for i in range(1, len(sleep_times)):
                # Each retry should generally increase the sleep time
                # (with second delay > first delay, accounting for jitter)
                if i > 0:
                    assert sleep_times[i] > sleep_times[0] * 1.2
            
        # Restore original method
        self.client._execute_request = original_execute 