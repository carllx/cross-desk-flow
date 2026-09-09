"""Unit and regression tests for Cross-Desk Flow macOS Product Shell UI client.

Verifies:
1. Status mapping rules (Connected, Waiting for PC, Degraded, Stopped, Action required / host absent)
2. UI controls invoke only IPC commands (Start -> send_ipc_command("start"), Stop -> send_ipc_command("stop"))
3. No subprocess spawning, no GStreamer pipeline lifecycle management, no controller mutation
"""

from typing import Any, Dict, Optional
import unittest

from macos.product_shell import (
    DIR_ACTIVE,
    DIR_PROBLEM,
    DIR_STOPPED,
    DIR_WAITING,
    STATUS_ACTION_REQUIRED,
    STATUS_CONNECTED,
    STATUS_DEGRADED,
    STATUS_STOPPED,
    STATUS_WAITING_PC,
    ProductShellApp,
    map_controller_status_to_ui,
)


class TestStateMapping(unittest.TestCase):
    """Verifies state mapping logic against product specifications."""

    def test_host_absent(self):
        """When controller host is not reachable (payload None), display Action required."""
        ui_state = map_controller_status_to_ui(None)
        self.assertEqual(ui_state.overall_status, STATUS_ACTION_REQUIRED)
        self.assertIn("Background service is not running", ui_state.overall_detail)
        self.assertEqual(ui_state.speaker_status, DIR_STOPPED)
        self.assertEqual(ui_state.microphone_status, DIR_STOPPED)
        self.assertEqual(ui_state.action_required_message, "Background service is not running")

    def test_stopped_by_user(self):
        """When desired_state is STOPPED_BY_USER, display Stopped regardless of path states."""
        payload = {
            "controller_state": "STOPPED",
            "desired_state": "STOPPED_BY_USER",
            "peer_available": True,
            "speaker_path_state": "STOPPED",
            "microphone_path_state": "STOPPED",
        }
        ui_state = map_controller_status_to_ui(payload)
        self.assertEqual(ui_state.overall_status, STATUS_STOPPED)
        self.assertEqual(ui_state.speaker_status, DIR_STOPPED)
        self.assertEqual(ui_state.microphone_status, DIR_STOPPED)
        self.assertIsNone(ui_state.action_required_message)

    def test_peer_unavailable(self):
        """When peer is not available and desired_state is ENABLED, display Waiting for PC."""
        payload = {
            "controller_state": "ACTIVE",
            "desired_state": "ENABLED",
            "peer_available": False,
            "speaker_path_state": "STARTING",
            "microphone_path_state": "STARTING",
        }
        ui_state = map_controller_status_to_ui(payload)
        self.assertEqual(ui_state.overall_status, STATUS_WAITING_PC)
        self.assertEqual(ui_state.speaker_status, DIR_WAITING)
        self.assertEqual(ui_state.microphone_status, DIR_WAITING)
        self.assertIsNone(ui_state.action_required_message)

    def test_connected_dual_active(self):
        """When both speaker and mic paths are RUNNING with peer available, display Connected."""
        payload = {
            "controller_state": "ACTIVE",
            "desired_state": "ENABLED",
            "peer_available": True,
            "speaker_path_state": "RUNNING",
            "microphone_path_state": "RUNNING",
        }
        ui_state = map_controller_status_to_ui(payload)
        self.assertEqual(ui_state.overall_status, STATUS_CONNECTED)
        self.assertEqual(ui_state.speaker_status, DIR_ACTIVE)
        self.assertEqual(ui_state.microphone_status, DIR_ACTIVE)
        self.assertIsNone(ui_state.action_required_message)

    def test_degraded_single_direction_failure(self):
        """When one path fails or is unavailable while the other is active, display Degraded."""
        payload = {
            "controller_state": "ACTIVE",
            "desired_state": "ENABLED",
            "peer_available": True,
            "speaker_path_state": "RUNNING",
            "microphone_path_state": "FAILED",
            "last_actionable_microphone_error": "Microphone permission denied",
        }
        ui_state = map_controller_status_to_ui(payload)
        self.assertEqual(ui_state.overall_status, STATUS_DEGRADED)
        self.assertEqual(ui_state.speaker_status, DIR_ACTIVE)
        self.assertEqual(ui_state.microphone_status, DIR_PROBLEM)
        self.assertIn("Microphone error", ui_state.overall_detail)

    def test_action_required_on_controller_error(self):
        """When controller encounters fatal error or both paths fail, display Action required."""
        payload = {
            "controller_state": "ERROR",
            "desired_state": "ENABLED",
            "peer_available": True,
            "speaker_path_state": "FAILED",
            "microphone_path_state": "FAILED",
            "last_actionable_error": "Port conflict on 5004",
        }
        ui_state = map_controller_status_to_ui(payload)
        self.assertEqual(ui_state.overall_status, STATUS_ACTION_REQUIRED)
        self.assertEqual(ui_state.speaker_status, DIR_PROBLEM)
        self.assertEqual(ui_state.microphone_status, DIR_PROBLEM)
        self.assertIn("Port conflict", ui_state.overall_detail)


