"""Toxiproxy administration from an isolated helper; fixed owned binding only."""

import os
import time

import httpx


def command(value):
    address = os.environ["FL_PROXY_ADMIN"]
    if address != "proxy-control":
        raise ValueError("proxy admin must use the owned control-network alias")
    action = value["action"]
    with httpx.Client(
        base_url=f"http://{address}:8474", timeout=3, trust_env=False, follow_redirects=False
    ) as client:

        def call(method, path, payload=None):
            response = client.request(method, path, json=payload)
            response.raise_for_status()
            if len(response.content) > 65536:
                raise ValueError("oversized proxy acknowledgment")
            return response.json() if response.content else None

        if action == "prepare":
            expected = {
                "name": "database",
                "listen": "0.0.0.0:8666",
                "upstream": "database:5432",
                "enabled": True,
            }
            call("POST", "/proxies", expected)
            current = call("GET", "/proxies/database")
            if any(
                current.get(k) != v for k, v in expected.items() if k != "listen"
            ) or current.get("listen") not in {"0.0.0.0:8666", "[::]:8666"}:
                raise ValueError(f"proxy binding acknowledgment mismatch: {current}")
        elif action in {"disable", "enable"}:
            enabled = action == "enable"
            call("POST", "/proxies/database", {"enabled": enabled})
            current = call("GET", "/proxies/database")
            if current.get("enabled") is not enabled:
                raise ValueError("proxy enabled acknowledgment mismatch")
        elif action == "latency":
            expected = {
                "name": value["id"],
                "type": "latency",
                "stream": value["direction"],
                "toxicity": 1.0,
                "attributes": {"latency": value["latency_ms"], "jitter": 0},
            }
            call("POST", "/proxies/database/toxics", expected)
            current = call("GET", f"/proxies/database/toxics/{value['id']}")
            if current != expected:
                raise ValueError("proxy latency acknowledgment mismatch")
        elif action == "remove":
            call("DELETE", f"/proxies/database/toxics/{value['id']}")
            current = call("GET", "/proxies/database/toxics")
            if any(toxic["name"] == value["id"] for toxic in current):
                raise ValueError("proxy toxic removal not acknowledged")
        else:
            raise ValueError("unknown proxy control action")
    return {
        "acknowledged": True,
        "action": action,
        "configuration": current,
        "source_monotonic_ns": time.monotonic_ns(),
    }
