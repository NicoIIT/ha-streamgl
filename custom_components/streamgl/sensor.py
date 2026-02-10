"""Sensor for StreaMGL."""

from datetime import datetime, timedelta

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .gallery import Gallery, async_get_gallery
from .util import BaseStreamerEntity, StreaMGL, async_get_all_streams


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    """Entr setup."""
    streamer: StreaMGL = (await async_get_all_streams(hass))[entry.entry_id]
    gallery: Gallery = await async_get_gallery(hass)
    size_sensor = GallerySizeSensor(streamer, gallery)
    await size_sensor.compute_init_size()
    entities = [size_sensor]
    async_add_entities(entities, True)


class GallerySizeSensor(BaseStreamerEntity, SensorEntity):
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
        self._init_value = await self._gallery.get_stream_gallery_sizes(self._streamer.id, None, self._init_timestamp)
        self._attr_extra_state_attributes = self._init_value.copy()
        await self._update()

    async def _update(self) -> None:
        # Compute only additional sizes since init in order for optimization
        new_sizes = await self._gallery.get_stream_gallery_sizes(self._streamer.id, self._init_timestamp, None)
        for trig, val in new_sizes.items():
            self._attr_extra_state_attributes[trig] = self._init_value.get(trig, 0) + val
        self._attr_native_value = sum(size for size in self._attr_extra_state_attributes.values())
        self._streamer.logger.debug(f"Total: {self._attr_native_value} - Triggers: {self._attr_extra_state_attributes}")
        if self.hass is not None:
            await self.async_update_ha_state(force_refresh=True)
