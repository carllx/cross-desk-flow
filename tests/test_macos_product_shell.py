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
        try:
            self.root = tk.Tk()
            self.root.withdraw()  # Hide window during test execution
        except Exception as e:
            self.skipTest(f"Tkinter display not available: {e}")
        self.commands_sent = []
        self.mock_status: Optional[Dict[str, Any]] = {
            "controller_state": "ACTIVE",
            "desired_state": "ENABLED",
            "peer_available": True,
            "speaker_path_state": "RUNNING",
            "microphone_path_state": "RUNNING",
        }

    def tearDown(self):
        if hasattr(self, "root") and self.root:
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

    def test_voice_ducking_ui_state_and_slider(self):
        """Product shell reflects duck_level and local_voice_active, and slider emits set-duck-level IPC."""
        ipc_calls = []
        def mock_ipc(cmd, **kwargs):
            ipc_calls.append((cmd, kwargs))
            return {
                "controller_state": "ACTIVE",
                "desired_state": "ENABLED",
                "peer_available": True,
                "speaker_path_state": "RUNNING",
                "microphone_path_state": "RUNNING",
                "duck_level": 35,
                "local_voice_active": True,
            }

        app = ProductShellApp(self.root, ipc_client=mock_ipc, auto_refresh_ms=0)
        self.assertEqual(app.lbl_voice_status.cget("text"), "Active")
        self.assertEqual(app.lbl_duck_title.cget("text"), "When Mac audio focus is active: 35%")
        self.assertEqual(int(round(app.duck_scale.get())), 35)

        # Move slider
        ipc_calls.clear()
        app.on_duck_slider_change("50")
        self.assertEqual(app.lbl_duck_title.cget("text"), "When Mac audio focus is active: 50%")
        self.assertEqual(ipc_calls, [("set-duck-level", {"level": 50})])


if __name__ == "__main__":
    unittest.main()


class TestMacOSDiagnosticsAndRecovery(unittest.TestCase):
    """Verifies macOS Diagnostics & Recovery helper functions and UI controls."""

    def test_sanitize_diagnostic_text(self):
        """Sanitizer redacts paths, GUIDs, and credentials."""
        from macos.diagnostics import sanitize_diagnostic_text

        raw = "User path: /Users/johndoe/Library/Logs/err.log with GUID {12345678-ABCD-EF01-2345-6789ABCDEF01} and token: secret123"
        sanitized = sanitize_diagnostic_text(raw)
        self.assertNotIn("/Users/johndoe", sanitized)
        self.assertIn("<path redacted>", sanitized)
        self.assertNotIn("12345678-ABCD", sanitized)
        self.assertIn("<guid redacted>", sanitized)
        self.assertNotIn("secret123", sanitized)
        self.assertIn("token=<redacted>", sanitized)

    def test_build_diagnostic_report_host_absent(self):
        """Report for absent host truthfully reflects NOT RUNNING and safe defaults."""
        from macos.diagnostics import build_diagnostic_report

        report = build_diagnostic_report(None)
        self.assertIn("Controller: NOT RUNNING", report)
        self.assertIn("Peer State: NONE", report)
        self.assertIn("Background service is not running", report)
        self.assertIn("Deployed SHA:", report)

    def test_build_diagnostic_report_running(self):
        """Report for running controller reflects paths, mode, and sanitized errors."""
        from macos.diagnostics import build_diagnostic_report

        status = {
            "controller_state": "ACTIVE",
            "desired_state": "ENABLED",
            "owner_pid": 4321,
            "peer_available": True,
            "peer_address": "198.168.10.5",
            "local_bind_address": "198.168.10.4",
            "speaker_path_state": "RUNNING",
            "microphone_path_state": "IDLE",
            "last_actionable_error": None,
        }
        report = build_diagnostic_report(status)
        self.assertIn("RUNNING (PID 4321)", report)
        self.assertIn("Connected (198.168.10.5)", report)
        self.assertIn("Network Path: Ethernet", report)
        self.assertIn("Speaker Path: RUNNING", report)
        self.assertIn("Voice Input / Dictation: Automatic / Standby", report)

    def test_get_diagnostics_view_data_clean_ui(self):
        """View data does not expose raw PID or IP in UI text."""
        from macos.diagnostics import get_diagnostics_view_data

        status = {
            "controller_state": "ACTIVE",
            "desired_state": "ENABLED",
            "owner_pid": 4321,
            "peer_available": True,
            "peer_address": "198.168.10.5",
            "local_bind_address": "198.168.10.4",
            "speaker_path_state": "RUNNING",
            "microphone_path_state": "RUNNING",
            "last_actionable_error": None,
        }
        data = get_diagnostics_view_data(status)
        self.assertEqual(data["service"][0], "Running")
        self.assertNotIn("4321", data["service"][0])
        self.assertEqual(data["peer"][0], "Connected")
        self.assertNotIn("198.168.10.5", data["peer"][0])
        self.assertEqual(data["spk"][0], "RUNNING")
        self.assertEqual(data["mic"][0], "RUNNING")

    def test_start_controller_via_lifecycle_absent_task_fails_closed(self):
        """When LaunchAgent is not installed/disabled, recovery fails closed and never installs."""
        from unittest.mock import MagicMock, patch
        from macos.diagnostics import start_controller_via_lifecycle

        with patch("macos.diagnostics._query_launchagent_status", return_value="Not installed"), \
             patch("macos.lifecycle.install_launch_agent") as mock_install, \
             patch("macos.lifecycle.run_launchctl") as mock_launchctl:

            recovered = start_controller_via_lifecycle(timeout_sec=0.2)
            self.assertFalse(recovered)
            mock_install.assert_not_called()
            mock_launchctl.assert_not_called()

    def test_start_controller_via_lifecycle_present_kickstarts(self):
        """When LaunchAgent is installed and loaded, recovery kickstarts existing service."""
        from unittest.mock import MagicMock, patch
        from macos.diagnostics import start_controller_via_lifecycle

        mock_res = MagicMock()
        mock_res.returncode = 0

        with patch("macos.diagnostics._query_launchagent_status", return_value="Installed"), \
             patch("macos.diagnostics.is_service_loaded", return_value=True), \
             patch("macos.diagnostics.run_launchctl", return_value=mock_res) as mock_launchctl, \
             patch("macos.lifecycle.install_launch_agent") as mock_install, \
             patch("macos.cli.send_ipc_command", return_value={"owner_pid": 9999}):

            recovered = start_controller_via_lifecycle(timeout_sec=1.0)
            self.assertTrue(recovered)
            mock_install.assert_not_called()
            mock_launchctl.assert_called()
            args = mock_launchctl.call_args[0][0]
            self.assertIn("kickstart", args)

    def test_reconcile_button_alive_controller(self):
        """When controller is alive, Reconcile sends reconcile command via IPC."""
        import tkinter as tk
        try:
            root = tk.Tk()
            root.withdraw()
        except Exception as e:
            self.skipTest(f"Tkinter display not available: {e}")

        commands = []
        def mock_ipc(cmd):
            commands.append(cmd)
            return {
                "controller_state": "ACTIVE",
                "desired_state": "ENABLED",
                "peer_available": True,
                "speaker_path_state": "RUNNING",
                "microphone_path_state": "IDLE",
            }

        try:
            app = ProductShellApp(root, ipc_client=mock_ipc, auto_refresh_ms=0)
            commands.clear()
            app.on_reconcile()
            self.assertIn("reconcile", commands)
        finally:
            root.destroy()

    def test_reconcile_button_dead_controller_recovers_via_lifecycle(self):
        """When controller is absent, Reconcile delegates to start_controller_via_lifecycle."""
        import tkinter as tk
        from unittest.mock import patch
        try:
            root = tk.Tk()
            root.withdraw()
        except Exception as e:
            self.skipTest(f"Tkinter display not available: {e}")

        def mock_ipc(cmd):
            return None

        try:
            app = ProductShellApp(root, ipc_client=mock_ipc, auto_refresh_ms=0)
            with patch("macos.product_shell.start_controller_via_lifecycle", return_value=True) as mock_recover:
                app.on_reconcile()
                mock_recover.assert_called_once()
        finally:
            root.destroy()


