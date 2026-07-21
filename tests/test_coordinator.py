"""Coordinator behaviour tests.

We mock the API client at the boundary (its async methods) and `import_usage_statistics`
so these tests don't need a running recorder — that keeps them fast and the assertions
focused on the lifecycle decisions the coordinator makes (which error → which HA failure
mode, when to persist rotated credentials, etc.).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.greenbutton.api import (
    CustomerInfo,
    CustomerResponse,
    MeterReadingSeries,
    NewCredentials,
    NormalizedReadingType,
    OpenGbApi,
    OpenGbApiError,
    OpenGbAuthExpiredError,
    OpenGbPermanentError,
    UsagePoint,
    UsageReading,
    UsageResponse,
)
from custom_components.greenbutton.const import (
    CONF_CUSTOMER_LABEL,
    CONF_ENCRYPTED_REFRESH_BLOB,
    CONF_LAST_FETCHED_AT,
    CONF_POLL_INTERVAL_SECONDS,
    CONF_PROXY_TOKEN,
    CONF_UTILITY_ID,
    CONF_UTILITY_NAME,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    INITIAL_FETCH_LOOKBACK,
    LAST_FETCHED_OVERLAP,
)
from custom_components.greenbutton.coordinator import GreenButtonCoordinator

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant


def _entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_UTILITY_ID: "example_utility",
            CONF_UTILITY_NAME: "Example Utility",
            CONF_ENCRYPTED_REFRESH_BLOB: "original_blob",
            CONF_PROXY_TOKEN: "original_token",
            # Mark customer-labeling as already resolved so the shared entry doesn't trigger a
            # (real) fetch_customer during these lifecycle tests. The dedicated customer-label
            # tests use a fresh entry without this key.
            CONF_CUSTOMER_LABEL: "",
        },
    )
    entry.add_to_hass(hass)
    return entry


def _empty_response() -> UsageResponse:
    """A valid-shape UsageResponse with no readings — keeps stats writes trivial in tests."""
    return UsageResponse(updated=None, usage_points=[], new_credentials=None)


def _reading_type() -> NormalizedReadingType:
    return NormalizedReadingType(
        commodity="ELECTRICITY_SECONDARY_METERED",
        flow_direction="FORWARD",
        accumulation_behaviour="DELTA_DATA",
        interval_length_seconds=3600,
        unit_of_measure="WATT_HOURS",
        unit_of_measure_symbol="Wh",
        power_of_ten_multiplier=0,
        currency_numeric_code=124,
    )


def _response_with_readings(*starts: datetime) -> UsageResponse:
    """A UsageResponse carrying one FORWARD series with a reading at each given start."""
    readings = [UsageReading(start=s, duration_seconds=3600, value=1000.0) for s in starts]
    series = MeterReadingSeries(
        meter_reading_id="mr1", reading_type=_reading_type(), readings=readings
    )
    up = UsagePoint(usage_point_id="up1", service_kind="electricity", series=[series])
    return UsageResponse(updated=None, usage_points=[up], new_credentials=None)


async def test_first_refresh_calls_api_and_imports_stats(hass: HomeAssistant) -> None:
    """Happy path: coordinator fetches, then hands the response to the stats importer."""
    api = OpenGbApi(session=None, server_base_url="http://test")  # type: ignore[arg-type]
    response = _empty_response()
    api.fetch_usage = AsyncMock(return_value=response)  # type: ignore[method-assign]

    entry = _entry(hass)
    coordinator = GreenButtonCoordinator(hass, api, entry)

    with patch(
        "custom_components.greenbutton.coordinator.import_usage_statistics",
        new=AsyncMock(),
    ) as import_mock:
        await coordinator.async_refresh()

    # Both window params are always set — sending neither makes the GBA test-lab harness
    # omit IntervalBlocks. On a first refresh (no CONF_LAST_FETCHED_AT), published_min looks
    # back INITIAL_FETCH_LOOKBACK (2 years); published_max is always now + a small buffer.
    api.fetch_usage.assert_awaited_once()
    call = api.fetch_usage.await_args
    assert call.kwargs["encrypted_refresh_blob"] == "original_blob"
    assert call.kwargs["proxy_token"] == "original_token"  # noqa: S105

    from custom_components.greenbutton.const import (
        INITIAL_FETCH_LOOKBACK,
        PUBLISHED_MAX_LOOKAHEAD,
    )

    now = datetime.now(UTC)
    expected_min = now - INITIAL_FETCH_LOOKBACK
    expected_max = now + PUBLISHED_MAX_LOOKAHEAD
    # Tolerate the few seconds of clock drift between the coordinator and the assertion.
    assert abs((call.kwargs["published_min"] - expected_min).total_seconds()) < 60
    assert abs((call.kwargs["published_max"] - expected_max).total_seconds()) < 60
    import_mock.assert_awaited_once()
    call_args = import_mock.await_args
    assert call_args.args[1] is entry
    assert call_args.args[2] is response
    assert call_args.kwargs == {"utility_display_name": "Example Utility"}
    assert coordinator.last_exception is None


async def test_auth_expired_becomes_config_entry_auth_failed(hass: HomeAssistant) -> None:
    """OpenGbAuthExpiredError → ConfigEntryAuthFailed (triggers HA reauth flow).

    Calls the internal update method directly: ``async_config_entry_first_refresh`` enforces
    an entry-state precondition that we don't satisfy in unit tests (we never call
    ``async_setup_entry``), and ``async_refresh`` swallows exceptions into ``last_exception``
    instead of re-raising. The mapping logic lives in ``_async_update_data`` either way.
    """
    api = OpenGbApi(session=None, server_base_url="http://test")  # type: ignore[arg-type]
    api.fetch_usage = AsyncMock(side_effect=OpenGbAuthExpiredError("expired"))  # type: ignore[method-assign]

    entry = _entry(hass)
    coordinator = GreenButtonCoordinator(hass, api, entry)

    with (
        patch(
            "custom_components.greenbutton.coordinator.import_usage_statistics",
            new=AsyncMock(),
        ) as import_mock,
        pytest.raises(ConfigEntryAuthFailed),
    ):
        await coordinator._async_update_data()
    import_mock.assert_not_awaited()


async def test_generic_api_error_becomes_update_failed(hass: HomeAssistant) -> None:
    """Non-auth API errors → UpdateFailed (HA retries with backoff, no reauth)."""
    api = OpenGbApi(session=None, server_base_url="http://test")  # type: ignore[arg-type]
    api.fetch_usage = AsyncMock(side_effect=OpenGbApiError("upstream 502"))  # type: ignore[method-assign]

    entry = _entry(hass)
    coordinator = GreenButtonCoordinator(hass, api, entry)

    with (
        patch(
            "custom_components.greenbutton.coordinator.import_usage_statistics",
            new=AsyncMock(),
        ),
        pytest.raises(UpdateFailed),
    ):
        await coordinator._async_update_data()


async def test_rotated_credentials_persisted_on_upstream_error(hass: HomeAssistant) -> None:
    """A post-refresh upstream failure still rotates a one-time refresh token. The coordinator
    must persist the rotated blob (carried on the error) even though it raises UpdateFailed —
    otherwise the retry reuses the burned token and cascades into a spurious reauth.
    """
    api = OpenGbApi(session=None, server_base_url="http://test")  # type: ignore[arg-type]
    api.fetch_usage = AsyncMock(  # type: ignore[method-assign]
        side_effect=OpenGbApiError(
            "upstream 502",
            new_credentials=NewCredentials(
                encrypted_refresh_blob="rotated_blob",
                proxy_token="rotated_token",  # noqa: S106
            ),
        ),
    )

    entry = _entry(hass)
    coordinator = GreenButtonCoordinator(hass, api, entry)

    with (
        patch(
            "custom_components.greenbutton.coordinator.import_usage_statistics",
            new=AsyncMock(),
        ),
        pytest.raises(UpdateFailed),
    ):
        await coordinator._async_update_data()

    # The burned token was replaced despite the failed refresh, so the next poll uses the fresh one.
    assert entry.data[CONF_ENCRYPTED_REFRESH_BLOB] == "rotated_blob"
    assert entry.data[CONF_PROXY_TOKEN] == "rotated_token"


async def test_data_pending_raises_repair_issue_and_update_failed(hass: HomeAssistant) -> None:
    """A 202 (async background load) → UpdateFailed + a non-fixable repair issue with the link.

    We don't support the ESPI async batch flow yet, so the only user-facing behaviour is a
    repair issue pointing at the tracking GitHub issue; the entry then fails this refresh.
    """
    from homeassistant.helpers import issue_registry as ir

    from custom_components.greenbutton.api import OpenGbDataPendingError
    from custom_components.greenbutton.const import BACKGROUND_LOAD_ISSUE_URL

    api = OpenGbApi(session=None, server_base_url="http://test")  # type: ignore[arg-type]
    api.fetch_usage = AsyncMock(  # type: ignore[method-assign]
        side_effect=OpenGbDataPendingError("data pending (202)"),
    )

    entry = _entry(hass)
    coordinator = GreenButtonCoordinator(hass, api, entry)

    with (
        patch(
            "custom_components.greenbutton.coordinator.import_usage_statistics",
            new=AsyncMock(),
        ) as import_mock,
        pytest.raises(UpdateFailed),
    ):
        await coordinator._async_update_data()

    import_mock.assert_not_awaited()
    issue = ir.async_get(hass).async_get_issue(DOMAIN, f"background_load_{entry.entry_id}")
    assert issue is not None
    assert issue.is_fixable is False
    assert issue.severity == ir.IssueSeverity.ERROR
    assert issue.learn_more_url == BACKGROUND_LOAD_ISSUE_URL
    assert issue.translation_placeholders == {"utility": "Example Utility"}


async def test_successful_refresh_clears_background_load_issue(hass: HomeAssistant) -> None:
    """Once a fetch succeeds, any previously-raised background-load issue is cleared."""
    from homeassistant.helpers import issue_registry as ir

    entry = _entry(hass)
    api = OpenGbApi(session=None, server_base_url="http://test")  # type: ignore[arg-type]
    coordinator = GreenButtonCoordinator(hass, api, entry)

    # Pre-seed the issue as if a prior poll had hit a 202.
    coordinator._async_create_background_load_issue()
    assert (
        ir.async_get(hass).async_get_issue(DOMAIN, f"background_load_{entry.entry_id}") is not None
    )

    coordinator.api.fetch_usage = AsyncMock(return_value=_empty_response())  # type: ignore[method-assign]
    with patch(
        "custom_components.greenbutton.coordinator.import_usage_statistics",
        new=AsyncMock(),
    ):
        await coordinator._async_update_data()

    assert ir.async_get(hass).async_get_issue(DOMAIN, f"background_load_{entry.entry_id}") is None


async def test_rotated_credentials_are_persisted_before_stats_import(
    hass: HomeAssistant,
) -> None:
    """New credentials must land in entry.data BEFORE the stats import runs.

    Otherwise a stats-write failure mid-refresh leaves HA with the stale token,
    which the next poll then sends to the utility → spurious reauth flow.
    """
    api = OpenGbApi(session=None, server_base_url="http://test")  # type: ignore[arg-type]
    response = UsageResponse(
        updated=None,
        usage_points=[],
        new_credentials=NewCredentials(
            encrypted_refresh_blob="rotated_blob",
            proxy_token="rotated_token",  # noqa: S106
        ),
    )
    api.fetch_usage = AsyncMock(return_value=response)  # type: ignore[method-assign]

    entry = _entry(hass)
    coordinator = GreenButtonCoordinator(hass, api, entry)

    observed: dict[str, str] = {}

    async def capture_entry_at_import_time(*args, **kwargs):
        observed["blob"] = entry.data[CONF_ENCRYPTED_REFRESH_BLOB]
        observed["token"] = entry.data[CONF_PROXY_TOKEN]

    with patch(
        "custom_components.greenbutton.coordinator.import_usage_statistics",
        new=AsyncMock(side_effect=capture_entry_at_import_time),
    ):
        await coordinator.async_refresh()

    # Entry sees the rotated values both at import time and after the refresh completes.
    assert observed == {"blob": "rotated_blob", "token": "rotated_token"}
    assert entry.data[CONF_ENCRYPTED_REFRESH_BLOB] == "rotated_blob"
    assert entry.data[CONF_PROXY_TOKEN] == "rotated_token"


async def test_rebuild_refetches_full_history_then_purges(hass: HomeAssistant) -> None:
    """Rebuild re-fetches the FULL window (ignoring the cursor), then clears + re-imports.

    A stored `last_fetched_at` would normally scope the next fetch to a small incremental
    slice — the rebuild must override that and pull the whole initial-history window so the
    recomputed statistics cover all history, not just since the last poll. The fetch happens
    before the purge (see test_rebuild_leaves_stats_intact_when_refetch_fails).
    """
    api = OpenGbApi(session=None, server_base_url="http://test")  # type: ignore[arg-type]
    api.fetch_usage = AsyncMock(return_value=_empty_response())  # type: ignore[method-assign]

    entry = _entry(hass)
    # Simulate an established entry mid-way through incremental polling.
    hass.config_entries.async_update_entry(
        entry,
        data={**entry.data, CONF_LAST_FETCHED_AT: "2026-06-01T00:00:00+00:00"},
    )
    coordinator = GreenButtonCoordinator(hass, api, entry)

    with (
        patch(
            "custom_components.greenbutton.coordinator.async_clear_statistics_for_entry",
            new=AsyncMock(return_value=[f"{DOMAIN}:x_cost", f"{DOMAIN}:x_forward"]),
        ) as clear_mock,
        patch(
            "custom_components.greenbutton.coordinator.import_usage_statistics",
            new=AsyncMock(),
        ) as import_mock,
    ):
        await coordinator.async_rebuild_statistics()

    clear_mock.assert_awaited_once_with(hass, entry.entry_id)
    api.fetch_usage.assert_awaited_once()
    import_mock.assert_awaited_once()
    # Rebuild imports from a zero baseline — fresh=True bypasses the resume-point read that a
    # rebuild raced against (stale cursor → every reading skipped → empty store).
    assert import_mock.await_args.kwargs["fresh"] is True

    # published_min looks back the full initial window, NOT to the 2026-06-01 cursor.
    now = datetime.now(UTC)
    expected_min = now - INITIAL_FETCH_LOOKBACK
    published_min = api.fetch_usage.await_args.kwargs["published_min"]
    assert abs((published_min - expected_min).total_seconds()) < 120

    # The one-shot flag is cleared after success → the next scheduled poll is incremental again.
    assert coordinator._force_full_history is False


async def test_rebuild_leaves_stats_intact_when_refetch_fails(hass: HomeAssistant) -> None:
    """A failed re-fetch must NOT purge — the existing statistics stay put.

    Regression guard for the destructive-rebuild bug: the utility's resource server is
    intermittently flaky, and clearing before a fetch that then fails wiped the user's
    history with no way to recover (the incremental window sits ahead of the utility's
    lagged data). Fetching first makes a failed rebuild a no-op, and the caller still gets a
    clear error.
    """
    api = OpenGbApi(session=None, server_base_url="http://test")  # type: ignore[arg-type]
    api.fetch_usage = AsyncMock(side_effect=OpenGbApiError("upstream 502"))  # type: ignore[method-assign]

    entry = _entry(hass)
    coordinator = GreenButtonCoordinator(hass, api, entry)

    with (
        patch(
            "custom_components.greenbutton.coordinator.async_clear_statistics_for_entry",
            new=AsyncMock(return_value=[f"{DOMAIN}:x_cost"]),
        ) as clear_mock,
        patch(
            "custom_components.greenbutton.coordinator.import_usage_statistics",
            new=AsyncMock(),
        ) as import_mock,
        pytest.raises(HomeAssistantError),
    ):
        await coordinator.async_rebuild_statistics()

    clear_mock.assert_not_awaited()  # fetch failed first → nothing purged
    import_mock.assert_not_awaited()
    # The one-shot flag is cleared even on the failure path.
    assert coordinator._force_full_history is False


async def test_cursor_advances_to_newest_reading_not_wall_clock(hass: HomeAssistant) -> None:
    """The incremental cursor is anchored to the newest reading, never to wall-clock `now`.

    Regression guard for the window-outruns-data bug: if the cursor advanced to `now`, then
    `published-min` (= cursor − overlap) would march past a utility that publishes on a lag,
    and every later poll would return nothing.
    """
    newest = datetime(2026, 7, 4, 5, 0, tzinfo=UTC)
    older = datetime(2026, 7, 3, 5, 0, tzinfo=UTC)
    api = OpenGbApi(session=None, server_base_url="http://test")  # type: ignore[arg-type]
    api.fetch_usage = AsyncMock(  # type: ignore[method-assign]
        return_value=_response_with_readings(older, newest)
    )

    entry = _entry(hass)
    coordinator = GreenButtonCoordinator(hass, api, entry)

    with patch(
        "custom_components.greenbutton.coordinator.import_usage_statistics",
        new=AsyncMock(),
    ):
        await coordinator.async_refresh()

    # Cursor is the newest reading start — well behind wall-clock now (2026-07-06).
    assert entry.data[CONF_LAST_FETCHED_AT] == newest.isoformat()


async def test_cursor_not_advanced_on_empty_response(hass: HomeAssistant) -> None:
    """An empty (0-reading) response must leave the cursor pinned to the last real data.

    Otherwise the window would creep forward on every empty poll and permanently outrun a
    lagging utility's not-yet-published data.
    """
    prior = "2026-06-01T00:00:00+00:00"
    api = OpenGbApi(session=None, server_base_url="http://test")  # type: ignore[arg-type]
    api.fetch_usage = AsyncMock(return_value=_empty_response())  # type: ignore[method-assign]

    entry = _entry(hass)
    hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_LAST_FETCHED_AT: prior})
    coordinator = GreenButtonCoordinator(hass, api, entry)

    with patch(
        "custom_components.greenbutton.coordinator.import_usage_statistics",
        new=AsyncMock(),
    ):
        await coordinator.async_refresh()

    assert entry.data[CONF_LAST_FETCHED_AT] == prior  # unchanged


async def test_cursor_never_regresses_on_late_partial_window(hass: HomeAssistant) -> None:
    """A window that only returns older readings must not pull the cursor backwards."""
    prior_dt = datetime(2026, 7, 4, 5, 0, tzinfo=UTC)
    older = datetime(2026, 7, 2, 5, 0, tzinfo=UTC)
    api = OpenGbApi(session=None, server_base_url="http://test")  # type: ignore[arg-type]
    api.fetch_usage = AsyncMock(  # type: ignore[method-assign]
        return_value=_response_with_readings(older)
    )

    entry = _entry(hass)
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_LAST_FETCHED_AT: prior_dt.isoformat()}
    )
    coordinator = GreenButtonCoordinator(hass, api, entry)

    with patch(
        "custom_components.greenbutton.coordinator.import_usage_statistics",
        new=AsyncMock(),
    ):
        await coordinator.async_refresh()

    assert entry.data[CONF_LAST_FETCHED_AT] == prior_dt.isoformat()  # held, not regressed


# ---------------------------------------------------------------------------------------
# Customer-labeling: give two otherwise-identical entries a distinguishable title.
# These use a fresh entry WITHOUT CONF_CUSTOMER_LABEL so the one-time fetch actually runs.
# ---------------------------------------------------------------------------------------


def _fresh_entry(hass: HomeAssistant) -> MockConfigEntry:
    """An entry with no customer label yet — triggers the one-time customer fetch."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Milton Hydro (SANDBOX for testing only)",
        data={
            CONF_UTILITY_ID: "milton_hydro",
            CONF_UTILITY_NAME: "Milton Hydro (SANDBOX for testing only)",
            CONF_ENCRYPTED_REFRESH_BLOB: "original_blob",
            CONF_PROXY_TOKEN: "original_token",
        },
    )
    entry.add_to_hass(hass)
    return entry


