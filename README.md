# WORK ON GOING - NOT USABLE YET

# Stream, Motion and Gallery 
Low Cost alternative to HA Camera / Motion / Frigate using `pyav` library and `go2rtc` ha integration:
* Define new `stream` instead of Camera directly in `go2rtc` config. Streams can be directly seen using [WebRTC HA custom card](https://github.com/AlexxIT/WebRTC)
* Define Zones and Alerts with basic / low CPU algorithm
* Visualize Alert results in embedded `Gallery` view
* Various sensors containing stream state / Alert status / ...

NOTE: the goal is NOT to replace the excellent Motion / Frigate but to provide a simple alternative component fully integrated in HA and designed to have the lowest possible CPU consumption.

## Why such component?
Well the Camera and Stream integrations in HA have strong limitations:
* The added value of `camera` in  HA is now questionnable since the integration of `go2rtc` and `WebRTC`: a Front End view can be produced very easilly from A LOT of various protocols if directly adding streams in go2rtc.
* The existing `camera` features are incomplete or unstable: start recording without stop, awfull motion detection, onvif very unstable.
* The existing `stream` integration allowing to share streams for different purposes (preload, HLS stream view, Recording) is by design limited by its 'Segment' implementation not compatible with Motion Detection needs.
