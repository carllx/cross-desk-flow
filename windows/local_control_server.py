"""Local TCP control server for WindowsBridgeController CLI requests."""

from __future__ import annotations

import json
import logging
import socket
import threading
from typing import TYPE_CHECKING, Optional

from bridge_core.contract import DEFAULT_LOCAL_IPC_PORT

if TYPE_CHECKING:
    from .controller import WindowsBridgeController

logger = logging.getLogger("windows.local_control_server")


class LocalControlServer:
    """TCP server running on 127.0.0.1:50106 to serve CLI requests."""

    def __init__(self, controller: "WindowsBridgeController", port: int = DEFAULT_LOCAL_IPC_PORT):
        self.controller = controller
        self.port = port
        self._server_sock: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._running = False

    def start(self) -> bool:
        if self._running:
            return True
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("127.0.0.1", self.port))
            s.listen(5)
            self._server_sock = s
            self._running = True
            self._thread = threading.Thread(
                target=self._serve_loop, daemon=True, name="LocalControlServer"
            )
            self._thread.start()
            return True
        except Exception as exc:
            logger.debug("Failed to start LocalControlServer on port %d: %s", self.port, exc)
            return False

    def stop(self) -> None:
        self._running = False
        if self._server_sock:
            try:
                self._server_sock.close()
            except Exception:
                pass
            self._server_sock = None
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)
        self._thread = None

    def _serve_loop(self) -> None:
        while self._running and self._server_sock:
            try:
                client, _ = self._server_sock.accept()
            except (OSError, socket.error):
                break

            try:
                data = client.recv(4096)
                if not data:
                    client.close()
                    continue
                req = json.loads(data.decode("utf-8"))
                cmd = req.get("command")

                res = {}
                if cmd == "start":
                    success = self.controller.start()
                    res = {"success": success, "desired_state": self.controller.get_status().desired_state}
                elif cmd == "stop":
                    success = self.controller.stop()
                    res = {"success": success, "desired_state": self.controller.get_status().desired_state}
                elif cmd == "reconcile":
                    self.controller.reconcile()
                    res = {"success": True}
                elif cmd == "status":
                    res = self.controller.get_status().to_dict()
                elif cmd == "mic-enable":
                    success = self.controller.set_microphone_enabled(True)
                    res = {"success": success, "microphone_path_state": self.controller.get_status().microphone_path_state}
                elif cmd == "mic-disable":
                    success = self.controller.set_microphone_enabled(False)
                    res = {"success": success, "microphone_path_state": self.controller.get_status().microphone_path_state}
                elif cmd == "dictation-start":
                    success = self.controller.start_dictation()
                    st = self.controller.get_status()
                    res = {
                        "success": success,
                        "mode": st.mode,
                        "microphone_path_state": st.microphone_path_state,
                        "speaker_path_state": st.speaker_path_state,
                        "error": st.last_actionable_microphone_error or st.last_actionable_error,
                    }
                elif cmd == "dictation-end":
                    success = self.controller.end_dictation()
                    st = self.controller.get_status()
                    res = {
                        "success": success,
                        "mode": st.mode,
                        "microphone_path_state": st.microphone_path_state,
                        "speaker_path_state": st.speaker_path_state,
                    }
                elif cmd == "shutdown":
                    self.controller.request_host_shutdown()
                    res = {"success": True}
                else:
                    res = {"error": f"Unknown command {cmd}"}

                client.sendall(json.dumps(res).encode("utf-8"))
            except Exception as exc:
                try:
                    client.sendall(json.dumps({"error": str(exc)}).encode("utf-8"))
                except Exception:
                    pass
            finally:
                try:
                    client.close()
                except Exception:
                    pass
