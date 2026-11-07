"""Tests for the warm-up Live Activity."""

from collections.abc import Callable, Generator
from datetime import timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    async_fire_time_changed,
    async_mock_service,
)

from custom_components.sagecoffee.const import (
    CONF_LIVE_ACTIVITY,
    CONF_LIVE_ACTIVITY_DISMISS,
    CONF_LIVE_ACTIVITY_TARGETS,
)
from custom_components.sagecoffee.live_activity import (
    MIN_UPDATE_INTERVAL,
    SageCoffeeLiveActivity,
    _warm_up_percent,
)

from .conftest import MOCK_SERIAL

TAG = f"sagecoffee_{MOCK_SERIAL}"


class FakeCoordinator:
    """Just enough of the coordinator for the Live Activity to follow."""

    def __init__(self) -> None:
        self.appliances = [SimpleNamespace(serial_number=MOCK_SERIAL, name="Kitchen")]
        self._states: dict[str, dict[str, Any]] = {}
        self._listeners: list[Callable[[], None]] = []

    def get_state(self, serial: str) -> dict[str, Any] | None:
        return self._states.get(serial)

    def async_add_listener(self, update_callback: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(update_callback)
        return lambda: self._listeners.remove(update_callback)

    def push(self, state: dict[str, Any]) -> None:
        self._states[MOCK_SERIAL] = state
        for listener in list(self._listeners):
            listener()


def machine(state: str, brew: float | None = None, steam: float | None = None) -> dict:
    """A coordinator state for a machine with a 93°C brew target."""
    boilers = []
    if steam is not None:
        boilers.append({"id": "0", "role": "steam", "cur_temp": steam, "temp_sp": 135.0})
    if brew is not None:
        boilers.append({"id": "1", "role": "brew", "cur_temp": brew, "temp_sp": 93.0})
    return {"reported_state": state, "boiler_temps": boilers}


@pytest.fixture
def notify_calls(hass: HomeAssistant) -> list[ServiceCall]:
    """Record calls to the companion app notifier."""
    return async_mock_service(hass, "notify", "mobile_app_phone")


@pytest.fixture
def clock() -> Generator[list[float]]:
    """Control the monotonic clock used for throttling."""
    now = [1000.0]
    with patch(
        "custom_components.sagecoffee.live_activity.time.monotonic",
        side_effect=lambda: now[0],
    ):
        yield now


async def start_activity(
    hass: HomeAssistant, coordinator: FakeCoordinator, dismiss: int = 5
) -> SageCoffeeLiveActivity:
    """Create and start a Live Activity for the fake coordinator."""
    activity = SageCoffeeLiveActivity(
        hass,
        coordinator,
        {
            CONF_LIVE_ACTIVITY: True,
            CONF_LIVE_ACTIVITY_TARGETS: ["mobile_app_phone"],
            CONF_LIVE_ACTIVITY_DISMISS: dismiss,
        },
    )
    activity.async_start()
    await hass.async_block_till_done()
    return activity


@pytest.mark.parametrize(
    ("current", "start", "expected"),
    [(20.0, 20.0, 0), (56.5, 20.0, 50), (93.0, 20.0, 100), (99.0, 20.0, 100), (95.0, 95.0, 100)],
)
def test_warm_up_percent(current: float, start: float, expected: int) -> None:
    """Progress runs from the temperature at the start of warm-up to the target."""
    assert _warm_up_percent(current, 93.0, start) == expected


async def test_warm_up_starts_activity(
    hass: HomeAssistant, notify_calls: list[ServiceCall], clock: list[float]
) -> None:
    """The activity starts, at full priority, when the machine begins warming."""
    coordinator = FakeCoordinator()
    await start_activity(hass, coordinator)

    coordinator.push(machine("warming", brew=20.0))
    await hass.async_block_till_done()

    assert len(notify_calls) == 1
    call = notify_calls[0].data
    assert call["title"] == "Kitchen"
    assert call["message"] == "Heating · 20°C of 93°C"
    assert call["data"]["tag"] == TAG
    assert call["data"]["live_update"] is True
    assert call["data"]["progress"] == 0
    assert call["data"]["progress_max"] == 100
    assert "silent" not in call["data"]


async def test_progress_updates_are_throttled(
    hass: HomeAssistant, notify_calls: list[ServiceCall], clock: list[float]
) -> None:
    """Progress is sent at most every MIN_UPDATE_INTERVAL seconds, quietly."""
    coordinator = FakeCoordinator()
    await start_activity(hass, coordinator)
    coordinator.push(machine("warming", brew=20.0))
    await hass.async_block_till_done()

    clock[0] += MIN_UPDATE_INTERVAL - 1
    coordinator.push(machine("warming", brew=40.0))
    await hass.async_block_till_done()
    assert len(notify_calls) == 1

    clock[0] += 2
    coordinator.push(machine("warming", brew=56.5))
    await hass.async_block_till_done()
    assert len(notify_calls) == 2
    assert notify_calls[1].data["data"]["progress"] == 50
    assert notify_calls[1].data["data"]["silent"] is True

    # Unchanged content isn't re-sent, however long it has been.
    clock[0] += MIN_UPDATE_INTERVAL * 10
    coordinator.push(machine("warming", brew=56.5))
    await hass.async_block_till_done()
    assert len(notify_calls) == 2


async def test_dual_boiler_message_includes_steam(
    hass: HomeAssistant, notify_calls: list[ServiceCall], clock: list[float]
) -> None:
    """On dual boiler machines the steam boiler temperature is shown too."""
    coordinator = FakeCoordinator()
    await start_activity(hass, coordinator)

    coordinator.push(machine("warming", brew=40.0, steam=80.0))
    await hass.async_block_till_done()

    assert notify_calls[0].data["message"] == "Heating · 40°C of 93°C · Steam 80°C"


async def test_missing_temperatures_still_start_activity(
    hass: HomeAssistant, notify_calls: list[ServiceCall], clock: list[float]
) -> None:
    """Without boiler data the activity starts without a progress bar."""
    coordinator = FakeCoordinator()
    await start_activity(hass, coordinator)

    coordinator.push(machine("warming"))
    await hass.async_block_till_done()

    assert notify_calls[0].data["message"] == "Heating up"
    assert "progress" not in notify_calls[0].data["data"]


async def test_ready_then_dismissed_after_delay(
    hass: HomeAssistant, notify_calls: list[ServiceCall], clock: list[float]
) -> None:
    """When ready the activity says so, then ends after the configured delay."""
    coordinator = FakeCoordinator()
    await start_activity(hass, coordinator, dismiss=5)
    coordinator.push(machine("warming", brew=20.0))
    await hass.async_block_till_done()

    coordinator.push(machine("ready", brew=93.0))
    await hass.async_block_till_done()

    assert len(notify_calls) == 2
    ready = notify_calls[1].data
    assert ready["message"] == "Ready to brew"
    assert ready["data"]["progress"] == 100
    assert "silent" not in ready["data"]

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=4))
    await hass.async_block_till_done()
    assert len(notify_calls) == 2

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=6))
    await hass.async_block_till_done()
    assert len(notify_calls) == 3
    assert notify_calls[2].data == {"message": "clear_notification", "data": {"tag": TAG}}


