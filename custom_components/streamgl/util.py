"""Utils."""

import asyncio
import logging
from typing import Any, Literal, cast
from urllib.parse import SplitResult, urlsplit

import voluptuous as vol
from aiohttp import ClientError, ClientResponse, ClientTimeout
from homeassistant.components.camera import Camera
from homeassistant.components.camera.const import DOMAIN as CAMERA_DOMAIN
from homeassistant.components.lovelace.const import DOMAIN as LOVELACE_DOMAIN
from homeassistant.components.lovelace.resources import ResourceStorageCollection
from homeassistant.const import CONF_DEVICE_ID, CONF_OPTIONS, CONF_SOURCE, CONF_TYPE
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import DATA_DOMAIN_PLATFORM_ENTITIES
from homeassistant.helpers.singleton import singleton
from homeassistant.util.hass_dict import HassKey
from yarl import URL

from .const import CONF_CREATE_GO2RTC, CONF_DEFAULT_RTSP_OPTIONS, CONF_STREAM, CONF_TYPE_GO2RTC, DOMAIN
from .stream import PacketFramer, PacketRecorder, SnapshotHandler, Streamer

_LOGGER = logging.getLogger(__name__)

WEBRTC_DOMAIN = "webrtc"
try:
    # webrtc may not be included
    from custom_components.webrtc.utils import Server
except ImportError:
    _LOGGER.info("webrtc not available")
    WEBRTC_DOMAIN = None


logging.getLogger("aiohttp").setLevel(logging.DEBUG)


def is_webrtc_camera_installed(hass: HomeAssistant) -> bool:
    """Check if the WebRTC Camera component is installed."""
    if WEBRTC_DOMAIN is None:
        return False
    webrtc_entries = hass.config_entries.async_entries(WEBRTC_DOMAIN, False, False)
    return len(webrtc_entries) > 0


def get_url_redacted(url: str) -> str:
    """Get an url suitable for use in displays, meaning without user / password."""
    pu = urlsplit(url, "", False)
    netloc = f"***:***@{pu.hostname}" if pu.username or pu.password else pu.netloc
    fragments = "***" if pu.fragment else ""
    return SplitResult(pu.scheme, netloc, pu.path, pu.query, fragments).geturl()


def get_cameras(hass: HomeAssistant) -> dict[str, Camera]:
    """Get all the camera entities registered."""
    cam_dict: dict[str, Camera] = {}
    for (platform, _), entities in hass.data.get(DATA_DOMAIN_PLATFORM_ENTITIES, {}).items():
        if platform == CAMERA_DOMAIN:
            for name, camera in entities.items():
                cam_dict[name] = cast("Camera", camera)
    return cam_dict


class StreaMGL(Streamer):
    """Wrapper StreaMGL including Recorder, Snapper and support for go2rtc and rtsp source."""

    def __init__(self, hass: HomeAssistant, conf: dict[str, Any], max_inactivity_seconds: int = 15, max_lookback: int = 10) -> None:
        super().__init__(hass, conf[CONF_DEVICE_ID], max_inactivity_seconds)
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
        via = None
        if self.conf[CONF_CREATE_GO2RTC]:
            via = f"go2rtc: {DOMAIN}.{self.conf[CONF_DEVICE_ID]} - {get_url_redacted(self.conf[CONF_SOURCE])}"
        elif self.conf[CONF_TYPE] == CONF_TYPE_GO2RTC:
            via = f"go2rtc: {self.conf[CONF_SOURCE]}"
        else:
            via = f"direct: {get_url_redacted(self.conf[CONF_SOURCE])}"
        return DeviceInfo(identifiers={(DOMAIN, self.id)}, name=self.id, model=f"id: {self.conf[CONF_DEVICE_ID]}", manufacturer=via)

    async def _get_go2rtc_rtsp(self, go2rtc_name: str) -> tuple[str, dict[str, str]]:
        grest = await get_server(self.hass)
        info = await grest.info()
        return f"rtsp://{info['host'].split(':')[0]}{info['rtsp']['listen']}/{go2rtc_name}", CONF_DEFAULT_RTSP_OPTIONS

    async def get_go2rtc_source(self) -> str | None:
        """Get the generated source to be added to go2rtc if needed."""
        if not self.conf[CONF_CREATE_GO2RTC]:
            return None
        if self.conf[CONF_OPTIONS] and self.conf[CONF_OPTIONS] != CONF_DEFAULT_RTSP_OPTIONS:
            raw_options = " ".join([f"-{nm} {val}" for nm, val in self.conf[CONF_OPTIONS].items()])
            return f"ffmpeg:{self.conf[CONF_SOURCE]}#raw={raw_options}"
        return self.conf[CONF_SOURCE]

    async def async_get_src(self) -> tuple[str, dict[str, str]]:
        """Get the tuple src / src_options."""
        if self.conf[CONF_CREATE_GO2RTC]:
            return await self._get_go2rtc_rtsp(f"{DOMAIN}.{self.conf[CONF_DEVICE_ID]}")
        if self.conf[CONF_TYPE] == CONF_TYPE_GO2RTC:
            return await self._get_go2rtc_rtsp(self.conf[CONF_SOURCE])
        return self.conf.get(CONF_SOURCE, ""), self.conf.get(CONF_OPTIONS, {})

    async def refresh_source(self) -> None:
        """Refresh the webrtc server if needed."""
        if self.conf[CONF_TYPE] == CONF_TYPE_GO2RTC or self.conf[CONF_CREATE_GO2RTC]:
            grest = await get_server(self.hass)
            await grest.restart()


