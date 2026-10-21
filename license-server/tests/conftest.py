import os
import tempfile
from pathlib import Path

_TEST_ROOT = Path(tempfile.mkdtemp(prefix="x5license-test-"))

os.environ["ENVIRONMENT"] = "test"
os.environ["DATABASE_URL"] = f"sqlite:///{(_TEST_ROOT / 'test.db').as_posix()}"
os.environ["KEYS_DIR"] = str(_TEST_ROOT / "keys")
os.environ["JWT_SECRET"] = "test-secret-do-not-use-in-production"
os.environ["ADMIN_BOOTSTRAP_EMAIL"] = "admin@example.com"
os.environ["ADMIN_BOOTSTRAP_PASSWORD"] = "AdminPass123"
os.environ["SANDBOX_SHARED_SECRET"] = "test-sandbox-secret"
os.environ["SANDBOX_PUBLIC_URL"] = "http://sandbox.test:8789"
os.environ["CHECKOUT_PROVIDER"] = "sandbox"
# The suite signs in many times; rate limits are covered by test_ratelimit.py.
os.environ["RATE_LIMIT_ENABLED"] = "false"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as test_client:
        yield test_client
