"""Constants for Stream / motion / Gallery component."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "streamgl"
PLATFORMS: Final = ["binary_sensor", "sensor", "switch"]

CONF_STREAM: Final = "stream"
CONF_ZONES: Final = "zones"
CONF_ALERTS: Final = "alerts"
CONF_CREATE_GO2RTC: Final = "create_go2rtc"
CONF_MAX_LOOKBACK: Final = "max_lookback"

CONF_TYPE_RAW: Final = "raw"
CONF_TYPE_GO2RTC: Final = "go2rtc"

CONF_DEFAULT_RTSP_OPTIONS = {"rtsp_transport": "tcp"}

CONF_STREAM_NAME_REGEX = r"^[\da-z\-_]*$"

CONF_GALLERY: Final = "gallery"
