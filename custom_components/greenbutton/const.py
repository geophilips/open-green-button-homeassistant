"""Constants for the Open Green Button integration."""

from __future__ import annotations

from datetime import timedelta

DOMAIN = "greenbutton"

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
