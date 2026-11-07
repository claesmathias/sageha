"""Config flow for Sage Coffee integration."""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

import httpx
from sagecoffee.auth import DEFAULT_CLIENT_ID, AuthClient, MfaRequiredError
from sagecoffee.models import TokenSet
import voluptuous as vol

from homeassistant.config_entries import (
    SOURCE_REAUTH,
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.data_entry_flow import AbortFlow
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.helpers.httpx_client import get_async_client
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import (
    BRAND_OPTIONS,
    CONF_BRAND,
    CONF_LIVE_ACTIVITY,
    CONF_LIVE_ACTIVITY_DISMISS,
    CONF_LIVE_ACTIVITY_TARGETS,
    CONF_REFRESH_TOKEN,
    DEFAULT_LIVE_ACTIVITY_DISMISS,
    DOMAIN,
    MACHINE_TYPE_SAGE,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): TextSelector(
            TextSelectorConfig(type=TextSelectorType.EMAIL)
        ),
        vol.Required(CONF_PASSWORD): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD)
        ),
        vol.Required(CONF_BRAND): SelectSelector(
            SelectSelectorConfig(
                options=BRAND_OPTIONS
            )
        ),
    }
)

STEP_TOKEN_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_REFRESH_TOKEN): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD)
        ),
        vol.Required(CONF_BRAND): SelectSelector(
            SelectSelectorConfig(
                options=BRAND_OPTIONS
            )
        ),
    }
)

CONF_OTP = "otp"

STEP_MFA_DATA_SCHEMA = vol.Schema({vol.Required(CONF_OTP): str})

class SageCoffeeConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Sage Coffee."""

    VERSION = 2

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> SageCoffeeOptionsFlow:
        """Return the options flow."""
        return SageCoffeeOptionsFlow()

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._refresh_token: str | None = None
        self._mfa_token: str | None = None
        self._mfa_brand: str | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step - choose auth method."""
        return self.async_show_menu(
            step_id="user",
            menu_options=["password", "token"],
        )

    async def async_step_password(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle username/password authentication."""
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                auth_client = AuthClient(client_id=DEFAULT_CLIENT_ID)
                tokens = await auth_client.password_realm_login(
                    user_input[CONF_USERNAME],
                    user_input[CONF_PASSWORD],
                )
                return await self._async_finish_password_login(
                    tokens, user_input[CONF_BRAND]
                )

            except AbortFlow:
                raise
            except MfaRequiredError as err:
                self._mfa_token = err.mfa_token
                self._mfa_brand = user_input[CONF_BRAND]
                return await self.async_step_mfa()
            except Exception as err:
                _LOGGER.exception("Authentication failed: %s", err)
                errors["base"] = "invalid_auth"

        return self.async_show_form(
            step_id="password",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )

    async def async_step_mfa(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the one-time code for accounts with MFA enabled."""
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                auth_client = AuthClient(
                    client_id=DEFAULT_CLIENT_ID,
                    http_client=get_async_client(self.hass),
                )
                tokens = await auth_client.mfa_otp_login(
                    self._mfa_token, user_input[CONF_OTP]
                )
                return await self._async_finish_password_login(
                    tokens, self._mfa_brand
                )
            except AbortFlow:
                raise
            except httpx.HTTPStatusError as err:
                _LOGGER.debug("MFA code rejected: %s", err)
                errors["base"] = "invalid_mfa_code"
            except Exception as err:
                _LOGGER.exception("MFA verification failed: %s", err)
                errors["base"] = "unknown"

        return self.async_show_form(
            step_id="mfa",
            data_schema=STEP_MFA_DATA_SCHEMA,
            errors=errors,
        )

    async def _async_finish_password_login(
        self, tokens: TokenSet, brand: str
    ) -> ConfigFlowResult:
        """Create or update the entry once a password login has succeeded."""
        if self.source == SOURCE_REAUTH:
            return self.async_update_reload_and_abort(
                self._get_reauth_entry(),
                data={
                    CONF_REFRESH_TOKEN: tokens.refresh_token,
                    CONF_BRAND: brand,
                },
            )

        self._refresh_token = tokens.refresh_token

        # Use auth0 subject as unique ID to prevent duplicates
        unique_id = tokens.auth0_sub()
        if unique_id:
            await self.async_set_unique_id(unique_id)
            self._abort_if_unique_id_configured()

        return self.async_create_entry(
            title="Sage Coffee",
            data={
                CONF_REFRESH_TOKEN: self._refresh_token,
                CONF_BRAND: brand,
            },
        )

    async def async_step_token(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle refresh token authentication."""
        errors: dict[str, str] = {}

        if user_input is not None:
            refresh_token = user_input[CONF_REFRESH_TOKEN]

            try:
                # Validate the token by trying to refresh it
                auth_client = AuthClient(client_id=DEFAULT_CLIENT_ID)
                tokens = await auth_client.refresh(refresh_token)

                # Use the potentially rotated token
                self._refresh_token = tokens.refresh_token or refresh_token

                # Use auth0 subject as unique ID to prevent duplicates
                unique_id = tokens.auth0_sub()
                if unique_id:
                    await self.async_set_unique_id(unique_id)
                    self._abort_if_unique_id_configured()

                return self.async_create_entry(
                    title="Sage Coffee",
                    data={
                        CONF_REFRESH_TOKEN: self._refresh_token,
                        CONF_BRAND: user_input[CONF_BRAND],
                    },
                )

            except AbortFlow:
                raise
            except Exception as err:
                _LOGGER.exception("Token validation failed: %s", err)
                errors["base"] = "invalid_auth"

        return self.async_show_form(
            step_id="token",
            data_schema=STEP_TOKEN_DATA_SCHEMA,
            errors=errors,
            description_placeholders={
                "bootstrap_command": "sagectl bootstrap --username your.email@example.com"
            },
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Handle re-authentication."""
        return self.async_show_menu(
            step_id="reauth",
            menu_options=["reauth_password", "reauth_token"],
        )

    async def async_step_reauth_password(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle re-authentication via email and password."""
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                auth_client = AuthClient(client_id=DEFAULT_CLIENT_ID)
                tokens = await auth_client.password_realm_login(
                    user_input[CONF_USERNAME],
                    user_input[CONF_PASSWORD],
                )
                return await self._async_finish_password_login(
                    tokens, user_input[CONF_BRAND]
                )
            except AbortFlow:
                raise
            except MfaRequiredError as err:
                self._mfa_token = err.mfa_token
                self._mfa_brand = user_input[CONF_BRAND]
                return await self.async_step_mfa()
            except Exception as err:
                _LOGGER.exception("Re-authentication failed: %s", err)
                errors["base"] = "invalid_auth"

        entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
        existing_brand = (
            entry.data.get(CONF_BRAND) if entry else None
        ) or MACHINE_TYPE_SAGE

        return self.async_show_form(
            step_id="reauth_password",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_USERNAME): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.EMAIL)
                    ),
                    vol.Required(CONF_PASSWORD): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.PASSWORD)
                    ),
                    vol.Required(CONF_BRAND, default=existing_brand): SelectSelector(
                        SelectSelectorConfig(
                            options=BRAND_OPTIONS
                        )
                    ),
                }
            ),
            errors=errors,
        )

    async def async_step_reauth_token(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle re-authentication via refresh token."""
        errors: dict[str, str] = {}

        if user_input is not None:
            refresh_token = user_input[CONF_REFRESH_TOKEN]
            try:
                auth_client = AuthClient(client_id=DEFAULT_CLIENT_ID)
                tokens = await auth_client.refresh(refresh_token)
                entry = self.hass.config_entries.async_get_entry(
                    self.context["entry_id"]
                )
                return self.async_update_reload_and_abort(
                    entry,
                    data={
                        CONF_REFRESH_TOKEN: tokens.refresh_token or refresh_token,
                        CONF_BRAND: user_input[CONF_BRAND],
                    },
                )
            except AbortFlow:
                raise
            except Exception as err:
                _LOGGER.exception("Re-authentication failed: %s", err)
                errors["base"] = "invalid_auth"

        entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
        existing_brand = (
            entry.data.get(CONF_BRAND) if entry else None
        ) or MACHINE_TYPE_SAGE

        return self.async_show_form(
            step_id="reauth_token",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_REFRESH_TOKEN): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.PASSWORD)
                    ),
                    vol.Required(CONF_BRAND, default=existing_brand): SelectSelector(
                        SelectSelectorConfig(
                            options=BRAND_OPTIONS
                        )
                    ),
                }
            ),
            errors=errors,
            description_placeholders={
                "bootstrap_command": "sagectl bootstrap --username your.email@example.com"
            },
        )


