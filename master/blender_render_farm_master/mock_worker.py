from __future__ import annotations

from argparse import ArgumentParser
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event, Lock, Thread
import json
import socket
import time

from app.core.models import normalize_render_device_preference


class MockWorkerState:
    def __init__(
        self,
        name: str,
        host: str,
        port: int,
        blender_version: str,
        gpu: str,
        seconds_per_frame: float,
        render_device_preference: str = "GPU",
    ) -> None:
        self.name = name
        self.host = host
        self.port = port
        self.blender_version = blender_version
        self.gpu = gpu
        self.seconds_per_frame = seconds_per_frame
        self.blender_found = True
        self.blender_path = r"C:\Program Files\Blender Foundation\Blender 4.3\blender.exe"
        self.status = "idle"
        self.current_job: dict | None = None
        self.last_log = ""
        self.error: str | None = None
        self.render_device_preference = normalize_render_device_preference(render_device_preference) or "GPU"
        self.logs: deque[str] = deque(maxlen=500)
        self._lock = Lock()
        self._cancel_event = Event()
        self._thread: Thread | None = None

    @property
    def effective_use_gpu(self) -> bool:
        return self.render_device_preference == "GPU"

    def log(self, message: str) -> None:
        entry = f"[{time.strftime('%H:%M:%S')}] {message}"
        with self._lock:
            self.last_log = message
            self.logs.append(entry)

    def machine_ip(self) -> str:
        if self.host not in {"0.0.0.0", "::"}:
            return self.host
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return "127.0.0.1"

    def capabilities(self) -> dict:
        return {
            "machineName": self.name,
            "blenderFound": self.blender_found,
            "blenderPath": self.blender_path,
            "blenderVersion": self.blender_version,
            "gpu": self.gpu,
            "renderDevicePreference": self.render_device_preference,
            "effectiveUseGpu": self.effective_use_gpu,
            "useGpu": True,
        }

    def render_device(self) -> dict:
        with self._lock:
            return {
                "machineName": self.name,
                "renderDevicePreference": self.render_device_preference,
                "effectiveUseGpu": self.effective_use_gpu,
            }

    def set_render_device(self, render_device_preference: str) -> tuple[dict, int]:
        normalized = normalize_render_device_preference(render_device_preference)
        if normalized is None:
            return {
                "ok": False,
                "message": "renderDevicePreference invalido.",
            }, 400
        with self._lock:
            self.render_device_preference = normalized
        self.log(f"Render device local atualizado para {normalized}.")
        return {
            "ok": True,
            "renderDevicePreference": self.render_device_preference,
            "effectiveUseGpu": self.effective_use_gpu,
        }, 200

    def health(self) -> dict:
        with self._lock:
            return {
                "ok": True,
                "machineName": self.name,
                "status": self.status,
            }

    def status_payload(self) -> dict:
        with self._lock:
            payload = {
                "machineName": self.name,
                "ip": self.machine_ip(),
                "port": self.port,
                "status": self.status,
                "blenderFound": self.blender_found,
                "blenderPath": self.blender_path,
                "blenderVersion": self.blender_version,
                "gpu": self.gpu,
                "renderDevicePreference": self.render_device_preference,
                "effectiveUseGpu": self.effective_use_gpu,
                "lastLog": self.last_log,
                "error": self.error,
            }
            if not self.current_job:
                payload["currentJob"] = None
                payload["currentBlock"] = None
                return payload

            payload.update(
                {
                    "jobId": self.current_job["jobId"],
                    "blockId": self.current_job["blockId"],
                    "blendFile": self.current_job["blendFile"],
                    "outputPattern": self.current_job["outputPattern"],
                    "startFrame": self.current_job["startFrame"],
                    "endFrame": self.current_job["endFrame"],
                    "currentFrame": self.current_job.get("currentFrame"),
                    "progress": self.current_job.get("progress", 0.0),
                    "elapsedSeconds": self.current_job.get("elapsedSeconds", 0),
                }
            )
            return payload

    def validate_paths(self, blend_file: str, output_folder: str) -> dict:
        blend_path = Path(blend_file)
        output_path = Path(output_folder)
        blend_ok = blend_path.exists() and blend_path.suffix.lower() == ".blend"
        output_ok = output_path.exists() and output_path.is_dir()
        message = "Paths validos." if blend_ok and output_ok else "Worker sem acesso a um ou mais caminhos."
        self.log(message)
        return {
            "ok": blend_ok and output_ok,
            "blendAccessible": blend_ok,
            "outputAccessible": output_ok,
            "message": message,
        }

    def start_render(self, payload: dict) -> tuple[dict, int]:
        with self._lock:
            if self.status == "rendering":
                return {
                    "ok": False,
                    "message": "Worker ja esta renderizando.",
                }, 409
            self._cancel_event.clear()
            self.status = "rendering"
            self.error = None
            self.current_job = {
                **payload,
                "currentFrame": payload["startFrame"],
                "progress": 0.0,
                "elapsedSeconds": 0,
                "effectiveUseGpu": self.effective_use_gpu,
            }

        self.log(
            "Recebido bloco "
            f"{payload['blockId']} ({payload['startFrame']}-{payload['endFrame']}) "
            f"com useGpu legado={payload.get('useGpu')} e device efetivo={self.render_device_preference}."
        )
        self._thread = Thread(target=self._render_loop, daemon=True)
        self._thread.start()
        return {"ok": True, "accepted": True}, 200

    def cancel(self) -> dict:
        self._cancel_event.set()
        self.log("Cancelamento solicitado.")
        return {"ok": True}

    def list_logs(self) -> dict:
        with self._lock:
            return {"logs": list(self.logs)}

    def _render_loop(self) -> None:
        start_time = time.time()
        job_snapshot = None
        with self._lock:
            if self.current_job:
                job_snapshot = dict(self.current_job)
        if not job_snapshot:
            return

        total_frames = max(1, job_snapshot["endFrame"] - job_snapshot["startFrame"] + 1)
        for index, frame_number in enumerate(range(job_snapshot["startFrame"], job_snapshot["endFrame"] + 1), start=1):
            if self._cancel_event.is_set():
                with self._lock:
                    self.status = "idle"
                    self.current_job = None
                    self.error = "Render cancelado."
                self.log("Render cancelado antes da conclusao do bloco.")
                return

            with self._lock:
                if self.current_job:
                    self.current_job["currentFrame"] = frame_number
                    self.current_job["progress"] = ((index - 1) / total_frames) * 100.0
                    self.current_job["elapsedSeconds"] = int(time.time() - start_time)
            self.log(f"Fra:{frame_number} Mock render em andamento")
            time.sleep(self.seconds_per_frame)
            frame_path = self._frame_path(
                output_pattern=job_snapshot["outputPattern"],
                frame_number=frame_number,
                image_format=job_snapshot.get("format", "PNG"),
            )
            frame_path.parent.mkdir(parents=True, exist_ok=True)
            frame_path.write_bytes(f"mock frame {frame_number}\n".encode("utf-8"))

        with self._lock:
            if self.current_job:
                self.current_job["progress"] = 100.0
                self.current_job["elapsedSeconds"] = int(time.time() - start_time)
            self.status = "idle"
            self.current_job = None
            self.error = None
        self.log(f"Bloco {job_snapshot['blockId']} concluido.")

    def _frame_path(self, output_pattern: str, frame_number: int, image_format: str) -> Path:
        placeholder = "####" if "####" in output_pattern else "#" * output_pattern.count("#")
        padded = str(frame_number).zfill(len(placeholder))
        candidate = Path(output_pattern.replace(placeholder, padded))
        if candidate.suffix:
            return candidate
        return candidate.with_suffix(f".{image_format.lower()}")


