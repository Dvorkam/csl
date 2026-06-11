"""Tests for shared/registration.py."""

import base64
import json

import pytest

from control_station_lite.shared.registration import RegistrationBundle, encode_bundle

_SAMPLE = {
    "private_key": "-----BEGIN OPENSSH PRIVATE KEY-----\nABC\n-----END OPENSSH PRIVATE KEY-----\n",
    "key_fingerprint": "SHA256:abc123",
    "agent_port": 47731,
    "scripts_dir": "/home/user/.csl/scripts",
    "hostname_hint": "my-pc",
    "platform": "linux",
    "ssh_user": "alice",
    "api_token": "test-token-abc123",
}


class TestEncodeBundle:
    def test_returns_string(self) -> None:
        result = encode_bundle(**_SAMPLE)
        assert isinstance(result, str)

    def test_is_valid_base64(self) -> None:
        result = encode_bundle(**_SAMPLE)
        decoded = base64.b64decode(result)
        assert decoded  # non-empty

    def test_decoded_is_valid_json(self) -> None:
        result = encode_bundle(**_SAMPLE)
        data = json.loads(base64.b64decode(result))
        assert data["agent_port"] == 47731
        assert data["platform"] == "linux"

    def test_all_fields_present(self) -> None:
        result = encode_bundle(**_SAMPLE)
        data = json.loads(base64.b64decode(result))
        for key in _SAMPLE:
            assert key in data

    def test_round_trip_via_decode(self) -> None:
        encoded = encode_bundle(**_SAMPLE)
        bundle = RegistrationBundle.decode(encoded)
        assert bundle.agent_port == _SAMPLE["agent_port"]
        assert bundle.key_fingerprint == _SAMPLE["key_fingerprint"]
        assert bundle.platform == _SAMPLE["platform"]
        assert bundle.scripts_dir == _SAMPLE["scripts_dir"]
        assert bundle.hostname_hint == _SAMPLE["hostname_hint"]
        assert bundle.ssh_user == _SAMPLE["ssh_user"]
        assert bundle.api_token == _SAMPLE["api_token"]


class TestRegistrationBundleDecode:
    def test_valid_bundle(self) -> None:
        encoded = encode_bundle(**_SAMPLE)
        bundle = RegistrationBundle.decode(encoded)
        assert bundle.agent_port == 47731

    def test_encode_method_matches_function(self) -> None:
        encoded_fn = encode_bundle(**_SAMPLE)
        bundle = RegistrationBundle(**_SAMPLE)
        assert bundle.encode() == encoded_fn

    def test_invalid_base64_raises(self) -> None:
        with pytest.raises(ValueError, match="not valid base64"):
            RegistrationBundle.decode("!!!not-base64!!!")

    def test_invalid_json_raises(self) -> None:
        bad = base64.b64encode(b"not json").decode()
        with pytest.raises(ValueError, match="malformed"):
            RegistrationBundle.decode(bad)

    def test_missing_field_raises(self) -> None:
        data = dict(_SAMPLE)
        del data["private_key"]
        encoded = base64.b64encode(json.dumps(data).encode()).decode()
        with pytest.raises(ValueError, match="missing fields"):
            RegistrationBundle.decode(encoded)

    def test_unknown_platform_raises(self) -> None:
        data = dict(_SAMPLE, platform="amiga")
        encoded = base64.b64encode(json.dumps(data).encode()).decode()
        with pytest.raises(ValueError, match="unknown platform"):
            RegistrationBundle.decode(encoded)

    @pytest.mark.parametrize("platform", ["linux", "windows", "macos"])
    def test_all_valid_platforms_accepted(self, platform: str) -> None:
        data = dict(_SAMPLE, platform=platform)
        encoded = base64.b64encode(json.dumps(data).encode()).decode()
        bundle = RegistrationBundle.decode(encoded)
        assert bundle.platform == platform
