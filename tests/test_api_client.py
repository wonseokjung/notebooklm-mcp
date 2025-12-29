"""Tests for api_client parsing functions.

These tests verify the parsing and extraction logic without making network calls.
They use mocked/anonymized response fixtures to lock in current behavior.
"""

import json
from unittest.mock import patch

import pytest

# Import the functions we're testing
from src.notebooklm_consumer_mcp.api_client import (
    parse_timestamp,
    extract_cookies_from_chrome_export,
    ConsumerNotebookLMClient,
)


def create_test_client():
    """Create a client instance for testing without network calls.
    
    Patches _refresh_auth_tokens to avoid network calls during tests.
    """
    with patch.object(ConsumerNotebookLMClient, '_refresh_auth_tokens'):
        return ConsumerNotebookLMClient(
            cookies={"SID": "test", "HSID": "test", "SSID": "test", "APISID": "test", "SAPISID": "test"},
            csrf_token="test_token",
            session_id="test_session",
        )


# =============================================================================
# Test fixtures - anonymized real response shapes
# =============================================================================

# Example batchexecute response for list notebooks
MOCK_BATCHEXECUTE_RESPONSE = """)]}'

42
[["wrb.fr","wXbhsf","[\\"data\\"]",null,null,null,"generic"]]
"""

# More complex multi-chunk response
MOCK_MULTI_CHUNK_RESPONSE = """)]}'

85
[["wrb.fr","wXbhsf","[[\\"Notebook Title\\",[[[\\"src-id-1\\"],\\"Source 1\\"]],\\"nb-uuid-123\\"]]",null,null,null,"generic"]]

45
[["di",42],["af.httprm",42,"5765654567658787",9]]
"""

# Response with nested JSON that needs double-parsing
MOCK_NESTED_RESPONSE = """)]}'

150
[["wrb.fr","rLM1Ne","[\\"My Notebook\\",[[[\\"source-uuid\\"],\\"Doc Title\\",null]],\\"notebook-uuid\\",null,null,[1,false,true]]",null,null,null,"generic"]]
"""


# =============================================================================
# Tests for parse_timestamp
# =============================================================================

class TestParseTimestamp:
    """Tests for the parse_timestamp function."""

    def test_valid_timestamp(self):
        """Valid [seconds, nanos] should return ISO format string."""
        # 1704067200 = 2024-01-01 00:00:00 UTC
        result = parse_timestamp([1704067200, 0])
        assert result == "2024-01-01T00:00:00Z"

    def test_valid_timestamp_ignores_nanos(self):
        """Nanoseconds are present but not used in output."""
        result = parse_timestamp([1704067200, 123456789])
        assert result == "2024-01-01T00:00:00Z"

    def test_none_input(self):
        """None input should return None."""
        assert parse_timestamp(None) is None

    def test_empty_list(self):
        """Empty list should return None."""
        assert parse_timestamp([]) is None

    def test_non_list_input(self):
        """Non-list input should return None."""
        assert parse_timestamp("not a list") is None
        assert parse_timestamp(12345) is None
        assert parse_timestamp({"seconds": 12345}) is None

    def test_non_numeric_seconds(self):
        """Non-numeric seconds should return None."""
        assert parse_timestamp(["not a number", 0]) is None
        assert parse_timestamp([None, 0]) is None

    def test_overflow_timestamp(self):
        """Extremely large timestamp should return None (overflow protection)."""
        # This would overflow datetime
        result = parse_timestamp([99999999999999, 0])
        assert result is None

    def test_negative_timestamp(self):
        """Negative timestamp (before epoch) should still work."""
        # -86400 = 1969-12-31 00:00:00 UTC
        result = parse_timestamp([-86400, 0])
        assert result == "1969-12-31T00:00:00Z"

    def test_float_seconds(self):
        """Float seconds should work (truncated to seconds)."""
        result = parse_timestamp([1704067200.5, 0])
        assert result == "2024-01-01T00:00:00Z"


# =============================================================================
# Tests for extract_cookies_from_chrome_export
# =============================================================================

class TestExtractCookies:
    """Tests for the extract_cookies_from_chrome_export function."""

    def test_basic_cookie_parsing(self):
        """Basic semicolon-separated cookies should parse correctly."""
        header = "SID=abc123; HSID=def456; SSID=ghi789"
        result = extract_cookies_from_chrome_export(header)
        assert result == {
            "SID": "abc123",
            "HSID": "def456",
            "SSID": "ghi789",
        }

    def test_cookies_with_spaces(self):
        """Cookies with varying whitespace should parse correctly."""
        header = "SID=abc123;HSID=def456;  SSID=ghi789"
        result = extract_cookies_from_chrome_export(header)
        assert result == {
            "SID": "abc123",
            "HSID": "def456",
            "SSID": "ghi789",
        }

    def test_cookie_value_with_equals(self):
        """Cookie values containing = should preserve the full value."""
        header = "TOKEN=abc=def=ghi; OTHER=simple"
        result = extract_cookies_from_chrome_export(header)
        assert result["TOKEN"] == "abc=def=ghi"
        assert result["OTHER"] == "simple"

    def test_empty_string(self):
        """Empty string should return empty dict."""
        assert extract_cookies_from_chrome_export("") == {}

    def test_single_cookie(self):
        """Single cookie should work."""
        header = "SID=abc123"
        result = extract_cookies_from_chrome_export(header)
        assert result == {"SID": "abc123"}

    def test_complex_cookie_values(self):
        """Real-world cookie values with special chars should work."""
        header = "__Secure-1PSID=abc.xyz_123; APISID=def/ghi-jkl"
        result = extract_cookies_from_chrome_export(header)
        assert result["__Secure-1PSID"] == "abc.xyz_123"
        assert result["APISID"] == "def/ghi-jkl"


