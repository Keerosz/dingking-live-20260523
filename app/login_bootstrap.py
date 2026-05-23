from __future__ import annotations

import os
from pathlib import Path

from .auth import PropFinderAuthManager


def main() -> None:
    root = Path(__file__).resolve().parent.parent
    state_path = root / "data" / "propfinder_storage_state.json"
    url = os.getenv("PROPFINDER_CHEATSHEET_URL", "https://propfinder.app/mlb/cheatsheets")

    manager = PropFinderAuthManager(state_path=state_path)
    manager.bootstrap_interactive_login(url=url)
    print(f"Saved authenticated state to: {state_path}")


if __name__ == "__main__":
    main()
