"""Switch for StreaMGL."""

from typing import Any

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .util import BaseStreamerEntity, StreaMGL, async_get_all_streams


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    """Entr setup."""
    streamer: StreaMGL = (await async_get_all_streams(hass))[entry.entry_id]
    entities = [ActivationSwitch(streamer)]
    async_add_entities(entities, True)


class ActivationSwitch(SwitchEntity, BaseStreamerEntity):
    """Switch to activate / deactivate the StreaMGL."""

    _attr_device_class = SwitchDeviceClass.SWITCH

    def __init__(self, streamer: StreaMGL) -> None:
        super().__init__(streamer, "activation")
        self._streamer.add_on_update(self._update)

    async def _update(self) -> None:
        await self.async_update_ha_state(force_refresh=True)

    @property
    def is_on(self) -> bool:
        """If the streamer is activated."""
        return self._streamer.activated

    async def async_added_to_hass(self) -> None:
        """Restore state and state attributes."""
        await super().async_added_to_hass()

        if last_state := await self.async_get_last_state():
            is_activated = last_state.state == "on"
        else:
            # no previous state stored: StreaMGL is activated by default
            is_activated = True

        if is_activated and not self.is_on:
            await self.async_turn_on()
        elif not is_activated and self.is_on:
            await self.async_turn_off()

    async def async_turn_on(self, **_: dict[str, Any]) -> None:
        """Turn the streamer on."""
        await self._streamer.async_init()
        self._streamer.logger.info("Activated")

    async def async_turn_off(self, **_: dict[str, Any]) -> None:
        """Turn the streamer off."""
        await self._streamer.async_final()
        self._streamer.logger.info("De-activated")
