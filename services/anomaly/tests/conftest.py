"""Make the repo-root importable so ``services.anomaly.X`` resolves whether
pytest is launched from this directory or from the repo root."""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
