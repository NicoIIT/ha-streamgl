"""StreaMGL main handler."""

import asyncio
import datetime
import logging
from collections.abc import Callable, Coroutine, MutableMapping
from dataclasses import dataclass, fields
from io import BytesIO
from pathlib import Path
from typing import Any, Self, cast

import av
import av.container
import av.error
import av.stream
import numpy as np
from homeassistant.core import CALLBACK_TYPE, HomeAssistant
from homeassistant.helpers.event import async_call_later

_LOGGER = logging.getLogger(__name__)


def is_supported_codec(container_format: str, codec_name: str) -> bool:
    """Check if a given container type supports a given codec."""
    return codec_name in av.open(BytesIO(), "w", format=container_format).supported_codecs


class _StreamLoggingAdapter(logging.LoggerAdapter):
    def process(self, msg: str, kwargs: MutableMapping[str, Any]) -> tuple[str, MutableMapping[str, Any]]:
        return (f"[{self.extra['name']}] {msg}", kwargs) if self.extra is not None else (msg, kwargs)


@dataclass
class _TimePacket:
    end: datetime.datetime
    pkt: av.Packet
    is_first_video_keyframe: bool


class Updatable:
    """Define an updatable class registering callbacks to be called on update."""

    def __init__(self) -> None:
        self._update_clbks: list[Callable[[], Coroutine]] = []

    def add_on_update(self, callback: Callable[[], Coroutine]) -> None:
        """Register the update callback."""
        self._update_clbks.append(callback)

    def reset_updatable(self) -> None:
        """Reset the callbacks."""
        self._update_clbks.clear()

    async def update(self) -> None:
        """To be called by children on update."""
        for clbk in self._update_clbks:
            await clbk()


class DataClassLazyInit:
    """Base class defning a function to lazy init a @dataclass."""

    @classmethod
    def create_from_dict(cls, dict_: dict[str, Any]) -> Self:
        """Lazy init dataclass from dictionnary with extra keys."""
        class_fields = {f.name for f in fields(cls)}  # type: ignore [usage]
        return cls(**{k: v for k, v in dict_.items() if k in class_fields})


@dataclass
class StreamerOptions(DataClassLazyInit):
    """Options for the Streamer."""

    open_to: int = 20
    read_to: int = 10
    max_retry: int = 20
    with_audio: bool = True
    audio_transcoding: bool = True


