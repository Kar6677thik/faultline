"""Read-only local evidence API and the packaged investigation viewer.

The server only reads bundles under one root chosen on the command line. It never
starts experiments, imports adapters, modifies artifacts, or accepts a filesystem path
from a request. It binds to 127.0.0.1 and requires a per-process bearer token.
"""

import base64
import hashlib
import hmac
import json
import re
import secrets
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response

from faultline.artifacts import read_bundle
from faultline.verifier import EvidenceError, UnsupportedArtifact

API_VERSION = 1
ASSETS = Path(__file__).parent / "viewer"
SAFE_INTEGER = 2**53 - 1
VERDICTS = {"PASS", "FAIL", "INCONCLUSIVE", "ERROR", "RUNNING", "INTERRUPTED"}
IDENTIFIER = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
ASSET_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}\.(?:js|css)$")
# An unfinished bundle with no writes for this long is treated as interrupted.
RUNNING_WINDOW_SECONDS = 120
MAX_RUN_DIRECTORIES = 10000
MAX_SMALL_JSON = 8388608
MAX_SNAPSHOT_BYTES = 52428800
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Cross-Origin-Opener-Policy": "same-origin",
}


class Problem(Exception):
    def __init__(self, status: int, code: str, message: str, details: dict | None = None):
        self.status, self.code, self.message, self.details = status, code, message, details or {}
        super().__init__(message)


