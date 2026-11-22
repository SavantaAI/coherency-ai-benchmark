"""Watcher tests. Pure logic runs anywhere; tests marked `linux_inotify` need
real inotify and run inside a Linux container via tests/watcher/run_linux.sh.
On Linux those tests are never skipped: if inotify does not work they fail."""

from __future__ import annotations

import sys

import pytest


def pytest_configure(config):
    config.addinivalue_line("markers", "linux_inotify: needs Linux inotify; run with tests/watcher/run_linux.sh")


def pytest_collection_modifyitems(config, items):
    if sys.platform.startswith("linux"):
        return
    skip = pytest.mark.skip(reason="needs Linux inotify: run tests/watcher/run_linux.sh")
    for item in items:
        if "linux_inotify" in item.keywords:
            item.add_marker(skip)