async def async_get_all_streams(hass: HomeAssistant) -> dict[str, StreaMGL]:
    """Help get all streams stored in hass data."""
    return hass.data[DOMAIN][CONF_STREAM] if DOMAIN in hass.data and CONF_STREAM in hass.data[DOMAIN] else {}


async def async_get_streamer(hass: HomeAssistant, device_id: str) -> StreaMGL:
    """Resolve streamer for services."""
    all_streams = (await async_get_all_streams(hass)).values()
    streamers = [s for s in all_streams if s.id == device_id]
    if not streamers:
        msg = f"Invalid streamgl {device_id}"
        raise vol.Invalid(msg)
    return streamers[0]


class UseNotAvailableWebrtcError(Exception):
    """WebRTC Camera not available."""


class WebRtcGo2rtcClient:
    """Client for the webrtc go2rtc referenced server."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize Client."""
        self.hass = hass
        if WEBRTC_DOMAIN is None:
            raise UseNotAvailableWebrtcError
        entry = self.hass.data[WEBRTC_DOMAIN]
        self._server: Server | None = None
        if isinstance(entry, Server):
            self._server = entry
            self._base_url = URL("http://localhost:1984/")
        else:
            self._base_url = URL(entry)

        self._need_avail_check: bool = True
        self._restart_on_going = False

    async def _request(
        self,
        method: Literal["GET", "PUT", "POST", "DELETE", "PATCH"],
        path: str,
        *,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        skip_avail_check: bool = False,
    ) -> ClientResponse:
        """Make a request to the server."""
        if not skip_avail_check and self._need_avail_check:
            await self._wait_available()
        url = self._base_url.with_path(path)
        _LOGGER.debug("%s - %s, data=%s, params=%s", method, get_url_redacted(str(url)), data, params)
        try:
            resp = await async_get_clientsession(self.hass).request(method, url, timeout=ClientTimeout(total=10), params=params, json=data)
        except ClientError as err:
            msg = f"Server communication failure: {err}"
            raise ClientError(msg) from err

        resp.raise_for_status()
        return resp

    async def _wait_available(self) -> None:
        connected = False
        connected_attempts = 0
        while not connected:
            connected_attempts += 1
            try:
                await self._request("GET", "api", skip_avail_check=True)
                connected = True
            except Exception as err:
                if connected_attempts > 5:
                    raise ServerMissingError("Webrtc go2rtc server not available") from err
                _LOGGER.debug("Webrtc go2rtc server not ready, retrying")
                await asyncio.sleep(1.0)
        self._need_avail_check = False
        _LOGGER.debug("Wertc go2rtc server Available")

    async def info(self) -> dict[str, Any]:
        """Get server info."""
        resp = await self._request("GET", "api")
        return await resp.json()

    async def restart(self) -> None:
        """Restart the daemon / reload the webrtc integration."""
        if self._restart_on_going:
            return  # Only one restart at a time
        self._restart_on_going = True
        self._need_avail_check = True
        try:
            # Restart the go2rtc server using the API
            # BUT if there was a ghost one from a previously wrongly cleaned webrtc, it will just exit
            _LOGGER.debug("Trying to restart Go2Rtc server using API.")
            await self._request("POST", "api/restart", skip_avail_check=True)
            await self.info()
            _LOGGER.info("Go2Rtc server restarted OK using API.")
        except Exception as err:
            _LOGGER.warning("Failed to restart Go2Rtc using api service: %s", err)
            # Try to reload webrtc integration to restart a new binary if needed
            try:
                _LOGGER.debug("Trying to restart Go2Rtc server by WebRtc integration reload.")
                entries = self.hass.config_entries.async_entries(WEBRTC_DOMAIN)
                await self.hass.config_entries.async_reload(entries[0].entry_id)
                await self.info()
                _LOGGER.info("Go2Rtc server restarted OK by WebRtc integration reload.")
            except Exception as err:
                _LOGGER.warning("Failed to restart Go2Rtc using WebRtc integration reload: %s", err)
        self._restart_on_going = False

    async def streams_list(self) -> dict[str, Any]:
        """Get the list of defined streams."""
        resp = await self._request("GET", "api/streams")
        return {nm: [st["url"] for st in data["producers"]] for nm, data in (await resp.json()).items()}

    async def streams_add(self, name: str, srcs: str | list[str]) -> bool:
        """Add / Update a stream."""
        src = srcs if isinstance(srcs, list) else [srcs]
        restart_needed = False
        try:
            await self._request("PUT", "api/streams", params={"name": name, "src": src})
        except Exception:
            await self._request("PUT", "api/streams", params={"name": name, "src": ["dumb"]})
            await self._request("PATCH", "api/config", data={"streams": {name: src}})
            restart_needed = True
        return restart_needed

    async def streams_del(self, name: str) -> None:
        """Delete a stream."""
        await self._request("DELETE", "api/streams", params={"src": name})

    async def refresh_streams(self, streams: dict[str, str], delete_non_exists_startswith: str | None = None) -> None:
        """Refresh streams."""
        exist_streams = await self.streams_list()
        _LOGGER.debug(f"Existing streams: {exist_streams}")
        up_streams = {nm: st for nm, st in streams.items() if exist_streams.get(nm, []) != [st]}
        restart_needed = False
        for nm, st in up_streams.items():
            restart_needed |= await self.streams_add(nm, st)
        if delete_non_exists_startswith is not None:
            del_streams = [nm for nm in exist_streams if nm.startswith(delete_non_exists_startswith) and nm not in streams]
            for stream in del_streams:
                await self.streams_del(stream)
        if restart_needed:
            _LOGGER.debug(f"Reloading webrtc after updated streams via config: {up_streams}")
            await self.restart()
            _LOGGER.debug(f"Updated streams: {await self.streams_list()}")


