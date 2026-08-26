"""Temporary intentional-red proof for OPS-integration-db-locality Step 4."""

import pytest


pytestmark = pytest.mark.integration


def test_integration_failure_blocks_deploy() -> None:
    pytest.fail("intentional Step 4 deploy-gate proof")
