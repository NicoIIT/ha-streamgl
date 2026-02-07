"""Stream, Motion and Gallery."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Final

import voluptuous as vol
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_OPTIONS, EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Event, HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType
from homeassistant.util import dt

from .const import (
    CONF_GALLERY,
    CONF_STREAM,
    CONF_STREAM_NAME_REGEX,
    DOMAIN,
    PLATFORMS,
)
from .gallery import Gallery
from .util import StreaMGL, async_get_all_streams, async_get_streamer, async_register_custom_card, get_server

_LOGGER = logging.getLogger(__name__)

START_RECORDING_SERVICE: Final = "start_recording"
STOP_RECORDING_SERVICE: Final = "stop_recording"
SNAPSHOT_SERVICE: Final = "snapshot"

CONF_STREAMGL: Final = "streamgl"
CONF_TRIGGER: Final = "trigger"
CONF_DURATION: Final = "duration"
CONF_LOOKBACK: Final = "lookback"

VALID_GALLERY = cv.matches_regex(r"^[\da-zA-Z_/]*$")
VALID_STREAM_NAME = cv.matches_regex(CONF_STREAM_NAME_REGEX)
VALID_TRIGGER = cv.slug

DEFAULT_GALLERY_PATH = "medias"

CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Schema(
            {
                vol.Optional(CONF_GALLERY, default=DEFAULT_GALLERY_PATH): VALID_GALLERY,
            }
        )
    },
    extra=vol.ALLOW_EXTRA,
)

START_RECORDING_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_STREAMGL): VALID_STREAM_NAME,
        vol.Optional(CONF_TRIGGER, default="manual"): VALID_TRIGGER,
        vol.Optional(CONF_DURATION, default=30): cv.positive_int,
        vol.Optional(CONF_LOOKBACK, default=0): cv.positive_int,
    }
)

STOP_RECORDING_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_STREAMGL): VALID_STREAM_NAME,
        vol.Optional(CONF_TRIGGER, default="manual"): VALID_TRIGGER,
    }
)

SNAPSHOT_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_STREAMGL): VALID_STREAM_NAME,
        vol.Optional(CONF_TRIGGER, default="manual"): VALID_TRIGGER,
    }
)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the streamgl component."""
    dom_data = hass.data.setdefault(DOMAIN, {})
    dom_data[CONF_OPTIONS] = config.get(DOMAIN, {})
    dom_data[CONF_STREAM] = {}

    # Gallery media services
    dom_data[CONF_GALLERY] = gallery = Gallery(hass, dom_data[CONF_OPTIONS].get(CONF_GALLERY, DEFAULT_GALLERY_PATH))
    await gallery.register()

    # Custom Gallery Front End Card, best effort
    try:
        path = str(Path(__file__).parent.joinpath("www"))
        await hass.http.async_register_static_paths([StaticPathConfig(f"/{DOMAIN}/www", path, True)])
        await async_register_custom_card(hass, "gallery-card.js")
    except Exception as err:
        _LOGGER.warning("Failed to setup Custom Galery Card.", exc_info=err, stack_info=True)

    # streamgl start record / stop record / snapshot services
    async def start_recording(call: ServiceCall) -> ServiceResponse | None:
        streamer = await async_get_streamer(hass, call.data[CONF_STREAMGL])
        await asyncio.wait_for(streamer.wait_for_streaming(), 30.0)
        trigger = call.data[CONF_TRIGGER]
        now = dt.as_local(dt.utcnow())
        tnb_path = await gallery.async_create_media_path(streamer.id, trigger, now, "tnb")
        await streamer.snapper.take(tnb_path, 320)
        clip_path = await gallery.async_create_media_path(streamer.id, trigger, now, "clip")
        await streamer.recorder.start_recording(trigger, clip_path, call.data[CONF_LOOKBACK], call.data[CONF_DURATION])
        if call.return_response:
            return {
                "tnb": await gallery.get_media_url(tnb_path),
                "clip": await gallery.get_media_url(clip_path),
                "date": now.isoformat(sep=" ", timespec="seconds"),
                "file": clip_path.as_posix(),
            }
        return None

    async def stop_recording(call: ServiceCall) -> None:
        streamer = await async_get_streamer(hass, call.data[CONF_STREAMGL])
        await streamer.recorder.stop_recording(call.data[CONF_TRIGGER])

    async def snapshot(call: ServiceCall) -> ServiceResponse | None:
        streamer = await async_get_streamer(hass, call.data[CONF_STREAMGL])
        await asyncio.wait_for(streamer.wait_for_streaming(), 30.0)
        trigger = call.data[CONF_TRIGGER]
        now = dt.as_local(dt.utcnow())
        tnb_path = await gallery.async_create_media_path(streamer.id, trigger, now, "tnb")
        await streamer.snapper.take(tnb_path, 320)
        snap_path = await gallery.async_create_media_path(streamer.id, trigger, now, "snap")
        await streamer.snapper.take(snap_path)
        if call.return_response:
            return {
                "tnb": await gallery.get_media_url(tnb_path),
                "snap": await gallery.get_media_url(snap_path),
                "date": now.isoformat(sep=" ", timespec="seconds"),
                "file": snap_path.as_posix(),
            }
        return None

    hass.services.async_register(DOMAIN, START_RECORDING_SERVICE, start_recording, START_RECORDING_SCHEMA, SupportsResponse.OPTIONAL)
    hass.services.async_register(DOMAIN, STOP_RECORDING_SERVICE, stop_recording, STOP_RECORDING_SCHEMA)
    hass.services.async_register(DOMAIN, SNAPSHOT_SERVICE, snapshot, SNAPSHOT_SCHEMA, SupportsResponse.OPTIONAL)

    # Stop streamers on HA Stop
    async def _async_stop(_: Event) -> None:
        all_streams = (await async_get_all_streams(hass)).values()
        _LOGGER.debug(f"Stopping {len(all_streams)} streamers")
        for streamer in all_streams:
            await streamer.async_final()

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _async_stop)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up from a config entry."""
    _LOGGER.debug(f"Config entry: {entry.data}")
    stream_conf: dict[str, Any] = entry.data[CONF_STREAM]

    # Create the StreaMGL and IMMEDIATLY check if last setup entry, triggering Post Setup
    hass.data[DOMAIN][CONF_STREAM][entry.entry_id] = StreaMGL(hass, stream_conf)
    if len(hass.config_entries.async_entries(DOMAIN, False, False)) == len(hass.data[DOMAIN][CONF_STREAM]):
        await async_post_setup(hass)

    # initialize entities
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_post_setup(hass: HomeAssistant) -> None:
    """Post setup once all entries are setup, or on new entry."""
    all_streams = (await async_get_all_streams(hass)).values()
    g2_owned_streams = {f"{DOMAIN}.{st.id}": src for st in all_streams if (src := await st.get_go2rtc_source()) is not None}
    if g2_owned_streams:
        grest = await get_server(hass)
        await grest.refresh_streams(g2_owned_streams, f"{DOMAIN}.")

    _LOGGER.debug(f"Starting {len(all_streams)} streamers")
    for streamer in all_streams:
        await streamer.async_init()


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    _LOGGER.debug(f"Unloading entry {entry.entry_id}")
    if (stream := (await async_get_all_streams(hass)).pop(entry.entry_id, None)) is not None:
        await stream.async_final()
    await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    return True


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle removal of an entry."""
    _LOGGER.debug(f"Removing entry {entry.entry_id}")
    if (src := await StreaMGL(hass, entry.data[CONF_STREAM]).get_go2rtc_source()) is not None:
        grest = await get_server(hass)
        await grest.streams_del(src)
