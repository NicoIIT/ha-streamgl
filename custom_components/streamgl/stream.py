"""StreaMGL main handler."""

import asyncio
import datetime
import logging
from collections.abc import MutableMapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import av
import av.container
import av.error
import av.stream
import numpy as np
from homeassistant.core import CALLBACK_TYPE, HomeAssistant
from homeassistant.helpers.event import async_call_later

_LOGGER = logging.getLogger(__name__)


class _StreamLoggingAdapter(logging.LoggerAdapter):
    def process(self, msg: str, kwargs: MutableMapping[str, Any]) -> tuple[str, MutableMapping[str, Any]]:
        return (f"[{self.extra['name']}] {msg}", kwargs) if self.extra is not None else (msg, kwargs)


@dataclass
class _TimePacket:
    end: datetime.datetime
    pkt: av.Packet


class Streamer:
    """Handle the Stream."""

    def __init__(self, hass: HomeAssistant, name: str, max_inactivity_seconds: int = 15) -> None:
        self.hass = hass
        self.name: str = name
        self.logger = _StreamLoggingAdapter(_LOGGER, {"name": self.name})
        self._nb_sec_no_frame: int = max_inactivity_seconds

        self._packet_handlers: list[PacketHandler] = []
        self._con: av.container.InputContainer | None = None
        self._permanent_read_task: asyncio.Task | None = None
        self._read_task: asyncio.Future | None = None

        self._running: bool = False
        self._exit_requested: bool = False

        self._src: str = ""
        self._src_options: dict[str, str] = {}

    def add_packet_handler(self, ph) -> None:  # noqa: ANN001
        """Register a Packet handler."""
        ph.set_parent(self)
        self._packet_handlers.append(ph)

    async def async_get_src(self) -> tuple[str, dict[str, str]]:
        """Get the tuple src / src_options."""
        raise NotImplementedError

    async def async_init(self) -> None:
        """Init, ensuring the process is done only once."""
        if self._permanent_read_task is None:
            self._src, self._src_options = await self.async_get_src()
            self._permanent_read_task = asyncio.create_task(self._permanent_read())

    async def async_final(self) -> None:
        """Finalize."""
        for ph in self._packet_handlers:
            await ph.close()
        self._packet_handlers.clear()
        self._exit_requested = True
        if self._permanent_read_task is not None:
            while not self._permanent_read_task.done:
                await asyncio.sleep(0.1)
            self._permanent_read_task = None

    async def _permanent_read(self) -> None:
        while not self._exit_requested:
            # if not connected, launch connection
            if not self._running:
                self._con = await self.hass.loop.run_in_executor(None, self._open)
                if self._con is not None:
                    self._running = True
                    self._read_task = self.hass.loop.run_in_executor(None, self._read, self.hass.loop)
            await asyncio.sleep(5.0)
        self.logger.debug("Closed")

    async def refresh_source(self) -> None:
        """Refresh the source if needed."""
        raise NotImplementedError

    def _open(self) -> av.container.InputContainer | None:
        """Open stream."""
        try:
            return av.open(self._src, options=self._src_options, mode="r", timeout=(20.0, 10.0))
        except av.error.ConnectionRefusedError:
            self.logger.error("Connection to source refused: handle source issue")
            asyncio.run_coroutine_threadsafe(self.refresh_source(), self.hass.loop)
            return None
        except Exception:
            self.logger.exception("Failed to open source.")
            return None

    def _read(self, loop: asyncio.AbstractEventLoop) -> None:
        self.logger.debug("Read started")
        try:
            if self._con is None:
                self.logger.error("Trying to read not initialized container")
                return
            for packet in self._con.demux(self._con.streams.video[0]):
                if packet.dts is None:
                    continue
                asyncio.run_coroutine_threadsafe(self._async_process_packet(packet), loop)
                if self._exit_requested:
                    break
            self.logger.debug("No more packet to read")
        except av.error.ExitError:
            self.logger.warning("Timeout waiting for frame, re opening source.")
        except Exception:
            self.logger.exception("Exception reading packet")
        self._running = False

    async def _async_process_packet(self, packet: av.Packet) -> None:
        # Process a Packet
        for ph in self._packet_handlers:
            await ph.enqueue(_TimePacket(datetime.datetime.now(), packet))

    async def flush_video_stream_context(self) -> None:
        """Flush the video context. Needed in case of frame decoding started, then stopped and started again."""
        if self._con is not None:
            self._con.streams.video[0].codec_context.flush_buffers()


class PacketHandler:
    """Handle Packets read from stream."""

    def __init__(self) -> None:
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
            self.logger.warning("Queue Full, packet skipped")

    async def run(self) -> None:
        """Process the Packets."""
        processing = True
        while processing:
            try:
                packet = await self._queue.get()
                await self.process(packet)
            except asyncio.QueueShutDown:
                self.logger.debug("Closing")
                processing = False
            except Exception:
                self.logger.exception("Failed processing the packet")


