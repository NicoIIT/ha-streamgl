"""Stream, Motion and Gallery."""

from __future__ import annotations

from io import BytesIO
import logging
import os

from datetime import datetime
from functools import partial
from typing import Callable, Any
from PIL import Image
import voluptuous as vol
from homeassistant.components.camera import (
    async_handle_record_service,
    Camera, 
    CONF_DURATION, 
    CONF_LOOKBACK, 
    DOMAIN as CAMERA_DOMAIN, 
)
from homeassistant.components.stream import RECORDER_PROVIDER

from homeassistant.core import HassJob, HassJobType, HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.const import CONF_FILENAME
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv, template
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import DATA_DOMAIN_PLATFORM_ENTITIES
from homeassistant.helpers.typing import ConfigType
from homeassistant.helpers.service import entity_service_call

from .const import (
    CONF_GALLERY,
    DOMAIN,
    SERVICE_START_RECORDING,
    SERVICE_STOP_RECORDING,
)

TNB_FACTOR = 0.25

_LOGGER = logging.getLogger(__name__)

START_RECORDING_SCHEMA = VolDictType = {
    vol.Optional(CONF_FILENAME): cv.template,
    vol.Optional(CONF_GALLERY): vol.Coerce(str),
    vol.Optional(CONF_DURATION, default=30): vol.Coerce(int),
    vol.Optional(CONF_LOOKBACK, default=0): vol.Coerce(int),
}

def async_register_camera_entity_service(
    hass: HomeAssistant,
    service_name: str,
    schema,
    func: Callable[..., Any],
) -> None:
    """Help registering a camera entity service."""

    def get_entities() -> dict[str, Entity]:
        cam_entities = {}
        for (et, dom), et_dict in hass.data.get(DATA_DOMAIN_PLATFORM_ENTITIES, {}).items():
            if et == CAMERA_DOMAIN:
                cam_entities.update(et_dict)
        return cam_entities

    hass.services.async_register(
        DOMAIN,
        service_name,
        partial(
            entity_service_call,
            hass,
            get_entities,
            HassJob(func),
            entity_device_classes=None,
            required_features=None,
        ),
        cv.make_entity_service_schema(schema),
        SupportsResponse.NONE,
        job_type=HassJobType.Coroutinefunction,
    )

async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the camera utils component."""

    async_register_camera_entity_service(
        hass,
        SERVICE_START_RECORDING,
        START_RECORDING_SCHEMA,
        async_handle_start_recording_service,
    )

    async_register_camera_entity_service(
        hass,
        SERVICE_STOP_RECORDING,
        {},
        async_handle_stop_recording_service,
    )

    return True

def _set_camera_recording(camera: Camera, recording: bool) -> None:
    camera._attr_is_recording = recording
    camera.async_write_ha_state()

async def _snapshot(camera: Camera, snap_file: str, tnb_file: str) -> None:
    if (stream := await camera.async_create_stream()) is None:
        return
    snap_image = await stream.async_get_image()

    def _write_image(to_file: str, image_data: bytes) -> None:
        """Executor helper to write image."""
        os.makedirs(os.path.dirname(to_file), exist_ok=True)
        with open(to_file, "wb") as img_file:
            img_file.write(image_data)

    try:
        await camera.hass.async_add_executor_job(_write_image, snap_file, snap_image)
        pil_img = Image.open(BytesIO(snap_image))
        pil_tnb_img = pil_img.resize((int(pil_img.size[0] * TNB_FACTOR), int(pil_img.size[1] * TNB_FACTOR)), Image.Resampling.LANCZOS)
        tnb_buf = BytesIO()
        pil_tnb_img.save(tnb_buf, "JPEG", optimize=True)
        await camera.hass.async_add_executor_job(_write_image, tnb_file, tnb_buf.getvalue())
    except OSError as err:
        raise HomeAssistantError(f"Can't write image to file: {err}") from err

async def async_handle_start_recording_service(
    camera: Camera, service_call: ServiceCall
) -> None:
    """Handle stream recording service calls. adapted from async_handle_record_service."""
    if camera.is_recording:
        raise HomeAssistantError(f"{camera.entity_id} is already recording")

    _set_camera_recording(camera, True)

    try:
        if CONF_GALLERY in service_call.data:
            now_time = datetime.now()
            gallery_path = service_call.data[CONF_GALLERY]
            _LOGGER.info("Start snapshot")
            await _snapshot(camera, 
                            f"{gallery_path}/snap/{now_time.strftime("%y%m%d/%H%M%S")}.jpg", 
                            f"{gallery_path}/tnb/{now_time.strftime("%y%m%d/%H%M%S")}.jpg")
            _LOGGER.info("Start recording")
            new_data = {k:v for k,v in service_call.data.items() if k not in [CONF_GALLERY, CONF_FILENAME]}
            new_data[CONF_FILENAME] = template.Template(f"{gallery_path}/clip/{now_time.strftime("%y%m%d/%H%M%S")}.mp4", camera.hass)
            service_call.data = new_data
            await async_handle_record_service(camera, service_call)
        else:
            _LOGGER.info("Start recording")
            await async_handle_record_service(camera, service_call)
        _LOGGER.info("End recording")
    except:
        _set_camera_recording(camera, False)
        raise
    _set_camera_recording(camera, False)


async def async_handle_stop_recording_service(
    camera: Camera, service_call: ServiceCall
) -> None:
    """Handle stop stream recording service calls: stops recording if any is on going else does nothing."""
    if camera.stream is not None and (recorder := camera.stream.outputs().get(RECORDER_PROVIDER)) is not None:
        await camera.stream.remove_provider(recorder)
    _set_camera_recording(camera, False)
