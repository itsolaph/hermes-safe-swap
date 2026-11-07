"""Safe-Swap must never turn a command Hermes hard-blocks into an unblocked move."""

from __future__ import annotations

import itertools
import re

import pytest

from safe_swap.config import Config
from safe_swap.rewriter import rewrite

approval = pytest.importorskip("tools.approval_detection", reason="needs a Hermes checkout on sys.path")

DIRS = re.findall(r"/[a-z]+", approval._HARDLINE_SYSTEM_DIRS.replace("\\*", "")) + ["/", "//", "/.", "/..", "~", "$HOME", "${HOME}"]
SUFFIXES = ["", "/", "/*", "/."]
FLAGS = ["-rf", "-fr", "-r -f", "-Rf", "--recursive --force", "-rf --"]
WRAPPERS = ["{rm}", "/bin/rm", "\\rm", "FOO=1 {rm}"]


def variants():
    for d, suffix, flags, wrapper in itertools.product(sorted(set(DIRS)), SUFFIXES, FLAGS, WRAPPERS):
        yield f"{wrapper.format(rm='rm')} {flags} {d}{suffix}"
        yield f"{wrapper.format(rm='rm')} {flags} \"{d}{suffix}\""


def test_sweep_is_not_vacuous() -> None:
    hard = [c for c in variants() if approval.detect_hardline_command(c)[0]]
    assert len(hard) > 200


@pytest.mark.parametrize("command", list(variants()))
def test_hardline_commands_are_never_rewritten(command: str) -> None:
    if approval.detect_hardline_command(command)[0]:
        assert rewrite(command, Config(), "abc123") is None, command
