# PM7000i Energy Meter – Home Assistant Integration

Unofficial custom integration that reads live measurements from a **PM7000i** energy meter
(including attached **PS7000** sub-meters) over your local network and exposes them as Home Assistant sensors.

ATTENTION: AI Slop... much vibecode... works for me :tm:

- Local polling, no cloud account required
- Power, energy counters, voltage, current, power factor and frequency – per phase and as sum
- Smart meter and up to two SU-100 devices
- Config flow (set up entirely from the UI), diagnostics download
- Translated UI: English and German – more languages are easy to add

> **Disclaimer:** This project is not affiliated with, endorsed by or supported by the manufacturer of the PM7000i.
> It talks to the device's local web API, which was analysed for interoperability and may change with firmware updates.
> Use at your own risk. The integration is read-only; it never writes to the device.

## Unknowns:

My device has a client_id and client_secret shown in 
https://<device_ip>/static/js/app.327e9fab.js
This is available even before login, so I guess it is just hardcoded,
so right now this is hardcoded in protocol.py. Maybe your device has other credentials, feel free to file an issue!

## Requirements

- Home Assistant **2026.10** or newer
- A PM7000i reachable from Home Assistant via HTTPS (port 443)
- The device's web login (user name and password)
- Python package `websocket-client >= 1.8.0` (installed automatically by Home Assistant)

Tested with: 
Produktname: Power Meter 7000i
Gerätetyp: hw0200
Software-Version: 3.3.1 
which has 2 Power Sensor PS7000 attached.

## Installation

### HACS (custom repository)

1. In HACS open **⋮ → Custom repositories**.
2. Add `https://github.com/z8i/pm7000i` with category **Integration**.
3. Install **PM7000i Energy Meter** and restart Home Assistant.

### Manual

1. Copy the folder `custom_components/pm7000i` into the `custom_components` directory of your Home Assistant configuration.
2. Restart Home Assistant.

## Setup

1. Go to **Settings → Devices & services → Add integration** and search for **PM7000i**.
2. Enter:
   - **Host** – IP address or host name of the meter
   - **Username** – default `admin` (Usually you can't change username for that device)
   - **Password** – the device password
3. Home Assistant validates the login and creates the entries. Each host can only be added once.

Wrong credentials are reported as *Authentication failed*, an unreachable device as *Failed to connect*.

## Entities

The integration creates devices for the smart meter and for each SU-100 it discovers. Typical sensors:

| Measurement | Notes |
|---|---|
| Active / reactive / apparent power | Per phase (L1–L3) and sum (Σ). Import is positive, export negative. |
| Active / reactive / apparent energy (import/export) | Counters; active energy is usable in the Energy dashboard (`total_increasing`). |
| Voltage, current | Per phase |
| Power factor, frequency | |
| Device information | Label, serial number, unit ID and device type as diagnostic sensors |

Values the integration does not recognise are still exposed (as *Unknown (C=…)* or with the label defined on the device),
so nothing is silently dropped.

The device is polled **every 10 seconds**. The session token is renewed automatically before it expires, and the
integration logs in again if the device stops delivering data.

## Translations

All user-facing texts should live in `custom_components/pm7000i/translations/<lang>.json`; currently `en` and `de`.
To add a language, copy `en.json` to `<code>.json`, translate the values and open a pull request.
Log messages are English only.

## Troubleshooting

**Sensors become unavailable** – Check that the host is reachable and the password is still valid.
Then enable debug logging and look at the output:

```yaml
logger:
  default: warning
  logs:
    custom_components.pm7000i: debug
```

**"No readings received from PM7000i"** – The device delivered no data. The integration retries once with a fresh login;
if the message persists, the device is not reachable or is not sending measurements. Attach the debug log to an issue.

**Download diagnostics** – *Settings → Devices & services → PM7000i → ⋮ → Download diagnostics.*
Credentials and tokens are not included.

## Security notes

- The device uses a self-signed certificate, so **TLS certificate verification is disabled** for connections to it.
  Only use the integration on a network you trust.
- Credentials are stored in Home Assistant's config entry storage like for any other integration.

## Development

```
custom_components/pm7000i/
├── __init__.py      # coordinator, polling, lifecycle
├── client.py        # blocking device facade (run in the executor)
├── protocol.py      # login, WebSocket transport, protobuf/OBIS decoding – no Home Assistant code, no UI texts
├── sensor.py        # entities, built from coordinator data only
├── config_flow.py   # credential validation
├── diagnostics.py   # safe operational summary
└── translations/    # en.json, de.json
```

`PM7000iClient(host, username, password)` offers `validate_connection()`, `connect()` and `fetch_data()`.
All three are blocking and must be called through `hass.async_add_executor_job`.

Pull requests are welcome – please keep protocol logic in `protocol.py` and texts in `translations/`.

## License

Released under the [Apache License 2.0](LICENSE).
