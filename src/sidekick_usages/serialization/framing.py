"""Bounded length-prefixed binary framing."""

import struct

_FRAME_PREFIX = struct.Struct(">I")


class FramingError(ValueError):
    """A bounded frame is empty, oversized, or incomplete."""


class EmptyFrameError(FramingError):
    """A frame declared or encoded an empty payload."""


class OversizedFrameError(FramingError):
    """A frame exceeded its caller-owned payload limit."""


class IncompleteFrameError(FramingError):
    """A byte stream ended inside a prefix or payload."""


class BoundedFrameDecoder:
    """Incrementally decode frames within one explicit payload bound."""

    def __init__(self, maximum_payload_bytes: int) -> None:
        if maximum_payload_bytes < 1:
            raise ValueError("Frame payload limit must be positive.")
        self._maximum = maximum_payload_bytes
        self._buffer = bytearray()
        self._expected: int | None = None

    def feed(
        self,
        chunk: bytes | bytearray | memoryview,
    ) -> tuple[bytearray, ...]:
        """Consume one byte fragment and return complete mutable payloads."""
        if not isinstance(chunk, bytes | bytearray | memoryview):
            raise TypeError("Frame chunks must be bytes-like.")
        self._buffer.extend(chunk)
        frames: list[bytearray] = []
        while True:
            if self._expected is None:
                if len(self._buffer) < _FRAME_PREFIX.size:
                    break
                (declared,) = _FRAME_PREFIX.unpack(
                    self._buffer[: _FRAME_PREFIX.size]
                )
                self._buffer[: _FRAME_PREFIX.size] = (
                    b"\x00" * _FRAME_PREFIX.size
                )
                del self._buffer[: _FRAME_PREFIX.size]
                if declared == 0:
                    raise EmptyFrameError
                if declared > self._maximum:
                    raise OversizedFrameError
                self._expected = declared
            if len(self._buffer) < self._expected:
                break
            frames.append(self._buffer[: self._expected])
            self._buffer[: self._expected] = b"\x00" * self._expected
            del self._buffer[: self._expected]
            self._expected = None
        if len(self._buffer) > self._maximum:
            raise OversizedFrameError
        return tuple(frames)

    def finish(self) -> None:
        """Reject and clear any incomplete frame at end of stream."""
        incomplete = bool(self._buffer or self._expected is not None)
        self.clear()
        if incomplete:
            raise IncompleteFrameError

    def clear(self) -> None:
        """Overwrite buffered payload bytes and reset decoder state."""
        self._buffer[:] = b"\x00" * len(self._buffer)
        self._buffer.clear()
        self._expected = None

    @property
    def pending(self) -> bool:
        """Return whether an incomplete frame remains buffered."""
        return bool(self._buffer or self._expected is not None)


def clear_mutable_buffer(payload: bytearray) -> None:
    """Overwrite and release one mutable payload buffer."""
    payload[:] = b"\x00" * len(payload)
    payload.clear()


def encode_bounded_frame(
    payload: bytes | bytearray,
    maximum_payload_bytes: int,
) -> bytearray:
    """Return one mutable length-prefixed frame within an explicit bound."""
    if not payload:
        raise EmptyFrameError
    if maximum_payload_bytes < 1:
        raise ValueError("Frame payload limit must be positive.")
    if len(payload) > maximum_payload_bytes:
        raise OversizedFrameError
    return bytearray(_FRAME_PREFIX.pack(len(payload))) + payload
