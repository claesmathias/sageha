"""Sensor platform for Sage Coffee integration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging
from typing import Any
import zoneinfo

from sagecoffee.models import BOILER_ROLE_BREW, BOILER_ROLE_STEAM

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import EntityCategory, UnitOfTemperature, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import SageCoffeeConfigEntry, SageCoffeeCoordinator
from .entity import SageCoffeeEntity

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class SageCoffeeSensorEntityDescription(SensorEntityDescription):
    """Describes a Sage Coffee sensor entity."""

    value_fn: Callable[[dict[str, Any]], Any]
    attributes_fn: Callable[[dict[str, Any]], dict[str, Any]] | None = None
    steam_boiler: bool = False


DAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
WEEKDAYS = frozenset(range(5))
WEEKEND = frozenset({5, 6})
MAX_STATE_LENGTH = 255


def _parse_cron(cron: str) -> tuple[int, int, frozenset[int]] | None:
    """Parse the device cron format into (hour, minute, weekdays).

    Expects "MM HH * * DAYS" where DAYS uses 1=Mon through 7=Sun (0 is also
    Sun); weekdays are returned as Python weekdays (0=Mon…6=Sun).
    """
    parts = cron.split()
    if len(parts) != 5:
        return None
    try:
        minute = int(parts[0])
        hour = int(parts[1])
    except ValueError:
        return None
    if not (0 <= hour < 24 and 0 <= minute < 60):
        return None

    days_str = parts[4]
    if days_str == "*":
        return hour, minute, frozenset(range(7))
    allowed: set[int] = set()
    try:
        for part in days_str.split(","):
            if "-" in part:
                start, end = part.split("-", 1)
                for d in range(int(start), int(end) + 1):
                    allowed.add((d - 1) % 7)
            else:
                allowed.add((int(part) - 1) % 7)
    except ValueError:
        return None
    return hour, minute, frozenset(allowed)


def _format_days(days: frozenset[int]) -> str:
    """Describe a set of weekdays, e.g. "Weekdays", "Mon–Thu" or "Mon, Wed"."""
    if len(days) == 7:
        return "Every day"
    if days == WEEKDAYS:
        return "Weekdays"
    if days == WEEKEND:
        return "Weekends"
    ordered = sorted(days)
    if len(ordered) >= 3 and ordered[-1] - ordered[0] == len(ordered) - 1:
        return f"{DAY_NAMES[ordered[0]]}–{DAY_NAMES[ordered[-1]]}"
    return ", ".join(DAY_NAMES[d] for d in ordered)


def _schedule_entries(state: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the wake schedule as readable entries, sorted by time."""
    entries: list[dict[str, Any]] = []
    for entry in state.get("wake_schedule") or []:
        if not isinstance(entry, dict):
            continue
        cron = entry.get("cron")
        if not isinstance(cron, str) or not cron:
            continue
        parsed = _parse_cron(cron)
        if parsed is None:
            entries.append(
                {"time": None, "days": None, "enabled": bool(entry.get("on")), "cron": cron}
            )
            continue
        hour, minute, days = parsed
        entries.append(
            {
                "time": f"{hour:02d}:{minute:02d}",
                "days": _format_days(days),
                "enabled": bool(entry.get("on")),
                "cron": cron,
            }
        )
    entries.sort(key=lambda e: (e["time"] is None, e["time"] or ""))
    return entries


def _parse_cron_next(
    cron: str, after: datetime, tz: zoneinfo.ZoneInfo
) -> datetime | None:
    """Return the next occurrence of a cron schedule after `after`."""
    parsed = _parse_cron(cron)
    if parsed is None:
        return None
    hour, minute, allowed = parsed

    # Walk forward up to 8 days to find the next matching slot
    candidate = after.replace(second=0, microsecond=0)
    for _ in range(8):
        if candidate.weekday() in allowed:
            scheduled = candidate.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if scheduled > after:
                return scheduled
        next_date = (candidate + timedelta(days=1)).date()
        candidate = datetime(next_date.year, next_date.month, next_date.day, 0, 0, 0, tzinfo=tz)

    return None


def _get_next_wake_time(state: dict[str, Any]) -> datetime | None:
    """Return the next scheduled wake time across all enabled schedule entries."""
    schedules = state.get("wake_schedule") or []
    tz_name = state.get("timezone") or "UTC"
    try:
        tz = zoneinfo.ZoneInfo(tz_name)
    except (zoneinfo.ZoneInfoNotFoundError, KeyError):
        tz = zoneinfo.ZoneInfo("UTC")

    now = datetime.now(tz)
    next_wake: datetime | None = None
    for entry in schedules:
        if not isinstance(entry, dict) or not entry.get("on"):
            continue
        cron = entry.get("cron", "")
        if not isinstance(cron, str):
            continue
        dt = _parse_cron_next(cron, now, tz)
        if dt is not None and (next_wake is None or dt < next_wake):
            next_wake = dt

    return next_wake


