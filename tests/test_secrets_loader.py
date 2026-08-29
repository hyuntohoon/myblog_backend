"""CHORE-secrets-ssm-migration: `_load_secrets` is SSM-only and fails loudly.

These pin the one behaviour the migration's final leg changed. Before it, an SSM
failure logged "falling back to Secrets Manager", found no ARN, and returned
`{}`; the caller then continued with whatever defaults it had. Neither of these
properties had a test on this service — the whole suite was green either way,
which is exactly why the change needed its own.
"""

from __future__ import annotations

import json
import logging
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


# --- required-key contract ---------------------------------------------------
#
# `_load_secrets` raising covers "the parameter could not be read". These cover
# the condition it explicitly did NOT: the parameter was read, parsed as valid
# JSON, and a key the service cannot serve without is absent. Before this,
# `/myblog/backend` written without EDGE_SECRET booted cleanly and 403'd every
# CloudFront request, and written without DATABASE_URL booted against the
# localhost Settings default — the two failure modes named in the docstring
# above as "left as its own piece of work".

_DB = "postgresql+psycopg://u:pw@db.example/blog"
_EDGE = "edge-s3cret-value"
_FULL = {"DATABASE_URL": _DB, "EDGE_SECRET": _EDGE, "GITHUB_TOKEN": "ghp_x"}


def _get_settings(secrets, *, param: str = _PARAM, env: dict | None = None):
    """get_settings() with the SSM load stubbed and the singleton cache cleared.

    `SECRETS_PARAM` is set through the environment because that is how a Lambda
    supplies it (infra/lambda.tf). DATABASE_URL/EDGE_SECRET are blanked so a
    conftest-seeded env cannot stand in for a key the payload is missing.
    """
    import app.core.config as cfg

    cfg.get_settings.cache_clear()
    base = {"SECRETS_PARAM": param, "DATABASE_URL": "", "EDGE_SECRET": ""}
    try:
        with (
            patch("app.core.config._load_secrets", return_value=secrets) as loader,
            patch.dict("os.environ", {**base, **(env or {})}, clear=False),
        ):
            return cfg.get_settings(), loader
    finally:
        cfg.get_settings.cache_clear()


def test_all_required_keys_present_boots_and_applies_them():
    s, loader = _get_settings(_FULL)
    assert s.DATABASE_URL == _DB
    assert s.EDGE_SECRET == _EDGE
    assert s.GITHUB_TOKEN == "ghp_x"
    loader.assert_called_once_with(_PARAM)


def test_missing_edge_secret_fails_closed_at_boot():
    with pytest.raises(ValueError, match="EDGE_SECRET"):
        _get_settings({"DATABASE_URL": _DB, "GITHUB_TOKEN": "ghp_x"})


def test_missing_database_url_fails_closed_instead_of_using_the_localhost_default():
    """The check must read the payload, not the resolved attribute.

    `Settings.DATABASE_URL` defaults to a 127.0.0.1 URL, so `if not
    s.DATABASE_URL` — the formulation myblog_music/myblog_worker use, where the
    default is "" — can never be true here and would let this boot.
    """
    with pytest.raises(ValueError, match="DATABASE_URL"):
        _get_settings({"EDGE_SECRET": _EDGE})


def test_env_var_cannot_mask_a_key_missing_from_the_parameter():
    """A stale Lambda env var must not satisfy the contract.

    Presence is judged on what SSM supplied. If it were judged on the resolved
    Settings, an `EDGE_SECRET` left behind in the function's environment would
    mark the parameter healthy while the two values silently diverged.
    """
    with pytest.raises(ValueError, match="EDGE_SECRET"):
        _get_settings({"DATABASE_URL": _DB}, env={"EDGE_SECRET": "from-the-env"})


@pytest.mark.parametrize("bad", ["", "   ", None, 123, [], {}])
def test_unusable_required_value_counts_as_missing(bad):
    """`null`, empty, whitespace-only and non-strings are all "absent".

    The parameter is hand-edited JSON; each of these has a plausible way of
    being written, and none of them is a secret the service can use.
    """
    with pytest.raises(ValueError, match="EDGE_SECRET"):
        _get_settings({"DATABASE_URL": _DB, "EDGE_SECRET": bad})


