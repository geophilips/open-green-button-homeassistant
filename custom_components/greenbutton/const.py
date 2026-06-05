"""Constants for the Open Green Button integration."""

from __future__ import annotations

from datetime import timedelta

DOMAIN = "greenbutton"

# How far back to overlap the window when re-fetching. Generous to absorb clock skew between
# us and the utility, and to forgive late-arriving corrections. The statistics writer is
# idempotent on (statistic_id, hour) so duplicates are harmless.
LAST_FETCHED_OVERLAP = timedelta(days=1)

# How far back to look on the first fetch (no recorded `last_fetched_at`). Our requested
# scope is 36 months of history (HistoryLength=94608000 in utilities.conf); 5 years gives
# headroom for utilities that retain longer histories.
INITIAL_FETCH_LOOKBACK = timedelta(days=5 * 365)

# Small forward buffer added to `published_max` to absorb clock skew between us and the
# utility — a meter reading published at `now()` on the utility's clock might be a few
# minutes in our future, and we don't want to miss it on the next sliding-window poll.
PUBLISHED_MAX_LOOKAHEAD = timedelta(days=1)

# The default cadence at which the (future) DataUpdateCoordinator polls the proxy for new
# usage data. Configurable later via the options flow.
DEFAULT_SCAN_INTERVAL = timedelta(hours=6)

# The hosted proxy server. May be overridden per-config-entry for self-hosters via the
# server_base_url in entry.data.
DEFAULT_SERVER_BASE_URL = "https://api.opengreenbutton.org"

# Stripe-style API version this client was built against. Sent as OpenGB-Api-Version on every
# request. When the server bumps its API, this constant moves with the integration version.
API_VERSION = "2026-05-22"

# Config entry data keys.
CONF_UTILITY_ID = "utility_id"
CONF_UTILITY_NAME = "utility_name"
CONF_SERVER_BASE_URL = "server_base_url"
CONF_CLAIM_CODE = "claim_code"  # noqa: S105 — config key, not a secret
CONF_PROXY_TOKEN = "proxy_token"  # noqa: S105
CONF_ENCRYPTED_REFRESH_BLOB = "encrypted_refresh_blob"  # noqa: S105
CONF_SUBSCRIPTION_URI = "subscription_uri"
CONF_SCOPE = "scope"
CONF_API_VERSION = "api_version"
CONF_LAST_IMPORTED = "last_imported"

# UTC ISO 8601 timestamp of the most recent successful /proxy/usage call. The coordinator
# uses this to scope subsequent requests via ESPI's `published-min` query param — first
# refresh fetches everything (since this field is absent on a new entry), every subsequent
# refresh asks the utility only for what's been published since last time.
CONF_LAST_FETCHED_AT = "last_fetched_at"
