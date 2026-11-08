"""Live Activity (iOS) / Live Update (Android) for machine warm-up.

Uses the Home Assistant companion app's live notifications: a notification with
``live_update: true`` and a stable ``tag`` starts the activity, later ones with
the same tag update it in place, and ``clear_notification`` ends it. See
https://companion.home-assistant.io/docs/notifications/live-activities
"""

from __future__ import annotations

from collections.abc import Mapping
import asyncio
import logging
import re
import time
from typing import TYPE_CHECKING, Any

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_call_later

from .const import (
    CONF_LIVE_ACTIVITY_DISMISS,
    CONF_LIVE_ACTIVITY_TARGETS,
    DEFAULT_LIVE_ACTIVITY_DISMISS,
    DEFAULT_NAME,
    STATE_READY,
    STATE_WARMING,
)

if TYPE_CHECKING:
    from . import SageCoffeeCoordinator

_LOGGER = logging.getLogger(__name__)

NOTIFY_DOMAIN = "notify"
CLEAR_NOTIFICATION = "clear_notification"

# iOS throttles frequent Live Activity pushes, so progress updates are sent at
# most this often (the start and the ready update are always sent).
MIN_UPDATE_INTERVAL = 30

# Steps of the test sequence: (seconds to wait first, progress, brew temp).
TEST_STEPS = ((0, 25, 40), (5, 70, 78))
TEST_READY_DELAY = 5
TEST_CLEAR_DELAY = 20
TEST_TARGET = 93

ICON_WARMING = "mdi:coffee-maker"
ICON_READY = "mdi:coffee"
COLOR = "#A0522D"


def _tag(serial: str) -> str:
    """Return the notification tag for an appliance (max 64, [A-Za-z0-9_-])."""
    return re.sub(r"[^A-Za-z0-9_-]", "_", f"sagecoffee_{serial}")[:64]


def _boiler(state: Mapping[str, Any], role: str) -> Mapping[str, Any] | None:
    """Return the boiler with the given role ("brew" or "steam")."""
    for boiler in state.get("boiler_temps") or []:
        if boiler.get("role") == role:
            return boiler
    return None


def _warm_up_percent(current: float, target: float, start: float) -> int:
    """Return warm-up progress from the start temperature to the target, 0-100."""
    if target <= start:
        return 100
    percent = (current - start) / (target - start) * 100
    return max(0, min(100, round(percent)))


def _base_data(tag: str) -> dict[str, Any]:
    """Return the data fields shared by every live notification."""
    return {
        "tag": tag,
        "live_update": True,
        "progress_max": 100,
        "progress_bar_direction": "increasing",
        "notification_icon_color": COLOR,
        "color": COLOR,
        "alert_once": True,
    }


async def _async_notify_targets(
    hass: HomeAssistant, targets: list[str], payload: dict[str, Any]
) -> None:
    """Send a payload to every notify target, logging failures."""
    for target in targets:
        try:
            await hass.services.async_call(
                NOTIFY_DOMAIN, target, payload, blocking=True
            )
        except HomeAssistantError as err:
            _LOGGER.warning(
                "Failed to update the live activity on notify.%s: %s", target, err
            )


async def async_run_test(
    hass: HomeAssistant, targets: list[str], serial: str, title: str
) -> None:
    """Play a short simulated warm-up so the Live Activity can be checked.

    Uses its own tag, so it doesn't interfere with a real warm-up.
    """
    tag = _tag(f"{serial}_test")
    for delay, progress, temp in TEST_STEPS:
        await asyncio.sleep(delay)
        data = _base_data(tag)
        data.update(
            progress=progress,
            critical_text=f"{temp}°C",
            notification_icon=ICON_WARMING,
        )
        await _async_notify_targets(
            hass,
            targets,
            {
                "title": title,
                "message": f"Test · Heating · {temp}°C of {TEST_TARGET}°C",
                "data": data,
            },
        )
    await asyncio.sleep(TEST_READY_DELAY)
    data = _base_data(tag)
    data.update(progress=100, critical_text="Ready", notification_icon=ICON_READY)
    await _async_notify_targets(
        hass, targets, {"title": title, "message": "Test · Ready to brew", "data": data}
    )
    await asyncio.sleep(TEST_CLEAR_DELAY)
    await _async_notify_targets(
        hass, targets, {"message": CLEAR_NOTIFICATION, "data": {"tag": tag}}
    )


