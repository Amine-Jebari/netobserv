"""Entry point for ``python -m services.agent``.

Delegates to :func:`services.agent.main.main` so the same image runs whether
the container's CMD/ENTRYPOINT invokes the package or the module directly.
"""

from __future__ import annotations

import sys

from .main import main


if __name__ == "__main__":
    sys.exit(main())
