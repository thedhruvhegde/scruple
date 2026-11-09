"""Load tests (roadmap 169). The full runs are `@pytest.mark.slow` (`make test-slow`);
a small smoke run of the same code stays in the default suite. Measured numbers are
recorded in docs/PERFORMANCE.md ("Load: episodes and HTTP"). Synthetic data only.

Targets: at least 1,000 simulated episodes per minute through the episode SDK and
through the episode CLI, and at least 200 requests/s on the cheapest HTTP route and
20 requests/s on a write route, against one uvicorn worker on this machine. The
targets are deliberately far below what was measured, so a shared CI box does not
flake; a regression of an order of magnitude still fails.
"""

from __future__ import annotations

import pytest

from tests.load import cli_episodes_per_minute, episodes_per_minute, http_requests_per_second

EPISODES_PER_MINUTE = 1_000


def test_load_harness_smoke() -> None:
    sdk = episodes_per_minute(n=40)
    cli = cli_episodes_per_minute(n=5)
    assert sdk["episodes"] == 40 and sdk["per_minute"] > 0
    assert cli["episodes"] == 5 and cli["per_minute"] > 0


@pytest.mark.slow
def test_sdk_rollouts_reach_a_thousand_per_minute() -> None:
    r = episodes_per_minute(n=2_000)
    assert r["per_minute"] >= EPISODES_PER_MINUTE, r


@pytest.mark.slow
def test_cli_episodes_reach_a_thousand_per_minute() -> None:
    r = cli_episodes_per_minute(n=300)
    assert r["per_minute"] >= EPISODES_PER_MINUTE, r


@pytest.mark.slow
def test_http_throughput_and_no_server_errors() -> None:
    r = http_requests_per_second(seconds=4.0, clients=4)
    for route in ("GET /health", "GET /rules", "GET /audit/verify", "POST /rules"):
        assert r[route]["errors"] == 0, (route, r[route])
    assert r["GET /health"]["per_second"] >= 200, r
    assert r["GET /rules"]["per_second"] >= 100, r
    assert r["POST /rules"]["per_second"] >= 20, r
