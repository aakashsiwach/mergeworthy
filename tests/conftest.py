"""Integration tests need Docker and the network, so they only run with ``--run-integration``.

A plain ``pytest`` never touches the network.
"""

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--run-integration",
        action="store_true",
        default=False,
        help="run tests marked 'integration' (need Docker and network access)",
    )


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "integration: needs Docker and network; run with --run-integration")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if config.getoption("--run-integration"):
        return
    skip = pytest.mark.skip(reason="integration test: run with --run-integration (needs Docker and network)")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip)
