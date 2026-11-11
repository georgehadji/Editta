"""How the rate limiter is built from the configured store."""

from editta.api.middleware import build_limiter


def test_without_a_store_counters_stay_in_the_process() -> None:
    limiter = build_limiter(None)

    assert limiter._storage_uri is None
    assert limiter._swallow_errors is False


def test_with_a_shared_store_an_outage_does_not_fail_requests() -> None:
    limiter = build_limiter("memory://")

    assert limiter._storage_uri == "memory://"
    assert limiter._swallow_errors is True
