"""CHORE-secrets-ssm-migration: `_load_secrets` is SSM-only and fails loudly.

These pin the one behaviour the migration's final leg changed. Before it, an SSM
failure logged "falling back to Secrets Manager", found no ARN, and returned
`{}`; the caller then continued with whatever defaults it had. Neither of these
properties had a test on this service — the whole suite was green either way,
which is exactly why the change needed its own.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from app.core.config import _load_secrets

_PARAM = "/myblog/backend"


def test_returns_parsed_json_from_ssm():
    payload = {"DATABASE_URL": "postgresql://host/db"}
    ssm = MagicMock()
    ssm.get_parameter.return_value = {"Parameter": {"Value": json.dumps(payload)}}
    with patch("boto3.client", return_value=ssm) as mk:
        assert _load_secrets(_PARAM) == payload
    assert mk.call_args.args[0] == "ssm"
    assert ssm.get_parameter.call_args.kwargs == {"Name": _PARAM, "WithDecryption": True}


def test_never_constructs_a_secretsmanager_client():
    ssm = MagicMock()
    ssm.get_parameter.return_value = {"Parameter": {"Value": "{}"}}
    seen: list[str] = []

    def client(name, **kw):
        seen.append(name)
        return ssm

    with patch("boto3.client", side_effect=client):
        _load_secrets(_PARAM)
    assert seen == ["ssm"]


def test_ssm_error_raises_instead_of_returning_empty(caplog):
    ssm = MagicMock()
    ssm.get_parameter.side_effect = Exception("AccessDenied")
    with patch("boto3.client", return_value=ssm):
        with pytest.raises(Exception, match="AccessDenied"):
            _load_secrets(_PARAM)
    assert _PARAM in caplog.text, "the failure log must name the parameter"


def test_unparseable_parameter_value_raises_and_is_logged(caplog):
    """A value that is not JSON is a load failure too.

    `/myblog/*` has been written with unquoted JSON before. If `json.loads` sat
    outside the try, this surfaced as a bare JSONDecodeError naming nothing.
    """
    ssm = MagicMock()
    ssm.get_parameter.return_value = {"Parameter": {"Value": "not json"}}
    with patch("boto3.client", return_value=ssm):
        with pytest.raises(json.JSONDecodeError):
            _load_secrets(_PARAM)
    assert _PARAM in caplog.text


def test_client_construction_failure_raises_and_is_logged(caplog):
    with patch("boto3.client", side_effect=Exception("NoRegionError")):
        with pytest.raises(Exception, match="NoRegionError"):
            _load_secrets(_PARAM)
    assert _PARAM in caplog.text
