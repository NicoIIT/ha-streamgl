"""Sensor for StreaMGL."""

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_STREAM, DOMAIN
from .util import BaseStreamerEntity, StreaMGL


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    """Entr setup."""
    streamer: StreaMGL = hass.data[DOMAIN][CONF_STREAM][entry.entry_id]
    entities = [RecorderSensor(streamer), StreamingSensor(streamer)]
    async_add_entities(entities, True)


class RecorderSensor(BaseStreamerEntity, BinarySensorEntity):
    """Sensor tracking the recording state of the stream."""

    _attr_device_class = BinarySensorDeviceClass.RUNNING

    def __init__(self, streamer: StreaMGL) -> None:
        super().__init__(streamer, "recorder")
        self._streamer.recorder.add_on_update(self._update)
        self._attr_extra_state_attributes = {}

    async def _update(self) -> None:
        self._attr_extra_state_attributes = dict.fromkeys(self._streamer.recorder.triggers, True)
        await self.async_update_ha_state(force_refresh=True)

    @property
    def is_on(self) -> bool:
        """If the recorder is recording."""
        return len(self._attr_extra_state_attributes) > 0


class StreamingSensor(BaseStreamerEntity, BinarySensorEntity):
    """Sensor tracking the state of the stream."""

    _attr_device_class = BinarySensorDeviceClass.RUNNING

    def __init__(self, streamer: StreaMGL) -> None:
        super().__init__(streamer, "state")
        self._streamer.add_on_update(self._update)
        self._attr_is_on = False

    async def _update(self) -> None:
        self._attr_is_on = self._streamer.streaming
        self._attr_extra_state_attributes = self._streamer.info
        await self.async_update_ha_state(force_refresh=True)