def test_payload_that_is_valid_json_but_not_an_object_fails():
    """`json.loads` succeeds on `"abc"` and `[1,2]`; `.get` would then explode.

    The failure must name the parameter, not surface as an AttributeError.
    """
    for payload in ("just-a-string", [1, 2], 7):
        with pytest.raises(ValueError, match=_PARAM):
            _get_settings(payload)


def test_missing_optional_github_token_still_boots_and_warns(caplog):
    """GITHUB_TOKEN is degradable — it must not take the read API down.

    Its only consumers (owner-only publish, content_sync restore re-publish)
    already fail at their own call site. Boot continues; a WARNING is logged,
    which prod's LOG_LEVEL=WARNING actually shows.
    """
    with caplog.at_level(logging.WARNING, logger="app.core.config"):
        s, _ = _get_settings({"DATABASE_URL": _DB, "EDGE_SECRET": _EDGE})
    assert s.DATABASE_URL == _DB
    assert "GITHUB_TOKEN" in caplog.text
    assert _PARAM in caplog.text


def test_no_ssm_call_and_no_validation_when_secrets_param_is_unset():
    """Local dev and the unit suite: empty SECRETS_PARAM, no contract at all.

    This pins LOCAL ergonomics — no AWS call from a laptop or a CI runner. Read
    narrowly: it is NOT a guarantee that a deployed environment may run without
    `SECRETS_PARAM`. It may not, and that hole is still open — an empty
    `SECRETS_PARAM` on the real Lambda would skip this whole contract and boot on
    the localhost DB default with an empty EDGE_SECRET, exactly the state the
    contract exists to prevent. Closing it means asserting ENV-vs-SECRETS_PARAM
    consistency, which overlaps SEC-system-hardening open question 3 and is
    deliberately not decided here. If that assertion lands, this test changes
    shape rather than being deleted.
    """
    import app.core.config as cfg

    cfg.get_settings.cache_clear()
    try:
        with (
            patch("app.core.config._load_secrets") as loader,
            patch.dict("os.environ", {"SECRETS_PARAM": ""}, clear=False),
        ):
            s = cfg.get_settings()
        loader.assert_not_called()
        assert s.DATABASE_URL  # the local default, untouched
    finally:
        cfg.get_settings.cache_clear()


def test_secret_values_never_reach_the_exception_or_the_logs(caplog):
    """The error names the parameter and the KEYS. Never a value.

    A config error is read off CloudWatch by whoever is paged; a value pasted
    into it is a secret published to a log group with a different audience than
    the parameter it came from.
    """
    leaked = "SUPERSECRET-DB-PASSWORD"
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(ValueError) as exc:
            _get_settings({"DATABASE_URL": f"postgresql://u:{leaked}@h/db"})
    assert leaked not in str(exc.value)
    assert leaked not in caplog.text
    assert "EDGE_SECRET" in str(exc.value)
    assert _PARAM in str(exc.value)


def test_successful_boot_does_not_log_the_secret_values(caplog):
    """The DEBUG line masks the DB password and must not print EDGE_SECRET."""
    with caplog.at_level(logging.DEBUG):
        _get_settings({"DATABASE_URL": "postgresql://u:pw-in-url@h/db", "EDGE_SECRET": _EDGE})
    assert "pw-in-url" not in caplog.text
    assert _EDGE not in caplog.text


def test_required_values_are_assigned_verbatim_and_never_stripped():
    """Whitespace decides presence but must NOT be trimmed off the value.

    Kills a mutant that survived the first version of this file: rewriting the
    assignments as `secrets["EDGE_SECRET"].strip()` left all of them green. That
    refactor is not hypothetical — "normalize the config values" is a natural
    tidy-up, and it would 403 the entire site. `EDGE_SECRET` is compared
    byte-for-byte in `app/main.py` against the header CloudFront injects from
    this same parameter, so trimming here and not at the edge makes the two
    diverge; `DATABASE_URL` is asserted alongside it because a driver DSN is no
    safer to silently rewrite.
    """
    padded_edge = "  edge-with-padding  "
    padded_db = " postgresql+psycopg://u:pw@h/db "
    s, _ = _get_settings({"DATABASE_URL": padded_db, "EDGE_SECRET": padded_edge, "GITHUB_TOKEN": " g "})
    assert s.EDGE_SECRET == padded_edge
    assert s.DATABASE_URL == padded_db
    assert s.GITHUB_TOKEN == " g "
