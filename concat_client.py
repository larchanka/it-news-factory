from __future__ import annotations

import argparse
import http.client
import json
import logging
import secrets
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any, Self

if TYPE_CHECKING:
    from timeline import TimelineResult

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = "~/.config/concat/automation.json"
DEFAULT_HOST = "127.0.0.1"


# --- Exceptions ---


class ConcatError(Exception):
    """Base exception for all Concat client errors."""


class ConcatConnectionError(ConcatError):
    """Raised when connecting to Concat Desktop or automation config fails."""


class ConcatAuthError(ConcatError):
    """Raised when authentication with Bearer token fails (401/403)."""


class ConcatRPCError(ConcatError):
    """Raised when Concat JSON-RPC server returns an error response."""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(f"Concat RPC error ({code}): {message}")
        self.code = code
        self.message = message
        self.data = data


class ConcatExportTimeoutError(ConcatError):
    """Raised when Concat export does not finish within the specified timeout."""


# --- Configuration Model & Parser ---


@dataclass(frozen=True)
class AutomationConfig:
    port: int
    token: str
    host: str = DEFAULT_HOST


def load_automation_config(config_path: str | Path = DEFAULT_CONFIG_PATH) -> AutomationConfig:
    """Read and validate connection parameters from Concat automation.json."""
    resolved_path = Path(config_path).expanduser().resolve()
    if not resolved_path.is_file():
        raise ConcatConnectionError(
            f"Automation config file not found at '{resolved_path}'. "
            "Ensure Concat Desktop is running with automation enabled under Settings > Automation."
        )

    try:
        content = resolved_path.read_text(encoding="utf-8")
        data = json.loads(content)
    except json.JSONDecodeError as e:
        raise ConcatConnectionError(
            f"Failed to parse automation config '{resolved_path}': {e}"
        ) from e
    except OSError as e:
        raise ConcatConnectionError(
            f"Failed to read automation config '{resolved_path}': {e}"
        ) from e

    if not isinstance(data, dict):
        raise ConcatConnectionError(
            f"Invalid automation config '{resolved_path}': expected JSON object, got {type(data).__name__}"
        )

    port = data.get("port")
    token = data.get("token")
    host = str(data.get("host") or DEFAULT_HOST)

    if port is None or token is None:
        raise ConcatConnectionError(
            f"Automation config at '{resolved_path}' must contain 'port' and 'token' fields."
        )

    try:
        port_int = int(port)
    except (TypeError, ValueError) as e:
        raise ConcatConnectionError(f"Invalid 'port' value in '{resolved_path}': {port}") from e

    return AutomationConfig(port=port_int, token=str(token), host=host)


# --- Mock Server for Offline Testing ---


class _MockConcatHTTPHandler(BaseHTTPRequestHandler):
    server: MockConcatServer.MockHTTPServer  # type: ignore[name-defined]

    def log_message(self, format: str, *args: Any) -> None:
        pass  # Suppress standard logging during tests

    def do_POST(self) -> None:
        server = self.server.owner
        auth_header = self.headers.get("Authorization", "")
        expected_auth = f"Bearer {server.token}"

        if auth_header != expected_auth:
            self.send_response(http.client.UNAUTHORIZED)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            err_body = json.dumps({"error": "Unauthorized: invalid bearer token"}).encode("utf-8")
            self.wfile.write(err_body)
            return

        content_length = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(content_length) if content_length > 0 else b"{}"

        try:
            req_data = json.loads(raw_body.decode("utf-8"))
        except json.JSONDecodeError:
            self.send_response(http.client.BAD_REQUEST)
            self.end_headers()
            return

        headers_dict = {k.lower(): v for k, v in self.headers.items()}
        server.record_request({"headers": headers_dict, "body": req_data})

        req_id = req_data.get("id", 1)
        method = req_data.get("method", "")
        params = req_data.get("params", {})

        response_payload = server.handle_rpc(req_id, method, params)

        self.send_response(http.client.OK)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(response_payload).encode("utf-8"))


