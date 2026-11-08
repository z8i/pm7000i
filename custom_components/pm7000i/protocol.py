#!/usr/bin/env python3
"""Protocol helpers for the PM7000i Home Assistant integration.

Provides authentication, WebSocket transport, protobuf decoding, OBIS
interpretation, and device identity helpers. No command-line interface or
console output is executed by importing this module.
"""
import json, logging, ssl, threading, time, urllib.parse, urllib.request
from types import SimpleNamespace

import websocket  # pip install websocket-client

_LOGGER = logging.getLogger(__name__)

# =====================================================================
#  1. CONFIGURATION - adjust here
# =====================================================================

TOPIC_SU100 = "su100/metering"
TOPIC_SMARTMETER = "smart-meter"
CLIENT_ID = "emos"
CLIENT_SECRET = "56951025"

# Which of the discovered SU-100 devices is "A" and which is "B"?
# None = automatic (alphabetical by device name/ID). Otherwise a substring of name, ID or UUID, e.g. "SN12345".
SU100_A = None
SU100_B = None

# Which measurements should be shown?  None = all, otherwise a function  reading -> bool.
# Fields of a reading (language independent):
#   v.column   "smartmeter" | "su100_a" | "su100_b"
#   v.kind     "voltage", "current", "power_factor", "frequency",
#              "active_power" / "reactive_power" / "apparent_power"          (NET_POWER = True)
#              "active_power_import" / "active_power_export" / ...          (NET_POWER = False)
#              "active_energy_import" / "active_energy_export" / ...        (energy counters)
#              "unknown_<C>" for unknown OBIS codes; for flex values the label defined on the device
#   v.phase    "Σ" (sum) | "L1" | "L2" | "L3" | "" (no phase)
#   v.channel  channel number (0 = sum)      v.device  device name        v.obis  OBIS code as text
#   v.value    scaled value                  v.unit    unit              v.raw   raw value
#   v.source   "obis" | "flex"               v.is_counter  True for energy counters
# Examples:
#   FILTER = lambda v: v.kind == "voltage"
#   FILTER = lambda v: v.kind in ("voltage", "current") and v.phase != "Σ"
#   FILTER = lambda v: v.kind == "active_power" and v.phase == "Σ"      (with NET_POWER = False: "active_power_import")
FILTER = None

# Power with sign instead of separate rows: import = positive, export = negative.
# (Energy counters in kWh are always kept separate.)  False = two rows (import / export).
NET_POWER = True

# Energy counters (kWh): True = show only the sum (Σ), hide the phase values L1-L3.
ENERGY_SUM_ONLY = False

# Decimal places per unit (default for units not listed: 2)
DECIMALS = {"V": 1, "A": 2, "W": 1, "VA": 1, "var": 1, "kWh": 3, "kVAh": 3, "kvarh": 3, "Hz": 2, "": 2}

# Table columns (internal keys, order = display order)
COLUMN_SMARTMETER, COLUMN_SU100_A, COLUMN_SU100_B = "smartmeter", "su100_a", "su100_b"
COLUMN_KEYS = [COLUMN_SMARTMETER, COLUMN_SU100_A, COLUMN_SU100_B]

# Names of the data sources (internal, for threads and the shared cache)
SOURCE_SMARTMETER, SOURCE_SU100 = "smartmeter", "su100"

# =====================================================================
#  3. PROTOBUF DECODING (schema taken from su-100.js / smartmeter.js)
# =====================================================================
# Short overview of the Protobuf binary format ("wire format"):
#   * A message is a sequence of fields. Each field starts with a "tag":
#         tag = (field number << 3) | wire type
#   * Wire type 0 = varint (integer of variable length)   1 = fixed 8 bytes
#                 2 = length + bytes (text, sub-message, map entry)   5 = fixed 4 bytes
#   * Default values (0, "", empty message) are NOT transmitted - a missing field means "default value".
#   * The meaning of the field numbers is not part of the data but of the schema, which was taken
#     from the original JavaScript files.

# Units of "flex" values (freely definable extra values), index = number from the configuration.
FLEX_UNIT_NAMES = ["", "A", "V", "W", "Hz", "VA", "var", "Wh", "kWh", "s", "min", "h", "day",
                   "week", "month", "year", "°C", "K", "°F"]
