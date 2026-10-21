"""Pytest-wide isolation for the vector database.

Tests write fake embeddings (2- and 8-dim) and one test drops the whole chat
collection. The real store lives in X5Agent/Data/vector/chroma and must never
be touched by a test run, so every test process points ChromaDB at a
throwaway temp folder using the single owner's own override
(core/embedding/vector_db.py: X5_VECTOR_DB_DIR). This also keeps fake
embeddings from colliding with real collection dimensions.

Import-time on purpose: it must run before any test module imports the store.
An explicit X5_VECTOR_DB_DIR in the environment always wins.
"""
from __future__ import annotations

import os
import tempfile

if not os.environ.get("X5_VECTOR_DB_DIR"):
    os.environ["X5_VECTOR_DB_DIR"] = tempfile.mkdtemp(prefix="x5-vector-test-")

# Licensing is verified against the central server. Tests never touch it:
# the bypass flag only works while PYTEST_CURRENT_TEST is set (see
# core/licensing/config.py), and the state file is redirected to temp so a
# test can never read or overwrite the real X5Agent/Data session.
os.environ.setdefault("X5_LICENSE_BYPASS", "1")
if not os.environ.get("X5_LICENSE_STATE_PATH"):
    os.environ["X5_LICENSE_STATE_PATH"] = os.path.join(
        tempfile.mkdtemp(prefix="x5-license-test-"), "license.json"
    )

# Tests do not boot through agent_boot.py, and a few suites execute tools at
# import time (before any fixture can run). Relax ONLY the official-launch
# check for this pytest process; test_agent_points still monkeypatches it to
# prove the refusal paths, and licensing has its own bypass above.
from core.agent_guard import points as _test_points

_test_points._require_official = lambda: None

# Module-level test bodies run before PYTEST_CURRENT_TEST exists, so the
# licensing bypass flag alone is not enough during collection. The gate itself
# is covered in test_agent_points; keep tool tests focused on their own logic.
_test_points._license_gate = lambda: (True, "")
