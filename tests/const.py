"""Fixed test data used across config-flow tests."""

from __future__ import annotations

SERVER_BASE_URL = "https://api.opengreenbutton.org"
UTILITIES_URL = f"{SERVER_BASE_URL}/utilities"


def claim_url(code: str) -> str:
    """Return the claim-redemption URL for a specific code."""
    return f"{SERVER_BASE_URL}/claim/{code}"


MOCK_UTILITIES = [
    {"id": "burlington_hydro", "displayName": "Burlington Hydro"},
    {"id": "pge", "displayName": "Pacific Gas & Electric"},
]

VALID_CLAIM_CODE = "gb_live_abc123def456"

MOCK_CLAIM_RESPONSE = {
    "utilityId": "burlington_hydro",
    "encryptedRefreshBlob": "AQABABCDEF==",
    "proxyToken": "proxy_token_xyz",
    "subscriptionUri": "https://utility.example/Subscription/42",
    "scope": "FB=1_3_4_5;IntervalDuration=3600",
    "currentApiVersion": "2026-05-22",
}