class Streamer(Updatable):
    """Handle the Stream."""

    def __init__(self, hass: HomeAssistant, device_id: str, options: StreamerOptions) -> None:
        Updatable.__init__(self)
        self.hass = hass
        self.id: str = device_id
        self.logger = _StreamLoggingAdapter(_LOGGER, {"name": self.id})
        self.options = options

        self._packet_handlers: list[PacketHandler] = []
        self._con: av.container.InputContainer | None = None
        self._permanent_read_task: asyncio.Task | None = None

        self._start_streaming_event: asyncio.Event = asyncio.Event()
        self._deactivation_event: asyncio.Event = asyncio.Event()
        self._deactivation_event.set()  # deactivated by default. Activation by async_init()

        self._streaming: bool = False
        self._info: dict[str, Any] = {}
        self._act_lock: asyncio.Lock = asyncio.Lock()

        self._src: str = ""
        self._src_options: dict[str, str] = {}

    @property
    def activated(self) -> bool:
        """Get to know if the stream is activated."""
        return not self._deactivation_event.is_set()

    @property
    def streaming(self) -> bool:
        """Get to know if the stream is streaming."""
        return self._streaming

    @property
    def info(self) -> dict[str, Any]:
        """Get the info of the stream."""
        return self._info

    async def _set_streaming(self, streaming: bool) -> None:
        if streaming:
            self._start_streaming_event.set()
        else:
            self._start_streaming_event.clear()
        if streaming != self._streaming:
            self.logger.info(f"{'Streaming started' if streaming else 'Streaming stopped'}")
        self._streaming = streaming
        await self.update()

    def add_packet_handler(self, ph) -> None:  # noqa: ANN001
        """Register a Packet handler."""
        ph.set_parent(self)
        self._packet_handlers.append(ph)

    async def async_get_src(self) -> tuple[str, dict[str, str]]:
        """Get the tuple src / src_options."""
        raise NotImplementedError

    async def async_init(self) -> None:
        """Init, ensuring the process is done only once."""
        async with self._act_lock:
            if not self.activated:
                self._deactivation_event.clear()  # Activate
                await self.update()
                self._src, self._src_options = await self.async_get_src()
                self._permanent_read_task = asyncio.create_task(self._permanent_read())

    async def wait_for_streaming_start(self) -> None:
        """Wait for the stream to be effectively streaming."""
        await self._start_streaming_event.wait()

    async def async_final(self) -> None:
        """Finalize."""
        async with self._act_lock:
            if self.activated:
                for ph in self._packet_handlers:
                    await ph.close()
                self._deactivation_event.set()
                await self.update()

    async def _permanent_read(self) -> None:
        retry_nb: int = 0
        while not self._deactivation_event.is_set() and (self.options.max_retry == 0 or retry_nb < self.options.max_retry):
            read_task: asyncio.Future | None = None
            async with self._act_lock:  # Protect from closure while acquiring connection
                retry_nb += 1
                self.logger.debug(f"Opening source, attempt {retry_nb}/{self.options.max_retry}")
                self._con = await self.hass.loop.run_in_executor(None, self._open)
                if self._con is not None:
                    retry_nb = 0
                    read_task = self.hass.loop.run_in_executor(None, self._read, self.hass.loop)
            wait_deactivation = asyncio.create_task(self._deactivation_event.wait())
            if read_task is not None:
                # Connected, wait for either a disconnection (completion of read_task) or a deactivation
                await asyncio.wait([wait_deactivation, read_task], return_when=asyncio.FIRST_COMPLETED)
                self._start_streaming_event.clear()
                self.logger.info("Streaming stopped")
                self._streaming = False
                await self.update()
            else:
                # Not connected, wait for either (5 * nb_retry) seconds (max 5min) or a deactivation
                sleep_task = asyncio.create_task(asyncio.sleep(min(5.0 * retry_nb, 600)))
                await asyncio.wait([wait_deactivation, sleep_task], return_when=asyncio.FIRST_COMPLETED)

        if self.options.max_retry > 0 and retry_nb == self.options.max_retry:
            self.logger.error(f"Cannot connect to source after {self.options.max_retry} attempts, aborting. Please Re-activate the stream to retry.")
            asyncio.run_coroutine_threadsafe(self.async_final(), self.hass.loop)

        self.logger.debug("Closed")

    async def refresh_source(self) -> None:
        """Refresh the source if needed."""
        raise NotImplementedError

    def _open(self) -> av.container.InputContainer | None:
        """Open stream."""
        try:
            return av.open(self._src, options=self._src_options, mode="r", timeout=(self.options.open_to, self.options.read_to))
        except av.error.ConnectionRefusedError:
            self.logger.error("Connection to source refused: handle source issue")
            asyncio.run_coroutine_threadsafe(self.refresh_source(), self.hass.loop)
            return None
        except av.error.HTTPNotFoundError as err:
            self.logger.error("Stream not available: %s", err)
            return None
        except Exception:
            self.logger.exception("Failed to open source.")
            return None

    def _build_info(self) -> None:
        self._info = {}
        if self._con is None:
            return
        try:
            if self._con.streams.video:
                vs = self._con.streams.video[0]
                fps = str(vs.codec_context.rate)
                self._info = {"video": vs.codec.name, "width": vs.width, "height": vs.height, "fps": fps, "pix_fmt": str(vs.pix_fmt), "audio": None}
                if self._con.streams.audio:
                    self._info.update({"audio": self._con.streams.audio[0].codec.name})
        except Exception as err:  # Best effort info extraction
            self.logger.debug("Info extraction failed: %s", err)

    def get_streams(self) -> tuple[av.VideoStream | None, av.AudioStream | None]:
        """Get the opened streams."""
        if self._con and self._con.streams.video:
            if self.options.with_audio and self._con.streams.audio:
                return self._con.streams.video[0], self._con.streams.audio[0]
            return self._con.streams.video[0], None
        return None, None

    def _read(self, loop: asyncio.AbstractEventLoop) -> None:
        self.logger.debug("Read started")
        first_keyframe_recvd = False
        try:
            if self._con is None:
                self.logger.error("Trying to read not initialized container")
                return
            for packet in self._con.demux():
                if packet.dts is None:
                    continue
                if not first_keyframe_recvd:
                    if packet.is_keyframe and packet.stream.type == "video":
                        first_keyframe_recvd = True
                        self.logger.debug("First Video keyframe received.")
                        self._build_info()
                        asyncio.run_coroutine_threadsafe(self._async_process_packet(packet, True), loop)
                else:
                    asyncio.run_coroutine_threadsafe(self._async_process_packet(packet, False), loop)
                if self._deactivation_event.is_set():
                    break
            self.logger.debug("No more packet to read")
        except av.error.ExitError:
            self.logger.warning("Timeout waiting for packet.")
        except Exception:
            self.logger.exception("Exception reading packet")

    async def _async_process_packet(self, packet: av.Packet, is_first_key_frame: bool) -> None:
        # Process a Packet
        if is_first_key_frame:
            self._start_streaming_event.set()
            self.logger.info("Streaming started")
            self._streaming = True
            await self.update()
        for ph in self._packet_handlers:
            await ph.enqueue(_TimePacket(datetime.datetime.now(), packet, is_first_key_frame))

    async def flush_video_stream_context(self) -> None:
        """Flush the video context. Needed in case of frame decoding started, then stopped and started again."""
        if self._con is not None:
            self._con.streams.video[0].codec_context.flush_buffers()