# =============================================================================
# Tests for _parse_response (via client instance)
# =============================================================================

class TestParseResponse:
    """Tests for the _parse_response method."""

    @pytest.fixture
    def client(self):
        """Create a client instance for testing (no network calls)."""
        return create_test_client()

    def test_strips_anti_xssi_prefix(self, client):
        """Response starting with )]}' should have it stripped."""
        response = """)]}'

10
["data"]
"""
        result = client._parse_response(response)
        assert len(result) == 1
        assert result[0] == ["data"]

    def test_parses_byte_count_format(self, client):
        """Standard byte-count + JSON format should parse."""
        response = """25
["some","json","data"]
"""
        result = client._parse_response(response)
        assert len(result) == 1
        assert result[0] == ["some", "json", "data"]

    def test_parses_multiple_chunks(self, client):
        """Multiple chunks should all be parsed."""
        response = """)]}'

10
["chunk1"]

10
["chunk2"]
"""
        result = client._parse_response(response)
        assert len(result) == 2
        assert result[0] == ["chunk1"]
        assert result[1] == ["chunk2"]

    def test_handles_empty_response(self, client):
        """Empty response should return empty list."""
        result = client._parse_response("")
        assert result == []

    def test_handles_only_prefix(self, client):
        """Response with only prefix should return empty list."""
        result = client._parse_response(")]}'")
        assert result == []

    def test_skips_invalid_json(self, client):
        """Invalid JSON lines should be skipped, valid ones parsed."""
        response = """10
["valid"]

15
not valid json

12
["also_ok"]
"""
        result = client._parse_response(response)
        assert len(result) == 2
        assert ["valid"] in result
        assert ["also_ok"] in result


# =============================================================================
# Tests for _extract_rpc_result (via client instance)
# =============================================================================

class TestExtractRpcResult:
    """Tests for the _extract_rpc_result method."""

    @pytest.fixture
    def client(self):
        """Create a client instance for testing."""
        return create_test_client()

    def test_extracts_matching_rpc_id(self, client):
        """Should extract result for matching RPC ID."""
        parsed = [
            [["wrb.fr", "wXbhsf", '["notebook1","notebook2"]', None, None, None, "generic"]]
        ]
        result = client._extract_rpc_result(parsed, "wXbhsf")
        assert result == ["notebook1", "notebook2"]

    def test_returns_none_for_missing_rpc(self, client):
        """Should return None if RPC ID not found."""
        parsed = [
            [["wrb.fr", "wXbhsf", '["data"]', None, None, None, "generic"]]
        ]
        result = client._extract_rpc_result(parsed, "nonexistent")
        assert result is None

    def test_handles_empty_parsed_response(self, client):
        """Empty parsed response should return None."""
        result = client._extract_rpc_result([], "wXbhsf")
        assert result is None

    def test_handles_nested_json_string(self, client):
        """JSON string result should be parsed."""
        parsed = [
            [["wrb.fr", "testRpc", '{"key": "value"}', None, None, None, "generic"]]
        ]
        result = client._extract_rpc_result(parsed, "testRpc")
        assert result == {"key": "value"}

    def test_handles_non_json_string_result(self, client):
        """Non-JSON string result should be returned as-is."""
        parsed = [
            [["wrb.fr", "testRpc", "plain string", None, None, None, "generic"]]
        ]
        result = client._extract_rpc_result(parsed, "testRpc")
        assert result == "plain string"

    def test_handles_multiple_chunks_finds_correct_one(self, client):
        """Should find result in correct chunk when multiple present."""
        parsed = [
            [["di", 42]],  # Different structure, should be skipped
            [["wrb.fr", "otherRpc", '["other"]', None, None, None, "generic"]],
            [["wrb.fr", "targetRpc", '["found"]', None, None, None, "generic"]],
        ]
        result = client._extract_rpc_result(parsed, "targetRpc")
        assert result == ["found"]


# =============================================================================
# Integration-style tests with realistic fixtures
# =============================================================================

class TestRealisticResponses:
    """Tests with more realistic response shapes."""

    @pytest.fixture
    def client(self):
        """Create a client instance for testing."""
        return create_test_client()

    def test_parse_and_extract_list_notebooks_response(self, client):
        """End-to-end parse + extract for list notebooks."""
        # Simulated response from wXbhsf (list notebooks)
        response = """)]}'

120
[["wrb.fr","wXbhsf","[[[\\"My Notebook\\",[[[\\"src-1\\"],\\"Source Title\\"]],\\"nb-uuid-123\\",null,null,[1,false,true]]]]",null,null,null,"generic"]]
"""
        parsed = client._parse_response(response)
        result = client._extract_rpc_result(parsed, "wXbhsf")
        
        # Result should be the parsed notebook list (double-wrapped: [[notebook_data]])
        assert result is not None
        assert isinstance(result, list)
        assert len(result) == 1
        # result[0] is the notebooks list, result[0][0] is the first notebook
        notebooks_list = result[0]
        assert isinstance(notebooks_list, list)
        notebook = notebooks_list[0]
        assert notebook[0] == "My Notebook"
        assert notebook[2] == "nb-uuid-123"

