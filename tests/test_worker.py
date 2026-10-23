import json
import socket
import struct
from http.client import HTTPResponse

import pytest

from faultline.worker import WorkerSession


class FragmentedSocket:
    def __init__(self, payload):
        self.payload = bytearray(payload)

    def settimeout(self, _timeout):
        pass

    def recv(self, size):
        # Real streams may return fewer bytes than requested, including partial headers.
        result = self.payload[: min(size, 3)]
        del self.payload[: len(result)]
        return bytes(result)


def frame(payload):
    return bytes([1, 0, 0, 0]) + struct.pack(">I", len(payload)) + payload


def session(payload):
    value = object.__new__(WorkerSession)
    value.socket = FragmentedSocket(payload)
    value.buffer = bytearray()
    return value


def test_fragmented_frames_and_multiple_messages():
    one = json.dumps({"one": 1}).encode() + b"\n"
    two = json.dumps({"two": 2}).encode() + b"\n"
    worker = session(frame(one[:4]) + frame(one[4:] + two))
    assert worker.read_message(1) == {"one": 1}
    assert worker.read_message(1) == {"two": 2}


def test_truncated_stream_is_a_harness_error():
    worker = session(frame(b'{"unfinished":'))
    with pytest.raises(RuntimeError, match="closed"):
        worker.read_message(1)


def test_oversized_frame_is_rejected_before_body_read():
    worker = session(bytes([1, 0, 0, 0]) + struct.pack(">I", 1048577))
    with pytest.raises(RuntimeError, match="oversized"):
        worker.read_message(1)


def test_closing_session_closes_owning_http_response_without_finalizer_error():
    left, right = socket.socketpair()
    response = HTTPResponse(left)
    worker = object.__new__(WorkerSession)
    worker.connection = response.fp.raw
    worker.connection._response = response
    worker.socket = worker.connection._sock
    try:
        worker.close()
        assert response.closed
        assert worker.socket.fileno() == -1
        worker.close()  # Cleanup is idempotent after EOF or a previous close.
    finally:
        response.close()
        left.close()
        right.close()