def test_get_deployed_sha_without_git_cli(tmp_path):
    """When git CLI fails or is not in PATH, get_deployed_sha resolves SHA from repo metadata."""
    from unittest.mock import patch
    from macos.diagnostics import get_deployed_sha

    test_sha = "aabbccddeeff00112233445566778899aabbccdd"

    # Create dummy detached repo directory
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    head_file = git_dir / "HEAD"
    head_file.write_text(test_sha + "\n", encoding="utf-8")

    with patch("subprocess.run", side_effect=FileNotFoundError("git not found")):
        resolved = get_deployed_sha(str(tmp_path))
        assert resolved == test_sha


def test_get_deployed_sha_worktree_indirection(tmp_path):
    """When target directory is a worktree with a .git file, get_deployed_sha follows indirection."""
    from unittest.mock import patch
    import subprocess
    from macos.diagnostics import get_deployed_sha

    test_sha = "11223344556677889900aabbccddeeff11223344"

    # Create main repo git dir
    main_git = tmp_path / "main_repo" / ".git"
    main_git.mkdir(parents=True)
    (main_git / "refs" / "heads").mkdir(parents=True)
    (main_git / "refs" / "heads" / "feature-x").write_text(test_sha + "\n", encoding="utf-8")

    # Create worktree gitdir
    wt_gitdir = main_git / "worktrees" / "wt1"
    wt_gitdir.mkdir(parents=True)
    (wt_gitdir / "commondir").write_text("../..\n", encoding="utf-8")
    (wt_gitdir / "HEAD").write_text("ref: refs/heads/feature-x\n", encoding="utf-8")

    # Create worktree directory with .git file
    wt_dir = tmp_path / "wt1"
    wt_dir.mkdir()
    (wt_dir / ".git").write_text(f"gitdir: {wt_gitdir}\n", encoding="utf-8")

    with patch("subprocess.run", side_effect=subprocess.SubprocessError("git error")):
        resolved = get_deployed_sha(str(wt_dir))
        assert resolved == test_sha


