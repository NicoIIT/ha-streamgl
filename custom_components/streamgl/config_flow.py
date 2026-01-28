"""Config flow to configure the StreamGL integration."""

from __future__ import annotations

import logging
from types import MappingProxyType
from typing import Any

from .util import get_base_info

import asyncio
import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import (
    CONF_NAME,
    CONF_IP_ADDRESS,
    CONF_ID,
)
from homeassistant.core import HomeAssistant, callback

from .const import DOMAIN

LOGGER = logging.getLogger(__name__)


@callback
def async_get_schema(
    defaults: dict[str, Any] | MappingProxyType[str, Any], show_name: bool = False
) -> vol.Schema:
    """Return MJPEG IP Camera schema."""
    schema = {
        vol.Required(CONF_IP_ADDRESS, default=defaults.get(CONF_IP_ADDRESS)): str,
    }

    if show_name:
        schema = {
            vol.Required(CONF_NAME, default=defaults.get(CONF_NAME)): str,
            **schema,
        }

    return vol.Schema(schema)


async def async_validate_input(
    hass: HomeAssistant, user_input: dict[str, Any]
) -> tuple[dict[str, str], int, str]:
    """Manage MJPEG UDP Camera options."""
    errors: dict[str, str] = {}

    # Validate IP
    error, port_nb, key = await get_base_info(user_input[CONF_IP_ADDRESS])
    if error:
        LOGGER.error("Cannot retrieve base info from %s - %s" % (user_input[CONF_IP_ADDRESS], error))
        errors = {CONF_IP_ADDRESS: "cannot_connect"}

    return (errors, port_nb, key)


class StreamGLFlowHandler(ConfigFlow, domain=DOMAIN):
    """Config flow for streamgl."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> MJPEGOptionsFlowHandler:
        """Get the options flow for this handler."""
        return MJPEGOptionsFlowHandler()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle a flow initialized by the user."""
        errors: dict[str, str] = {}

        if user_input is not None:
            errors, port_nb, key = await async_validate_input(self.hass, user_input)
            if not errors:
                self._async_abort_entries_match(
                    {CONF_IP_ADDRESS: user_input[CONF_IP_ADDRESS]}
                )

                # Storing data in option, to allow for changing them later
                # using an options flow.
                return self.async_create_entry(
                    title=user_input.get(CONF_NAME, user_input[CONF_IP_ADDRESS]),
                    data={},
                    options={
                        CONF_IP_ADDRESS: user_input[CONF_IP_ADDRESS],
                    },
                )
        else:
            user_input = {}

        return self.async_show_form(
            step_id="user",
            data_schema=async_get_schema(user_input, show_name=True),
            errors=errors,
        )