class MockConcatServer:
    """In-process mock Concat JSON-RPC server for integration and unit testing."""

    class MockHTTPServer(HTTPServer):
        def __init__(self, server_address: tuple[str, int], owner: MockConcatServer) -> None:
            super().__init__(server_address, _MockConcatHTTPHandler)
            self.owner = owner

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        port: int = 0,
        token: str | None = None,
        version: str = "0.2.9",
    ) -> None:
        self.host = host
        self.token = token or secrets.token_hex(16)
        self.version = version
        self._httpd = self.MockHTTPServer((self.host, port), self)
        self.port = int(self._httpd.server_address[1])
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._requests: list[dict[str, Any]] = []
        self._export_behavior = "succeed"
        self._poll_counts: dict[str, int] = {}
        self._lock = threading.Lock()

    def __enter__(self) -> Self:
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object,
    ) -> None:
        self.stop()

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        if self._thread.is_alive():
            self._thread.join(timeout=2.0)

    def write_automation_json(self, path: Path | str) -> Path:
        target = Path(path).resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps({"host": self.host, "port": self.port, "token": self.token}),
            encoding="utf-8",
        )
        return target

    def set_export_behavior(self, behavior: str) -> None:
        with self._lock:
            self._export_behavior = behavior

    def record_request(self, req: dict[str, Any]) -> None:
        with self._lock:
            self._requests.append(req)

    @property
    def last_request(self) -> dict[str, Any] | None:
        with self._lock:
            return self._requests[-1] if self._requests else None

    def _handle_project_rpc(
        self, req_id: Any, method: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        if method == "project_create":
            name = params.get("name", "short")
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"project_id": f"proj_{secrets.token_hex(4)}_{name}"},
            }
        return {"jsonrpc": "2.0", "id": req_id, "result": {"status": "ok"}}

    def _handle_media_and_timeline_rpc(
        self, req_id: Any, method: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        if method == "media_import":
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"media_id": f"med_{secrets.token_hex(4)}"},
            }
        if method == "clip_add":
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"clip_id": f"clip_{secrets.token_hex(4)}"},
            }
        if method == "captions_add":
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"status": "ok", "captions_id": f"cap_{secrets.token_hex(4)}"},
            }
        return {"jsonrpc": "2.0", "id": req_id, "result": {"status": "ok"}}

    def _handle_export_rpc(
        self, req_id: Any, method: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        if method == "export_start":
            out_p = params.get("output_path")
            if out_p:
                try:
                    p = Path(out_p)
                    p.parent.mkdir(parents=True, exist_ok=True)
                    if not p.is_file():
                        p.write_bytes(b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00isommp42")
                except OSError:
                    pass
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"export_id": f"exp_{secrets.token_hex(4)}"},
            }

        exp_id = params.get("export_id", "default")
        with self._lock:
            behavior = self._export_behavior
            self._poll_counts[exp_id] = self._poll_counts.get(exp_id, 0) + 1
            count = self._poll_counts[exp_id]

        if behavior == "always_pending":
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"status": "processing", "progress": 0.5},
            }
        if behavior == "fail":
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"status": "failed", "error": "Export failed during render"},
            }

        status = "completed" if count >= 2 else "processing"
        progress = 1.0 if count >= 2 else 0.8
        return {"jsonrpc": "2.0", "id": req_id, "result": {"status": status, "progress": progress}}

    def handle_rpc(self, req_id: Any, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method in ("ping", "window.ping", "status"):
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"status": "ok", "version": self.version},
            }
        if method == "echo_test":
            return {"jsonrpc": "2.0", "id": req_id, "result": {"echo": params.get("message")}}
        if method == "trigger_error":
            code = params.get("code", -32000)
            msg = params.get("message", "Simulated RPC error")
            return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": msg}}
        if method in ("project_create", "project_set_video", "project_save"):
            return self._handle_project_rpc(req_id, method, params)
        if method in ("media_import", "clip_add", "keyframe_set", "captions_add"):
            return self._handle_media_and_timeline_rpc(req_id, method, params)
        if method in ("export_start", "export_status", "export_wait"):
            return self._handle_export_rpc(req_id, method, params)

        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32601, "message": f"Method '{method}' not found"},
        }


