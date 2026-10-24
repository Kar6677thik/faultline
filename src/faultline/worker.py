"""A bounded JSON exchange over an attached Docker exec stream.

Start Python once before workload dispatch. Process startup must not consume every
HTTP attempt's deadline. Docker's eight-byte stdout framing is decoded explicitly.
"""

import json
import queue
import struct
import threading
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
        self.pending = {}
        self.lock = threading.Lock()
        self.send_lock = threading.Lock()
        self.failure = None
        try:
            if self.read_message(10) != {"protocol_version": 2, "ready": True, "max_in_flight": 32}:
                raise RuntimeError("unsupported worker handshake")
        except BaseException:
            self.close()
            raise
        threading.Thread(target=self._receive, daemon=True).start()

    def _receive(self):
        try:
            while True:
                message = self.read_message(900)
                with self.lock:
                    request_id = message.get("request_id")
                    if message.get("protocol_version") != 2 or request_id not in self.pending:
                        raise RuntimeError("worker response correlation mismatch")
                    self.pending[request_id].put_nowait(message)
        except Exception as exc:
            with self.lock:
                self.failure = exc
                for mailbox in self.pending.values():
                    try:
                        mailbox.put_nowait(exc)
                    except queue.Full:
                        pass

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
        message = {"protocol_version": 2, "request_id": request_id, "request": request}
        mailbox = queue.Queue(maxsize=3)
        deadline = time.monotonic() + timeout
        with self.lock:
            if self.failure is not None:
                raise RuntimeError("worker stream failed") from self.failure
            if len(self.pending) >= 32:
                raise RuntimeError("worker capacity exceeded")
            self.pending[request_id] = mailbox
        try:
            with self.send_lock:
                self.socket.sendall(json.dumps(message).encode() + b"\n")
            messages = []
            for _ in range(2):
                try:
                    item = mailbox.get(timeout=max(0, deadline - time.monotonic()))
                except queue.Empty as exc:
                    raise TimeoutError("worker acknowledgment/result deadline exceeded") from exc
                if isinstance(item, Exception):
                    raise RuntimeError("worker stream failed") from item
                messages.append(item)
            acknowledgment, response = messages
            if acknowledgment.get("accepted") is not True or "result" in acknowledgment:
                raise RuntimeError("worker did not acknowledge admission")
        finally:
            with self.lock:
                self.pending.pop(request_id, None)
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
