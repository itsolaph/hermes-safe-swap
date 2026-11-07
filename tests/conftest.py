import pytest

from .shellrun import SHELLS, Sandbox


@pytest.fixture(params=SHELLS)
def box(request, tmp_path):
    return Sandbox(tmp_path, request.param)