# Device types (enum from the schema, index = number; without the prefix "DEVICE_TYPE_")
DEVICE_TYPE_NAMES = [
    "UNKNOWN", "PHOTOVOLTAIC_SYSTEM", "ELECTRIC_VEHICLE", "BATTERY", "OVEN", "FLOW_HEATER", "BOILER",
    "IMMERSION_HEATER", "STOVE", "COOLER", "VENTILATION", "DISHWASHER", "DRYER", "HEAT_PUMP", "WASHING_MACHINE",
    "INVERTER_ONEPHASE", "INVERTER_THREEPHASE", "CHP", "BUILDING_OFFICE", "BUILDING_COMMERCIAL",
    "BUILDING_FACTORY", "BUILDING_SINGLE_HOME", "BUILDING_HOTEL", "BUILDING_APARTMENTS", "BUILDING_PARKING",
    "BUILDING_RESIDENTIAL", "ROOM_BATH", "ROOM_GARAGE", "ROOM_BASEMENT", "ROOM_CHILD", "ROOM_KITCHEN",
    "ROOM_SAUNA", "ROOM_BED", "ROOM_LIVING", "ROOM_GENERIC", "CONTROLLABLE_LOAD", "LIGHTING", "OFFICES",
    "DOMESTIC_APPLIANCES", "HEATER_OF_HEAT_PUMP", "INDUSTRIAL_ENGINE", "AIR_CONDITIONING", "COMPRESSOR",
    "PC_DATA_CENTER", "FUSES_THREE", "FUSES_SIX", "FUSES_NINE", "FUSES_TWELVE", "COMPACTOR", "WHITE_GOODS",
    "COLD_STORAGE_ROOM", "GARDEN_SHED", "COOLING_COMBINATION", "FACILITIES", "FREEZER", "FRIDGE",
    "GRID_CONNECTION_POINT", "INVERTER_HYBRID", "CIRCUIT_PROTECTION", "DATA_TRANSCEIVER",
]
# Status of a data record (enum from the schema)
STATUS_NAMES = ["UNKNOWN", "OK", "WARNING", "ERROR"]


class ProtobufReader:
    """Reads values one after another from a byte buffer and remembers the current position."""

    def __init__(self, data):
        self.data = data          # the complete buffer (bytes)
        self.pos = 0              # index of the next unread byte

    @property
    def at_end(self):
        return self.pos >= len(self.data)

    def read_varint(self):
        """Varint: 7 payload bits per byte, least significant group first. If bit 7 is set, another byte follows.
        Always returns an unsigned integer (for negative values see as_signed_int64 / to_signed_value)."""
        result, shift = 0, 0
        while True:
            byte = self.data[self.pos]
            self.pos += 1
            result |= (byte & 0x7F) << shift      # move the lower 7 bits to the right position
            shift += 7
            if not byte & 0x80:                   # bit 7 not set -> last byte
                return result

    def read_bytes(self):
        """Length-prefixed field: first the length (varint), then exactly that many bytes."""
        length = self.read_varint()
        chunk = self.data[self.pos:self.pos + length]
        self.pos += length
        return chunk

    def read_string(self):
        """Text = length-prefixed bytes, UTF-8 encoded."""
        return self.read_bytes().decode("utf-8", "replace")

    def skip_field(self, wire_type):
        """Skips an unknown field so that newer device firmware with additional fields does not break anything."""
        if wire_type == 0:
            self.read_varint()
        elif wire_type == 1:
            self.pos += 8
        elif wire_type == 2:
            self.read_bytes()
        elif wire_type == 5:
            self.pos += 4
        else:
            raise ValueError(f"Unknown wire type {wire_type}")


def parse_message(data, on_field):
    """Walks through all fields of a message and calls `on_field(field_no, wire_type, reader)` for each one.
    If on_field returns True the field was handled (the reader then stands behind the value);
    otherwise the field is skipped."""
    reader = ProtobufReader(data)
    while not reader.at_end:
        tag = reader.read_varint()
        field_no, wire_type = tag >> 3, tag & 7   # split the tag into field number and wire type
        if not on_field(field_no, wire_type, reader):
            reader.skip_field(wire_type)


def as_signed_int64(value):
    """Varints are unsigned. For fields of type int64 the value is read as 64-bit two's complement
    (>= 2^63 means negative)."""
    return value - (1 << 64) if value >= (1 << 63) else value


def decode_zigzag32(value):
    """Type sint32 uses "ZigZag": 0,-1,1,-2,2,... are transmitted as 0,1,2,3,4,... so that small negative
    numbers stay short."""
    return (value >> 1) ^ -(value & 1)