class PacketHandler:
    """Handle Packets read from stream."""

    def __init__(self, name: str) -> None:
        self._name = name
        self._queue: asyncio.Queue[_TimePacket] = asyncio.Queue()
        self._process_task = asyncio.create_task(self.run())

    def set_parent(self, parent: Streamer) -> None:
        """Set parent streamer."""
        self.parent: Streamer = parent
        self.hass = parent.hass
        self.logger: logging.LoggerAdapter = parent.logger

    async def close(self) -> None:
        """Clean and close."""
        self._queue.shutdown(immediate=True)

    async def process(self, packet: _TimePacket) -> None:
        """Process the packet."""

    async def enqueue(self, packet: _TimePacket) -> None:
        """Enqueue a packet to be processed."""
        if not self._queue.full():
            await self._queue.put(packet)
        else:
            self.logger.warning(f"{self._name} - Queue Full, packet skipped")

    async def run(self) -> None:
        """Process the Packets."""
        processing = True
        while processing:
            try:
                packet = await self._queue.get()
                await self.process(packet)
            except asyncio.QueueShutDown:
                self.logger.debug(f"{self._name} - Closing Packet Handler")
                processing = False
            except Exception:
                self.logger.exception(f"{self._name} - Failed processing the packet")


class _OutputStream:
    _stream: av.VideoStream | av.AudioStream | None = None
    _transcoded: bool = False
    _offset_ts: int | None = None
    _prev_ts: int = 0
    _prev_duration: int = 0

    def set_stream(self, output: av.container.OutputContainer, input_stream: av.stream.Stream) -> None:
        if input_stream.codec.name in output.supported_codecs:
            self._stream = output.add_stream_from_template(input_stream)  # pyright: ignore[reportAttributeAccessIssue]
        else:
            recommended_codec = output.default_video_codec if input_stream.type == "video" else output.default_audio_codec
            self._stream = output.add_stream(recommended_codec, rate=input_stream.codec_context.rate)  # pyright: ignore[reportAttributeAccessIssue]
            self._transcoded = True

    def _adjust_pts(self, next_ts: int, next_duration: int, reset: bool) -> int:
        if self._offset_ts is None:
            self._offset_ts = next_ts
        elif next_ts <= self._prev_ts or reset:
            self._offset_ts = next_ts - self._prev_ts - self._prev_duration
        self._prev_ts = next_ts
        self._prev_duration = next_duration
        return self._prev_ts - self._offset_ts

    def _safe_mux(self, packet: av.Packet | list[av.Packet]) -> None:
        try:
            output = cast("av.container.OutputContainer", self._stream.container)  # pyright: ignore[reportOptionalMemberAccess]
            output.mux(packet)
        except Exception as err:
            _LOGGER.debug("Exception in mux: %s", err)

    def mux_packet(self, packet: av.Packet, reset: bool) -> None:
        if self._stream is None:
            _LOGGER.debug("Stream not initialized")
            return
        if self._transcoded:
            eff_reset = reset
            for frame in packet.decode():
                frame.pts = self._adjust_pts(frame.pts, frame.duration, eff_reset)  # pyright: ignore[reportAttributeAccessIssue, reportArgumentType]
                eff_reset = False
                self._safe_mux(self._stream.encode(frame))  # pyright: ignore[reportArgumentType]
        else:
            new_pts = self._adjust_pts(packet.pts, packet.duration, reset)  # pyright: ignore[reportArgumentType]
            # Copy the av.Packet buffer fully else the update of the pts on the original packet will break the global demuxing
            # which prevents simultaneous recordings, and even further recordings...
            new_pkt = av.Packet(packet.buffer_size)
            new_pkt.update(packet)  # pyright: ignore[reportArgumentType], the method definition in pyAv is wrong
            new_pkt.pts = new_pts
            new_pkt.dts = new_pts
            new_pkt.stream = self._stream
            new_pkt.duration = packet.duration
            new_pkt.time_base = packet.time_base
            self._safe_mux(new_pkt)


