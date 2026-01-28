"""Config flow to configure the StreaMGL integration."""

from __future__ import annotations

import logging
from typing import Any, Final

import voluptuous as vol
from homeassistant.config_entries import (
    SOURCE_RECONFIGURE,
    ConfigFlow,
    ConfigFlowResult,
)
from homeassistant.const import CONF_NAME

from .const import (
    CONF_ALERTS,
    CONF_G2_NAME,
    CONF_G2_STREAM,
    CONF_GALLERY,
    CONF_STREAM,
    CONF_ZONES,
    DOMAIN,
)
from .util import WebRtcGo2rtcClient

LOGGER = logging.getLogger(__name__)


ERROR_DESC: Final = {
    "not_implemented": "The feature is not implemented yet",
    "no_go2rtc_stream": "There is no go2rtc stream defined",
}


class StreaMGFlowHandler(ConfigFlow, domain=DOMAIN):
    """Config flow for streamgl."""

    VERSION = 1
    _abort_back: str = ""
    _abort_reason: str = ""
    _data: dict[str, Any] = {}

    async def async_step_abort(self, _: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Throw Abort."""
        return self.async_abort(reason=self._abort_reason)

    async def async_step_back(self, _: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Go back to previous Step."""
        return await getattr(self, f"async_step_{self._abort_back}")()

    async def async_abort_or_back(self, back_step_id: str, reason: str = "not_implemented") -> ConfigFlowResult:
        """Throw Not Implememented or go back."""
        self._abort_back = back_step_id
        self._abort_reason = reason
        return await self.async_step_abort_or_back()

    async def async_step_abort_or_back(self, _user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Go back to previous Step."""
        ph = {"desc": ERROR_DESC.get(self._abort_reason, "Unknown")}
        return self.async_show_menu(step_id="abort_or_back", menu_options=["abort", "back"], description_placeholders=ph)

    async def async_step_user(self, _user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Handle a flow initiated by the user."""
        return self.async_show_menu(step_id="user", menu_options=["add_rtsp", "new_go2rtc", "exist_go2rtc"])

    async def async_step_add_rtsp(self, _: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Add direct RTSP TCP stream Step."""
        return await self.async_abort_or_back("user")

    async def async_step_new_go2rtc(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Add new go2rtc stream Step."""
        errors: dict[str, str] = {}
        self._data = {CONF_STREAM: {}, CONF_GALLERY: {}, CONF_ZONES: {}, CONF_ALERTS: {}}
        if user_input is not None:
            self._data[CONF_STREAM] = user_input
            return await self.async_step_configure()

        data_schema = vol.Schema(
            {
                vol.Required(CONF_G2_NAME, default=self._data[CONF_STREAM].get(CONF_G2_NAME, "")): str,
                vol.Required(CONF_G2_STREAM, default=self._data[CONF_STREAM].get(CONF_G2_STREAM, "")): str,
            }
        )

        return self.async_show_form(step_id="new_go2rtc", data_schema=data_schema, errors=errors)

    async def async_step_exist_go2rtc(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Add existing go2rtc stream Step."""
        errors: dict[str, str] = {}
        if user_input is not None:
            self._data[CONF_STREAM] = user_input
            return await self.async_step_configure()

        # get the available streams in go2rtc
        grest = WebRtcGo2rtcClient(self.hass)
        go2rtc_streams = (await grest.streams_list()).keys()
        if not go2rtc_streams:
            return await self.async_abort_or_back("user", "no_go2rtc_stream")

        data_schema = vol.Schema({vol.Required(CONF_G2_NAME): vol.In(go2rtc_streams)})

        return self.async_show_form(step_id="exist_go2rtc", data_schema=data_schema, errors=errors)

    async def async_step_configure(self, _: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Configure choice Step."""
        opts = ["config_options", "config_zone", "config_alert", "finalize"]
        return self.async_show_menu(step_id="configure", menu_options=opts)

    async def async_step_config_options(self, _: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Configure Options."""
        return await self.async_abort_or_back("configure")

    async def async_step_config_zone(self, _: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Configure Zones."""
        return await self.async_abort_or_back("configure")

    async def async_step_config_alert(self, _: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Configure Alerts."""
        return await self.async_abort_or_back("configure")

    async def async_step_finalize(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Finalize Step."""
        if self.source == SOURCE_RECONFIGURE:
            return self.async_update_reload_and_abort(self._get_reconfigure_entry(), data_updates=self._data)

        if CONF_G2_NAME in self._data[CONF_STREAM]:
            return self.async_create_entry(title=self._data[CONF_STREAM][CONF_G2_NAME], data=self._data)

        if user_input is not None:
            return self.async_create_entry(title=user_input[CONF_NAME], data=self._data)

        return self.async_show_form(step_id="finalize", data_schema=vol.Schema({vol.Required(CONF_NAME): str}))

    async def async_step_reconfigure(self, _: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Reconfigure Step."""
        self._data = {**self._get_reconfigure_entry().data}
        return await self.async_step_configure()