async def test_customer_label_retitles_entry_and_persists_details(hass: HomeAssistant) -> None:
    """First successful refresh fetches customer data and folds a distinguisher into the title."""
    from custom_components.greenbutton.const import (
        CONF_CUSTOMER_ACCOUNT_ID,
        CONF_CUSTOMER_ADDRESS,
    )

    api = OpenGbApi(session=None, server_base_url="http://test")  # type: ignore[arg-type]
    api.fetch_usage = AsyncMock(return_value=_empty_response())  # type: ignore[method-assign]
    api.fetch_customer = AsyncMock(  # type: ignore[method-assign]
        return_value=CustomerResponse(
            customer=CustomerInfo(
                account_id="100001-0000001",
                service_address="123 EXAMPLE ST, MILTON ON, L0L 0L0",
                customer_name=None,
            ),
            new_credentials=None,
        )
    )

    entry = _fresh_entry(hass)
    coordinator = GreenButtonCoordinator(hass, api, entry)

    with patch(
        "custom_components.greenbutton.coordinator.import_usage_statistics",
        new=AsyncMock(),
    ):
        await coordinator.async_refresh()

    api.fetch_customer.assert_awaited_once()
    # Title gains the service address (the label's first preference).
    assert entry.title == (
        "Milton Hydro (SANDBOX for testing only) — 123 EXAMPLE ST, MILTON ON, L0L 0L0"
    )
    assert entry.data[CONF_CUSTOMER_LABEL] == "123 EXAMPLE ST, MILTON ON, L0L 0L0"
    assert entry.data[CONF_CUSTOMER_ACCOUNT_ID] == "100001-0000001"
    assert entry.data[CONF_CUSTOMER_ADDRESS] == "123 EXAMPLE ST, MILTON ON, L0L 0L0"


