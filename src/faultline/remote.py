"""Bounded helpers invoked with an argv array inside owned containers, never a shell."""

import asyncio
import json
import sys
import time

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
                "http://application:8000" + request["path"],
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
    if request["path"] not in {"/health", "/orders"}:
        raise ValueError("unsupported internal route")
    return asyncio.run(_http_request(request))


def main():
    command = sys.argv[1]
    if command == "session":
        print(json.dumps({"protocol_version": 1, "ready": True}), flush=True)
        while line := sys.stdin.readline(1048577):
            if len(line) > 1048576 or not line.endswith("\n"):
                raise ValueError("oversized worker request")
            message = json.loads(line)
            if message["protocol_version"] != 1:
                raise ValueError("unsupported worker protocol")
            result = http_request(message["request"])
            print(
                json.dumps(
                    {"protocol_version": 1, "request_id": message["request_id"], "result": result}
                ),
                flush=True,
            )
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
    else:
        raise ValueError("unknown helper command")
    print(json.dumps(result, separators=(",", ":")), flush=True)


if __name__ == "__main__":
    main()
