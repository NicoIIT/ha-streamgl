"""Sensor for StreaMGL."""

import logging
from datetime import datetime, timedelta

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from streamgl.gallery import Gallery

from . import StreaMGL, async_get_all_streams, async_get_gallery
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    """Entr setup."""
    streamer: StreaMGL = (await async_get_all_streams(hass))[entry.entry_id]
    gallery: Gallery = await async_get_gallery(hass)
    size_sensor = GallerySizeSensor(streamer, gallery)
    await size_sensor.compute_init_size()
    entities = [size_sensor]
    async_add_entities(entities, True)


class _BaseStreamerSensor(SensorEntity):
    _attr_has_entity_name = True

    def __init__(self, streamer: StreaMGL, key: str) -> None:
        self._streamer = streamer
        self._attr_device_info: DeviceInfo = streamer.device_info
        self._attr_unique_id: str = f"{DOMAIN}_{streamer.name}_{key}"
        self._attr_translation_key: str = key
        self._attr_available = True


class GallerySizeSensor(_BaseStreamerSensor):
    """Sensor tracking the size of the stream gallery."""

    _attr_device_class = SensorDeviceClass.DATA_SIZE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "B"

    def __init__(self, streamer: StreaMGL, gallery: Gallery) -> None:
        super().__init__(streamer, "size")
        self._gallery = gallery
        self._streamer.recorder.add_on_update(self._update)
        self._streamer.snapper.add_on_update(self._update)
        self._init_timestamp: float | None = None
        self._init_value: dict[str, int] = {}
        self._attr_extra_state_attributes = {}

    async def compute_init_size(self) -> None:
        """Compute an initial size once."""
        self._init_timestamp = (datetime.now() - timedelta(days=1)).timestamp()  # taking yesterday as ref
        self._init_value = await self._gallery.get_stream_gallery_sizes(self._streamer.name, None, self._init_timestamp)
        self._attr_extra_state_attributes = self._init_value.copy()
        await self._update()

    async def _update(self) -> None:
        # Compute only additional sizes since init in order for optimization
        new_sizes = await self._gallery.get_stream_gallery_sizes(self._streamer.name, self._init_timestamp, None)
        for trig, val in new_sizes.items():
            self._attr_extra_state_attributes[trig] = self._init_value.get(trig, 0) + val
        self._attr_native_value = sum(size for size in self._attr_extra_state_attributes.values())
        _LOGGER.debug(f"[{self._streamer.name}] Total: {self._attr_native_value} - Triggers: {self._attr_extra_state_attributes}")
        if self.hass is not None:
            await self.async_update_ha_state(force_refresh=True)