def _resolve_clip_track_and_media(
    first: str | None,
    second: str | None,
    track: str | None,
    track_type: str | None,
    media_id: str | None,
) -> tuple[str, str]:
    chosen_track = track if track else track_type
    chosen_media = media_id

    if chosen_media is not None:
        return chosen_track if chosen_track else "video", chosen_media

    if first is not None and second is not None:
        if first in ("video", "audio"):
            return first, second
        if second in ("video", "audio"):
            return second, first

    target_media = first if first is not None else ""
    return chosen_track if chosen_track else "video", target_media


# --- Concat Client ---


class ConcatClient:
    """Manages IPC with Concat Desktop in isolated non-destructive window mode."""

    def __init__(
        self,
        config_path: str | Path | None = None,
        host: str | None = None,
        port: int | None = None,
        token: str | None = None,
        timeout: float = 30.0,
        mock: bool = False,
    ) -> None:
        self.config_path = config_path or DEFAULT_CONFIG_PATH
        self.host = host or DEFAULT_HOST
        self.port = port
        self.token = token
        self._static_credentials = port is not None and token is not None
        self.timeout = timeout
        self.mock = mock

        self._request_counter = 0
        self._connected = False
        self._mock_data: dict[str, Any] = {}

    def _next_id(self) -> int:
        self._request_counter += 1
        return self._request_counter

    def connect(self, config_path: str | Path | None = None) -> None:
        """Resolve dynamic port and token, and verify connection to Concat Desktop."""
        if self.mock:
            self._connected = True
            logger.info("ConcatClient connected in offline mock mode.")
            return

        cfg_path = config_path or self.config_path
        if not self._static_credentials:
            config = load_automation_config(cfg_path)
            self.host = config.host
            self.port = config.port
            self.token = config.token

        self.ping()
        self._connected = True
        logger.info("Connected to Concat Desktop at %s:%s", self.host, self.port)

    def disconnect(self) -> None:
        """Close connection session."""
        self._connected = False

    def _mock_rpc_call(self, method: str, params: dict[str, Any]) -> Any:
        token_hex = secrets.token_hex(4)
        mock_map: dict[str, Any] = {
            "ping": {"status": "ok", "version": "0.2.9", "mock": True},
            "window.ping": {"status": "ok", "version": "0.2.9", "mock": True},
            "status": {"status": "ok", "version": "0.2.9", "mock": True},
            "project_create": {
                "project_id": f"mock_proj_{token_hex}_{params.get('name', 'short')}"
            },
            "media_import": {"media_id": f"mock_med_{token_hex}"},
            "clip_add": {"clip_id": f"mock_clip_{token_hex}"},
            "captions_add": {"status": "ok", "captions_id": f"mock_cap_{token_hex}"},
            "export_start": {"export_id": f"mock_exp_{token_hex}"},
            "export_status": {"status": "completed", "progress": 1.0},
            "export_wait": {"status": "completed", "progress": 1.0},
        }
        return mock_map.get(method, {"status": "ok"})

    def _send_http_request(self, payload: dict[str, Any], timeout: float | None) -> bytes:
        data = json.dumps(payload).encode("utf-8")
        url = f"http://{self.host}:{self.port}/rpc"
        req = urllib.request.Request(
            url,
            data=data,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.token}",
            },
            method="POST",
        )
        effective_timeout = timeout if timeout is not None else self.timeout

        try:
            with urllib.request.urlopen(req, timeout=effective_timeout) as resp:
                return bytes(resp.read())
        except urllib.error.HTTPError as e:
            if e.code in (http.client.UNAUTHORIZED, http.client.FORBIDDEN):
                raise ConcatAuthError(
                    f"Unauthorized connection to Concat Desktop at {self.host}:{self.port} (HTTP {e.code}). "
                    "Verify token in automation.json."
                ) from e
            raise ConcatConnectionError(
                f"HTTP error communicating with Concat Desktop ({e.code}): {e.reason}"
            ) from e
        except (urllib.error.URLError, ConnectionError, OSError) as e:
            raise ConcatConnectionError(
                f"Failed to communicate with Concat Desktop at {self.host}:{self.port}: {e}"
            ) from e

    def _parse_rpc_response(self, resp_bytes: bytes) -> Any:
        try:
            resp_json = json.loads(resp_bytes.decode("utf-8"))
        except json.JSONDecodeError as e:
            raise ConcatRPCError(-32700, f"Malformed JSON response from Concat Desktop: {e}") from e

        if "error" in resp_json and resp_json["error"] is not None:
            err = resp_json["error"]
            code = err.get("code", -32000) if isinstance(err, dict) else -32000
            msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
            data = err.get("data") if isinstance(err, dict) else None
            raise ConcatRPCError(code=code, message=msg, data=data)

        return resp_json.get("result")

    def rpc_call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> Any:
        """Send a JSON-RPC 2.0 request over HTTP/TCP with Bearer authentication."""
        call_params = params or {}
        if self.mock:
            return self._mock_rpc_call(method, call_params)

        if self.port is None or self.token is None:
            raise ConcatConnectionError("ConcatClient not connected. Call connect() first.")

        payload = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": method,
            "params": call_params,
        }
        resp_bytes = self._send_http_request(payload, timeout)
        return self._parse_rpc_response(resp_bytes)

    def ping(self) -> dict[str, Any]:
        """Test connection and return Concat version/status handshake."""
        res = self.rpc_call("ping")
        return res if isinstance(res, dict) else {"status": "ok"}

    def project_create(self, name: str = "short", path: str | None = None, **kwargs: Any) -> str:
        """Create an isolated non-destructive project in window mode."""
        payload: dict[str, Any] = {"name": name, "window_mode": True, **kwargs}
        if path is not None:
            payload["path"] = str(Path(path).resolve())

        res = self.rpc_call("project_create", payload)
        if isinstance(res, dict) and "project_id" in res:
            return str(res["project_id"])
        if isinstance(res, str):
            return res
        return str(res or f"proj_{secrets.token_hex(4)}")

    def project_set_video(
        self,
        width: int = 1080,
        height: int = 1920,
        fps: int = 30,
        timeline_background: str = "blur",
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Set project resolution, frame rate, and canvas background fill."""
        payload = {
            "width": int(width),
            "height": int(height),
            "fps": int(fps),
            "timeline_background": timeline_background,
            **kwargs,
        }
        res = self.rpc_call("project_set_video", payload)
        return res if isinstance(res, dict) else {"status": "ok"}

    def media_import(self, file_path: str | Path, **kwargs: Any) -> str:
        """Import an asset into the project media library."""
        abs_path = str(Path(file_path).resolve())
        payload = {"file_path": abs_path, **kwargs}
        res = self.rpc_call("media_import", payload)
        if isinstance(res, dict) and "media_id" in res:
            return str(res["media_id"])
        if isinstance(res, str):
            return res
        return str(res or f"med_{secrets.token_hex(4)}")

    def clip_add(
        self,
        first: str | None = None,
        second: str | None = None,
        start_time: float = 0.0,
        duration: float = 0.0,
        in_point: float = 0.0,
        *,
        track: str | None = None,
        track_type: str | None = None,
        media_id: str | None = None,
        **kwargs: Any,
    ) -> str:
        """Add a media clip to the timeline.

        Supports positional and keyword call styles:
          clip_add(track, media_id, start_time, duration, in_point=0)
          clip_add(media_id, track_type, start_time, duration)
          clip_add(track="video", media_id=media_id, start_time=0.0, duration=8.0)
        """
        resolved_track, resolved_media_id = _resolve_clip_track_and_media(
            first, second, track, track_type, media_id
        )

        payload = {
            "track": resolved_track,
            "track_type": resolved_track,
            "media_id": resolved_media_id,
            "start_time": float(start_time),
            "duration": float(duration),
            "in_point": float(in_point),
            **kwargs,
        }
        res = self.rpc_call("clip_add", payload)
        if isinstance(res, dict) and "clip_id" in res:
            return str(res["clip_id"])
        if isinstance(res, str):
            return res
        return str(res or f"clip_{secrets.token_hex(4)}")

    def keyframe_set(
        self,
        clip_id: str,
        property: str,
        time: float,
        value: float,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Apply a property keyframe (e.g. Ken Burns zoom scale)."""
        payload = {
            "clip_id": clip_id,
            "property": property,
            "time": float(time),
            "value": float(value),
            **kwargs,
        }
        res = self.rpc_call("keyframe_set", payload)
        return res if isinstance(res, dict) else {"status": "ok"}

    def captions_add(
        self,
        clip_id: str,
        language: str = "en",
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Generate word-synced subtitles via in-process whisper.cpp."""
        payload = {"clip_id": clip_id, "language": language, **kwargs}
        res = self.rpc_call("captions_add", payload)
        return res if isinstance(res, dict) else {"status": "ok"}

    def project_save(self, **kwargs: Any) -> dict[str, Any]:
        """Persist project state."""
        res = self.rpc_call("project_save", kwargs)
        return res if isinstance(res, dict) else {"status": "ok"}

    def export_start(
        self,
        output_path: str | Path,
        preset: str = "youtube_shorts",
        **kwargs: Any,
    ) -> str:
        """Trigger project rendering to destination video file."""
        abs_path = str(Path(output_path).resolve())
        payload = {"output_path": abs_path, "preset": preset, **kwargs}
        res = self.rpc_call("export_start", payload)
        if isinstance(res, dict) and "export_id" in res:
            return str(res["export_id"])
        if isinstance(res, str):
            return res
        return str(res or f"exp_{secrets.token_hex(4)}")

    def export_wait(
        self,
        export_id: str,
        timeout: float = 300.0,
        poll_interval: float = 1.0,
    ) -> dict[str, Any]:
        """Poll export status until render completes, fails, or times out."""
        start_time = time.time()
        while True:
            elapsed = time.time() - start_time
            if elapsed > timeout:
                raise ConcatExportTimeoutError(
                    f"Export '{export_id}' timed out after {elapsed:.1f}s (limit: {timeout}s)."
                )

            res = self.rpc_call("export_status", {"export_id": export_id})
            status_dict = res if isinstance(res, dict) else {"status": str(res)}
            status = str(status_dict.get("status", "")).lower()

            if status in ("completed", "success", "done"):
                return status_dict
            if status in ("failed", "error"):
                err_msg = status_dict.get("error", "Unknown export error")
                raise ConcatRPCError(code=-32001, message=f"Export failed: {err_msg}")

            time.sleep(poll_interval)

    def create_shorts_project(
        self,
        audio_path: str | Path,
        image_paths: Sequence[str | Path],
        video_paths: Sequence[str | Path] | None = None,
        project_name: str = "short",
        output_resolution: tuple[int, int] | list[int] = (1080, 1920),
        **kwargs: Any,
    ) -> TimelineResult:
        """High-level builder to construct complete shorts timeline conforming to audio duration."""
        from timeline import build_timeline

        return build_timeline(
            client=self,
            audio_path=audio_path,
            image_paths=image_paths,
            video_paths=video_paths,
            project_name=project_name,
            output_resolution=output_resolution,
            **kwargs,
        )


# --- CLI Handshake ---


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Concat Desktop Window JSON-RPC automation client."
    )
    parser.add_argument(
        "--ping",
        action="store_true",
        help="Test connection to running Concat instance and print status handshake.",
    )
    parser.add_argument(
        "--config",
        type=str,
        default=DEFAULT_CONFIG_PATH,
        help="Path to Concat automation.json configuration file.",
    )
    args = parser.parse_args(argv)

    if args.ping:
        try:
            client = ConcatClient(config_path=args.config)
            client.connect()
            handshake = client.ping()
            print("Connected to Concat Desktop successfully.")
            print(f"Host: {client.host}")
            print(f"Port: {client.port}")
            print(f"Version / Response: {handshake}")
            return 0
        except (ConcatError, OSError, ValueError) as e:
            print(f"Failed to connect to Concat Desktop: {e}", file=sys.stderr)
            return 1

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
