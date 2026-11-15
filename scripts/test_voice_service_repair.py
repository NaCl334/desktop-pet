"""Port ownership, races, bounded repair and actionable voice error regression."""
import json
from pathlib import Path
import socket
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from core.config import ROOT
from core.gpt_sovits import GPTSoVITSClient
from core import voice_service_repair as repair


class VoiceServiceRepairTests(unittest.TestCase):
    def client(self, host="127.0.0.1"):
        client = GPTSoVITSClient({"host": host, "port": 9880, "fallback_ports": [9881, 9882]})
        client.start = Mock()
        client.wait_ready = Mock()
        client.stop = Mock()
        client._request = Mock(side_effect=lambda *args, **kwargs: json.dumps({
            "project_root": str(ROOT), "instance_id": "owned"}).encode())
        return client

    def test_foreign_port_is_skipped_and_only_selected_port_is_started(self):
        client = self.client()
        starts = []
        client.start.side_effect = lambda: starts.append(client.port)
        def identity(*args, **kwargs):
            return json.dumps({"project_root": str(ROOT if client.port == 9881 else ROOT.parent), "instance_id": "fixture"})
        client._request.side_effect = identity
        saved = Mock()
        with patch.object(repair, "port_available", side_effect=lambda host, port: port == 9881):
            repair.start_managed_service(client, persist=saved)
        self.assertEqual(starts, [9881])
        self.assertEqual(client.port, 9881)
        saved.assert_called_once_with(client)
        client.stop.assert_not_called()

    def test_existing_owned_service_is_reused_without_spawn(self):
        client = self.client()
        with patch.object(repair, "port_available", return_value=False):
            repair.start_managed_service(client, persist=Mock())
        client.start.assert_not_called()
        self.assertEqual(client._service_identity["instance_id"], "owned")

    def test_all_foreign_ports_fail_without_killing_or_starting_any_process(self):
        client = self.client()
        client._request.side_effect = RuntimeError("foreign protocol")
        with patch.object(repair, "port_available", return_value=False), self.assertRaisesRegex(RuntimeError, "均不可用"):
            repair.start_managed_service(client, persist=Mock())
        client.start.assert_not_called()
        client.stop.assert_not_called()
        self.assertEqual(client.port, 9880)

    def test_bind_race_switches_to_next_port(self):
        client = self.client()
        attempts = []
        def start():
            attempts.append(client.port)
            if client.port == 9880:
                raise RuntimeError("address already in use")
        client.start.side_effect = start
        client._request.side_effect = lambda *args, **kwargs: json.dumps({
            "project_root": str(ROOT if client.port == 9881 else ROOT.parent), "instance_id": "fixture"})
        calls = []
        def available(host, port):
            calls.append(port)
            return port == 9881 or len(calls) == 1
        with patch.object(repair, "port_available", side_effect=available):
            repair.start_managed_service(client, persist=Mock())
        self.assertEqual(attempts, [9880, 9881])
        self.assertEqual(client.port, 9881)

    def test_dependency_error_does_not_cycle_all_ports_or_save_failed_port(self):
        client = self.client()
        client.start.side_effect = RuntimeError("No module named torch")
        saved = Mock()
        with patch.object(repair, "port_available", return_value=True), self.assertRaisesRegex(RuntimeError, "torch"):
            repair.start_managed_service(client, persist=saved)
        client.start.assert_called_once()
        saved.assert_not_called()
        self.assertEqual(client.port, 9880)

    def test_remote_host_never_auto_changes_ports_or_requires_local_identity(self):
        client = self.client("remote.example")
        with patch.object(repair, "port_available") as available:
            repair.start_managed_service(client, persist=Mock())
        available.assert_not_called()
        client._request.assert_not_called()
        self.assertEqual(client.port, 9880)

    def test_live_process_is_not_duplicated_when_health_check_is_slow(self):
        client = self.client()
        client.process = SimpleNamespace(poll=lambda: None)
        client._request.side_effect = RuntimeError("busy")
        with patch.object(repair, "port_available") as available, self.assertRaisesRegex(RuntimeError, "仍在运行"):
            repair.start_managed_service(client, persist=Mock())
        available.assert_not_called()
        client.start.assert_not_called()
        client.stop.assert_not_called()

    def test_cancellation_never_spawns_or_persists(self):
        client = self.client()
        saved = Mock()
        with self.assertRaisesRegex(RuntimeError, "取消"):
            repair.start_managed_service(client, cancelled=lambda: True, persist=saved)
        client.start.assert_not_called()
        saved.assert_not_called()

    def test_last_successful_port_is_preferred_and_candidates_are_bounded(self):
        self.assertEqual(repair.candidate_ports({"last_successful_port": 9882, "port": 9880})[:2], [9882, 9880])
        self.assertLessEqual(len(repair.candidate_ports({"fallback_ports": list(range(9000, 9999))})), 8)

    def test_real_occupied_port_is_detected_without_touching_listener(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            self.assertFalse(repair.port_available("127.0.0.1", listener.getsockname()[1]))
            self.assertGreater(listener.fileno(), 0)

    def test_error_classification_keeps_resource_problems_out_of_environment_repair(self):
        cases = [("参考音频不存在", "reference", False), ("角色权重文件不完整", "weights", False),
                 ("CUDA out of memory", "retry", False), ("No module named torch", "repair", True),
                 ("语音端口均不可用", "repair", False), ("等待本地服务超时", "repair", False),
                 ("unexpected", "retry", False)]
        for error, action, environment in cases:
            with self.subTest(error=error):
                issue = repair.classify_voice_issue(error)
                self.assertEqual((issue.action, issue.environment_repair), (action, environment))

    def test_unknown_failure_never_runs_installer(self):
        with patch("core.installer.install_profile") as install:
            issue = repair.classify_voice_issue("unknown error")
        install.assert_not_called()
        self.assertFalse(issue.environment_repair)

    def test_repair_reuses_installer_with_explicit_force_and_cancellation(self):
        progress, cancel = Mock(), threading.Event()
        with patch("core.installer.install_profile") as install:
            repair.repair_environment(progress, cancel)
        install.assert_called_once_with("full", progress=progress, cancel_event=cancel, force_repair=True)

    def test_skipped_installation_and_broken_environment_are_distinct(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch("core.gpt_sovits.ENGINE_DIR", Path(directory)), \
                patch("core.gpt_sovits.runtime_ready", return_value=False):
            client = self.client()
            self.assertEqual(repair.local_environment_status(client)["action"], "install")
            (Path(directory) / "GPT_SoVITS").mkdir()
            self.assertEqual(repair.local_environment_status(client)["action"], "repair")
            self.assertIsNone(repair.local_environment_status(self.client("remote.example")))

    def test_install_reuses_installer_without_forcing_existing_environment_replacement(self):
        progress, cancel = Mock(), threading.Event()
        with patch("core.installer.install_profile") as install:
            repair.install_environment(progress, cancel)
        install.assert_called_once_with("full", progress=progress, cancel_event=cancel)

    def test_dependency_trace_with_weight_paths_is_not_a_missing_weight(self):
        issue = repair.classify_voice_issue("启动失败", diagnostic="load('model.pth')\nModuleNotFoundError: No module named torch")
        self.assertEqual(issue.kind, "environment")

    def test_missing_environment_never_downloads_on_automatic_start(self):
        from ui.app import DesktopPetApp
        app = DesktopPetApp.__new__(DesktopPetApp)
        app.gpt_sovits_client = self.client()
        with patch.object(repair, "local_environment_status", return_value={"state": "missing", "action": "install"}), \
                patch("core.installer.install_profile") as install:
            app._start_tts_service()
        self.assertEqual(app._tts_service_status["state"], "missing")
        install.assert_not_called()
        app.gpt_sovits_client.start.assert_not_called()

    def test_manual_install_has_download_explanation_and_separate_install_entry(self):
        from ui.app import DesktopPetApp
        app = DesktopPetApp.__new__(DesktopPetApp)
        app.root = Mock()
        app.store = SimpleNamespace(settings={})
        app.gpt_sovits_client = self.client()
        app._start_tts_service = Mock()
        with patch.object(repair, "local_environment_status", return_value={"state": "missing", "action": "install"}), \
                patch("core.installer.preflight_space"), \
                patch("ui.app.messagebox.askyesno", return_value=True) as ask:
            app._start_tts_service_manually()
        app._start_tts_service.assert_called_once_with(install_environment_required=True)
        self.assertEqual(ask.call_args.args[0], "安装语音服务")
        self.assertIn("32 GiB", ask.call_args.args[1])

    def test_install_progress_completion_starts_service_once(self):
        from ui.app import DesktopPetApp
        app = DesktopPetApp.__new__(DesktopPetApp)
        app.gpt_sovits_client = self.client()
        app._runtime_errors = []
        completed = threading.Event()
        app.gpt_sovits_client.wait_ready.side_effect = completed.set
        observed = []
        def install(progress, **kwargs):
            progress("正在下载", 42)
            observed.append(dict(app._tts_service_status))
        with patch.object(repair, "install_environment", side_effect=install), \
                patch("ui.app.start_managed_service") as start:
            app._start_tts_service(install_environment_required=True)
            self.assertTrue(completed.wait(2))
        self.assertEqual(observed[0]["percent"], 42)
        self.assertEqual(observed[0]["operation"], "install")
        self.assertEqual(app._tts_service_status["state"], "ready")
        start.assert_called_once()

    def test_install_failure_remains_installable_and_does_not_start(self):
        from ui.app import DesktopPetApp
        app = DesktopPetApp.__new__(DesktopPetApp)
        app.gpt_sovits_client = self.client()
        app._runtime_errors = []
        threads = []
        real_thread = threading.Thread
        def thread(**kwargs):
            instance = real_thread(**kwargs)
            threads.append(instance)
            return instance
        with patch.object(repair, "install_environment", side_effect=OSError("offline")), \
                patch("ui.app.threading.Thread", side_effect=thread):
            app._start_tts_service(install_environment_required=True)
            threads[0].join(2)
        self.assertEqual(app._tts_service_status["action"], "install")
        app.gpt_sovits_client.start.assert_not_called()

    def test_failed_port_persistence_does_not_turn_a_live_service_into_failed_start(self):
        client = self.client()
        with patch.object(repair, "port_available", return_value=True):
            repair.start_managed_service(client, persist=Mock(side_effect=PermissionError("read-only")))
        self.assertEqual(client._service_identity["instance_id"], "owned")


if __name__ == "__main__":
    unittest.main()