class ServerMissingError(Exception):
    """Server missing Exception."""


@singleton(f"{DOMAIN}/go2rtc_server")
async def get_server(hass: HomeAssistant) -> WebRtcGo2rtcClient:
    """Get and initiate the webrtc referenced go2rtc server."""
    grest = WebRtcGo2rtcClient(hass)
    await grest.info()
    return grest


def get_component_version(hass: HomeAssistant) -> str:
    """Get the component version as defined in the manifest.json."""
    return str(getattr(hass.data[HassKey("integrations")][DOMAIN], "version", 0))


async def async_register_custom_card(hass: HomeAssistant, name: str) -> None:
    """Register Embedded Custom Card."""
    url = f"/{DOMAIN}/www/{name}"
    _LOGGER.debug(f"Registering Custom Card: {url}")

    # register as lovelace resource, with version
    version = get_component_version(hass)
    resources: ResourceStorageCollection = hass.data[HassKey(LOVELACE_DOMAIN)].resources
    await resources.async_get_info()

    url2 = f"{url}?v={version}"
    if (item := next((it for it in resources.async_items() if it.get("url", "").startswith(url)), None)) is not None:
        if item["url"] != url2:
            _LOGGER.debug(f"Updating Lovelace resource to: {url2}")
            await resources.async_update_item(item["id"], {"res_type": "module", "url": url2})
        else:
            _LOGGER.debug(f"Already registered as {item}")
    else:
        _LOGGER.debug(f"Adding Lovelace resource: {url2}")
        await resources.async_create_item({"res_type": "module", "url": url2})
