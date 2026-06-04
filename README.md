# Open Green Button — Home Assistant integration

Bridges your utility's [Green Button](https://www.greenbuttondata.org/) (NAESB ESPI) energy data into the Home Assistant Energy dashboard via a stateless OAuth proxy server.

🚧 **Pre-alpha.** Burlington Hydro (Ontario, Canada) is the first targeted utility. Their Green Button review is in progress; the proxy server itself is hosted on Fly.io.

## How it works

This integration talks to a hosted proxy server at `https://api.opengreenbutton.org`. The proxy exists only because utilities require a stable public HTTPS callback URL for OAuth — your data never lives on it. Refresh tokens are stored encrypted in your Home Assistant config entry; every API call carries the token through the proxy and the server discards it immediately after the round-trip.

Server source code: [rocketraman/open-green-button](https://github.com/rocketraman/open-green-button).

## Installation

### HACS (custom repository, until accepted into HACS default)

1. In HACS, open **Integrations**.
2. Click the **⋮** menu → **Custom repositories**.
3. Add `https://github.com/rocketraman/open-green-button-homeassistant` with category **Integration**.
4. Install **Open Green Button**.
5. Restart Home Assistant.

### Manual

Copy `custom_components/greenbutton/` into your Home Assistant config directory at `<config>/custom_components/greenbutton/` and restart.

## Configuration

1. **Settings → Devices & Services → Add Integration → Open Green Button**.
2. Pick your utility from the dropdown.
3. Click the authorization link, complete the Green Button consent flow with your utility.
4. Paste the claim code (starts with `gb_live_`) back into Home Assistant.

The integration writes hourly consumption data into the HA Energy dashboard's long-term statistics.

## Supported utilities

| Utility | Status |
| --- | --- |
| Burlington Hydro (Ontario, Canada) | Pre-launch — Green Button review in progress |

Want your utility added? [Open an issue](https://github.com/rocketraman/open-green-button-homeassistant/issues) or check the [server-side configuration](https://github.com/rocketraman/open-green-button/blob/master/server/app/src/main/resources/utilities.conf).

## Privacy

- Your refresh token, usage data, and account identifiers live **only on your Home Assistant instance**.
- The hosted proxy server holds **zero per-user durable state** — no accounts, no databases, no usage history.
- Open source under MIT — read the code, run your own proxy, or fork it.
- **Clean removal:** deleting the integration via Devices & Services purges every long-term statistic it created. Multiple config entries on the same utility (e.g. a sandbox account beside a real one, or several meters at one address) get distinct, per-entry statistic IDs so they never bleed together in the Energy dashboard.

## Development

Toolchain pinned via [mise](https://mise.jdx.dev/). Python 3.13.

```sh
mise trust            # one-time
mise install          # installs Python 3.13, auto-creates .venv
pip install -r requirements_test.txt

ruff check .
ruff format --check .
pytest
```

The venv at `.venv/` is auto-activated when you `cd` into the repo.

## Roadmap

**Working today**

- OAuth authorization against the proxy server, with refresh-token rotation handled automatically
- Polls the proxy every 6 hours and writes hourly consumption into the Energy dashboard's long-term statistics via [`async_add_external_statistics`](https://developers.home-assistant.io/docs/core/entity/sensor#statistics-imported-from-external-sources)
- Reauth flow surfaces as an HA notification when the utility revokes our refresh token

**Pending**

- Burlington Hydro production credentials — currently in test-lab certification with the Green Button Alliance
- Cost data from ESPI `UsageSummary` blocks — the proxy already parses these, the statistics writer doesn't import them yet
- Push-based delivery (ESPI FB_39 NotificationURI) instead of polling, once a real utility supports it
- Additional utilities — [open an issue](https://github.com/rocketraman/open-green-button-homeassistant/issues) with your provider's name

## Contributing

Issues and PRs welcome. For substantial features, open an issue first so we can talk through the approach.

If this integration is useful to you and you want to help keep it maintained and the proxy server hosted:

- [GitHub Sponsors](https://github.com/sponsors/rocketraman)
- [Buy Me a Coffee](https://www.buymeacoffee.com/rocketraman)

Suggested $5/month — covers proxy hosting plus time spent adding utilities and keeping up with Home Assistant changes.

## License

[MIT](LICENSE).

"Green Button" is a trademark of the Green Button Alliance; this project uses the name in reference to the open data standard.
