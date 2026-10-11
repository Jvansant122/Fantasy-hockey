import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import pytest


@pytest.fixture(autouse=True)
def no_retry_waits(monkeypatch):
    """rating.nhl_get backs off between retries; the fakes fail on purpose, so don't wait for real."""
    import rating
    monkeypatch.setattr(rating.time, "sleep", lambda s: None)
