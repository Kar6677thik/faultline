"""A bounded JSON exchange over an attached Docker exec stream.

Start Python once before workload dispatch. Process startup must not consume every
HTTP attempt's deadline. Docker's eight-byte stdout framing is decoded explicitly.
"""

import json
import struct
import time
from uuid import uuid4


class WorkerSession:
    def __init__(self, api, container_id):
        self.api = api
        command = api.exec_create(
            container_id,
            ["python", "-m", "faultline.remote", "session"],
            stdin=True,
            stdout=True,
            stderr=False,
            tty=False,
        )
        self.connection = api.exec_start(command["Id"], socket=True, tty=False)
        self.socket = getattr(self.connection, "_sock", self.connection)
        self.buffer = bytearray()
        try:
            if self.read_message(10) != {"protocol_version": 1, "ready": True}:
                raise RuntimeError("unsupported worker handshake")
        except BaseException:
            self.close()
            raise

    def read_exact(self, count, deadline):
        result = bytearray()
        while len(result) < count:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("worker response deadline exceeded")
            self.socket.settimeout(remaining)
            chunk = self.socket.recv(count - len(result))
            if not chunk:
                raise RuntimeError("worker stream closed before a complete response")
            result.extend(chunk)
        return bytes(result)

    def read_message(self, timeout):
        deadline = time.monotonic() + timeout
        while b"\n" not in self.buffer:
            header = self.read_exact(8, deadline)
            stream, length = header[0], struct.unpack(">I", header[4:])[0]
            if stream != 1 or length > 1048576 or len(self.buffer) + length > 1048576:
                raise RuntimeError("invalid or oversized worker frame")
            self.buffer.extend(self.read_exact(length, deadline))
        line, _, remainder = self.buffer.partition(b"\n")
        self.buffer = bytearray(remainder)
        message = json.loads(line)
        if not isinstance(message, dict):
            raise RuntimeError("invalid worker message")
        return message

    def request(self, request, timeout):
        request_id = str(uuid4())
        message = {"protocol_version": 1, "request_id": request_id, "request": request}
        self.socket.settimeout(timeout)
        self.socket.sendall(json.dumps(message).encode() + b"\n")
        response = self.read_message(timeout)
        if response.get("protocol_version") != 1 or response.get("request_id") != request_id:
            raise RuntimeError("worker response correlation mismatch")
        result = response.get("result")
        if not isinstance(result, dict) or result.get("outcome") not in {
            "http_response",
            "timeout",
            "connection_error",
            "invalid_response",
        }:
            raise RuntimeError("invalid worker outcome")
        return result

    def close(self):
        try:
            # Docker retains the owning HTTP response on SocketIO. Close that owner
            # first so its buffered reader cannot later flush an already closed raw file.
            response = getattr(self.connection, "_response", None)
            if response is not None:
                response.close()
        finally:
            try:
                self.connection.close()
            finally:
                self.socket.close()