def presentable(value, key: str = ""):
    """JSON for JavaScript: money, nanoseconds, and unsafe integers become decimal strings."""
    if isinstance(value, dict):
        return {k: presentable(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [presentable(item, key) for item in value]
    if type(value) is int and (
        key.endswith("_minor") or key.endswith("_ns") or abs(value) > SAFE_INTEGER
    ):
        return str(value)
    return value


def canonical_id(value: str) -> str:
    try:
        parsed = str(UUID(value))
    except ValueError as exc:
        raise Problem(404, "not_found", "Unknown ID") from exc
    if parsed != value:
        raise Problem(404, "not_found", "Unknown ID")
    return parsed


def read_json(path: Path, limit: int = MAX_SMALL_JSON):
    if path.is_symlink() or not path.is_file():
        raise Problem(404, "artifact_not_ready", "Artifact has not been written")
    if path.stat().st_size > limit:
        raise Problem(422, "corrupt_evidence", "Artifact exceeds its size limit")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise Problem(422, "corrupt_evidence", "Artifact is not valid JSON") from exc


class EvidenceStore:
    """Bundle access with integrity checks; caches key on file size and mtime."""

    def __init__(self, runs_dir: Path, groups_dir: Path | None):
        self.runs_dir = runs_dir.resolve()
        self.groups_dir = groups_dir.resolve() if groups_dir else None
        self._cache: dict[str, tuple[tuple, dict]] = {}

    def run_path(self, run_id: str) -> Path:
        path = self.runs_dir / canonical_id(run_id)
        if path.is_symlink() or not path.is_dir():
            raise Problem(404, "not_found", "Unknown run")
        return path

    def _stamp(self, path: Path) -> tuple:
        stamp = []
        for name in ("manifest.json", "history.ndjson", "report.json"):
            try:
                info = (path / name).stat()
                stamp.append((info.st_size, info.st_mtime_ns))
            except FileNotFoundError:
                stamp.append(None)
        return tuple(stamp)

    def run(self, run_id: str) -> dict:
        path = self.run_path(run_id)
        stamp = self._stamp(path)
        cached = self._cache.get(run_id)
        if cached and cached[0] == stamp:
            return cached[1]
        value = self._load(path)
        if len(self._cache) > 256:
            self._cache.clear()
        self._cache[run_id] = (stamp, value)
        return value

    def _load(self, path: Path) -> dict:
        manifest = read_json(path / "manifest.json", 1048576)
        if not isinstance(manifest, dict) or manifest.get("run_id") != path.name:
            raise Problem(422, "corrupt_evidence", "Manifest identity does not match its run")
        if manifest.get("bundle_schema_version") != 1:
            raise Problem(
                409,
                "unsupported_version",
                "Unsupported bundle version",
                {"bundle_schema_version": manifest.get("bundle_schema_version")},
            )
        if manifest.get("stage") == "completed":
            try:
                manifest, report = read_bundle(path)
            except UnsupportedArtifact as exc:
                raise Problem(409, "unsupported_version", str(exc)) from exc
            except (EvidenceError, ValueError, OSError, KeyError, TypeError) as exc:
                raise Problem(422, "corrupt_evidence", str(exc)[:300]) from exc
            names = sorted(manifest["artifacts"])
            verdict, completeness = report["verdict"], "complete"
        else:
            report, names, completeness = None, [], "incomplete"
            latest = max(
                (p.stat().st_mtime for p in path.iterdir() if p.is_file() and not p.is_symlink()),
                default=0,
            )
            recent = time.time() - latest <= RUNNING_WINDOW_SECONDS
            verdict = "RUNNING" if recent else "INTERRUPTED"
        plan = None
        if (path / "plan.json").is_file():
            plan = read_json(path / "plan.json", 52428800)
        snapshots = sorted(
            name[len("snapshots/") : -len(".json")]
            for name in names
            if name.startswith("snapshots/") and name.endswith(".json")
        )
        if completeness == "incomplete" and (path / "snapshots").is_dir():
            snapshots = sorted(
                p.stem for p in (path / "snapshots").glob("*.json") if not p.is_symlink()
            )
        artifacts = {
            hashlib.sha256(f"{path.name}/{name}".encode()).hexdigest()[:32]: name for name in names
        }
        return {
            "path": path,
            "manifest": manifest,
            "report": report,
            "plan": plan,
            "verdict": verdict,
            "completeness": completeness,
            "snapshots": [s for s in snapshots if IDENTIFIER.fullmatch(s)],
            "artifacts": artifacts,
        }

    def summary(self, run_id: str) -> dict | None:
        try:
            run = self.run(run_id)
        except Problem as problem:
            # Corrupt or unsupported runs still appear, labelled, rather than vanishing.
            return {"run_id": run_id, "verdict": "ERROR", "problem": problem.code}
        manifest, report = run["manifest"], run["report"]
        return {
            "run_id": run_id,
            "scenario_id": manifest.get("scenario_id"),
            "variant": manifest.get("variant"),
            "created_at": manifest.get("created_at", ""),
            "verdict": run["verdict"],
            "completeness": run["completeness"],
            "origin": manifest.get("origin", "run"),
            "source_run_id": manifest.get("source_run_id"),
            "group_id": manifest.get("group_id"),
            "coverage": (
                {
                    "required_applied": report["coverage"]["required_applied"],
                    "required_total": report["coverage"]["required_total"],
                }
                if report
                else None
            ),
        }

    def run_ids(self) -> list[str]:
        if not self.runs_dir.is_dir():
            return []
        ids = []
        for index, entry in enumerate(self.runs_dir.iterdir()):
            if index >= MAX_RUN_DIRECTORIES:
                raise Problem(422, "corrupt_evidence", "Run directory exceeds the listing limit")
            try:
                if (
                    str(UUID(entry.name)) == entry.name
                    and entry.is_dir()
                    and not entry.is_symlink()
                ):
                    ids.append(entry.name)
            except ValueError:
                continue
        return ids

    def group(self, group_id: str) -> dict:
        if self.groups_dir is None:
            raise Problem(404, "not_found", "Group records are not served from this root")
        path = self.groups_dir / f"{canonical_id(group_id)}.json"
        if path.is_symlink() or not path.is_file():
            raise Problem(404, "not_found", "Unknown group")
        record = read_json(path)
        if not isinstance(record, dict) or record.get("group_id") != group_id:
            raise Problem(422, "corrupt_evidence", "Group identity does not match")
        if record.get("group_schema_version") != 1:
            raise Problem(409, "unsupported_version", "Unsupported group version")
        return record


def encode_cursor(value: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode()).decode()


def decode_cursor(value: str) -> dict:
    try:
        raw = json.loads(base64.urlsafe_b64decode(value.encode("ascii")))
        if not (
            isinstance(raw, dict)
            and set(raw) == {"boundary", "created_at", "run_id"}
            and all(isinstance(raw[k], str) for k in raw)
        ):
            raise ValueError
        return raw
    except (ValueError, UnicodeError) as exc:
        raise Problem(400, "invalid_query", "Malformed cursor") from exc


def bounded_int(request: Request, name: str, default: int, low: int, high: int) -> int:
    raw = request.query_params.get(name)
    if raw is None:
        return default
    if not re.fullmatch(r"\d{1,9}", raw) or not low <= int(raw) <= high:
        raise Problem(400, "invalid_query", f"{name} must be an integer from {low} to {high}")
    return int(raw)


def create_app(runs_dir: Path, groups_dir: Path | None, token: str, port_holder: dict) -> FastAPI:
    store = EvidenceStore(runs_dir, groups_dir)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    def allowed(kind: str) -> set[str]:
        port = port_holder["port"]
        hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        return hosts if kind == "host" else {f"http://{host}" for host in hosts}

    def envelope(request: Request, data, meta: dict | None = None, status: int = 200):
        body = json.dumps(
            {"data": presentable(data), "meta": {"api_version": API_VERSION, **(meta or {})}},
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
        etag = '"' + hashlib.sha256(body).hexdigest()[:40] + '"'
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers={"ETag": etag})
        return Response(body, status, media_type="application/json", headers={"ETag": etag})

    def problem(status: int, code: str, message: str, details: dict | None = None):
        return JSONResponse(
            {"error": {"code": code, "message": message, "details": details or {}}},
            status_code=status,
        )

    @app.middleware("http")
    async def boundary(request: Request, call_next):
        if request.headers.get("host") not in allowed("host"):
            response = problem(403, "forbidden_host", "Host header is not this local server")
        elif "origin" in request.headers and request.headers["origin"] not in allowed("origin"):
            response = problem(403, "forbidden_origin", "Cross-origin requests are rejected")
        elif request.method not in {"GET", "HEAD"}:
            response = problem(405, "method_not_allowed", "This API is read-only")
        elif request.url.path.startswith("/api/") and not hmac.compare_digest(
            request.headers.get("authorization", "").encode(), f"Bearer {token}".encode()
        ):
            response = problem(401, "unauthorized", "Missing or invalid local access token")
        else:
            try:
                response = await call_next(request)
            except Exception:
                response = problem(500, "internal_error", "Unexpected server error")
        for name, value in SECURITY_HEADERS.items():
            response.headers[name] = value
        if request.url.path.startswith("/api/") or request.url.path == "/health":
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(Problem)
    async def handle_problem(_request: Request, exc: Problem):
        return problem(exc.status, exc.code, exc.message, exc.details)

    @app.get("/health")
    def health(request: Request):
        return envelope(request, {"status": "ok", "api_version": API_VERSION})

    @app.get("/api/v1/runs")
    def runs(request: Request):
        limit = bounded_int(request, "limit", 50, 1, 200)
        verdict = request.query_params.get("verdict")
        if verdict is not None and verdict not in VERDICTS:
            raise Problem(400, "invalid_query", "Unknown verdict filter")
        cursor = request.query_params.get("cursor")
        position = decode_cursor(cursor) if cursor else None
        boundary = position["boundary"] if position else datetime.now(UTC).isoformat()
        summaries = [s for s in (store.summary(i) for i in store.run_ids()) if s]
        # Newest first; runs created after the first page's boundary never shift later pages.
        summaries = [s for s in summaries if s.get("created_at", "") <= boundary]
        if verdict:
            summaries = [s for s in summaries if s["verdict"] == verdict]
        summaries.sort(key=lambda s: (s.get("created_at", ""), s["run_id"]), reverse=True)
        if position:
            key = (position["created_at"], position["run_id"])
            summaries = [s for s in summaries if (s.get("created_at", ""), s["run_id"]) < key]
        page = summaries[:limit]
        following = None
        if len(summaries) > limit:
            last = page[-1]
            following = encode_cursor(
                {
                    "boundary": boundary,
                    "created_at": last.get("created_at", ""),
                    "run_id": last["run_id"],
                }
            )
        return envelope(request, page, {"next_cursor": following, "limit": limit})

    @app.get("/api/v1/runs/{run_id}")
    def run(request: Request, run_id: str):
        value = store.run(run_id)
        diagnostics = []
        if value["completeness"] == "incomplete":
            diagnostics.append(
                {"code": "incomplete_bundle", "message": "No final report; not a completed run"}
            )
        artifacts = [
            {
                "artifact_id": artifact_id,
                "name": name,
                "bytes": value["manifest"]["artifacts"][name]["bytes"],
                "sha256": value["manifest"]["artifacts"][name]["sha256"],
            }
            for artifact_id, name in value["artifacts"].items()
        ]
        manifest = {k: v for k, v in value["manifest"].items() if k != "execution_root"}
        data = {
            "run_id": run_id,
            "completeness": value["completeness"],
            "verdict": value["verdict"],
            "manifest": manifest,
            "report": value["report"],
            "plan": value["plan"],
            "artifacts": artifacts,
            "snapshots": value["snapshots"],
            "diagnostics": diagnostics,
        }
        return envelope(request, data, {"report_schema_version": 1})

    @app.get("/api/v1/runs/{run_id}/events")
    def events(request: Request, run_id: str):
        value = store.run(run_id)
        after = bounded_int(request, "after_sequence", 0, 0, 10**9)
        limit = bounded_int(request, "limit", 200, 1, 1000)
        history = value["path"] / "history.ndjson"
        page, more, notes, last = [], False, [], after
        if history.is_file() and not history.is_symlink():
            with history.open("rb") as stream:
                for raw in stream:
                    if not raw.endswith(b"\n"):
                        notes.append("Incomplete trailing history line excluded")
                        break
                    try:
                        event = json.loads(raw)
                    except ValueError:
                        notes.append("Unreadable history line; later events are not shown")
                        break
                    sequence = event.get("sequence")
                    if type(sequence) is not int or sequence <= after:
                        continue
                    if len(page) == limit:
                        more = True
                        break
                    page.append(event)
                    last = sequence
        if value["completeness"] == "incomplete":
            notes.append("Run is not complete; history may still grow or be truncated")
        data = {"events": page, "next_after_sequence": last, "has_more": more}
        return envelope(request, data, {"diagnostics": notes})

    @app.get("/api/v1/runs/{run_id}/invariants")
    def invariants(request: Request, run_id: str):
        value = store.run(run_id)
        if value["report"] is None:
            raise Problem(404, "artifact_not_ready", "No final report yet")
        return envelope(request, value["report"]["invariants"])

    @app.get("/api/v1/runs/{run_id}/snapshots/{snapshot_id}")
    def snapshot(request: Request, run_id: str, snapshot_id: str):
        value = store.run(run_id)
        if not IDENTIFIER.fullmatch(snapshot_id) or snapshot_id not in value["snapshots"]:
            raise Problem(404, "artifact_not_ready", "Snapshot is not in the inventory")
        content = read_json(value["path"] / "snapshots" / f"{snapshot_id}.json", MAX_SNAPSHOT_BYTES)
        records = content.get("records") if isinstance(content, dict) else None
        if not isinstance(records, dict):
            raise Problem(422, "corrupt_evidence", "Snapshot has no record sets")
        record_set = request.query_params.get("record_set")
        if record_set is None or record_set not in records:
            raise Problem(404, "not_found", "Unknown record set", {"record_sets": sorted(records)})
        offset = bounded_int(request, "offset", 0, 0, 10**9)
        limit = bounded_int(request, "limit", 100, 1, 1000)
        rows = records[record_set]
        data = {
            "snapshot_id": snapshot_id,
            "record_set": record_set,
            "record_sets": sorted(records),
            "rows": rows[offset : offset + limit],
            "total": len(rows),
            "offset": offset,
            "limit": limit,
            "complete": content.get("complete"),
            "quiescent": content.get("quiescent"),
            "content_hash": content.get("content_hash"),
        }
        return envelope(request, data)

    @app.get("/api/v1/runs/{run_id}/artifacts/{artifact_id}")
    def artifact(request: Request, run_id: str, artifact_id: str):
        value = store.run(run_id)
        name = value["artifacts"].get(artifact_id)
        if name is None:
            raise Problem(404, "not_found", "Unknown artifact")
        expected = value["manifest"]["artifacts"][name]
        raw = (value["path"] / name).read_bytes()
        if len(raw) != expected["bytes"] or hashlib.sha256(raw).hexdigest() != expected["sha256"]:
            raise Problem(422, "corrupt_evidence", "Artifact changed after verification")
        media = "application/json" if name.endswith(".json") else "text/plain; charset=utf-8"
        filename = re.sub(r"[^A-Za-z0-9._-]", "_", name.replace("/", "_"))
        return Response(
            raw,
            media_type=media,
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @app.get("/api/v1/groups/{group_id}")
    def group(request: Request, group_id: str):
        return envelope(request, store.group(group_id), {"group_schema_version": 1})

    @app.get("/api/{rest:path}")
    def unknown_api(rest: str):
        raise Problem(404, "not_found", "Unknown API route")

    @app.get("/assets/{name}")
    def asset(name: str):
        path = ASSETS / "assets" / name
        if not ASSET_NAME.fullmatch(name) or not path.is_file():
            raise Problem(404, "not_found", "Unknown asset")
        return FileResponse(path)

    @app.get("/{rest:path}")
    def index(rest: str):
        # Client-side routes all receive the same data-free page.
        page = ASSETS / "index.html"
        if not page.is_file():
            raise Problem(404, "viewer_not_built", "Viewer assets are not packaged")
        return FileResponse(page, media_type="text/html")

    return app


def new_token() -> str:
    return secrets.token_urlsafe(32)
