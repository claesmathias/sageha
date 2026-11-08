"""Button platform for Sage Coffee integration."""

from __future__ import annotations

from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import SageCoffeeConfigEntry, SageCoffeeCoordinator
from .const import CONF_LIVE_ACTIVITY_TARGETS, DEFAULT_NAME, DOMAIN
from .entity import SageCoffeeEntity
from .live_activity import async_run_test


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SageCoffeeConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Sage Coffee buttons."""
    coordinator = entry.runtime_data
    async_add_entities(
        SageCoffeeTestLiveActivityButton(coordinator, appliance, entry)
        for appliance in coordinator.appliances
    )


class SageCoffeeTestLiveActivityButton(SageCoffeeEntity, ButtonEntity):
    """Plays a short simulated warm-up on the Live Activity devices."""

    _attr_translation_key = "test_live_activity"
    _attr_icon = "mdi:cellphone-message"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        coordinator: SageCoffeeCoordinator,
        appliance: Any,
        entry: SageCoffeeConfigEntry,
    ) -> None:
        """Initialize the button."""
        super().__init__(coordinator, appliance)
        self._entry = entry
        self._attr_unique_id = f"{self._serial}_test_live_activity"

    @property
    def available(self) -> bool:
        """The test doesn't need the machine, only the companion app."""
        return True

    async def async_press(self) -> None:
        """Start the test sequence in the background."""
        targets = list(self._entry.options.get(CONF_LIVE_ACTIVITY_TARGETS) or [])
        if not targets:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="no_live_activity_targets",
            )
        self._entry.async_create_background_task(
            self.hass,
            async_run_test(
                self.hass,
                targets,
                self._serial,
                self._appliance.name or DEFAULT_NAME,
            ),
            f"sagecoffee test live activity {self._serial}",
        )