class _RecordItem:
    def __init__(self, path: Path) -> None:
        self._path: Path = path
        self._ongoing: bool = False
        self._output: av.container.OutputContainer | None = None
        self._out_v_stream: _OutputStream = _OutputStream()
        self._realign_next_audio: bool = False
        self._out_a_stream: _OutputStream = _OutputStream()
        self._packets: list[_TimePacket] = []
        self._nb_encoded_pkt: int = 0

    async def start(self, packets: list[_TimePacket], v_stream: av.VideoStream, a_stream: av.AudioStream | None) -> None:
        """Effective start of the recording executor."""
        self._ongoing = True
        await asyncio.get_running_loop().run_in_executor(None, self._open, packets, v_stream, a_stream)

    async def stop(self) -> None:
        """Stop the recording."""
        if self._ongoing:
            self._ongoing = False
            await asyncio.get_running_loop().run_in_executor(None, self._finish, self._packets)

    def _open(self, packets: list[_TimePacket], v_stream: av.VideoStream, a_stream: av.AudioStream | None) -> None:
        Path.mkdir(Path(self._path).parent, parents=True, exist_ok=True)
        self._output = av.open(self._path, "w")
        self._out_v_stream.set_stream(self._output, v_stream)
        if a_stream is not None:
            self._out_a_stream.set_stream(self._output, a_stream)
        self._write_packets(packets)

    def _write_packets(self, packets: list[_TimePacket]) -> None:
        if self._output is None:
            return
        for packet in packets:
            if packet.pkt.pts is not None and packet.pkt.duration is not None:
                self._realign_next_audio |= packet.is_first_video_keyframe
                if packet.pkt.stream.type == "video":
                    self._out_v_stream.mux_packet(packet.pkt, packet.is_first_video_keyframe)
                elif packet.pkt.stream.type == "audio":
                    self._out_a_stream.mux_packet(packet.pkt, self._realign_next_audio)
                    self._realign_next_audio = False

    def _finish(self, packets: list[_TimePacket]) -> None:
        if self._output is not None:
            self._write_packets(packets)
            self._output.close()
            self._output = None

    async def add_packet(self, packet: _TimePacket) -> None:
        """Add a packet."""
        if not self._ongoing:
            return
        if packet.pkt.is_keyframe and packet.pkt.stream.type == "video" and self._packets:
            packets = self._packets.copy()
            self._packets = [packet]
            await asyncio.get_running_loop().run_in_executor(None, self._write_packets, packets)
        else:
            self._packets.append(packet)


class NoVideoStreamError(Exception):
    """No Video Stream Error."""


class PacketRecorder(PacketHandler, Updatable):
    """Store Packets with a given lookback in order to be able to use them for recording."""

    def __init__(self, max_lookback: int = 10) -> None:
        PacketHandler.__init__(self, "recorder")
        Updatable.__init__(self)
        self._max_lookback = max_lookback
        self._lookback_packets: list[_TimePacket] = []
        self._records: dict[str, _RecordItem] = {}
        self._max_clbk: dict[str, CALLBACK_TYPE] = {}

    @property
    def triggers(self) -> list[str]:
        """Access the on going record triggers."""
        return list(self._records.keys())

    async def close(self) -> None:
        """Close the PacketRecorder."""
        await super().close()
        for rec in self._records.values():
            await rec.stop()
        self._records.clear()

    def _get_lookback_packets(self, lookback: int) -> list[_TimePacket]:
        """Get the lookback packets before 'lookback' seconds and starting with a video keyframe."""
        key_packet_index: int | None = None
        tm = datetime.datetime.now() - datetime.timedelta(seconds=lookback)
        for index, packet in enumerate(self._lookback_packets):
            if packet.pkt.is_keyframe and packet.pkt.stream.type == "video" and (tm > packet.end or key_packet_index is None):
                key_packet_index = index
        return self._lookback_packets[key_packet_index:]

    async def process(self, packet: _TimePacket) -> None:
        """Process the packet."""
        # Cleanup
        if packet.is_first_video_keyframe:
            self._lookback_packets.clear()
        else:
            self._lookback_packets = self._get_lookback_packets(self._max_lookback)

        # Process packet
        self._lookback_packets.append(packet)
        for rec in self._records.values():
            await rec.add_packet(packet)

    async def start_recording(self, trigger: str, path: Path, lookback: int = 2, max_duration: int = 360) -> None:
        """Start recording."""
        # Stop On Going record with the same name if any
        await self.stop_recording(trigger)

        # Create the RecordItem, get the relevant lookback packets and start the record
        self.logger.debug(f"Start Recording: '{trigger}' - {path}")
        rec = _RecordItem(path)
        v_stream, a_stream = self.parent.get_streams()
        if v_stream is None:
            raise NoVideoStreamError
        await rec.start(self._get_lookback_packets(lookback), v_stream, a_stream)

        async def stop_rec(_tm: datetime.datetime) -> None:
            await self.stop_recording(trigger)

        self._max_clbk[trigger] = async_call_later(self.hass, max_duration, stop_rec)
        self._records[trigger] = rec
        await self.update()

    async def stop_recording(self, trigger: str) -> None:
        """Stop recording."""
        if trigger in self._records:
            self.logger.debug(f"Stop Recording: '{trigger}'")
            self._max_clbk.pop(trigger)
            await self._records.pop(trigger).stop()
            await self.update()