async def test_zero_delay_ends_activity_when_ready(
    hass: HomeAssistant, notify_calls: list[ServiceCall], clock: list[float]
) -> None:
    """A delay of 0 ends the activity as soon as the machine is ready."""
    coordinator = FakeCoordinator()
    await start_activity(hass, coordinator, dismiss=0)
    coordinator.push(machine("warming", brew=20.0))
    coordinator.push(machine("ready", brew=93.0))
    await hass.async_block_till_done()

    assert [c.data["message"] for c in notify_calls] == [
        "Heating · 20°C of 93°C",
        "Ready to brew",
        "clear_notification",
    ]


async def test_sleep_during_warm_up_ends_activity(
    hass: HomeAssistant, notify_calls: list[ServiceCall], clock: list[float]
) -> None:
    """Turning the machine off mid warm-up ends the activity."""
    coordinator = FakeCoordinator()
    await start_activity(hass, coordinator)
    coordinator.push(machine("warming", brew=20.0))
    coordinator.push(machine("asleep", brew=20.0))
    await hass.async_block_till_done()

    assert notify_calls[-1].data["message"] == "clear_notification"

    # Asleep with nothing showing doesn't send anything further.
    coordinator.push(machine("asleep", brew=20.0))
    await hass.async_block_till_done()
    assert len(notify_calls) == 2


async def test_sleep_while_ready_cancels_dismissal(
    hass: HomeAssistant, notify_calls: list[ServiceCall], clock: list[float]
) -> None:
    """Going to sleep while "ready" ends the activity once, not again later."""
    coordinator = FakeCoordinator()
    await start_activity(hass, coordinator, dismiss=5)
    coordinator.push(machine("warming", brew=20.0))
    coordinator.push(machine("ready", brew=93.0))
    coordinator.push(machine("asleep", brew=93.0))
    await hass.async_block_till_done()

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=10))
    await hass.async_block_till_done()

    clears = [c for c in notify_calls if c.data["message"] == "clear_notification"]
    assert len(clears) == 1


async def test_already_ready_does_not_start_activity(
    hass: HomeAssistant, notify_calls: list[ServiceCall], clock: list[float]
) -> None:
    """A machine that is already warm doesn't start an activity."""
    coordinator = FakeCoordinator()
    coordinator._states[MOCK_SERIAL] = machine("ready", brew=93.0)
    await start_activity(hass, coordinator)

    coordinator.push(machine("ready", brew=93.0))
    await hass.async_block_till_done()

    assert notify_calls == []


async def test_warming_at_startup_starts_activity(
    hass: HomeAssistant, notify_calls: list[ServiceCall], clock: list[float]
) -> None:
    """A machine already warming when the integration starts gets an activity."""
    coordinator = FakeCoordinator()
    coordinator._states[MOCK_SERIAL] = machine("warming", brew=50.0)

    await start_activity(hass, coordinator)

    assert len(notify_calls) == 1


async def test_stop_ends_showing_activity(
    hass: HomeAssistant, notify_calls: list[ServiceCall], clock: list[float]
) -> None:
    """Stopping (e.g. unloading the entry) ends any activity that is showing."""
    coordinator = FakeCoordinator()
    activity = await start_activity(hass, coordinator)
    coordinator.push(machine("warming", brew=20.0))
    await hass.async_block_till_done()

    await activity.async_stop()

    assert notify_calls[-1].data["message"] == "clear_notification"
    assert coordinator._listeners == []


async def test_notify_failure_is_logged(
    hass: HomeAssistant, clock: list[float], caplog: pytest.LogCaptureFixture
) -> None:
    """A notifier that no longer exists is logged instead of raising."""
    coordinator = FakeCoordinator()
    await start_activity(hass, coordinator)

    coordinator.push(machine("warming", brew=20.0))
    await hass.async_block_till_done()

    assert "Failed to update the live activity on notify.mobile_app_phone" in caplog.text