async def test_customer_label_fetched_only_once(hass: HomeAssistant) -> None:
    """Once a label is stored, later refreshes don't refetch customer data."""
    api = OpenGbApi(session=None, server_base_url="http://test")  # type: ignore[arg-type]
    api.fetch_usage = AsyncMock(return_value=_empty_response())  # type: ignore[method-assign]
    api.fetch_customer = AsyncMock(  # type: ignore[method-assign]
        return_value=CustomerResponse(
            customer=CustomerInfo(account_id="ACC-1", service_address=None, customer_name=None),
            new_credentials=None,
        )
    )

    entry = _fresh_entry(hass)
    coordinator = GreenButtonCoordinator(hass, api, entry)

    with patch(
        "custom_components.greenbutton.coordinator.import_usage_statistics",
        new=AsyncMock(),
    ):
        await coordinator.async_refresh()
        await coordinator.async_refresh()

    api.fetch_customer.assert_awaited_once()
    assert entry.title == "Milton Hydro (SANDBOX for testing only) — ACC-1"


async def test_customer_label_permanent_error_marks_unavailable(hass: HomeAssistant) -> None:
    """A permanent (4xx) customer failure records an empty label so it isn't retried every poll.

    Covers both real cases the proxy now surfaces as a propagated 4xx → OpenGbPermanentError:
    the custodian advertising no customer resource (proxy 400 `no_customer_uri`) and refusing one
    our scope can't access (Burlington's upstream 403 `access_denied`).
    """
    for err in (
        OpenGbPermanentError("POST /proxy/customer returned 400 (permanent): no_customer_uri"),
        OpenGbPermanentError("POST /proxy/customer returned 403 (permanent): access_denied"),
    ):
        api = OpenGbApi(session=None, server_base_url="http://test")  # type: ignore[arg-type]
        api.fetch_usage = AsyncMock(return_value=_empty_response())  # type: ignore[method-assign]
        api.fetch_customer = AsyncMock(side_effect=err)  # type: ignore[method-assign]

        entry = _fresh_entry(hass)
        coordinator = GreenButtonCoordinator(hass, api, entry)

        with patch(
            "custom_components.greenbutton.coordinator.import_usage_statistics",
            new=AsyncMock(),
        ):
            await coordinator.async_refresh()
            await coordinator.async_refresh()

        # Attempted once, recorded unavailable, never retried; title unchanged.
        api.fetch_customer.assert_awaited_once()
        assert entry.data[CONF_CUSTOMER_LABEL] == ""
        assert entry.title == "Milton Hydro (SANDBOX for testing only)"


