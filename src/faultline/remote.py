"""Bounded helpers invoked with an argv array inside owned containers, never a shell."""

import asyncio
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx


async def _http_request(request: dict) -> dict:
    start = time.monotonic_ns()
    try:
        async with (
            asyncio.timeout(request["timeout_ms"] / 1000),
            httpx.AsyncClient(trust_env=False, follow_redirects=False) as client,
        ):
            async with client.stream(
                request["method"],
                f"http://application:{request.get('port', 8000)}" + request["path"],
                content=request.get("body"),
                headers=request.get("headers", {}),
                timeout=request["timeout_ms"] / 1000,
            ) as response:
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > 65536:
                        return {"outcome": "invalid_response", "reason": "response_too_large"}
                try:
                    body = json.loads(content)
                except (ValueError, UnicodeError):
                    return {"outcome": "invalid_response", "reason": "invalid_json"}
                return {
                    "outcome": "http_response",
                    "status": response.status_code,
                    "body": body,
                    "source_monotonic_ns": time.monotonic_ns(),
                    "duration_ms": (time.monotonic_ns() - start) // 1000000,
                }
    except (httpx.TimeoutException, TimeoutError):
        return {"outcome": "timeout"}
    except httpx.TransportError:
        return {"outcome": "connection_error"}


def http_request(request: dict) -> dict:
    from faultline.models import safe_path

    safe_path(request["path"])
    if type(request.get("port", 8000)) is not int or not 1 <= request.get("port", 8000) <= 65535:
        raise ValueError("invalid internal port")
    return asyncio.run(_http_request(request))


def main():
    command = sys.argv[1]
    if command == "session":
        output_lock = threading.Lock()
        slots = threading.BoundedSemaphore(32)
        seen = set()

        def send(value):
            with output_lock:
                print(json.dumps(value), flush=True)

        def execute(message):
            try:
                send({"protocol_version": 2, "request_id": message["request_id"], "accepted": True})
                result = http_request(message["request"])
                send({"protocol_version": 2, "request_id": message["request_id"], "result": result})
            finally:
                slots.release()

        send({"protocol_version": 2, "ready": True, "max_in_flight": 32})
        with ThreadPoolExecutor(max_workers=32) as executor:
            while line := sys.stdin.readline(1048577):
                if len(line) > 1048576 or not line.endswith("\n"):
                    raise ValueError("oversized worker request")
                message = json.loads(line)
                if message["protocol_version"] != 2:
                    raise ValueError("unsupported worker protocol")
                identity = message["request_id"]
                if identity in seen or len(seen) >= 10000 or not slots.acquire(blocking=False):
                    raise ValueError("duplicate command or exceeded worker budget")
                seen.add(identity)
                executor.submit(execute, message)
        return
    if command == "http":
        request = json.loads(sys.argv[2])
        result = http_request(request)
    elif command == "prepare":
        from faultline.orders import prepare

        result = prepare(sys.argv[2])
    elif command == "snapshot":
        from faultline.orders import snapshot

        result = snapshot(sys.argv[2])
    elif command == "proxy":
        from faultline.proxy import command as proxy_command

        result = proxy_command(json.loads(sys.argv[2]))
    elif command == "registry":
        from faultline.orders import registry_evidence

        result = registry_evidence()
    elif command == "clear_registry":
        from faultline.orders import clear_stale_registry

        result = clear_stale_registry()
    else:
        raise ValueError("unknown helper command")
    print(json.dumps(result, separators=(",", ":")), flush=True)


if __name__ == "__main__":
    main()