class SageCoffeeOptionsFlow(OptionsFlowWithReload):
    """Handle Sage Coffee options (Live Activity)."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Configure the warm-up Live Activity."""
        errors: dict[str, str] = {}

        if user_input is not None:
            if user_input[CONF_LIVE_ACTIVITY] and not user_input.get(
                CONF_LIVE_ACTIVITY_TARGETS
            ):
                errors[CONF_LIVE_ACTIVITY_TARGETS] = "no_targets"
            else:
                return self.async_create_entry(
                    data={
                        **user_input,
                        CONF_LIVE_ACTIVITY_DISMISS: int(
                            user_input[CONF_LIVE_ACTIVITY_DISMISS]
                        ),
                    }
                )

        # Live Activities are a companion app feature, so offer its notifiers
        mobile_targets = sorted(
            service
            for service in self.hass.services.async_services_for_domain("notify")
            if service.startswith("mobile_app_")
        )
        current = {**self.config_entry.options, **(user_input or {})}
        # Keep targets that are configured but currently unavailable selectable
        targets = sorted(
            set(mobile_targets) | set(current.get(CONF_LIVE_ACTIVITY_TARGETS) or [])
        )

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_LIVE_ACTIVITY,
                        default=current.get(CONF_LIVE_ACTIVITY, False),
                    ): BooleanSelector(),
                    vol.Optional(
                        CONF_LIVE_ACTIVITY_TARGETS,
                        default=current.get(CONF_LIVE_ACTIVITY_TARGETS) or [],
                    ): SelectSelector(
                        SelectSelectorConfig(options=targets, multiple=True)
                    ),
                    vol.Required(
                        CONF_LIVE_ACTIVITY_DISMISS,
                        default=current.get(
                            CONF_LIVE_ACTIVITY_DISMISS, DEFAULT_LIVE_ACTIVITY_DISMISS
                        ),
                    ): NumberSelector(
                        NumberSelectorConfig(
                            min=0,
                            max=60,
                            step=1,
                            mode=NumberSelectorMode.BOX,
                            unit_of_measurement="min",
                        )
                    ),
                }
            ),
            errors=errors,
        )
