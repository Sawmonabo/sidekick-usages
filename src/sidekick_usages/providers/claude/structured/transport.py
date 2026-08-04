"""Protected local socket framing for resident Claude participants."""

import socket

from sidekick_usages.providers.claude.structured.codec import (
    MAX_CLAUDE_PROTECTED_FRAME_BYTES,
    ClaudeProtectedChannelClosedError,
    ClaudeProtectedChannelError,
)
from sidekick_usages.serialization.framing import (
    BoundedFrameDecoder,
    clear_mutable_buffer,
)

_SOCKET_READ_BYTES = 64 * 1024


def receive_protected_socket_frame(endpoint: socket.socket) -> bytearray:
    """Receive one bounded frame from an exact protected endpoint."""
    decoder = BoundedFrameDecoder(MAX_CLAUDE_PROTECTED_FRAME_BYTES)
    while True:
        chunk = endpoint.recv(_SOCKET_READ_BYTES)
        if not chunk:
            decoder.finish()
            raise ClaudeProtectedChannelClosedError(
                "The protected participant channel closed."
            )
        frames = decoder.feed(chunk)
        if len(frames) > 1 or (frames and decoder.pending):
            for frame in frames:
                clear_mutable_buffer(frame)
            raise ClaudeProtectedChannelError(
                "The protected participant receipt is malformed."
            )
        if frames:
            return frames[0]
