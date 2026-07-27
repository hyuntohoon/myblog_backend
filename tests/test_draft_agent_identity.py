"""FIX-nightly-draft-identity Phase A: the draft agent may create drafts, nothing else.

Three properties are locked here, in order of how badly they would hurt if they broke:

1. An unset `DRAFT_AGENT_SUB` must NOT widen access. An empty string compares equal
   to a missing/empty `sub` in a naive implementation, which would turn "no agent
   configured" into "anyone with a token that has no sub gets in".
2. The agent must not be able to publish. Passing the route gate is not permission
   to set `status`; `create_post` coerces a non-owner's post to `draft`.
3. `require_owner` must stay exactly as narrow as it was. It guards 38 routes and
   this change must not have widened any of them.

Settings are monkeypatched on the module directly — `auth.py` binds `settings` at
import, so env-var patching would not reach the cached singleton (same pattern as
tests/test_auth_failclosed.py).
"""
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import app.core.auth as auth

OWNER = "0468fd3c-owner"
AGENT = "64885d4c-agent"


def _settings(**kw):
    base = dict(
        ENV="prod",
        COGNITO_USER_POOL_ID="pool",
        COGNITO_REGION="ap-northeast-2",
        OWNER_SUB=OWNER,
        DRAFT_AGENT_SUB="",
    )
    base.update(kw)
    return SimpleNamespace(**base)


# --- 1. an unset agent sub must never widen access -------------------------

@pytest.mark.parametrize("sub", ["", None, "someone-else", AGENT])
def test_unset_agent_sub_admits_nobody_but_the_owner(monkeypatch, sub):
    monkeypatch.setattr(auth, "settings", _settings(DRAFT_AGENT_SUB=""))
    with pytest.raises(HTTPException) as e:
        auth.require_owner_or_draft_agent(claims={"sub": sub})
    assert e.value.status_code == 403


def test_missing_sub_claim_does_not_match_a_configured_agent(monkeypatch):
    """`{}` has no `sub`; it must not slip through against any configured value."""
    monkeypatch.setattr(auth, "settings", _settings(DRAFT_AGENT_SUB=AGENT))
    for claims in ({}, {"sub": None}, {"sub": ""}):
        with pytest.raises(HTTPException) as e:
            auth.require_owner_or_draft_agent(claims=claims)
        assert e.value.status_code == 403


# --- 2. who gets in, when configured ---------------------------------------

def test_owner_always_passes(monkeypatch):
    monkeypatch.setattr(auth, "settings", _settings(DRAFT_AGENT_SUB=AGENT))
    assert auth.require_owner_or_draft_agent(claims={"sub": OWNER}) == {"sub": OWNER}


def test_configured_agent_passes(monkeypatch):
    monkeypatch.setattr(auth, "settings", _settings(DRAFT_AGENT_SUB=AGENT))
    assert auth.require_owner_or_draft_agent(claims={"sub": AGENT}) == {"sub": AGENT}


def test_stranger_is_rejected_even_when_an_agent_exists(monkeypatch):
    monkeypatch.setattr(auth, "settings", _settings(DRAFT_AGENT_SUB=AGENT))
    with pytest.raises(HTTPException) as e:
        auth.require_owner_or_draft_agent(claims={"sub": "member-1234"})
    assert e.value.status_code == 403


# --- 3. fail closed, exactly like require_owner ------------------------------

def test_unset_owner_sub_in_prod_is_503_not_403(monkeypatch):
    """A misconfiguration must not read as a permission failure, and must never open."""
    monkeypatch.setattr(auth, "settings", _settings(OWNER_SUB="", DRAFT_AGENT_SUB=AGENT))
    with pytest.raises(HTTPException) as e:
        auth.require_owner_or_draft_agent(claims={"sub": AGENT})
    assert e.value.status_code == 503


@pytest.mark.parametrize("env", ["local", "dev"])
def test_local_dev_bypasses_like_the_other_guards(monkeypatch, env):
    monkeypatch.setattr(auth, "settings", _settings(ENV=env, OWNER_SUB="", DRAFT_AGENT_SUB=""))
    assert auth.require_owner_or_draft_agent(claims={}) == {}


# --- 4. is_owner — the flag create_post uses to decide about coercion --------

def test_is_owner_true_only_for_the_owner(monkeypatch):
    monkeypatch.setattr(auth, "settings", _settings(DRAFT_AGENT_SUB=AGENT))
    assert auth.is_owner({"sub": OWNER}) is True
    assert auth.is_owner({"sub": AGENT}) is False
    assert auth.is_owner({}) is False


def test_is_owner_is_false_when_owner_sub_is_unset(monkeypatch):
    """Without a configured owner nobody is the owner — so nothing is exempt from
    coercion. Fail closed rather than treating 'unconfigured' as 'everyone'."""
    monkeypatch.setattr(auth, "settings", _settings(OWNER_SUB=""))
    assert auth.is_owner({"sub": OWNER}) is False
    assert auth.is_owner({"sub": ""}) is False


@pytest.mark.parametrize("env", ["local", "dev"])
def test_is_owner_true_in_local_dev(monkeypatch, env):
    """require_cognito_token yields {} locally; local admin work must stay owner-like."""
    monkeypatch.setattr(auth, "settings", _settings(ENV=env, OWNER_SUB=""))
    assert auth.is_owner({}) is True


# --- 5. require_owner itself must not have been widened ---------------------

def test_require_owner_still_rejects_the_draft_agent(monkeypatch):
    """The whole design rests on this: the agent gets ONE route, not the other 37."""
    monkeypatch.setattr(auth, "settings", _settings(DRAFT_AGENT_SUB=AGENT))
    with pytest.raises(HTTPException) as e:
        auth.require_owner(claims={"sub": AGENT})
    assert e.value.status_code == 403
