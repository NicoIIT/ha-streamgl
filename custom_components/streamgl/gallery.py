"""Gallery Storage."""

from __future__ import annotations

import logging
from collections.abc import Generator
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar, Final

from homeassistant.components.media_player.browse_media import async_process_play_media_url
from homeassistant.components.media_source.local_source import LocalMediaView, LocalSource
from homeassistant.components.media_source.models import MediaSourceItem
from homeassistant.core import HomeAssistant

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


async def create_path(hass: HomeAssistant, path: Path) -> None:
    """Create a Path if it does not exists yet."""

    def _internal_create() -> None:
        Path.mkdir(path, parents=True)

    if not path.exists():
        await hass.loop.run_in_executor(None, _internal_create)


def _safe_iter_path(path: Path) -> Generator[Path] | list:
    return path.iterdir() if path.exists() else []


class Gallery(LocalSource):
    """Gallery."""

    GALLERY_DIR: Final = "gallery"
    TYPE_MAP: ClassVar[dict[str, str]] = {"tnb": "tnb.jpg", "snap": "snap.jpg", "clip": "clip.mp4"}
    EXT_MAP: ClassVar[dict[str, str]] = {v: k for k, v in TYPE_MAP.items()}

    def __init__(self, hass: HomeAssistant, base_path: str) -> None:
        self._base_path: Path = Path(base_path, DOMAIN).absolute()
        super().__init__(hass, DOMAIN, "StreaMGL", {self.GALLERY_DIR: self._base_path.as_posix()}, f"/{DOMAIN}")

    async def register(self) -> None:
        """Create the Gallery path and Register the Gallery Media View."""
        await create_path(self.hass, self._base_path)
        _LOGGER.debug(f"Gallery location: {self._base_path}")
        self.hass.http.register_view(LocalMediaView(self.hass, self))

    async def async_create_media_path(self, streamgl: str, trig: str, adate: datetime, atype: str) -> Path:
        """Get the full Path where to store the media and create its parent dir if it does not exist yet."""
        dir_path = self._base_path.joinpath(streamgl, trig, adate.strftime("%y%m%d"))
        await create_path(self.hass, dir_path)
        return dir_path.joinpath(f"{adate.strftime('%H%M%S')}-{self.TYPE_MAP[atype]}")

    async def get_media_url(self, media_path: Path) -> str:
        """Get an authentified url from a full path."""
        identifier = Path(self.GALLERY_DIR, media_path.relative_to(self._base_path)).as_posix()
        media = await self.async_resolve_media(MediaSourceItem(self.hass, DOMAIN, identifier, None))
        return async_process_play_media_url(self.hass, media.url, allow_relative_url=True)

    async def async_get_medias(self, streamgl: str, trigs: list[str], adate: datetime) -> list[dict[str, Any]]:
        """Get the medias corresponding to the stram / triggers at the given date."""

        str_date = adate.strftime("%y%m%d")

        def _get_file_list() -> list[dict[str, Any]]:
            gallery: dict[str, dict[str, Any]] = {}
            trig_paths = [x for x in _safe_iter_path(self._base_path.joinpath(streamgl)) if not trigs or x.name in trigs]
            for trig_path in trig_paths:
                for f in _safe_iter_path(trig_path.joinpath(str_date)):
                    if len(f.name) < 10 or (file_type := self.EXT_MAP.get(f.name[7:], None)) is None:
                        continue
                    tm = f"{f.name[0:6]}"
                    if tm not in gallery:
                        gallery[tm] = {"name": f.name, "time": tm, "date": str_date, "trigger": trig_path.name, "streamgl": streamgl, "urls": {}}
                    gallery[tm]["urls"][file_type] = f
            return [gallery[x] for x in sorted(gallery.keys())]

        fl = await self.hass.loop.run_in_executor(None, _get_file_list)
        for data in fl:
            for x, f in data["urls"].items():
                data["urls"][x] = await self.get_media_url(f)
        return fl

    async def get_stream_gallery_sizes(self, streamgl: str, st: float | None, et: float | None) -> dict[str, int]:
        """Get the size of the gallery for a given stream, splitted by trigger, filtered by date folder creation start_time / end_time."""

        def _get_sizes() -> dict[str, int]:
            sizes: dict[str, int] = {}
            for trig_path in _safe_iter_path(self._base_path.joinpath(streamgl)):
                trig_size: int = 0
                for date_path in _safe_iter_path(trig_path):
                    mod_time = date_path.stat().st_ctime
                    if (st is None or mod_time >= st) and (et is None or mod_time <= et):
                        trig_size += sum(f.stat().st_size for f in date_path.glob("**/*") if f.is_file())
                sizes[trig_path.name] = trig_size
            return sizes

        return await self.hass.loop.run_in_executor(None, _get_sizes)
