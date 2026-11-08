"""Switch platform for Sage Coffee integration."""

from __future__ import annotations

import logging
import time
from typing import Any

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import SageCoffeeConfigEntry, SageCoffeeCoordinator
from .const import STATE_READY, STATE_WARMING
from .entity import SageCoffeeEntity

_LOGGER = logging.getLogger(__name__)

# How long the switch shows the requested state while waiting for the machine
# to report it. Waking can take a while to be reported over the cloud.
PENDING_TIMEOUT = 60


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SageCoffeeConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Sage Coffee switches."""
    coordinator = entry.runtime_data

    entities = [
        SageCoffeePowerSwitch(coordinator, appliance)
        for appliance in coordinator.appliances
    ]

    async_add_entities(entities)


class SageCoffeePowerSwitch(SageCoffeeEntity, SwitchEntity):
    """Represents the power state of a Sage Coffee machine."""

    _attr_device_class = SwitchDeviceClass.SWITCH
    _attr_translation_key = "power"

    def __init__(
        self,
        coordinator: SageCoffeeCoordinator,
        appliance: Any,
    ) -> None:
        """Initialize the switch."""
        super().__init__(coordinator, appliance)
        self._attr_unique_id = f"{self._serial}_power"
        # The state requested by the last turn on/off, shown until the machine
        # reports it (or the timeout passes), so the toggle doesn't flip back
        # while the cloud catches up.
        self._pending: bool | None = None
        self._pending_until = 0.0
        self._cancel_pending_timeout: CALLBACK_TYPE | None = None

    def _reported_on(self) -> bool | None:
        """Return whether the machine reports being on (ready or warming)."""
        state = self.coordinator.get_state(self._serial)
        if state is None:
            return None
        reported_state = (state.get("reported_state") or "").lower()
        return reported_state in (STATE_READY, STATE_WARMING)

    @property
    def is_on(self) -> bool | None:
        """Return True if the machine is on, or was just asked to turn on."""
        if self._pending is not None and time.monotonic() < self._pending_until:
            return self._pending
        return self._reported_on()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Stop showing the requested state once the machine reports it."""
        if self._pending is not None and self._reported_on() == self._pending:
            self._clear_pending()
        super()._handle_coordinator_update()

    @callback
    def _set_pending(self, on: bool) -> None:
        """Show the requested state until it is reported or times out."""
        self._clear_pending()
        self._pending = on
        self._pending_until = time.monotonic() + PENDING_TIMEOUT

        @callback
        def _timeout(_now: Any) -> None:
            self._cancel_pending_timeout = None
            self._pending = None
            self.async_write_ha_state()

        self._cancel_pending_timeout = async_call_later(
            self.hass, PENDING_TIMEOUT, _timeout
        )
        self.async_write_ha_state()

    @callback
    def _clear_pending(self) -> None:
        """Forget the requested state."""
        self._pending = None
        if self._cancel_pending_timeout:
            self._cancel_pending_timeout()
            self._cancel_pending_timeout = None

    async def async_will_remove_from_hass(self) -> None:
        """Cancel the pending timeout."""
        self._clear_pending()
        await super().async_will_remove_from_hass()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional state attributes."""
        state = self.coordinator.get_state(self._serial)
        if state is None:
            return {}
        return {
            "reported_state": state.get("reported_state"),
            "desired_state": state.get("desired_state"),
        }

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on the coffee machine (wake)."""
        _LOGGER.debug("Waking up coffee machine %s", self._serial)
        try:
            await self.coordinator.client.wake(self._serial)
        except Exception as err:
            raise HomeAssistantError(
                f"Failed to turn on the coffee machine: {err}"
            ) from err
        self._set_pending(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off the coffee machine (sleep)."""
        _LOGGER.debug("Putting coffee machine %s to sleep", self._serial)
        try:
            await self.coordinator.client.sleep(self._serial)
        except Exception as err:
            raise HomeAssistantError(
                f"Failed to turn off the coffee machine: {err}"
            ) from err
        self._set_pending(False)

