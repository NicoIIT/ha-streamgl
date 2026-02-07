"""Config flow to configure the StreaMGL integration."""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Final

import voluptuous as vol
from homeassistant.config_entries import (
    SOURCE_RECONFIGURE,
    ConfigFlow,
    ConfigFlowResult,
)
from homeassistant.const import CONF_DEVICE_ID, CONF_OPTIONS, CONF_SOURCE, CONF_TYPE
from homeassistant.helpers import selector

from . import StreaMGL, async_get_all_streams
from .const import (
    CONF_ALERTS,
    CONF_CREATE_GO2RTC,
    CONF_DEFAULT_RTSP_OPTIONS,
    CONF_STREAM,
    CONF_STREAM_NAME_REGEX,
    CONF_TYPE_GO2RTC,
    CONF_TYPE_RAW,
    CONF_ZONES,
    DOMAIN,
)
from .util import get_cameras, get_server, is_webrtc_camera_installed

VALID_STREAM_NAME_RE = re.compile(CONF_STREAM_NAME_REGEX)
_LOGGER = logging.getLogger(__name__)


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

    async def _is_valid_name(self, name: str) -> bool:
        exist_names = [st.id for st in (await async_get_all_streams(self.hass)).values()]
        return VALID_STREAM_NAME_RE.match(name) is not None and name not in exist_names

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Handle a flow initiated by the user."""
        # Compute the potential streams that can be used from existing cameras and go2rtc streams, and default
        streams = {"new_stream": {CONF_TYPE: CONF_TYPE_RAW, CONF_DEVICE_ID: "", CONF_SOURCE: "rtsp://", CONF_OPTIONS: CONF_DEFAULT_RTSP_OPTIONS}}
        streams.update(
            {
                name: {
                    CONF_TYPE: CONF_TYPE_RAW,
                    CONF_DEVICE_ID: name.split(".")[-1],
                    CONF_SOURCE: await cam.stream_source(),
                    CONF_OPTIONS: cam.stream_options,
                }
                for name, cam in get_cameras(self.hass).items()
            }
        )
        if is_webrtc_camera_installed(self.hass):
            grest = await get_server(self.hass)
            streams.update(
                {
                    f"go2rtc.{name}": {CONF_TYPE: CONF_TYPE_GO2RTC, CONF_DEVICE_ID: name, CONF_SOURCE: name, CONF_OPTIONS: ""}
                    for name in await grest.streams_list()
                    if await self._is_valid_name(name)
                }
            )
        _LOGGER.debug(streams)

        # Ask user choice
        self._data = {CONF_STREAM: {}, CONF_OPTIONS: {}, CONF_ZONES: {}, CONF_ALERTS: {}}
        if user_input is not None:
            self._data[CONF_STREAM] = streams[user_input["stream_key"]]
            return await self.async_step_stream_options()

        keys_selector = selector.SelectSelectorConfig(translation_key="stream_key", options=list(streams.keys()))
        data_schema = vol.Schema({vol.Required("stream_key"): selector.SelectSelector(keys_selector)})
        return self.async_show_form(step_id="user", data_schema=data_schema, last_step=False)

        # //return self.async_show_menu(step_id="user", menu_options=["add_rtsp", "new_go2rtc", "exist_go2rtc"])

    async def _validate_stream(self, conf: dict[str, Any]) -> dict[str, Any] | None:
        """Create a Stream and try to get info, with best effort closure."""
        info = None
        conf[CONF_DEVICE_ID] = "config_flow_fake_stream_name"
        stream = StreaMGL(self.hass, conf)
        if (src := await stream.get_go2rtc_source()) is not None:
            grest = await get_server(self.hass)
            await grest.refresh_streams({f"{DOMAIN}.{conf[CONF_DEVICE_ID]}": src})
        await stream.async_init()
        try:
            await asyncio.wait_for(stream.wait_for_streaming(), 20.0)
            _, info = stream.info
        except TimeoutError:
            _LOGGER.info("Unable to connect to stream in less than 20s")
            info = None
        await stream.async_final()
        if src:
            grest = await get_server(self.hass)
            await grest.streams_del(f"{DOMAIN}.{conf[CONF_DEVICE_ID]}")
        return info

    async def async_step_stream_options(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Customize stream option."""
        errors: dict[str, str] = {}
        if user_input is not None:
            self._data[CONF_STREAM].update(user_input)
            if await self._is_valid_name(user_input[CONF_DEVICE_ID]):
                if (
                    user_input[CONF_SOURCE].startswith("ffmpeg:")
                    and user_input[CONF_CREATE_GO2RTC]
                    and user_input[CONF_OPTIONS]
                    and user_input[CONF_OPTIONS] != CONF_DEFAULT_RTSP_OPTIONS
                ):
                    errors[CONF_OPTIONS] = "invalid_options"
                elif (info := await self._validate_stream(self._data[CONF_STREAM].copy())) is None:
                    errors["base"] = "stream_failed"
                else:
                    return await self.async_step_stream_sumup({"info": info})
            else:
                errors[CONF_DEVICE_ID] = "invalid_id"

        data_schema = vol.Schema({vol.Required(CONF_DEVICE_ID, default=self._data[CONF_STREAM].get(CONF_DEVICE_ID, "")): str})
        if self._data[CONF_STREAM][CONF_TYPE] == CONF_TYPE_RAW:
            data_schema = data_schema.extend(
                {
                    vol.Required(CONF_SOURCE, default=self._data[CONF_STREAM].get(CONF_SOURCE, "")): str,
                    vol.Optional(CONF_CREATE_GO2RTC, default=True): bool,
                    vol.Optional(CONF_OPTIONS, default=self._data[CONF_STREAM].get(CONF_OPTIONS, "")): selector.ObjectSelector(),
                }
            )

        return self.async_show_form(step_id="stream_options", data_schema=data_schema, errors=errors, last_step=False)

    async def async_step_stream_sumup(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Sum-up the stream params."""
        if user_input is not None:
            if "info" in user_input:
                ph = user_input["info"]
                return self.async_show_form(step_id="stream_sumup", description_placeholders=ph)
        return await self.async_step_finalize()

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

    async def async_step_finalize(self, _: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Finalize Step."""
        if self.source == SOURCE_RECONFIGURE:
            return self.async_update_reload_and_abort(self._get_reconfigure_entry(), data_updates=self._data)

        return self.async_create_entry(title=self._data[CONF_STREAM][CONF_DEVICE_ID], data=self._data)

    async def async_step_reconfigure(self, _: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Reconfigure Step."""
        self._data = {**self._get_reconfigure_entry().data}
        return await self.async_step_configure()
