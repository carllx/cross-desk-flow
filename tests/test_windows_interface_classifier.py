"""Focused regression tests for Windows interface classification and executable resolution."""

from unittest.mock import patch

from bridge_core.peer_discovery import InterfaceClassifier, InterfaceMedium


def _mock_check_output(cmd, *args, **kwargs):
    if cmd == "ver" or (isinstance(cmd, list) and cmd[0] == "ver"):
        return "Microsoft Windows [Version 10.0.22631.4169]\r\n"
    # PowerShell command
    return _mock_check_output.ps_return


def test_classify_windows_ethernet():
    """Windows + 802.3 -> WIRED_ETHERNET."""
    classifier = InterfaceClassifier()
    _mock_check_output.ps_return = "802.3\r\n"
    with patch.object(classifier, "_resolve_powershell_cmd", return_value=r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"):
        with patch("subprocess.check_output", side_effect=_mock_check_output) as mock_subproc:
            medium = classifier.classify_interface("198.168.10.5")
            assert medium == InterfaceMedium.WIRED_ETHERNET
            ps_calls = [c for c in mock_subproc.call_args_list if isinstance(c[0][0], list) and c[0][0][0] == r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"]
            assert len(ps_calls) == 1
            assert "198.168.10.5" in ps_calls[0][0][0][4]


def test_classify_windows_wifi():
    """Windows + Native 802.11 -> WIFI."""
    classifier = InterfaceClassifier()
    _mock_check_output.ps_return = "Native 802.11\r\n"
    with patch.object(classifier, "_resolve_powershell_cmd", return_value="pwsh.exe"):
        with patch("subprocess.check_output", side_effect=_mock_check_output) as mock_subproc:
            medium = classifier.classify_interface("192.168.10.10")
            assert medium == InterfaceMedium.WIFI
            ps_calls = [c for c in mock_subproc.call_args_list if isinstance(c[0][0], list) and c[0][0][0] == "pwsh.exe"]
            assert len(ps_calls) == 1
            assert "192.168.10.10" in ps_calls[0][0][0][4]


def test_classify_windows_executable_unavailable():
    """Executable unavailable -> InterfaceMedium.OTHER."""
    classifier = InterfaceClassifier()
    with patch.object(classifier, "_resolve_powershell_cmd", return_value=None):
        with patch("subprocess.check_output", side_effect=_mock_check_output) as mock_subproc:
            medium = classifier.classify_interface("198.168.10.5")
            assert medium == InterfaceMedium.OTHER
            ps_calls = [c for c in mock_subproc.call_args_list if isinstance(c[0][0], list)]
            assert len(ps_calls) == 0


def test_classify_windows_positive_cache_avoids_repeated_subprocess():
    """Confirmed WIRED_ETHERNET or WIFI uses process-local positive cache without restarting PowerShell."""
    classifier = InterfaceClassifier()
    _mock_check_output.ps_return = "802.3\r\n"
    with patch.object(classifier, "_resolve_powershell_cmd", return_value="powershell.exe"):
        with patch("subprocess.check_output", side_effect=_mock_check_output) as mock_subproc:
            medium1 = classifier.classify_interface("198.168.10.5")
            assert medium1 == InterfaceMedium.WIRED_ETHERNET
            ps_calls1 = [c for c in mock_subproc.call_args_list if isinstance(c[0][0], list) and c[0][0][0] == "powershell.exe"]
            assert len(ps_calls1) == 1

            medium2 = classifier.classify_interface("198.168.10.5")
            assert medium2 == InterfaceMedium.WIRED_ETHERNET
            ps_calls2 = [c for c in mock_subproc.call_args_list if isinstance(c[0][0], list) and c[0][0][0] == "powershell.exe"]
            assert len(ps_calls2) == 1


def test_classify_windows_other_not_cached():
    """OTHER is not cached, subsequent queries will retry classification."""
    classifier = InterfaceClassifier()
    _mock_check_output.ps_return = "Unspecified\r\n"
    with patch.object(classifier, "_resolve_powershell_cmd", return_value="powershell.exe"):
        with patch("subprocess.check_output", side_effect=_mock_check_output) as mock_subproc:
            medium1 = classifier.classify_interface("10.0.0.99")
            assert medium1 == InterfaceMedium.OTHER
            ps_calls1 = [c for c in mock_subproc.call_args_list if isinstance(c[0][0], list) and c[0][0][0] == "powershell.exe"]
            assert len(ps_calls1) == 1

            medium2 = classifier.classify_interface("10.0.0.99")
            assert medium2 == InterfaceMedium.OTHER
            ps_calls2 = [c for c in mock_subproc.call_args_list if isinstance(c[0][0], list) and c[0][0][0] == "powershell.exe"]
            assert len(ps_calls2) == 2


def test_classify_windows_uses_create_no_window():
    """Windows classification specifies CREATE_NO_WINDOW to avoid flashing terminal windows."""
    import subprocess
    classifier = InterfaceClassifier()
    _mock_check_output.ps_return = "802.3\r\n"
    with patch.object(classifier, "_resolve_powershell_cmd", return_value="powershell.exe"):
        with patch("subprocess.check_output", side_effect=_mock_check_output) as mock_subproc:
            classifier.classify_interface("198.168.10.5")
            assert mock_subproc.called
            call_kwargs = mock_subproc.call_args[1]
            expected_flag = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            assert call_kwargs.get("creationflags") == expected_flag
            assert call_kwargs.get("timeout") == 15.0