async def test_customer_label_transient_error_is_retried(hass: HomeAssistant) -> None:
    """A transient customer-fetch failure leaves no label stored, so the next poll retries."""
    api = OpenGbApi(session=None, server_base_url="http://test")  # type: ignore[arg-type]
    api.fetch_usage = AsyncMock(return_value=_empty_response())  # type: ignore[method-assign]
    api.fetch_customer = AsyncMock(  # type: ignore[method-assign]
        side_effect=[
            OpenGbApiError("POST /proxy/customer returned 502: utility_upstream_error"),
            CustomerResponse(
                customer=CustomerInfo(account_id="ACC-9", service_address=None, customer_name=None),
                new_credentials=None,
            ),
        ]
    )

    entry = _fresh_entry(hass)
    coordinator = GreenButtonCoordinator(hass, api, entry)

    with patch(
        "custom_components.greenbutton.coordinator.import_usage_statistics",
        new=AsyncMock(),
    ):
        await coordinator.async_refresh()
        assert CONF_CUSTOMER_LABEL not in entry.data  # transient → not marked, will retry
        await coordinator.async_refresh()

    assert api.fetch_customer.await_count == 2
    assert entry.title == "Milton Hydro (SANDBOX for testing only) — ACC-9"


async def test_customer_label_persists_rotated_credentials(hass: HomeAssistant) -> None:
    """The customer fetch can rotate a one-time refresh token; the coordinator must persist it."""
    api = OpenGbApi(session=None, server_base_url="http://test")  # type: ignore[arg-type]
    api.fetch_usage = AsyncMock(return_value=_empty_response())  # type: ignore[method-assign]
    api.fetch_customer = AsyncMock(  # type: ignore[method-assign]
        return_value=CustomerResponse(
            customer=CustomerInfo(account_id="ACC-1", service_address=None, customer_name=None),
            new_credentials=NewCredentials(
                encrypted_refresh_blob="rotated_blob",
                proxy_token="rotated_token",  # noqa: S106
            ),
        )
    )

    entry = _fresh_entry(hass)
    coordinator = GreenButtonCoordinator(hass, api, entry)

    with patch(
        "custom_components.greenbutton.coordinator.import_usage_statistics",
        new=AsyncMock(),
    ):
        await coordinator.async_refresh()

    assert entry.data[CONF_ENCRYPTED_REFRESH_BLOB] == "rotated_blob"
    assert entry.data[CONF_PROXY_TOKEN] == "rotated_token"  # noqa: S105


