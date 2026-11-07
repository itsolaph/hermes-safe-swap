"""Load the plugin root as the importable package ``safe_swap`` (no install needed)."""

import atexit
import importlib.util
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def _load() -> None:
    if "safe_swap" in sys.modules:
        return
    spec = importlib.util.spec_from_file_location("safe_swap", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    module = importlib.util.module_from_spec(spec)
    sys.modules["safe_swap"] = module
    spec.loader.exec_module(module)


def pytest_configure(config) -> None:
    """Tests that import Hermes must never see (or touch) a real Hermes home: point it at a throwaway directory."""
    base = config.option.basetemp
    scratch = Path(f"{base}-hermes-home") if base else Path(tempfile.mkdtemp(prefix="safe-swap-hermes-home-"))
    scratch.mkdir(parents=True, exist_ok=True)
    if not base:
        atexit.register(shutil.rmtree, scratch, True)
    os.environ["HERMES_HOME"] = str(scratch)


_load()