def parse_map_entry(data, read_key, read_value, default_key, default_value):
    """Decodes one entry of a Protobuf `map<K,V>`.
    Internally every entry is a tiny message: field 1 = key, field 2 = value.
    If the key or value has its default value (0, "", empty) the field is missing entirely - hence the
    pre-assignment with default_key / default_value. (Without it a measurement of 0 would arrive as None.)
    read_key / read_value: functions `reader -> value`."""
    entry = {"key": default_key, "value": default_value}

    def on_field(field_no, wire_type, reader):
        if field_no == 1:
            entry["key"] = read_key(reader); return True
        if field_no == 2:
            entry["value"] = read_value(reader); return True
        return False

    parse_message(data, on_field)
    return entry["key"], entry["value"]


def decode_timestamp(data):
    """google.protobuf.Timestamp: field 1 = seconds since 1970 (int64), field 2 = nanoseconds.
    Result: Unix time in seconds as float."""
    parts = {"seconds": 0, "nanos": 0}

    def on_field(field_no, wire_type, reader):
        if field_no == 1:
            parts["seconds"] = as_signed_int64(reader.read_varint()); return True
        if field_no == 2:
            parts["nanos"] = reader.read_varint(); return True
        return False

    parse_message(data, on_field)
    return parts["seconds"] + parts["nanos"] / 1e9


def decode_flex_value(data):
    """FlexValue (freely definable extra value, e.g. a temperature):
    field 1 = integer (int64), field 2 = text. Exactly one of them is set."""
    value = {"int_value": None, "string_value": None}

    def on_field(field_no, wire_type, reader):
        if field_no == 1:
            value["int_value"] = as_signed_int64(reader.read_varint()); return True
        if field_no == 2:
            value["string_value"] = reader.read_string(); return True
        return False

    parse_message(data, on_field)
    return value


def decode_flex_definition(data):
    """FlexDefinition (description of a flex value from the configuration):
    field 1 = label, 2 = data type, 3 = unit (index into FLEX_UNIT_NAMES),
    4 = power of ten (sint32) by which the integer is multiplied (-1 -> value / 10)."""
    definition = {"label": "", "type": 0, "unit": 0, "decimal_power": 0}

    def on_field(field_no, wire_type, reader):
        if field_no == 1:
            definition["label"] = reader.read_string(); return True
        if field_no == 2:
            definition["type"] = reader.read_varint(); return True
        if field_no == 3:
            definition["unit"] = reader.read_varint(); return True
        if field_no == 4:
            definition["decimal_power"] = decode_zigzag32(reader.read_varint()); return True
        return False

    parse_message(data, on_field)
    return definition


def decode_data_record(data):
    """GDR = data record of one device with its current measurements:
    field 1 = device ID, 2 = status, 3 = timestamp,
    field 4 = map<OBIS code (uint64), raw value (uint64)>   <- the actual measurements
    field 5 = map<name, FlexValue>                           <- extra values."""
    record = {"id": "", "status": 0, "timestamp": None, "obis_values": {}, "flex_values": {}}

    def on_field(field_no, wire_type, reader):
        if field_no == 1:
            record["id"] = reader.read_string(); return True
        if field_no == 2:
            record["status"] = reader.read_varint(); return True
        if field_no == 3:
            record["timestamp"] = decode_timestamp(reader.read_bytes()); return True
        if field_no == 4:
            # OBIS code and raw value are both varints; if the value is missing in the entry it is 0
            obis_code, raw_value = parse_map_entry(
                reader.read_bytes(), ProtobufReader.read_varint, ProtobufReader.read_varint, 0, 0)
            record["obis_values"][obis_code] = raw_value
            return True
        if field_no == 5:
            name, flex_value = parse_map_entry(
                reader.read_bytes(), ProtobufReader.read_string,
                lambda r: decode_flex_value(r.read_bytes()), "", decode_flex_value(b""))
            record["flex_values"][name] = flex_value
            return True
        return False

    parse_message(data, on_field)
    return record


def decode_config_record(data):
    """GCR = configuration record of one device (names, descriptions):
    field 1 = device ID, 2 = display name, 6 = device type (enum, index into DEVICE_TYPE_NAMES),
    7 = map<text,text> with metadata (e.g. phase assignment), 9 = map<name, FlexDefinition>.
    All other fields (class, sources, ...) are not needed and are skipped."""
    record = {"id": "", "label": "", "device_type": 0, "meta": {}, "flex_definitions": {}}

    def on_field(field_no, wire_type, reader):
        if field_no == 1:
            record["id"] = reader.read_string(); return True
        if field_no == 2:
            record["label"] = reader.read_string(); return True
        if field_no == 6:
            record["device_type"] = reader.read_varint(); return True
        if field_no == 7:
            key, value = parse_map_entry(
                reader.read_bytes(), ProtobufReader.read_string, ProtobufReader.read_string, "", "")
            record["meta"][key] = value
            return True
        if field_no == 9:
            name, definition = parse_map_entry(
                reader.read_bytes(), ProtobufReader.read_string,
                lambda r: decode_flex_definition(r.read_bytes()), "", decode_flex_definition(b""))
            record["flex_definitions"][name] = definition
            return True
        return False

    parse_message(data, on_field)
    return record