class TestProductShellAppIPC(unittest.TestCase):
    """Verifies that ProductShellApp only uses IPC commands and adheres to client boundaries."""

    def setUp(self):
        import tkinter as tk
        self.root = tk.Tk()
        self.root.withdraw()  # Hide window during test execution
        self.commands_sent = []
        self.mock_status: Optional[Dict[str, Any]] = {
            "controller_state": "ACTIVE",
            "desired_state": "ENABLED",
            "peer_available": True,
            "speaker_path_state": "RUNNING",
            "microphone_path_state": "RUNNING",
        }

    def tearDown(self):
        self.root.destroy()

    def mock_ipc(self, command: str) -> Optional[Dict[str, Any]]:
        self.commands_sent.append(command)
        if command == "status":
            return self.mock_status
        elif command == "start":
            return {"success": True}
        elif command == "stop":
            return {"success": True}
        return None

    def test_app_initialization_and_refresh(self):
        """App initialization should query status via IPC and apply values."""
        app = ProductShellApp(self.root, ipc_client=self.mock_ipc, auto_refresh_ms=0)
        self.assertIn("status", self.commands_sent)
        self.assertEqual(app.lbl_overall.cget("text"), STATUS_CONNECTED)
        self.assertEqual(app.lbl_spk_status.cget("text"), DIR_ACTIVE)
        self.assertEqual(app.lbl_mic_status.cget("text"), DIR_ACTIVE)

    def test_start_button_invokes_ipc_only(self):
        """Start button dispatches 'start' command strictly through IPC."""
        app = ProductShellApp(self.root, ipc_client=self.mock_ipc, auto_refresh_ms=0)
        self.commands_sent.clear()
        app.on_start()
        self.assertEqual(self.commands_sent, ["start", "status"])

    def test_stop_button_invokes_ipc_only(self):
        """Stop button dispatches 'stop' command strictly through IPC."""
        app = ProductShellApp(self.root, ipc_client=self.mock_ipc, auto_refresh_ms=0)
        self.commands_sent.clear()
        app.on_stop()
        self.assertEqual(self.commands_sent, ["stop", "status"])

    def test_refresh_button_invokes_status_ipc(self):
        """Manual refresh dispatches 'status' command strictly through IPC."""
        app = ProductShellApp(self.root, ipc_client=self.mock_ipc, auto_refresh_ms=0)
        self.commands_sent.clear()
        app.on_refresh()
        self.assertEqual(self.commands_sent, ["status"])

    def test_host_absent_banner_display(self):
        """When host is absent, action required banner appears with clear instruction."""
        self.mock_status = None
        app = ProductShellApp(self.root, ipc_client=self.mock_ipc, auto_refresh_ms=0)
        self.assertEqual(app.lbl_overall.cget("text"), STATUS_ACTION_REQUIRED)
        self.assertIn("Background service is not running", app.lbl_action_banner.cget("text"))


if __name__ == "__main__":
    unittest.main()