def _find_boiler(state: dict[str, Any], role: str) -> dict[str, Any] | None:
    """Find a boiler by its role ("brew" or "steam")."""
    for boiler in state.get("boiler_temps") or []:
        if boiler.get("role") == role:
            return boiler
    return None


def _get_boiler_temp(state: dict[str, Any], role: str) -> float | None:
    """Get current temperature for a boiler."""
    boiler = _find_boiler(state, role)
    return boiler.get("cur_temp") if boiler else None


def _get_boiler_target(state: dict[str, Any], role: str) -> float | None:
    """Get target temperature for a boiler."""
    boiler = _find_boiler(state, role)
    return boiler.get("temp_sp") if boiler else None


def _format_temperature_unit(state: dict[str, Any]) -> str | None:
    """Return the configured temperature unit."""
    unit = state.get("temperature_unit")
    if unit == 0:
        return "Celsius"
    if unit == 1:
        return "Fahrenheit"
    if unit is None:
        return None
    return str(unit)


def _format_remote_wake(state: dict[str, Any]) -> str | None:
    """Return whether remote wake is enabled."""
    remote_wake = state.get("remote_wake")
    if remote_wake is True:
        return "enabled"
    if remote_wake is False:
        return "disabled"
    return None


def _get_last_paired(state: dict[str, Any]) -> datetime | None:
    """Return the last paired timestamp from an epoch value."""
    last_paired = state.get("last_paired")
    if last_paired in (None, "", "none"):
        return None
    try:
        timestamp = float(last_paired)
    except (TypeError, ValueError):
        return None
    if timestamp > 10_000_000_000:
        timestamp /= 1000
    return datetime.fromtimestamp(timestamp, tz=timezone.utc)


def _get_errors_count(state: dict[str, Any]) -> int | None:
    """Return the number of reported appliance errors."""
    errors = state.get("errors")
    if isinstance(errors, list):
        return len(errors)
    return None


def _get_errors_attributes(state: dict[str, Any]) -> dict[str, Any]:
    """Return the reported appliance errors (e.g. WaterTankEmpty) as attributes."""
    errors = state.get("errors")
    return {"errors": errors if isinstance(errors, list) else []}


def _format_wake_schedule(state: dict[str, Any]) -> str | None:
    """Summarise the enabled wake times, e.g. "Weekdays 06:30 · Sat 08:00"."""
    entries = _schedule_entries(state)
    if not entries:
        return "none"
    enabled = [e for e in entries if e["enabled"]]
    if not enabled:
        return "off"
    summary = " · ".join(
        f"{e['days']} {e['time']}" if e["time"] else e["cron"] for e in enabled
    )
    if len(summary) > MAX_STATE_LENGTH:
        summary = summary[: MAX_STATE_LENGTH - 1] + "…"
    return summary


def _wake_schedule_attributes(state: dict[str, Any]) -> dict[str, Any]:
    """List every wake schedule, including disabled ones."""
    return {"schedules": _schedule_entries(state)}