class PacketFramer(PacketHandler):
    """Decode Packets into frames and process them for children processes."""

    def __init__(self) -> None:
        PacketHandler.__init__(self, "framer")
        self._frame_handlers: list[FrameHandler] = []
        self._decoding_frames = False
        self._pkt_from_keyframe: list[_TimePacket] = []

    def add_frame_handler(self, fh) -> None:  # noqa: ANN001
        """Register a Frame handler."""
        fh.set_parent(self.parent)
        self._frame_handlers.append(fh)

    async def close(self) -> None:
        """Close the PacketFramer."""
        await super().close()
        for fh in self._frame_handlers:
            await fh.close()

    async def process(self, packet: _TimePacket) -> None:
        """Process the packet."""
        if packet.pkt.stream.type != "video":
            return
        # store packets from last keyframe to be able to decode immediately a new frame
        if packet.pkt.is_keyframe:
            self._pkt_from_keyframe.clear()
        self._pkt_from_keyframe.append(packet)

        # Decode and Process frames only if needed
        fhs: list[FrameHandler] = [_fh for _fh in self._frame_handlers if _fh.activated]
        if fhs:
            if not self._decoding_frames:
                await self.parent.flush_video_stream_context()
                for prev_pkt in self._pkt_from_keyframe:
                    prev_pkt.pkt.decode()
                self._decoding_frames = True
            for frame in packet.pkt.decode():
                frame = cast("av.VideoFrame", frame)
                img = frame.to_ndarray(format="rgb24")
                for fh in fhs:
                    await fh.process(frame, img)  # type: ignore  # noqa: PGH003
        else:
            self._decoding_frames = False


class FrameHandler:
    """Handle decoded Frames from stream."""

    activated: bool = False

    def set_parent(self, parent: Streamer) -> None:
        """Set parent streamer."""
        self.parent: Streamer = parent
        self.hass = parent.hass
        self.logger: logging.LoggerAdapter = parent.logger

    async def close(self) -> None:
        """Close the frame handling."""

    async def process(self, img: np.ndarray) -> None:
        """Process the decoded frame as rgb24 numpy array [height,width,rgb]."""


class SnapshotHandler(FrameHandler, Updatable):
    """Handler to take a Snapshot."""

    def __init__(self) -> None:
        Updatable.__init__(self)
        self._frame: av.VideoFrame | None = None
        self._frame_recv: asyncio.Event = asyncio.Event()

    async def close(self) -> None:
        """Close the frame handling."""

    async def process(self, frame: av.VideoFrame, _img: np.ndarray) -> None:
        """Process the decoded frame as rgb24 numpy array [height,width,rgb]."""
        if self._frame is None:
            self.activated = False
            self._frame = frame
            self._frame_recv.set()

    async def take(self, path: Path, width: int | None = None) -> None:
        """Take a snapshot."""
        # activate the handler and Wait for a Frame to be received
        self.logger.debug(f"Snapshot requested to {path}")
        self._frame = None
        self._frame_recv.clear()
        self.activated = True
        try:
            async with asyncio.timeout(2.0):
                await self._frame_recv.wait()
        except TimeoutError:
            self.logger.warning("Failed to acquire Snapshot in 2 seconds.")
        if self._frame is None:
            return

        # Save the frame
        def save_frame(frame: av.VideoFrame) -> None:
            Path.mkdir(path.parent, parents=True, exist_ok=True)
            if width is not None:
                factor: float = width / frame.width
                frame.to_image(width=width, height=int(frame.height * factor)).save(path)
            else:
                frame.to_image().save(path)
            self.logger.debug("Snapshot saved")

        self.hass.loop.run_in_executor(None, save_frame, self._frame)
        await self.update()
