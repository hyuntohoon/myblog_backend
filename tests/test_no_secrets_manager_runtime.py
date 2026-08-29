"""CHORE-secrets-ssm-migration lint: no Secrets Manager identifier in runtime code.

**What actually prevents Secrets Manager from being used** is not this file. It
is that the account holds zero secrets (`secretsmanager:ListSecrets` in
ap-northeast-2 returns `[]`, verified 2026-08-28) and that `infra/secrets.tf`
grants no `secretsmanager:*` action to any Lambda role. A reintroduced call
would fail on both counts.

This test is a **lint against the realistic accident** — someone reinstating the
deleted branch, or copying it out of git history into a new module — so the
mistake is caught in CI instead of at a cold start. It is deliberately not
claimed as a security control, because a text scan cannot be one: a client name
assembled at runtime defeats it and is meant to.

Caught: a literal `boto3.client("secretsmanager")`, `get_secret_value`,
`put_secret_value`, `SECRETS_ARN`, and implicit concatenation such as
`("secrets" "manager")`.
Not caught, by construction: `"secrets" + "manager"`, an f-string, a name read
from the environment, `getattr`/`importlib` indirection, or a call made from
inside a dependency rather than this repository's own source.

Comments are stripped before scanning, on purpose: the history of why this path
is gone must stay readable in the source.
"""

from __future__ import annotations

import io
import tokenize
from pathlib import Path

import pytest

RUNTIME_ROOT = Path(__file__).resolve().parents[1] / "app"

# The identifiers a copied-back Secrets Manager call site would contain.
BANNED = (
    "secretsmanager",  # boto3.client("secretsmanager")
    "get_secret_value",
    "put_secret_value",
    "SECRETS_ARN",  # covers SPOTIFY_SECRETS_ARN too
)

_QUOTES = "\"'"


def _scannable(source: str) -> str:
    """`source` with comments dropped and adjacent string literals joined.

    Tokens are separated by newlines so unrelated identifiers cannot fuse into a
    false positive — except for consecutive STRING tokens, which Python itself
    concatenates, so they are joined with nothing. Without that, the implicit
    `("secrets" "manager")` spelling of a banned name would slip through.
    """
    out: list[str] = []
    prev_was_string = False
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        if tok.type == tokenize.COMMENT:
            continue
        if tok.type == tokenize.STRING:
            # Drop the prefix/quotes so the literal's *content* is what gets
            # matched and concatenated.
            body = tok.string.lstrip("rbuRBUfF").strip(_QUOTES)
            if prev_was_string and out:
                out[-1] += body
            else:
                out.append(body)
            prev_was_string = True
            continue
        if tok.string.strip():
            out.append(tok.string)
            prev_was_string = False
    return "\n".join(out)


def _runtime_files() -> list[Path]:
    return sorted(p for p in RUNTIME_ROOT.rglob("*.py") if "__pycache__" not in p.parts)


def test_runtime_tree_is_not_empty() -> None:
    """Guard the guard: an empty file list would make this suite vacuously green."""
    assert len(_runtime_files()) > 10


def test_implicit_string_concatenation_is_scanned() -> None:
    """Guard the guard: the join rule above is the one part easy to break."""
    assert "secretsmanager" in _scannable('boto3.client("secrets" "manager")')
    # ...and unrelated neighbours must not fuse into a false positive.
    assert "secretsmanager" not in _scannable('secrets\nmanager')


@pytest.mark.parametrize("path", _runtime_files(), ids=lambda p: str(p.name))
def test_no_secrets_manager_reference(path: Path) -> None:
    code = _scannable(path.read_text(encoding="utf-8"))
    hits = [needle for needle in BANNED if needle in code]
    assert not hits, (
        f"{path.relative_to(RUNTIME_ROOT.parent)} reintroduces AWS Secrets Manager "
        f"{hits!r}. Runtime secrets come from SSM Parameter Store only "
        f"(CHORE-secrets-ssm-migration); Secrets Manager is empty in this account "
        f"and no Lambda role can call it, so this path could only mask an SSM failure."
    )
