# WORK ON GOING - NOT USABLE YET, ONLY PRELIMINARY WORK - TARGET AVAILABILITY FEB 2026

# Stream, Motion and Gallery 
Low Cost alternative to HA Camera / Motion / Frigate using `pyav` library and `go2rtc` ha integration:
* Define new `stream` instead of Camera directly in `go2rtc` config. Streams can be directly seen using [WebRTC HA custom card](https://github.com/AlexxIT/WebRTC)
* Define Zones and Alerts with basic / low CPU algorithm
* Visualize Alert results in embedded `Gallery` view
* Various sensors containing stream state / Alert status / ...

NOTE: the goal is NOT to replace the excellent Motion / Frigate but to provide a simple alternative component fully integrated in HA and designed to have the lowest possible CPU consumption. You will NEVER find here all the features from those compoents: if you want a feature such as AI Detection / Human Detection it cannot be implemented in Low CPU so use Frigate or Motion directly.

## Why such component?
Well the Camera and Stream integrations in HA have strong limitations:
* The added value of `camera` in  HA is now questionnable since the integration of `go2rtc` and `WebRTC`: a Front End view can be produced very easilly from A LOT of various protocols if directly adding streams in go2rtc.
* The existing `camera` features are incomplete or unstable: start recording without stop, awfull motion detection, onvif very unstable (at least with all the camera I tested).
* The existing `stream` integration allowing to share streams for different purposes (preload, HLS stream view, Recording) is by design limited by its 'Segment' implementation not compatible with Motion Detection needs.

## How it is done
The internal implementation is a light / reviewed verion of the base ha `stream` component allowing only to read RTSP streams using `pyav`. If one needs to handle any kind of stream the component can create a `go2rtc` stream and connect to this stream via RTSP. Each stream produced is automatically demuxed with `pyav` producing a `Packet` feed (low CPU operation, it is equivalent to what `go2rtc` does internally, or HA `stream` with `preload` option). There is no need to produce any HLS or whatever streaming protocol as everything is already available in `go2rtc`.

Various plugins can register to this feed:
* `Recording` Plugin running in low CPU with possibility to look back several seconds before (max time to be defined in config), allowing to:
    * Start a recording, with a key and a max time. If another recording is existing with this same key it is stopped automatically.
    * Stop a recording before its max time, with a key
    * Permanent rolling recording
* `Alerting` Plugin analysing the changes in the feed. It needs to fully decode the `Packets` into `Frame` which can be a very costly operation in terms of CPU. It is highly recommended to run it on Low Resolution streams (~320*180, ~6 fps). There is no need to have a High Res stream in order to perform Low Cost Motion Detection. Features:
    * Define Zones and cut them in SubZones
    * Define alerts on those Zones / SubZones (threshold of change on SubZone, duration of the change, number of impacted SubZones) allowing to discard most of the non interesting changes and keep only the more accurrate ones
    * The goal here is to have basic features but also allow direct plugin implementation in HA / to share plugins within the Community

Plugins are sharing the work so that any common operation is only done once and then the CPU cost is the minimum possible.
