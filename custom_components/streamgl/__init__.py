"""Stream, Motion and Gallery."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Final

import voluptuous as vol
from homeassistant.components.http import StaticPathConfig
from homeassistant.components.websocket_api import async_register_command, connection, decorators
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_OPTIONS, EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Event, HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.typing import ConfigType
from homeassistant.util import dt

from .const import (
    CONF_G2_NAME,
    CONF_G2_STREAM,
    CONF_GALLERY,
    CONF_STREAM,
    DOMAIN,
    PLATFORMS,
)
from .gallery import Gallery
from .stream import PacketFramer, PacketRecorder, SnapshotHandler, Streamer
from .util import async_register_custom_card, get_server

_LOGGER = logging.getLogger(__name__)

START_RECORDING_SERVICE: Final = "start_recording"
STOP_RECORDING_SERVICE: Final = "stop_recording"
SNAPSHOT_SERVICE: Final = "snapshot"

CONF_STREAMGL: Final = "streamgl"
CONF_TRIGGER: Final = "trigger"
CONF_DURATION: Final = "duration"
CONF_LOOKBACK: Final = "lookback"

VALID_GALLERY = cv.matches_regex(r"^[\da-zA-Z_/]*$")
VALID_STREAM_NAME = cv.matches_regex(r"^[\da-z\-_]*$")
VALID_TRIGGER = cv.slug

CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Schema(
            {
                vol.Optional(CONF_GALLERY, default="medias"): VALID_GALLERY,
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


class StreaMGL(Streamer):
    """Wrapper StreaMGL including Recorder, Snapper and support for go2rtc and rtsp source."""

    def __init__(self, hass: HomeAssistant, name: str, conf: dict[str, Any], max_inactivity_seconds: int = 15, max_lookback: int = 10) -> None:
        super().__init__(hass, name, max_inactivity_seconds)
        self.conf = conf
        self.recorder: PacketRecorder = PacketRecorder(max_lookback)
        self.add_packet_handler(self.recorder)
        self.framer: PacketFramer = PacketFramer()
        self.add_packet_handler(self.framer)
        self.snapper: SnapshotHandler = SnapshotHandler()
        self.framer.add_frame_handler(self.snapper)

    @property
    def device_info(self) -> DeviceInfo:
        """Return the device info."""
        return DeviceInfo(identifiers={(DOMAIN, self.name)}, name=self.name)

    async def async_get_src(self) -> tuple[str, dict[str, str]]:
        """Get the tuple src / src_options."""
        if (g2_name := self.conf.get(CONF_G2_NAME, "")) != "":
            grest = await get_server(self.hass)
            return await grest.get_rtsp_feed(f"{DOMAIN}.{g2_name}"), {"rtsp_transport": "tcp"}
        return self.conf.get(CONF_STREAM, ""), {"rtsp_transport": "tcp"}

    async def refresh_source(self) -> None:
        """Refresh the webrtc server if needed."""
        if CONF_G2_NAME in self.conf:
            grest = await get_server(self.hass)
            await grest.restart()


async def async_get_all_streams(hass: HomeAssistant) -> dict[str, StreaMGL]:
    """Help get all streams stored in hass data."""
    return hass.data[DOMAIN][CONF_STREAM]


async def async_get_gallery(hass: HomeAssistant) -> Gallery:
    """Help get the Gallery Source from hass data."""
    return hass.data[DOMAIN][CONF_GALLERY]


async def async_get_streamer(hass: HomeAssistant, name: str) -> StreaMGL:
    """Resolve streamer for services."""
    all_streams = (await async_get_all_streams(hass)).values()
    streamers = [s for s in all_streams if s.name == name]
    if not streamers:
        msg = f"Invalid streamgl {name}"
        raise vol.Invalid(msg)
    return streamers[0]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the streamgl component."""
    dom_data = hass.data.setdefault(DOMAIN, {})
    dom_data[CONF_OPTIONS] = config.get(DOMAIN, {})
    dom_data[CONF_STREAM] = {}

    # Gallery media services
    dom_data[CONF_GALLERY] = gallery = Gallery(hass, dom_data[CONF_OPTIONS][CONF_GALLERY])
    await gallery.register()
    async_register_command(hass, websocket_gallery_list)
    async_register_command(hass, websocket_gallery_delete)

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
        tnb_path = await gallery.async_create_media_path(streamer.name, trigger, now, "tnb")
        await streamer.snapper.take(tnb_path, 320)
        clip_path = await gallery.async_create_media_path(streamer.name, trigger, now, "clip")
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
        tnb_path = await gallery.async_create_media_path(streamer.name, trigger, now, "tnb")
        await streamer.snapper.take(tnb_path, 320)
        snap_path = await gallery.async_create_media_path(streamer.name, trigger, now, "snap")
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
    hass.data[DOMAIN][CONF_STREAM][entry.entry_id] = StreaMGL(hass, entry.title, stream_conf)
    if len(hass.config_entries.async_entries(DOMAIN, False, False)) == len(hass.data[DOMAIN][CONF_STREAM]):
        await async_post_setup(hass)

    # initialize entities
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_post_setup(hass: HomeAssistant) -> None:
    """Post setup once all entries are setup, or on new entry."""
    all_streams = (await async_get_all_streams(hass)).values()
    g2_streams = {f"{DOMAIN}.{st.conf[CONF_G2_NAME]}": st.conf[CONF_G2_STREAM] for st in all_streams if CONF_G2_STREAM in st.conf}
    if g2_streams:
        grest = await get_server(hass)
        await grest.refresh_streams(g2_streams)

    _LOGGER.debug(f"Starting {len(all_streams)} streamers")
    for streamer in all_streams:
        await streamer.async_init()


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    _LOGGER.debug(f"Unloading entry {entry.unique_id}")
    await (await async_get_all_streams(hass)).pop(entry.entry_id).async_final()
    await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    return True


