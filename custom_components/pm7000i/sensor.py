from homeassistant.components.sensor import SensorEntity, SensorDeviceClass, SensorStateClass
from homeassistant.const import UnitOfPower, UnitOfEnergy, UnitOfElectricCurrent, UnitOfElectricPotential, UnitOfFrequency
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.helpers.device_registry import DeviceInfo

# Display names live in translations/<lang>.json (entity.sensor.<key>.name).
KNOWN_KINDS = frozenset({
    "active_power", "reactive_power", "apparent_power",
    "active_power_import", "active_power_export",
    "reactive_power_import", "reactive_power_export",
    "apparent_power_import", "apparent_power_export",
    "active_energy_import", "active_energy_export",
    "reactive_energy_import", "reactive_energy_export",
    "apparent_energy_import", "apparent_energy_export",
    "current", "voltage", "power_factor", "frequency",
})


def _translation(reading):
    """-> (translation_key, placeholders) for a reading; the phase variant is used only if a phase exists."""
    kind, placeholders = reading.kind, {}
    if kind in KNOWN_KINDS:
        key = kind
    elif kind.startswith("unknown_"):
        key, placeholders["code"] = "unknown", kind.removeprefix("unknown_")
    else:                                   # flex value: label defined on the device
        key, placeholders["kind"] = "measurement", kind
    if reading.phase:
        key += "_phase"
        placeholders["phase"] = reading.phase
    return key, placeholders


UNIT_MAP = {
 "W": UnitOfPower.WATT, "var": "var", "VA": "VA",
 "kWh": UnitOfEnergy.KILO_WATT_HOUR, "kvarh": "kvarh", "kVAh": "kVAh",
 "A": UnitOfElectricCurrent.AMPERE, "V": UnitOfElectricPotential.VOLT, "Hz": UnitOfFrequency.HERTZ
}
CLASS_MAP = {"active_energy_import": (SensorDeviceClass.ENERGY, SensorStateClass.TOTAL_INCREASING),
             "active_energy_export": (SensorDeviceClass.ENERGY, SensorStateClass.TOTAL_INCREASING)}

async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = hass.data["pm7000i"][entry.entry_id]
    entities = {}
    def update():
        found=[]
        for source, readings in (coordinator.data or {}).items():
            for r in readings:
                key=(r.uuid, r.kind, str(r.phase), str(r.channel), r.source)
                if key not in entities:
                    entities[key]=MeterSensor(coordinator, entry, r, source, key)
                    found.append(entities[key])
        # Expose device identity fields as diagnostic-style sensors on each device.
        identity_seen = getattr(coordinator, "device_identities", {})
        for uuid, identity in identity_seen.items():
            source = next((src for src, rs in (coordinator.data or {}).items() if any(r.uuid == uuid for r in rs)), "su100")
            for field in ("label", "serial", "unit_id", "type"):
                value = identity.get(field)
                if value in (None, ""):
                    continue
                key = (uuid, "identity", field, "", source)
                if key not in entities:
                    entities[key] = DeviceIdentitySensor(coordinator, entry, uuid, source, field, identity)
                    found.append(entities[key])
        if found: async_add_entities(found)
    update()
    coordinator.async_add_listener(update)

class MeterSensor(CoordinatorEntity, SensorEntity):
    def __init__(self, coordinator, entry, reading, source, key):
        super().__init__(coordinator)
        self.r=reading; self.source=source; self._attr_unique_id=f"{entry.entry_id}_{key[0]}_{key[1]}_{key[2]}_{key[3]}"
        identity = getattr(coordinator, "device_identities", {}).get(reading.uuid, {})
        unit_id = identity.get("unit_id", "") if source == "su100" else ""
        # For SU-100s, use the Modbus Unit ID as the suggested/default device name.
        device_name = unit_id or reading.device
        self._attr_has_entity_name = True
        self._attr_translation_key, self._attr_translation_placeholders = _translation(reading)
        self._attr_native_unit_of_measurement=UNIT_MAP.get(reading.unit, reading.unit or None)
        self._attr_device_info=DeviceInfo(
            identifiers={("pm7000i", reading.uuid)},
            name=device_name,
            manufacturer="PM7000i / Smartmeter",
            configuration_url=f"https://{coordinator.host}",
        )
        if reading.kind in CLASS_MAP:
            self._attr_device_class, self._attr_state_class=CLASS_MAP[reading.kind]
        elif reading.kind=="voltage": self._attr_device_class=SensorDeviceClass.VOLTAGE
        elif reading.kind=="current": self._attr_device_class=SensorDeviceClass.CURRENT
        elif reading.kind=="frequency": self._attr_device_class=SensorDeviceClass.FREQUENCY
        elif reading.kind in ("active_power","reactive_power","apparent_power"):
            self._attr_state_class=SensorStateClass.MEASUREMENT
            if reading.kind=="active_power": self._attr_device_class=SensorDeviceClass.POWER
        else: self._attr_state_class=SensorStateClass.MEASUREMENT
    @property
    def native_value(self):
        return self.coordinator.data[self.source] and next((x.value for x in self.coordinator.data[self.source] if x.uuid==self.r.uuid and x.kind==self.r.kind and x.phase==self.r.phase and x.channel==self.r.channel), None)


class DeviceIdentitySensor(CoordinatorEntity, SensorEntity):
    _attr_entity_category = "diagnostic"

    def __init__(self, coordinator, entry, uuid, source, field, identity):
        super().__init__(coordinator)
        self._uuid, self._source, self._field = uuid, source, field
        self._attr_unique_id = f"{entry.entry_id}_{uuid}_identity_{field}"
        self._attr_translation_key = field
        self._attr_has_entity_name = True
        unit_id = identity.get("unit_id", "") if source == "su100" else ""
        self._attr_device_info = DeviceInfo(
            identifiers={("pm7000i", uuid)},
            name=unit_id or next((r.device for r in (coordinator.data or {}).get(source, []) if r.uuid == uuid), "PM7000i"),
            manufacturer="PM7000i / Smartmeter",
            configuration_url=f"https://{coordinator.host}",
        )

    @property
    def native_value(self):
        return getattr(self.coordinator, "device_identities", {}).get(self._uuid, {}).get(self._field)