def decode_record_map(data, decode_item):
    """GDRs / GCRs = the complete WebSocket message: a map "device UUID -> record".
    Field 1 = map<UUID, record> (the record is decoded with `decode_item`, i.e. decode_data_record
    for measurements or decode_config_record for the configuration), field 2 = configuration UUID (not needed).
    Result: {uuid: record dict}"""
    records = {}

    def on_field(field_no, wire_type, reader):
        if field_no == 1:
            uuid, record = parse_map_entry(
                reader.read_bytes(), ProtobufReader.read_string,
                lambda r: decode_item(r.read_bytes()), "", decode_item(b""))
            records[uuid] = record
            return True
        if field_no == 2:
            reader.read_string()                  # read and discard configUuid
            return True
        return False

    parse_message(data, on_field)
    return records


# =====================================================================
#  4. OBIS INTERPRETATION
# =====================================================================
# An OBIS code has the form  A-B:C.D.E*F  and arrives as uint64. The 8 bytes are
# [0, 0, A, B, C, D, E, F]. Relevant here:
#     B = channel (0 = sum over all phases)
#     C = quantity; phase part = + 20 * phase  (C=12 voltage sum, C=32 voltage L1, C=52 L2, C=72 L3)
#     D = kind: 4 = momentary value, 8 = energy counter
# The device delivers all values as integers in milli-units (mV, mA, mW, mWh, milli-Hz).

# Base code C (without phase part) -> (quantity key, direction, unit of momentary value, unit of energy counter)
OBIS_QUANTITIES = {
    1:  ("active_power",   "import", "W",   "kWh"),
    2:  ("active_power",   "export", "W",   "kWh"),
    3:  ("reactive_power", "import", "var", "kvarh"),
    4:  ("reactive_power", "export", "var", "kvarh"),
    9:  ("apparent_power", "import", "VA",  "kVAh"),
    10: ("apparent_power", "export", "VA",  "kVAh"),
    11: ("current",        None,     "A",   None),
    12: ("voltage",        None,     "V",   None),
    13: ("power_factor",   None,     "",    None),
    14: ("frequency",      None,     "Hz",  None),
}
QUANTITY_CURRENT, QUANTITY_VOLTAGE, QUANTITY_POWER_FACTOR, QUANTITY_FREQUENCY = 11, 12, 13, 14
ENERGY_KEY_OF_POWER = {"active_power": "active_energy", "reactive_power": "reactive_energy",
                       "apparent_power": "apparent_energy"}      # the counters of a power quantity
OBIS_MODE_ENERGY_COUNTER = 8


def to_signed_value(raw_value):
    """The device also transmits signed values (e.g. power factor) as uint64. Negative numbers arrive
    as 32- or 64-bit two's complement (-16 -> 4294967280 or 2^64-16).
    This converts them back into a negative number."""
    if raw_value >= 1 << 63:
        return raw_value - (1 << 64)
    if (1 << 31) <= raw_value < (1 << 32):
        return raw_value - (1 << 32)
    return raw_value


