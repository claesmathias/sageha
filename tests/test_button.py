"""Tests for the Test Live Activity button."""

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_mock_service,
)

from custom_components.sagecoffee.const import CONF_LIVE_ACTIVITY_TARGETS

from .conftest import MOCK_SERIAL

TEST_TAG = f"sagecoffee_{MOCK_SERIAL}_test"


async def press(hass: HomeAssistant) -> None:
    """Press the Test Live Activity button."""
    entity_id = er.async_get(hass).async_get_entity_id(
        "button", "sagecoffee", f"{MOCK_SERIAL}_test_live_activity"
    )
    assert entity_id
    await hass.services.async_call(
        "button", "press", {ATTR_ENTITY_ID: entity_id}, blocking=True
    )


async def setup(hass: HomeAssistant, entry: MockConfigEntry, targets: list[str]) -> None:
    """Set up the entry with the given Live Activity devices."""
    hass.config_entries.async_update_entry(
        entry, options={CONF_LIVE_ACTIVITY_TARGETS: targets}
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def test_press_plays_test_sequence(
    hass: HomeAssistant, mock_client: MagicMock, config_entry: MockConfigEntry
) -> None:
    """Pressing plays a warm-up, a ready state, and then clears it."""
    calls: list[ServiceCall] = async_mock_service(hass, "notify", "mobile_app_phone")
    await setup(hass, config_entry, ["mobile_app_phone"])

    with (
        patch(
            "custom_components.sagecoffee.live_activity.TEST_STEPS",
            ((0, 25, 40), (0, 70, 78)),
        ),
        patch("custom_components.sagecoffee.live_activity.TEST_READY_DELAY", 0),
        patch("custom_components.sagecoffee.live_activity.TEST_CLEAR_DELAY", 0),
    ):
        await press(hass)
        async with asyncio.timeout(5):
            while len(calls) < 4:
                await asyncio.sleep(0)

    assert [c.data["message"] for c in calls] == [
        "Test · Heating · 40°C of 93°C",
        "Test · Heating · 78°C of 93°C",
        "Test · Ready to brew",
        "clear_notification",
    ]
    assert {c.data["data"]["tag"] for c in calls} == {TEST_TAG}
    assert calls[0].data["data"]["live_update"] is True
    assert calls[1].data["data"]["progress"] == 70
    assert calls[2].data["data"]["progress"] == 100


async def test_press_without_devices_explains_what_to_do(
    hass: HomeAssistant, mock_client: MagicMock, config_entry: MockConfigEntry
) -> None:
    """Without Live Activity devices configured, pressing raises a clear error."""
    await setup(hass, config_entry, [])

    with pytest.raises(ServiceValidationError) as err:
        await press(hass)
    assert err.value.translation_key == "no_live_activity_targets"