class SageCoffeeLiveActivity:
    """Mirror each machine's warm-up as a live notification on mobile devices."""

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: SageCoffeeCoordinator,
        options: Mapping[str, Any],
    ) -> None:
        """Initialize from the config entry options."""
        self._hass = hass
        self._coordinator = coordinator
        self._targets: list[str] = list(options.get(CONF_LIVE_ACTIVITY_TARGETS) or [])
        self._dismiss_minutes: int = int(
            options.get(CONF_LIVE_ACTIVITY_DISMISS, DEFAULT_LIVE_ACTIVITY_DISMISS)
        )
        self._phase: dict[str, str] = {}
        self._start_temp: dict[str, float] = {}
        self._last_sent: dict[str, tuple[tuple[Any, ...], float]] = {}
        self._dismiss_timers: dict[str, CALLBACK_TYPE] = {}
        self._remove_listener: CALLBACK_TYPE | None = None

    @callback
    def async_start(self) -> None:
        """Start following coordinator updates."""
        self._remove_listener = self._coordinator.async_add_listener(
            self._handle_coordinator_update
        )
        self._handle_coordinator_update()

    async def async_stop(self) -> None:
        """Stop following updates and end any activities that are showing."""
        if self._remove_listener:
            self._remove_listener()
            self._remove_listener = None
        for cancel in self._dismiss_timers.values():
            cancel()
        self._dismiss_timers.clear()
        # On shutdown the activity is left for the restart to pick up again,
        # since ending and re-starting it uses up the device's start budget.
        if not self._hass.is_stopping:
            for serial in list(self._phase):
                await self._async_clear(serial)
        self._phase.clear()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Advance each appliance's activity based on its reported state."""
        for appliance in self._coordinator.appliances:
            serial = appliance.serial_number
            state = self._coordinator.get_state(serial)
            reported = ((state or {}).get("reported_state") or "").lower()
            phase = self._phase.get(serial)

            if reported == STATE_WARMING:
                if phase != STATE_WARMING:
                    self._cancel_dismiss(serial)
                    brew = _boiler(state, "brew") or {}
                    self._start_temp[serial] = brew.get("cur_temp") or 0
                    self._phase[serial] = STATE_WARMING
                    self._send_update(serial, appliance, state, force=True)
                else:
                    self._send_update(serial, appliance, state)
            elif reported == STATE_READY:
                # Only a warm-up that was being followed turns into "ready";
                # a machine that is already ready doesn't start an activity.
                if phase == STATE_WARMING:
                    self._phase[serial] = STATE_READY
                    self._send_update(serial, appliance, state, force=True)
                    self._schedule_dismiss(serial)
            elif phase is not None:
                self._end(serial)

    def _content(
        self, serial: str, state: Mapping[str, Any]
    ) -> tuple[str, dict[str, Any]]:
        """Build the message and data payload for the current phase."""
        data = _base_data(_tag(serial))

        if self._phase.get(serial) == STATE_READY:
            data.update(
                progress=100,
                critical_text="Ready",
                notification_icon=ICON_READY,
            )
            return "Ready to brew", data

        data["notification_icon"] = ICON_WARMING
        brew = _boiler(state, "brew") or {}
        current, target = brew.get("cur_temp"), brew.get("temp_sp")
        if current is None or target is None:
            return "Heating up", data

        parts = [f"Heating · {current:.0f}°C of {target:.0f}°C"]
        steam = _boiler(state, "steam") or {}
        if steam.get("cur_temp") is not None:
            parts.append(f"Steam {steam['cur_temp']:.0f}°C")
        data.update(
            progress=_warm_up_percent(
                current, target, self._start_temp.get(serial, current)
            ),
            critical_text=f"{current:.0f}°C",
        )
        return " · ".join(parts), data

    @callback
    def _send_update(
        self,
        serial: str,
        appliance: Any,
        state: Mapping[str, Any],
        force: bool = False,
    ) -> None:
        """Send the activity's current content, throttled unless forced."""
        message, data = self._content(serial, state)
        fingerprint = (message, data.get("progress"), data.get("critical_text"))
        now = time.monotonic()
        last = self._last_sent.get(serial)
        if not force and last is not None:
            last_fingerprint, last_time = last
            if fingerprint == last_fingerprint:
                return
            if now - last_time < MIN_UPDATE_INTERVAL:
                return
        if not force:
            # Routine progress updates can be delivered at low priority.
            data["silent"] = True
        self._last_sent[serial] = (fingerprint, now)
        self._hass.async_create_task(
            self._async_notify(
                {
                    "title": appliance.name or DEFAULT_NAME,
                    "message": message,
                    "data": data,
                }
            ),
            eager_start=True,
        )

    @callback
    def _schedule_dismiss(self, serial: str) -> None:
        """End the "ready" activity after the configured delay."""
        self._cancel_dismiss(serial)
        if self._dismiss_minutes <= 0:
            self._end(serial)
            return

        @callback
        def _dismiss(_now: Any) -> None:
            self._dismiss_timers.pop(serial, None)
            if self._phase.get(serial) == STATE_READY:
                self._end(serial)

        self._dismiss_timers[serial] = async_call_later(
            self._hass, self._dismiss_minutes * 60, _dismiss
        )

    @callback
    def _cancel_dismiss(self, serial: str) -> None:
        """Cancel a pending dismissal."""
        if cancel := self._dismiss_timers.pop(serial, None):
            cancel()

    @callback
    def _end(self, serial: str) -> None:
        """End the activity for an appliance."""
        self._cancel_dismiss(serial)
        self._phase.pop(serial, None)
        self._start_temp.pop(serial, None)
        self._last_sent.pop(serial, None)
        self._hass.async_create_task(self._async_clear(serial), eager_start=True)

    async def _async_clear(self, serial: str) -> None:
        """Send the clear message for an appliance's activity."""
        await self._async_notify(
            {"message": CLEAR_NOTIFICATION, "data": {"tag": _tag(serial)}}
        )

    async def _async_notify(self, payload: dict[str, Any]) -> None:
        """Send a payload to every configured notify target."""
        await _async_notify_targets(self._hass, self._targets, payload)
