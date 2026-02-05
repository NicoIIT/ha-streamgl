"""Utils."""

import asyncio
import logging
from collections.abc import Callable, Coroutine
from typing import Any, Literal

from aiohttp import ClientError, ClientResponse, ClientTimeout
from homeassistant.components.lovelace.const import DOMAIN as LOVELACE_DOMAIN
from homeassistant.components.lovelace.resources import ResourceStorageCollection
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.singleton import singleton
from homeassistant.util.hass_dict import HassKey
from yarl import URL

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

try:
    # webrtc may not be included
    from custom_components.webrtc.utils import Server
except ImportError:
    _LOGGER.debug("webrtc not available")

WEBRTC_DOMAIN = "webrtc"

logging.getLogger("aiohttp").setLevel(logging.DEBUG)


class WebRtcGo2rtcClient:
    """Client for the webrtc go2rtc referenced server."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize Client."""
        self.hass = hass
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
        _LOGGER.debug("%s - %s, data=%s, params=%s", method, path, data, params)
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
                if connected_attempts > 10:
                    raise ServerMissingError("Webrtc go2rtc server not available") from err
                _LOGGER.debug("Webrtc go2rtc server not ready, retrying")
                await asyncio.sleep(1.0)
        self._need_avail_check = False
        _LOGGER.debug("Wertc go2rtc server Available")

    async def info(self) -> dict[str, Any]:
        """Get server info."""
        resp = await self._request("GET", "api")
        return await resp.json()

    async def get_rtsp_feed(self, src: str) -> str:
        """Get the RTSP url for a given src."""
        info = await self.info()
        return f"rtsp://{info['host'].split(':')[0]}{info['rtsp']['listen']}/{src}"

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
            await self._request("POST", "api")
            _LOGGER.info("Go2Rtc server restarted OK using API.")
        except Exception as err:
            _LOGGER.warning("Failed to restart Go2Rtc using api service: %s", err)
            # Try to reload webrtc integration to restart a new binary if needed
            try:
                _LOGGER.debug("Trying to restart Go2Rtc server by WebRtc integration reload.")
                entries = self.hass.config_entries.async_entries(WEBRTC_DOMAIN)
                await self.hass.config_entries.async_reload(entries[0].entry_id)
                await self._request("POST", "api")
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

    async def refresh_streams(self, streams: dict[str, str]) -> None:
        """Refresh streams."""
        exist_streams = await self.streams_list()
        _LOGGER.debug(f"Existing streams: {exist_streams}")
        up_streams = {nm: st for nm, st in streams.items() if exist_streams.get(nm, []) != [st]}
        restart_needed = False
        for nm, st in up_streams.items():
            restart_needed |= await self.streams_add(nm, st)
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


class Updatable:
    """Define an updatable class registering callbacks to be called on update."""

    def __init__(self) -> None:
        self._update_clbks: list[Callable[[], Coroutine]] = []

    def add_on_update(self, callback: Callable[[], Coroutine]) -> None:
        """Register the update callback."""
        self._update_clbks.append(callback)

    async def update(self) -> None:
        """To be called by children on update."""
        for clbk in self._update_clbks:
            await clbk()