class MockWorkerHandler(BaseHTTPRequestHandler):
    state: MockWorkerState

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            return self._send_json(self.state.health())
        if self.path == "/status":
            return self._send_json(self.state.status_payload())
        if self.path == "/capabilities":
            return self._send_json(self.state.capabilities())
        if self.path == "/render-device":
            return self._send_json(self.state.render_device())
        if self.path == "/logs":
            return self._send_json(self.state.list_logs())
        self._send_json({"ok": False, "message": "Endpoint nao encontrado."}, status=404)

    def do_POST(self) -> None:  # noqa: N802
        payload = self._read_json()
        if self.path == "/validate-paths":
            return self._send_json(
                self.state.validate_paths(
                    blend_file=payload.get("blendFile", ""),
                    output_folder=payload.get("outputFolder", ""),
                )
            )
        if self.path == "/render-block":
            body, status = self.state.start_render(payload)
            return self._send_json(body, status=status)
        if self.path == "/render-device":
            body, status = self.state.set_render_device(payload.get("renderDevicePreference", ""))
            return self._send_json(body, status=status)
        if self.path == "/cancel":
            return self._send_json(self.state.cancel())
        self._send_json({"ok": False, "message": "Endpoint nao encontrado."}, status=404)

    def log_message(self, format: str, *args) -> None:
        return

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            return {}

    def _send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def build_parser() -> ArgumentParser:
    parser = ArgumentParser(description="Mock worker para Blender Render Farm Master")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9000)
    parser.add_argument("--name", default="MOCK-WORKER-01")
    parser.add_argument("--blender-version", default="4.3")
    parser.add_argument("--gpu", default="Mock GPU")
    parser.add_argument("--seconds-per-frame", type=float, default=0.8)
    parser.add_argument("--render-device-preference", choices=["CPU", "GPU"], default="GPU")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    state = MockWorkerState(
        name=args.name,
        host=args.host,
        port=args.port,
        blender_version=args.blender_version,
        gpu=args.gpu,
        seconds_per_frame=args.seconds_per_frame,
        render_device_preference=args.render_device_preference,
    )
    MockWorkerHandler.state = state
    server = ThreadingHTTPServer((args.host, args.port), MockWorkerHandler)
    print(f"Mock worker ativo em http://{args.host}:{args.port} ({args.name})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Encerrando mock worker...")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
