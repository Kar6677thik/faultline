import json
import socket
import struct
import threading
from http.client import HTTPResponse
from unittest.mock import Mock

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


def test_protocol_multiplexes_reversed_results_by_command_identity():
    left, right = socket.socketpair()
    api = Mock()
    api.exec_create.return_value = {"Id": "owned-exec"}
    api.exec_start.return_value = left
    server_errors = []

    def server():
        try:
            right.sendall(
                frame(
                    json.dumps({"protocol_version": 2, "ready": True, "max_in_flight": 32}).encode()
                    + b"\n"
                )
            )
            with right.makefile("rb") as stream:
                messages = [json.loads(stream.readline()) for _ in range(2)]
                for message in reversed(messages):
                    common = {"protocol_version": 2, "request_id": message["request_id"]}
                    for response in (
                        {**common, "accepted": True},
                        {
                            **common,
                            "result": {
                                "outcome": "http_response",
                                "status": 201,
                                "body": message["request"],
                            },
                        },
                    ):
                        right.sendall(frame(json.dumps(response).encode() + b"\n"))
        except Exception as exc:
            server_errors.append(exc)

    background = threading.Thread(target=server, daemon=True)
    background.start()
    worker = WorkerSession(api, "owned-worker")
    results = {}
    threads = [
        threading.Thread(target=lambda i=i: results.update({i: worker.request({"value": i}, 2)}))
        for i in range(2)
    ]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=3)
            assert not thread.is_alive()
        assert not server_errors
        assert {i: result["body"]["value"] for i, result in results.items()} == {0: 0, 1: 1}
    finally:
        right.close()
        worker.close()
        background.join(timeout=2)
