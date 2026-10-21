import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("LICENSE_API_URL", "http://license.test:8788")
os.environ.setdefault("SANDBOX_SHARED_SECRET", "unit-test-secret")
