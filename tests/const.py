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

PROXY_USAGE_URL = f"{SERVER_BASE_URL}/proxy/usage"

# Minimal but realistic /proxy/usage payload — one usage point, one series, two hourly
# readings on 2026-02-24 from 05:00 to 06:00 UTC. Matches the shape produced by the server's
# UsageDto mapper (camelCase, seconds for durations, ISO strings for instants).
MOCK_USAGE_RESPONSE_OK = {
    "updated": "2026-06-03T14:00:00Z",
    "usagePoints": [
        {
            "usagePointId": "e082e9a9-390b-58fb-8ca5-4ee707c95652",
            "serviceKind": "ELECTRICITY",
            "series": [
                {
                    "meterReadingId": "022e1a41-a279-5af7-889e-3b46e67d9a01",
                    "readingType": {
                        "commodity": "ELECTRICITY_SECONDARY_METERED",
                        "flowDirection": "FORWARD",
                        "accumulationBehaviour": "DELTA_DATA",
                        "intervalLengthSeconds": 3600,
                        "unitOfMeasure": "WATT_HOURS",
                        "unitOfMeasureSymbol": "Wh",
                        "powerOfTenMultiplier": 0,
                        "currencyNumericCode": 124,
                    },
                    "readings": [
                        {
                            "start": "2026-02-24T05:00:00Z",
                            "durationSeconds": 3600,
                            "value": 1000.0,
                        },
                        {
                            "start": "2026-02-24T06:00:00Z",
                            "durationSeconds": 3600,
                            "value": 1500.0,
                        },
                    ],
                }
            ],
        }
    ],
}

# Same shape but with newCredentials, as the server returns on refresh-token rotation.
MOCK_USAGE_RESPONSE_WITH_ROTATION = {
    **MOCK_USAGE_RESPONSE_OK,
    "newCredentials": {
        "encryptedRefreshBlob": "rotated_blob_value",
        "proxyToken": "rotated_proxy_token",
    },
}