@decorators.websocket_command(
    {
        vol.Required("type"): "streamgl/gallery_list",
        vol.Required("streamgl"): VALID_STREAM_NAME,
        vol.Optional("date"): str,
        vol.Optional("triggers", default=[]): vol.All(cv.ensure_list, [VALID_TRIGGER]),
    }
)
@decorators.async_response
async def websocket_gallery_list(hass: HomeAssistant, connection: connection.ActiveConnection, msg: dict[str, Any]) -> None:
    """List elements of the streamgl gallery for the given date and types."""

    def error(atype: str, amsg: str) -> None:
        connection.send_error(msg["id"], atype, amsg)

    try:
        streamer = await async_get_streamer(hass, msg["streamgl"])
    except vol.Invalid as err:
        error("resolve_streamgl_failed", str(err))
        return

    try:
        trigs: list[str] = msg.get("triggers", [])
        if "date" in msg:
            adate = dt.parse_datetime(msg["date"])
            if adate is None:
                error("invalid_date", f"Invalid date: {msg['date']}")
                return
        else:
            adate = dt.utcnow()

        gallery = await async_get_gallery(hass)
        medias = await gallery.async_get_medias(streamer.name, trigs, dt.as_local(adate))

    except Exception as err:
        error("unknown_exception", str(err))
        return

    connection.send_result(msg["id"], medias)


@decorators.websocket_command(
    {
        vol.Required("type"): "streamgl/gallery_delete",
        vol.Required("streamgl"): VALID_STREAM_NAME,
        vol.Required("date"): str,
        vol.Required("trigger"): VALID_TRIGGER,
    }
)
@decorators.async_response
async def websocket_gallery_delete(hass: HomeAssistant, connection: connection.ActiveConnection, msg: dict[str, Any]) -> None:
    """Delete a streamgl gallery item."""

    def error(atype: str, amsg: str) -> None:
        connection.send_error(msg["id"], atype, amsg)

    try:
        streamer = await async_get_streamer(hass, msg["streamgl"])
    except vol.Invalid as err:
        error("resolve_streamgl_failed", str(err))
        return

    try:
        adate = dt.parse_datetime(msg["date"])
        if adate is None:
            error("invalid_date", f"Invalid date: {msg['date']}")
            return

        gallery = await async_get_gallery(hass)
        if not await gallery.async_del_media(streamer.name, msg["trigger"], adate):
            error("no_corresponding_media", "No media corresponding to the criterias.")
            return

    except Exception as err:
        error("unknown_exception", str(err))
        return

    connection.send_result(msg["id"], {})
