"""Load the plugin root as the importable package ``safe_swap`` (no install needed)."""

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def _load() -> None:
    if "safe_swap" in sys.modules:
        return
    spec = importlib.util.spec_from_file_location("safe_swap", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    module = importlib.util.module_from_spec(spec)
    sys.modules["safe_swap"] = module
    spec.loader.exec_module(module)


_load()
