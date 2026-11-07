import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from shellrun import SHELLS, Sandbox  # noqa: E402


@pytest.fixture(params=SHELLS)
def box(request, tmp_path):
    return Sandbox(tmp_path, request.param)
