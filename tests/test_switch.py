"""Tests for the power switch."""

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from homeassistant.const import ATTR_ENTITY_ID, STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)
from sagecoffee.models import DeviceState

from custom_components.sagecoffee.switch import PENDING_TIMEOUT

from .conftest import MOCK_SERIAL


def make_state(state: str) -> DeviceState:
    """A state report with the given reported state."""
    return DeviceState(
        raw_data={"reported": {"state": state}},
        serial_number=MOCK_SERIAL,
        version=1,
    )


@pytest.fixture
async def stream(
    hass: HomeAssistant, mock_client: MagicMock, config_entry: MockConfigEntry
) -> asyncio.Queue[DeviceState]:
    """Set up the entry, asleep, with a state stream fed from a queue."""
    queue: asyncio.Queue[DeviceState] = asyncio.Queue()

    async def tail_state():
        while True:
            yield await queue.get()

    mock_client.tail_state = tail_state
    mock_client.get_last_state.return_value = make_state("asleep")
    mock_client.wake = AsyncMock()
    mock_client.sleep = AsyncMock()
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    return queue


def power_entity_id(hass: HomeAssistant) -> str:
    """Return the power switch's entity ID."""
    entity_id = er.async_get(hass).async_get_entity_id(
        "switch", "sagecoffee", f"{MOCK_SERIAL}_power"
    )
    assert entity_id
    return entity_id


async def call(hass: HomeAssistant, service: str) -> None:
    """Call a switch service on the power switch."""
    await hass.services.async_call(
        "switch", service, {ATTR_ENTITY_ID: power_entity_id(hass)}, blocking=True
    )


async def push(
    hass: HomeAssistant, queue: asyncio.Queue[DeviceState], state: str
) -> None:
    """Deliver a state report."""
    await queue.put(make_state(state))
    await hass.async_block_till_done()


async def test_turn_on_wakes_this_appliance(
    hass: HomeAssistant, mock_client: MagicMock, stream: asyncio.Queue[DeviceState]
) -> None:
    """Waking targets this switch's appliance, not the account's first one."""
    await call(hass, "turn_on")
    mock_client.wake.assert_awaited_once_with(MOCK_SERIAL)


async def test_turn_off_sleeps_this_appliance(
    hass: HomeAssistant, mock_client: MagicMock, stream: asyncio.Queue[DeviceState]
) -> None:
    """Sleeping targets this switch's appliance, not the account's first one."""
    await call(hass, "turn_off")
    mock_client.sleep.assert_awaited_once_with(MOCK_SERIAL)


async def test_turn_on_does_not_flip_back_while_waking(
    hass: HomeAssistant, stream: asyncio.Queue[DeviceState]
) -> None:
    """The switch stays on while the machine still reports asleep after waking."""
    entity_id = power_entity_id(hass)
    assert hass.states.get(entity_id).state == STATE_OFF

    await call(hass, "turn_on")
    assert hass.states.get(entity_id).state == STATE_ON

    # The cloud echoes the old state before the machine starts warming.
    await push(hass, stream, "asleep")
    assert hass.states.get(entity_id).state == STATE_ON

    await push(hass, stream, "warming")
    assert hass.states.get(entity_id).state == STATE_ON

    # Once confirmed, the switch follows the machine again.
    await push(hass, stream, "asleep")
    assert hass.states.get(entity_id).state == STATE_OFF


async def test_turn_off_does_not_flip_back(
    hass: HomeAssistant, stream: asyncio.Queue[DeviceState]
) -> None:
    """The switch stays off while the machine still reports ready after sleeping."""
    entity_id = power_entity_id(hass)
    await push(hass, stream, "ready")
    assert hass.states.get(entity_id).state == STATE_ON

    await call(hass, "turn_off")
    await push(hass, stream, "ready")
    assert hass.states.get(entity_id).state == STATE_OFF


async def test_unconfirmed_turn_on_reverts_after_timeout(
    hass: HomeAssistant, stream: asyncio.Queue[DeviceState]
) -> None:
    """If the machine never wakes, the switch shows the real state again."""
    entity_id = power_entity_id(hass)
    await call(hass, "turn_on")
    assert hass.states.get(entity_id).state == STATE_ON

    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(seconds=PENDING_TIMEOUT + 1)
    )
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == STATE_OFF


async def test_failed_wake_does_not_show_on(
    hass: HomeAssistant, mock_client: MagicMock, stream: asyncio.Queue[DeviceState]
) -> None:
    """A wake command that fails leaves the switch off."""
    mock_client.wake.side_effect = RuntimeError("boom")
    with pytest.raises(HomeAssistantError, match="turn on the coffee machine: boom"):
        await call(hass, "turn_on")
    assert hass.states.get(power_entity_id(hass)).state == STATE_OFF


async def test_failed_sleep_does_not_show_off(
    hass: HomeAssistant, mock_client: MagicMock, stream: asyncio.Queue[DeviceState]
) -> None:
    """A sleep command that fails leaves the switch on, with a readable error."""
    await push(hass, stream, "ready")
    mock_client.sleep.side_effect = RuntimeError("boom")
    with pytest.raises(HomeAssistantError, match="turn off the coffee machine: boom"):
        await call(hass, "turn_off")
    assert hass.states.get(power_entity_id(hass)).state == STATE_ON


async def test_turn_off_while_warming(
    hass: HomeAssistant, stream: asyncio.Queue[DeviceState]
) -> None:
    """Turning off mid warm-up stays off, even if one more warming report arrives."""
    entity_id = power_entity_id(hass)
    await push(hass, stream, "warming")
    await call(hass, "turn_off")
    await push(hass, stream, "warming")
    assert hass.states.get(entity_id).state == STATE_OFF
    await push(hass, stream, "asleep")
    assert hass.states.get(entity_id).state == STATE_OFF