SENSOR_DESCRIPTIONS: tuple[SageCoffeeSensorEntityDescription, ...] = (
    SageCoffeeSensorEntityDescription(
        key="state",
        translation_key="machine_state",
        name="State",
        icon="mdi:coffee-maker",
        value_fn=lambda state: state.get("reported_state"),
    ),
    SageCoffeeSensorEntityDescription(
        key="brew_temp",
        translation_key="brew_temperature",
        name="Brew Temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        suggested_display_precision=1,
        value_fn=lambda state: _get_boiler_temp(state, BOILER_ROLE_BREW),
    ),
    SageCoffeeSensorEntityDescription(
        key="brew_target",
        translation_key="brew_target",
        name="Brew Target Temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        suggested_display_precision=1,
        value_fn=lambda state: _get_boiler_target(state, BOILER_ROLE_BREW),
    ),
    SageCoffeeSensorEntityDescription(
        key="steam_temp",
        steam_boiler=True,
        translation_key="steam_temperature",
        name="Steam Temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        suggested_display_precision=1,
        value_fn=lambda state: _get_boiler_temp(state, BOILER_ROLE_STEAM),
    ),
    SageCoffeeSensorEntityDescription(
        key="steam_target",
        steam_boiler=True,
        translation_key="steam_target",
        name="Steam Target Temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        suggested_display_precision=1,
        value_fn=lambda state: _get_boiler_target(state, BOILER_ROLE_STEAM),
    ),
    SageCoffeeSensorEntityDescription(
        key="grind_size",
        translation_key="grind_size",
        name="Grind Size",
        icon="mdi:grain",
        value_fn=lambda state: state.get("grind_size"),
    ),
    SageCoffeeSensorEntityDescription(
        key="auto_off",
        translation_key="auto_off",
        name="Auto-off Time",
        icon="mdi:timer-off",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.MINUTES,
        value_fn=lambda state: state.get("idle_time"),
    ),
    SageCoffeeSensorEntityDescription(
        key="firmware_version",
        translation_key="firmware_version",
        name="Firmware Version",
        icon="mdi:chip",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda state: (state.get("firmware") or {}).get("appVersion"),
    ),
    SageCoffeeSensorEntityDescription(
        key="firmware_mcu0",
        translation_key="firmware_mcu0",
        name="MCU Firmware Version",
        icon="mdi:chip",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda state: (state.get("firmware") or {}).get("mcu0"),
    ),
    SageCoffeeSensorEntityDescription(
        key="firmware_ota_service",
        translation_key="firmware_ota_service",
        name="OTA Service Version",
        icon="mdi:chip",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda state: (state.get("firmware") or {}).get("otaServiceVersion"),
    ),
    SageCoffeeSensorEntityDescription(
        key="firmware_sompro_image",
        translation_key="firmware_sompro_image",
        name="SOM Image Version",
        icon="mdi:chip",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda state: (state.get("firmware") or {}).get("sompro_image"),
    ),
    SageCoffeeSensorEntityDescription(
        key="wake_schedule_next",
        translation_key="wake_schedule_next",
        name="Next Wake Time",
        icon="mdi:alarm",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=_get_next_wake_time,
    ),
    SageCoffeeSensorEntityDescription(
        key="wake_schedule",
        translation_key="wake_schedule",
        name="Wake Schedule",
        icon="mdi:calendar-clock",
        value_fn=_format_wake_schedule,
        attributes_fn=_wake_schedule_attributes,
    ),
    SageCoffeeSensorEntityDescription(
        key="temperature_unit",
        translation_key="temperature_unit",
        name="Temperature Unit",
        icon="mdi:thermometer",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=_format_temperature_unit,
    ),
    SageCoffeeSensorEntityDescription(
        key="timezone",
        translation_key="timezone",
        name="Timezone",
        icon="mdi:map-clock",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda state: state.get("timezone"),
    ),
    SageCoffeeSensorEntityDescription(
        key="remote_wake",
        translation_key="remote_wake",
        name="Remote Wake",
        icon="mdi:power-settings",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=_format_remote_wake,
    ),
    SageCoffeeSensorEntityDescription(
        key="last_paired",
        translation_key="last_paired",
        name="Last Paired",
        icon="mdi:link-variant",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=_get_last_paired,
    ),
    SageCoffeeSensorEntityDescription(
        key="state_report_version",
        translation_key="state_report_version",
        name="State Report Version",
        icon="mdi:counter",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda state: state.get("state_report_version"),
    ),
    SageCoffeeSensorEntityDescription(
        key="errors_count",
        translation_key="errors_count",
        name="Errors Count",
        icon="mdi:alert-circle-outline",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=_get_errors_count,
        attributes_fn=_get_errors_attributes,
    ),
    SageCoffeeSensorEntityDescription(
        key="signal_strength",
        translation_key="signal_strength",
        name="Wi-Fi Signal Strength",
        icon="mdi:wifi",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda state: state.get("signal_strength"),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SageCoffeeConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Sage Coffee sensors."""
    coordinator = entry.runtime_data

    entities: list[SageCoffeeSensor] = []

    for appliance in coordinator.appliances:
        state = coordinator.get_state(appliance.serial_number) or {}
        single_boiler = bool(state.get("single_boiler"))
        for description in SENSOR_DESCRIPTIONS:
            if description.steam_boiler and single_boiler:
                continue
            entities.append(SageCoffeeSensor(coordinator, appliance, description))

    async_add_entities(entities)


class SageCoffeeSensor(SageCoffeeEntity, SensorEntity):
    """Represents a sensor for a Sage Coffee machine."""

    entity_description: SageCoffeeSensorEntityDescription

    def __init__(
        self,
        coordinator: SageCoffeeCoordinator,
        appliance: Any,
        description: SageCoffeeSensorEntityDescription,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, appliance)
        self.entity_description = description
        self._attr_unique_id = f"{self._serial}_{description.key}"

    @property
    def native_value(self) -> Any:
        """Return the sensor value."""
        state = self.coordinator.get_state(self._serial)
        if state is None:
            return None
        return self.entity_description.value_fn(state)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return additional state attributes."""
        attributes_fn = self.entity_description.attributes_fn
        if attributes_fn is None:
            return None
        state = self.coordinator.get_state(self._serial)
        if state is None:
            return None
        return attributes_fn(state)