def test_open_data_directory(tmp_path):
    """open_data_directory calls open with existing log or data directory."""
    from unittest.mock import patch
    from macos.diagnostics import open_data_directory

    with patch("subprocess.Popen") as mock_popen:
        assert open_data_directory() is True
        mock_popen.assert_called_once()
        args = mock_popen.call_args[0][0]
        assert args[0] == "open"


def test_start_controller_via_lifecycle_disabled_fails_closed():
    """When LaunchAgent is Disabled, recovery returns False with zero lifecycle mutation."""
    from unittest.mock import MagicMock, patch
    from macos.diagnostics import start_controller_via_lifecycle

    mock_res = MagicMock()
    mock_res.returncode = 0
    mock_res.stdout = '\t"com.carllx.desk-audio-bridge.controller" => disabled\n'

    with patch("macos.diagnostics.run_launchctl", return_value=mock_res) as mock_run, \
         patch("macos.lifecycle.install_launch_agent") as mock_install:

        recovered = start_controller_via_lifecycle(timeout_sec=0.2)
        assert recovered is False
        mock_install.assert_not_called()
        # Ensure kickstart or bootstrap was never called
        for call_args in mock_run.call_args_list:
            cmd = call_args[0][0]
            assert "kickstart" not in cmd
            assert "bootstrap" not in cmd
            assert "load" not in cmd


def test_query_launchagent_status_fails_closed_to_unknown_when_query_fails(tmp_path):
    """When print-disabled query fails or errors, status is Unknown even if plist exists."""
    from unittest.mock import MagicMock, patch
    from macos.diagnostics import _query_launchagent_status, start_controller_via_lifecycle

    dummy_plist = tmp_path / "com.carllx.desk-audio-bridge.controller.plist"
    dummy_plist.write_text("dummy", encoding="utf-8")

    err_res = MagicMock()
    err_res.returncode = 1
    err_res.stdout = ""
    err_res.stderr = "launchctl error"

    with patch("macos.diagnostics.run_launchctl", return_value=err_res) as mock_run, \
         patch("macos.lifecycle.install_launch_agent") as mock_install:

        status = _query_launchagent_status(plist_path=str(dummy_plist))
        assert status == "Unknown"

        recovered = start_controller_via_lifecycle(timeout_sec=0.2, plist_path=str(dummy_plist))
        assert recovered is False
        mock_install.assert_not_called()


def test_start_controller_bootstrap_failure_does_not_invoke_load_w(tmp_path):
    """When bootstrap fails, recovery returns False without calling load -w or mutating state."""
    from unittest.mock import MagicMock, patch
    from macos.diagnostics import start_controller_via_lifecycle

    dummy_plist = tmp_path / "com.carllx.desk-audio-bridge.controller.plist"
    dummy_plist.write_text("dummy", encoding="utf-8")

    print_disabled_res = MagicMock()
    print_disabled_res.returncode = 0
    print_disabled_res.stdout = ""

    bootstrap_fail_res = MagicMock()
    bootstrap_fail_res.returncode = 1
    bootstrap_fail_res.stderr = "bootstrap failed"

    def mock_run_launchctl(cmd):
        if "print-disabled" in cmd:
            return print_disabled_res
        if "bootstrap" in cmd:
            return bootstrap_fail_res
        return MagicMock(returncode=1)

    with patch("macos.diagnostics.run_launchctl", side_effect=mock_run_launchctl) as mock_run, \
         patch("macos.diagnostics.is_service_loaded", return_value=False), \
         patch("macos.lifecycle.install_launch_agent") as mock_install:

        recovered = start_controller_via_lifecycle(timeout_sec=0.2, plist_path=str(dummy_plist))
        assert recovered is False
        mock_install.assert_not_called()
        # Verify load -w was never called
        for call_args in mock_run.call_args_list:
            cmd = call_args[0][0]
            assert "load" not in cmd
            assert "-w" not in cmd


def test_start_controller_existing_enabled_and_loaded_kickstarts():
    """When LaunchAgent is verified enabled and loaded, kickstart recovery works."""
    from unittest.mock import MagicMock, patch
    from macos.diagnostics import start_controller_via_lifecycle

    print_disabled_res = MagicMock()
    print_disabled_res.returncode = 0
    print_disabled_res.stdout = '\t"com.carllx.desk-audio-bridge.controller" => enabled\n'

    kickstart_res = MagicMock()
    kickstart_res.returncode = 0

    def mock_run_launchctl(cmd):
        if "print-disabled" in cmd:
            return print_disabled_res
        if "kickstart" in cmd:
            return kickstart_res
        return MagicMock(returncode=0)

    with patch("macos.diagnostics.run_launchctl", side_effect=mock_run_launchctl) as mock_run, \
         patch("macos.diagnostics.is_service_loaded", return_value=True), \
         patch("macos.cli.send_ipc_command", return_value={"owner_pid": 8888}):

        recovered = start_controller_via_lifecycle(timeout_sec=1.0)
        assert recovered is True

        called_kickstart = any("kickstart" in call_args[0][0] for call_args in mock_run.call_args_list)
        assert called_kickstart is True