class _RecordItem:
    def __init__(self, path: Path) -> None:
        self._path: Path = path
        self._ongoing: bool = False
        self._output: av.container.OutputContainer | None = None
        self._out_stream: av.stream.Stream | None = None
        self._out_first_pts: int | None = None
        self._out_first_dts: int | None = None
        self._packets: list[_TimePacket] = []
        self._nb_encoded_pkt: int = 0

    async def start(self, packets: list[_TimePacket]) -> None:
        """Effective start of the recording executor."""
        self._ongoing = True
        await asyncio.get_running_loop().run_in_executor(None, self._open, packets)

    async def stop(self) -> None:
        """Stop the recording."""
        if self._ongoing:
            self._ongoing = False
            await asyncio.get_running_loop().run_in_executor(None, self._finish, self._packets)

    def _open(self, packets: list[_TimePacket]) -> None:
        Path.mkdir(Path(self._path).parent, parents=True, exist_ok=True)
        self._output = av.open(self._path, "w")
        self._out_stream = self._output.add_stream_from_template(packets[0].pkt.stream)
        self._out_first_pts = packets[0].pkt.pts
        self._out_first_dts = packets[0].pkt.dts
        self._write_packets(packets)

    def _write_packets(self, packets: list[_TimePacket]) -> None:
        if self._out_first_pts is None or self._out_first_dts is None or self._out_stream is None or self._output is None:
            return
        for packet in packets:
            if packet.pkt.pts is not None:
                # Copy the av.Packet else the update of the pts will break the global demuxing
                # which prevents simultaneous recordings, and even further recordings...
                new_pkt = av.Packet(packet.pkt.buffer_size)
                new_pkt.update(packet.pkt)  # pyright: ignore[reportArgumentType], the method definition in pyAv is wrong
                new_pkt.pts = packet.pkt.pts - self._out_first_pts
                new_pkt.dts = new_pkt.pts
                new_pkt.stream = self._out_stream
                new_pkt.duration = packet.pkt.duration
                new_pkt.time_base = packet.pkt.time_base
                self._output.mux(new_pkt)

    def _finish(self, packets: list[_TimePacket]) -> None:
        if self._output is not None and self._out_stream is not None:
            self._write_packets(packets)
            self._output.close()
            self._output = None

    async def add_packet(self, packet: _TimePacket) -> None:
        """Add a packet."""
        if not self._ongoing:
            return
        if packet.pkt.is_keyframe and self._packets:
            packets = self._packets.copy()
            self._packets = [packet]
            await asyncio.get_running_loop().run_in_executor(None, self._write_packets, packets)
        else:
            self._packets.append(packet)


class PacketRecorder(PacketHandler):
    """Store Packets with a given lookback in order to be able to use them for recording."""

    def __init__(self, max_lookback: int = 10) -> None:
        super().__init__()
        self._max_lookback = max_lookback
        self._lookback_packets: list[_TimePacket] = []
        self._records: dict[str, _RecordItem] = {}
        self._max_clbk: dict[str, CALLBACK_TYPE] = {}

    async def close(self) -> None:
        """Close the PacketRecorder."""
        for name in self._records:
            await self._records.pop(name).stop()
        self._records.clear()

    def _get_lookback_packets(self, lookback: int) -> list[_TimePacket]:
        """Get the lookback packets before 'lookback' seconds and starting with a keyframe."""
        key_packet_pts: int = 0
        tm = datetime.datetime.now() - datetime.timedelta(seconds=lookback)
        for packet in self._lookback_packets:
            if packet.pkt.is_keyframe and packet.pkt.pts is not None and (tm > packet.end or key_packet_pts == 0):
                key_packet_pts = packet.pkt.pts
        return [p for p in self._lookback_packets if p.pkt.pts is not None and p.pkt.pts >= key_packet_pts]

    async def process(self, packet: _TimePacket) -> None:
        """Process the packet."""
        # Cleanup
        self._lookback_packets = self._get_lookback_packets(self._max_lookback)

        # Process packet
        self._lookback_packets.append(packet)
        for rec in self._records.values():
            await rec.add_packet(packet)

    async def start_recording(self, key: str, path: Path, lookback: int = 2, max_duration: int = 360) -> None:
        """Start recording."""
        # Stop On Going record with the same name if any
        await self.stop_recording(key)

        # Create the RecordItem, get the relevant lookback packets and start the record
        self.logger.debug(f"Start Recording: '{key}' - {path}")
        rec = _RecordItem(path)
        await rec.start(self._get_lookback_packets(lookback))

        async def stop_rec(_tm: datetime.datetime) -> None:
            await self.stop_recording(key)

        self._max_clbk[key] = async_call_later(self.hass, max_duration, stop_rec)
        self._records[key] = rec

    async def stop_recording(self, key: str) -> None:
        """Stop recording."""
        if key in self._records:
            self.logger.debug(f"Stop Recording: '{key}'")
            self._max_clbk.pop(key)
            await self._records.pop(key).stop()


class PacketFramer(PacketHandler):
    """Decode Packets into frames and process them for children processes."""

    def __init__(self) -> None:
        super().__init__()
        self._frame_handlers: list[FrameHandler] = []
        self._decoding_frames = False
        self._pkt_from_keyframe: list[_TimePacket] = []

    def add_frame_handler(self, fh) -> None:  # noqa: ANN001
        """Register a Frame handler."""
        fh.set_parent(self.parent)
        self._frame_handlers.append(fh)

    async def close(self) -> None:
        """Close the PacketRecorder."""

    async def process(self, packet: _TimePacket) -> None:
        """Process the packet."""
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


class SnapshotHandler(FrameHandler):
    """Handler to take a Snapshot."""

    def __init__(self) -> None:
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

    async def take(self, path: Path, factor: float | None = None) -> None:
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
            if factor is not None:
                frame.to_image(width=int(frame.width * factor), height=int(frame.height * factor)).save(path)
            else:
                frame.to_image().save(path)
            self.logger.debug("Snapshot saved")

        self.hass.loop.run_in_executor(None, save_frame, self._frame)