def interpret_obis_value(obis_code, raw_value):
    """Converts (OBIS code, raw value) into a reading with kind, phase, unit and scaled value.
    The result is a SimpleNamespace with the fields documented at FILTER (kind, phase, channel, obis, ...)."""
    # 1) split the OBIS code into its bytes (least significant byte = F)
    a, b, c, d, e, f = ((obis_code >> (8 * shift)) & 0xFF for shift in (5, 4, 3, 2, 1, 0))
    obis_text = f"{a}-{b}:{c}.{d}.{e}*{f}"

    # 2) C = base quantity + 20 * phase number -> separate both (phase 0 = sum)
    phase_index = c // 20 if c >= 21 else 0
    quantity_code = c - 20 * phase_index
    phase = "Σ" if phase_index == 0 else f"L{phase_index}"
    if quantity_code == QUANTITY_FREQUENCY and phase_index == 0:
        phase = ""                                # grid frequency is not assigned to a phase

    common = dict(obis=obis_text, channel=b, phase=phase, source="obis", raw=raw_value, is_counter=False)
    quantity = OBIS_QUANTITIES.get(quantity_code)
    if quantity is None:                          # unknown code: show the raw value unchanged
        return SimpleNamespace(**common, kind=f"unknown_{c}", unit="", value=raw_value)
    name, direction, momentary_unit, energy_unit = quantity

    # 3) scaling depending on the quantity
    if quantity_code == QUANTITY_POWER_FACTOR:    # signed, raw / 1000
        signed = to_signed_value(raw_value)
        return SimpleNamespace(**{**common, "raw": signed}, kind=name, unit="", value=signed / 1000)
    if quantity_code in (QUANTITY_CURRENT, QUANTITY_VOLTAGE, QUANTITY_FREQUENCY):   # mA, mV, mHz -> A, V, Hz
        return SimpleNamespace(**common, kind=name, unit=momentary_unit, value=raw_value / 1000)
    if d == OBIS_MODE_ENERGY_COUNTER:             # energy counter: mWh -> kWh
        return SimpleNamespace(**{**common, "is_counter": True}, kind=f"{ENERGY_KEY_OF_POWER[name]}_{direction}",
                               unit=energy_unit, value=raw_value / 1e6)

    # Momentary power: mW -> W. "power_group" and "power_sign" are used by merge_power_directions
    # to combine import (+) and export (-).
    return SimpleNamespace(**common, kind=f"{name}_{direction}", unit=momentary_unit, value=raw_value / 1000,
                           power_group=name, power_sign=1 if direction == "import" else -1)


def merge_power_directions(readings):
    """Combines import and export per (device, channel, phase, power type) into ONE signed value:
    import positive, export negative. All other readings stay unchanged."""
    merged, power_groups = [], {}
    for reading in readings:
        group = getattr(reading, "power_group", None)
        if group is None:
            merged.append(reading)
        else:
            power_groups.setdefault((reading.uuid, reading.channel, reading.phase, group), []).append(reading)
    for (uuid, channel, phase, group), members in power_groups.items():
        signed_raw = sum(m.power_sign * m.raw for m in members)
        merged.append(SimpleNamespace(
            device=members[0].device, uuid=uuid, obis=" / ".join(m.obis for m in members),
            channel=channel, phase=phase, source="obis", kind=group, unit=members[0].unit,
            value=signed_raw / 1000, raw=signed_raw, is_counter=False))
    return merged


def readings_from_records(data_records, config_records):
    """Turns the decoded records into a flat list of single readings.
    data_records / config_records: {uuid: record}. The configuration only provides display names and
    flex definitions and may be missing."""
    readings = []
    for uuid, data_record in data_records.items():
        config = config_records.get(uuid)
        device_name = (config and config["label"]) or data_record["id"] or uuid

        for obis_code, raw_value in data_record["obis_values"].items():
            reading = interpret_obis_value(obis_code, raw_value)
            reading.device, reading.uuid = device_name, uuid
            readings.append(reading)

        for name, flex in data_record["flex_values"].items():
            definition = config and config["flex_definitions"].get(name)
            raw = flex["string_value"] if flex["int_value"] is None else flex["int_value"]
            # integer * 10^decimal_power gives the real value (e.g. 215 * 10^-1 = 21.5)
            value = raw * 10 ** definition["decimal_power"] if definition and isinstance(raw, int) else raw
            unit_index = definition["unit"] if definition else 0
            readings.append(SimpleNamespace(
                device=device_name, uuid=uuid, obis="–", channel="", phase="", source="flex", is_counter=False,
                kind=(definition and definition["label"]) or name,
                unit=FLEX_UNIT_NAMES[unit_index] if unit_index < len(FLEX_UNIT_NAMES) else "",
                value=value, raw=raw))
    return merge_power_directions(readings) if NET_POWER else readings


# =====================================================================
#  5. LOGIN + CONNECTIONS
# =====================================================================
TLS_CONTEXT = ssl.create_default_context()
TLS_CONTEXT.check_hostname = False
TLS_CONTEXT.verify_mode = ssl.CERT_NONE        # the device uses a self-signed certificate
WEBSOCKET_SSL_OPTIONS = {"cert_reqs": ssl.CERT_NONE, "check_hostname": False}


class LoginError(Exception):
    """Login rejected (wrong password etc.) - retrying does not help."""