# ---------------------------------------------------------------------------------------
# Single tight incremental window; cost is caught by ordinary polls (no probe/cost cursor).
# ---------------------------------------------------------------------------------------


def _api_returning(response: UsageResponse) -> OpenGbApi:
    """API whose fetch_usage returns `response`."""
    api = OpenGbApi(session=None, server_base_url="http://test")  # type: ignore[arg-type]
    api.fetch_usage = AsyncMock(return_value=response)  # type: ignore[method-assign]
    return api


async def test_update_interval_uses_server_poll_interval(hass: HomeAssistant) -> None:
    """The coordinator polls at the per-utility cadence the server sent in the claim response."""
    entry = _entry(hass)
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_POLL_INTERVAL_SECONDS: 3600}
    )
    coordinator = GreenButtonCoordinator(hass, _api_returning(_empty_response()), entry)
    assert coordinator.update_interval == timedelta(seconds=3600)


async def test_update_interval_falls_back_to_default(hass: HomeAssistant) -> None:
    """An entry without the server-supplied cadence uses the local default (daily)."""
    entry = _entry(hass)  # no CONF_POLL_INTERVAL_SECONDS
    coordinator = GreenButtonCoordinator(hass, _api_returning(_empty_response()), entry)
    assert coordinator.update_interval == DEFAULT_SCAN_INTERVAL


async def test_incremental_poll_uses_one_tight_window_at_usage_frontier(
    hass: HomeAssistant,
) -> None:
    """A routine poll issues a single fetch anchored at the usage frontier — no probe, no widen.

    Cost rides the same window: `published-min` filters by publication date, so a bill published
    late is caught here when it appears, and the importer distributes it over recorded usage.
    """
    usage_frontier = datetime(2026, 7, 18, 5, 0, tzinfo=UTC)
    entry = _entry(hass)
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_LAST_FETCHED_AT: usage_frontier.isoformat()}
    )
    api = _api_returning(_empty_response())
    coordinator = GreenButtonCoordinator(hass, api, entry)
    with patch(
        "custom_components.greenbutton.coordinator.import_usage_statistics", new=AsyncMock()
    ):
        await coordinator.async_refresh()

    api.fetch_usage.assert_awaited_once()  # exactly one fetch — no probe
    assert (
        api.fetch_usage.await_args.kwargs["published_min"] == usage_frontier - LAST_FETCHED_OVERLAP
    )
