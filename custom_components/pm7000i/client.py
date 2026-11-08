"""HA-facing synchronous client for PM7000i devices.

All blocking protocol operations are isolated here. Call methods from HA's
executor; entities and coordinators must not use protocol internals directly.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from . import protocol

_LOGGER = logging.getLogger(__name__)

LoginError = protocol.LoginError  # re-exported for the config flow


class PM7000iClient:
    """Client facade for authentication, polling and device metadata."""

    def __init__(self, host: str, username: str, password: str) -> None:
        self.host = host.strip().rstrip("/")
        self.username = username
        self._password = password
        self._session = None
        self._token: str | None = None

    def connect(self) -> None:
        """(Re-)authenticate and cache the session token."""
        self._token = None
        self._session = protocol.LoginSession(
            self.host, self.username, self._password
        )
        self._token = self._session.token

    def _token_needs_renewal(self) -> bool:
        """True shortly before the token lifetime stated by the device ends."""
        session = self._session
        if session is None or not isinstance(session.expires_in, (int, float)):
            return False
        return time.time() - session.login_time > session.expires_in * 0.8

    def _ensure_connected(self) -> str:
        if not self._token or self._token_needs_renewal():
            self.connect()
        assert self._token is not None
        return self._token

    def validate_connection(self) -> None:
        """Authenticate without retaining a client outside the config flow."""
        protocol.request_access_token(self.host, self.username, self._password)

    def fetch_data(self) -> dict[str, Any]:
        """Fetch readings and metadata; log in again once if the token has expired."""
        payload = self._fetch(self._ensure_connected())
        if not payload["readings"]:
            # The token is cached between polls; if it expired the websockets return nothing.
            _LOGGER.info("No readings from %s - logging in again and retrying", self.host)
            self.connect()
            payload = self._fetch(self._token)
        if not payload["readings"]:
            raise protocol.LoginError("No readings received from PM7000i")
        return payload

    def _fetch(self, token: str) -> dict[str, Any]:
        """One polling pass with the given token."""
        result: dict[str, Any] = {}
        configs_by_source: dict[str, dict] = {}
        su100_units: list = []

        for source, topic in (
            ("smartmeter", protocol.TOPIC_SMARTMETER),
            ("su100", protocol.TOPIC_SU100),
        ):
            ws = None
            try:
                ws = protocol.open_websocket(
                    self.host, "values", topic, token, timeout=8
                )
                records = protocol.decode_record_map(
                    protocol.receive_binary(ws), protocol.decode_data_record
                )
                configs = {}
                try:
                    if source == "smartmeter":
                        configs = protocol.fetch_config_via_http(
                            self.host, topic, token
                        )
                    else:
                        config_ws = protocol.open_websocket(
                            self.host, "config", topic, token, timeout=5
                        )
                        try:
                            configs = protocol.decode_record_map(
                                protocol.receive_binary(config_ws),
                                protocol.decode_config_record,
                            )
                        finally:
                            config_ws.close()
                except Exception:
                    _LOGGER.debug("Could not fetch %s configuration", source, exc_info=True)
                result[source] = protocol.readings_from_records(records, configs)
                configs_by_source[source] = configs
            except Exception:
                _LOGGER.debug("Reading %s failed", source, exc_info=True)
            finally:
                if ws is not None:
                    try:
                        ws.close()
                    except Exception:
                        _LOGGER.debug("WebSocket close failed", exc_info=True)

        try:
            su100_units = protocol.fetch_su100_sensor_units(self.host, token)
        except Exception:
            _LOGGER.debug("Could not fetch SU-100 unit metadata", exc_info=True)

        identities: dict[str, dict] = {}
        for source, readings in result.items():
            configs = configs_by_source.get(source, {})
            for reading in readings:
                cfg = configs.get(reading.uuid, {})
                unit = (
                    protocol.find_sensor_unit(
                        su100_units, cfg.get("id", ""), cfg.get("label", "")
                    )
                    if source == "su100"
                    else None
                )
                identity = protocol.build_identity(
                    reading.uuid, cfg, unit, source == "su100"
                )
                if unit and unit.get("rtu_address") not in (None, ""):
                    identity["unit_id"] = str(unit["rtu_address"])
                identities[reading.uuid] = identity

        return {
            "readings": result,
            "configs_by_source": configs_by_source,
            "su100_units": su100_units,
            "device_identities": identities,
        }