def request_access_token(host, user, password):
    """Logs in to the device (like the web app). Result: (access_token, expires_in or None)."""
    form_body = urllib.parse.urlencode({
        "grant_type": "password", "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
        "username": user, "password": password}).encode()
    request = urllib.request.Request(f"https://{host}/api/web-login/token", data=form_body,
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(request, context=TLS_CONTEXT, timeout=30) as response:
            answer = json.load(response)
    except urllib.error.HTTPError as error:
        if error.code in (400, 401, 403):
            raise LoginError(f"Login failed: HTTP {error.code} (check user/password)")
        raise
    if "error" in answer or "access_token" not in answer:
        raise LoginError("Login failed (check user/password)")
    return answer["access_token"], answer.get("expires_in")


def open_websocket(host, stream_kind, topic, token, timeout=10):
    """Opens the WebSocket. stream_kind: "values" (measurements) or "config" (names/configuration)."""
    ws = websocket.create_connection(
        f"wss://{host}/api/data-transfer/ws/protobuf/gdr/local/{stream_kind}/{topic}",
        origin=f"https://{host}", sslopt=WEBSOCKET_SSL_OPTIONS, timeout=timeout)
    ws.send("Bearer " + token)             # first message = token (like the web app)
    return ws


def receive_binary(ws):
    """Waits for the next binary message (Protobuf) and ignores everything else (e.g. ping/text)."""
    while True:
        opcode, payload = ws.recv_data()
        if opcode == websocket.ABNF.OPCODE_BINARY:
            return payload
        if opcode == websocket.ABNF.OPCODE_CLOSE:
            raise websocket.WebSocketConnectionClosedException()


def fetch_config_via_http(host, topic, token):
    """Smart meter: the configuration comes via HTTP GET (not via WebSocket). -> {uuid: config record}"""
    request = urllib.request.Request(
        f"https://{host}/api/data-transfer/protobuf/gdr/local/config/{topic}",
        headers={"Authorization": "Bearer " + token})
    with urllib.request.urlopen(request, context=TLS_CONTEXT, timeout=10) as response:
        return decode_record_map(response.read(), decode_config_record)


def fetch_su100_sensor_units(host, token):
    """List of the connected SU-100 (as the web app loads it): JSON list with fields such as
    serial (serial number), label, id, device (interface), rtu_address / tcp_address, phase_channel1..3."""
    request = urllib.request.Request(f"https://{host}/api/meter-su100/su100",
                                     headers={"Authorization": "Bearer " + token})
    with urllib.request.urlopen(request, context=TLS_CONTEXT, timeout=10) as response:
        sensor_units = json.load(response)
    return sensor_units if isinstance(sensor_units, list) else []


def format_duration(seconds):
    """Seconds -> "1 h 02 min 05 s" (days only if needed)."""
    seconds = int(round(seconds))
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    if days:
        return f"{days} d {hours} h {minutes:02d} min {seconds:02d} s"
    if hours:
        return f"{hours} h {minutes:02d} min {seconds:02d} s"
    return f"{minutes} min {seconds:02d} s"


def check_token_valid(host, token):
    """Checks via HTTP whether the token is still accepted.
    True = valid, False = rejected (401/403), None = unclear (e.g. network problem)."""
    request = urllib.request.Request(
        f"https://{host}/api/data-transfer/protobuf/gdr/local/config/{TOPIC_SMARTMETER}",
        headers={"Authorization": "Bearer " + token})
    try:
        with urllib.request.urlopen(request, context=TLS_CONTEXT, timeout=10):
            return True
    except urllib.error.HTTPError as error:
        return False if error.code in (401, 403) else None
    except OSError:
        return None


class LoginSession:
    """Holds token + password (in memory only, never on disk), logs in again automatically when the token
    expires and remembers how long each login was valid."""

    def __init__(self, host, user, password):
        self.host, self.user, self._password = host, user, password
        self.lock = threading.Lock()
        self.generation = 0            # counts logins; threads use it to see whether they are already renewed
        self.expired_logins = []       # [(time of expiry, validity in s)]
        self.fatal_error = None        # set when re-login fails for good (e.g. password changed)
        self.expires_in = None         # token lifetime stated by the server (if provided)
        self._sign_in()

    def _sign_in(self):
        self.token, self.expires_in = request_access_token(self.host, self.user, self._password)
        self.login_time = time.time()
        self.first_failure_time = None
        self.failure_count = 0
        self.generation += 1

    def current_credentials(self):
        """-> (generation, token) - threads remember the generation they connected with."""
        with self.lock:
            return self.generation, self.token

    def renew_if_expired(self, failed_generation):
        """Call after a connection loss: checks whether the token expired and logs in again if so."""
        with self.lock:
            if failed_generation != self.generation or self.fatal_error:
                return                                  # another thread was faster
            now = time.time()
            if self.first_failure_time is None:
                self.first_failure_time = now           # first sign of expiry
            self.failure_count += 1
            token_ok = check_token_valid(self.host, self.token)
            if token_ok is None:
                return                                  # device not reachable - try again later
            if token_ok and self.failure_count < 3:
                return                                  # token fine -> just reconnect
            lifetime = self.first_failure_time - self.login_time
            try:
                self._sign_in()
            except LoginError as error:
                self.fatal_error = str(error)
                return
            except OSError:
                return                                  # network problem during login - next attempt follows
            self.expired_logins.append((now, lifetime))
            _LOGGER.info("[login] was valid for %s - logged in again", format_duration(lifetime))

    def status_lines(self):
        """Language-neutral status entries (current login + last expired logins): [(key, params)]."""
        clock = lambda moment: time.strftime("%H:%M:%S", time.localtime(moment))
        with self.lock:
            lines = [("logged_in_since", {"since": clock(self.login_time),
                                          "age": format_duration(time.time() - self.login_time)})]
            if isinstance(self.expires_in, (int, float)):
                lines.append(("server_lifetime", {"lifetime": format_duration(self.expires_in)}))
            for expired_at, lifetime in self.expired_logins[-3:]:
                lines.append(("previous_login", {"lifetime": format_duration(lifetime),
                                                 "expired_at": clock(expired_at)}))
            return lines


class LiveData:
    """Latest data of both sources, thread-safe. Sources: SOURCE_SMARTMETER and SOURCE_SU100."""

    def __init__(self):
        self.lock = threading.Lock()
        self.latest_records = {SOURCE_SMARTMETER: None, SOURCE_SU100: None}   # last received measurement records
        self.config_records = {SOURCE_SMARTMETER: {}, SOURCE_SU100: {}}       # configuration (names etc.)
        self.su100_sensor_units = []                                          # HTTP list of the SU-100 (serial number, ...)
        self.received_first = {SOURCE_SMARTMETER: threading.Event(), SOURCE_SU100: threading.Event()}

    def store(self, source, records):
        with self.lock:
            self.latest_records[source] = records
        self.received_first[source].set()

    def snapshot(self):
        """Consistent copy for display: (latest_records, config_records, su100_sensor_units)."""
        with self.lock:
            return (dict(self.latest_records), {k: dict(v) for k, v in self.config_records.items()},
                    list(self.su100_sensor_units))


def stream_worker(source, session, live_data, stop_event):
    """Thread function: keeps the connection to ONE source open and stores every received message in
    live_data. On connection loss it checks whether the token expired and reconnects."""
    host = session.host
    topic = TOPIC_SMARTMETER if source == SOURCE_SMARTMETER else TOPIC_SU100
    while not stop_event.is_set() and not session.fatal_error:
        generation, token = session.current_credentials()
        try:
            if not live_data.config_records[source]:             # fetch names/configuration once
                try:
                    if source == SOURCE_SMARTMETER:
                        live_data.config_records[source] = fetch_config_via_http(host, topic, token)
                    else:
                        config_ws = open_websocket(host, "config", topic, token, timeout=5)
                        try:
                            live_data.config_records[source] = decode_record_map(receive_binary(config_ws), decode_config_record)
                        finally:
                            config_ws.close()
                except Exception:
                    pass                                         # names are optional

            if source == SOURCE_SU100:                           # serial numbers, unit IDs, ...
                try:
                    live_data.su100_sensor_units = fetch_su100_sensor_units(host, token)
                except Exception:
                    pass                                         # extra info - failure is not critical

            values_ws = open_websocket(host, "values", topic, token)
            while not stop_event.is_set():
                try:
                    live_data.store(source, decode_record_map(receive_binary(values_ws), decode_data_record))
                except websocket.WebSocketTimeoutException:
                    continue                                     # no data right now - keep waiting
        except (websocket.WebSocketException, OSError) as error:
            _LOGGER.warning("[%s] connection lost (%s)", source, error)
            session.renew_if_expired(generation)                 # token expired? -> log in again
            stop_event.wait(1 if session.generation != generation else 5)


# =====================================================================
#  6. TABLE + MAIN PROGRAM
# =====================================================================
def find_sensor_unit(sensor_units, record_id, label):
    """Finds the matching entry of the HTTP list for a GDR ID (the web app also compares via `id`)."""
    for sensor_unit in sensor_units:
        if record_id and sensor_unit.get("id") == record_id:
            return sensor_unit
    for sensor_unit in sensor_units:                      # fallback: same display name
        if label and sensor_unit.get("label") == label:
            return sensor_unit
    return None


def device_type_label(value):
    """Device type as plain text. Arrives either as a number (GCR) or as a name "DEVICE_TYPE_ROOM_KITCHEN"
    (HTTP list)."""
    if isinstance(value, int) and not isinstance(value, bool):
        return DEVICE_TYPE_NAMES[value] if 0 <= value < len(DEVICE_TYPE_NAMES) else str(value)
    return str(value).removeprefix("DEVICE_TYPE_")


# Identification rows (keys; display names live in translations/*.json as entity.sensor.<key>.name)
IDENTITY_ROW_KEYS = ["label", "serial", "unit_id", "type"]


def build_identity(uuid, config_record, sensor_unit, is_su100):
    """Identification of a device as {row key: text}; empty entries are omitted.
      label    GCR display name (fallback: label from the HTTP list)
      serial   HTTP list `serial`; fallback: UUID (the web app shows the UUID as serial number)
      unit_id  HTTP list `rtu_address` (Modbus address / unit ID)
      type     HTTP list `device_type`; fallback: device type from the GCR"""
    config_record, sensor_unit = config_record or {}, sensor_unit or {}
    device_type = sensor_unit.get("device_type") or config_record.get("device_type") or ""
    identity = {
        "label": config_record.get("label") or sensor_unit.get("label") or "",
        "serial": (sensor_unit.get("serial") or uuid) if is_su100 else "",
        "unit_id": sensor_unit.get("rtu_address", ""),
        "type": device_type_label(device_type) if device_type != "" else "",
    }
    return {key: str(text) for key, text in identity.items() if text not in ("", None)}


def readings_per_column(latest_records, config_records, su100_sensor_units):
    """Distributes readings and identification over the table columns.
    -> ({column key: [readings]}, info entries [(key, params)], {column key: {identity row key: text}})"""
    by_column, info_lines = {key: [] for key in COLUMN_KEYS}, []
    identities = {key: {} for key in COLUMN_KEYS}

    smartmeter_records = latest_records[SOURCE_SMARTMETER]
    if smartmeter_records:
        smartmeter_config = config_records[SOURCE_SMARTMETER]
        readings = readings_from_records(smartmeter_records, smartmeter_config)
        for reading in readings:
            reading.column = COLUMN_SMARTMETER
        by_column[COLUMN_SMARTMETER] = readings
        uuid, first = next(iter(smartmeter_records.items()))
        status = STATUS_NAMES[first["status"]] if first["status"] < len(STATUS_NAMES) else first["status"]
        info_lines.append(("smartmeter_status", {"status": status}))
        identities[COLUMN_SMARTMETER] = build_identity(uuid, smartmeter_config.get(uuid), None, is_su100=False)

    su100_records = latest_records[SOURCE_SU100]
    if su100_records:
        su100_config = config_records[SOURCE_SU100]
        device_names = {uuid: (su100_config.get(uuid) or {}).get("label") or rec["id"] or uuid
                        for uuid, rec in su100_records.items()}
        assignment = assign_su100_columns(device_names)
        readings_by_uuid = {}
        for reading in readings_from_records(su100_records, su100_config):
            readings_by_uuid.setdefault(reading.uuid, []).append(reading)
        for column, uuid in assignment.items():
            if uuid:
                for reading in readings_by_uuid.get(uuid, []):
                    reading.column = column
                by_column[column] = readings_by_uuid.get(uuid, [])
                sensor_unit = find_sensor_unit(su100_sensor_units, su100_records[uuid]["id"], device_names[uuid])
                identities[column] = build_identity(uuid, su100_config.get(uuid), sensor_unit, is_su100=True)
        not_shown = [device_names[u] for u in device_names if u not in assignment.values()]
        if not_shown:
            info_lines.append(("not_shown", {"names": ", ".join(not_shown)}))
    return by_column, info_lines, identities


# Row order: kinds in this order (unknown ones last), then phase Σ, L1, L2, L3; energy counters at the end
KIND_SORT_ORDER = [
    "active_power", "active_power_import", "active_power_export",
    "reactive_power", "reactive_power_import", "reactive_power_export",
    "apparent_power", "apparent_power_import", "apparent_power_export",
    "current", "voltage", "power_factor", "frequency",
    "active_energy_import", "active_energy_export", "reactive_energy_import", "reactive_energy_export",
    "apparent_energy_import", "apparent_energy_export",
]
PHASE_SORT_ORDER = {"": 0, "Σ": 0, "L1": 1, "L2": 2, "L3": 3}


