import platform

import pytest


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "linux_only: skip on non-Linux platforms")
    config.addinivalue_line("markers", "windows_only: skip on non-Windows platforms")


def pytest_runtest_setup(item: pytest.Item) -> None:
    if item.get_closest_marker("linux_only") and platform.system() != "Linux":
        pytest.skip("linux_only: skipped on non-Linux platform")
    if item.get_closest_marker("windows_only") and platform.system() != "Windows":
        pytest.skip("windows_only: skipped on non-Windows platform")
