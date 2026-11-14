import copy
import io
import json
import tkinter as tk
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from core.model_probe import ModelProbeError, probe_model
from core.conversation import ConversationError
from ui.app import DesktopPetApp


class ModelProbeTests(unittest.TestCase):
    def config(self):
        return {"base_url": "https://example.test/v1", "model": "draft-model",
                "api_key": "private-test-token", "max_tokens": 1200}

    def transport(self, text):
        response = io.BytesIO(json.dumps({"choices": [{"message": {"content": text}}]}).encode())
        return response

    def test_draft_configuration_one_request_and_no_history(self):
        config = self.config()
        before = copy.deepcopy(config)
        with patch("core.model_transport.open_model_request", return_value=self.transport("连接正常")) as send:
            result = probe_model(config)
        self.assertEqual(config, before)
        self.assertEqual(result["reply"], "连接正常")
        send.assert_called_once()
        payload = json.loads(send.call_args.args[0].data)
        self.assertEqual(payload["model"], "draft-model")
        self.assertEqual(payload["max_tokens"], 1200)
        self.assertEqual(len([m for m in payload["messages"] if m["role"] == "user"]), 1)
        self.assertNotIn("private-test-token", str(result))

    def test_vision_must_recognize_generated_image(self):
        colors = [("红", "red", "#e02020"), ("蓝", "blue", "#2050e0")]
        for answer, accepted in (("红色，蓝色", True), ("我能正常回复", False), ("蓝色，红色", False)):
            with self.subTest(answer=answer), patch("core.model_probe.random.sample", return_value=colors), patch(
                    "core.model_transport.open_model_request", return_value=self.transport(answer)) as send:
                if accepted:
                    self.assertTrue(probe_model(self.config(), vision=True)["reply"])
                else:
                    with self.assertRaisesRegex(ModelProbeError, "未通过图片识别"):
                        probe_model(self.config(), vision=True)
                content = json.loads(send.call_args.args[0].data)["messages"][-1]["content"]
                self.assertTrue(content[1]["image_url"]["url"].startswith("data:image/png;base64,"))

    def test_http_error_is_sanitized_and_not_retried(self):
        error = HTTPError("https://example.test", 401, "bad", {}, io.BytesIO(b'private-test-token'))
        with patch("core.model_transport.open_model_request", side_effect=error) as send:
            with self.assertRaises(ModelProbeError) as caught:
                probe_model(self.config())
        self.assertIn("401", str(caught.exception))
        self.assertNotIn("private-test-token", str(caught.exception))
        send.assert_called_once()

    def test_reset_failure_remains_readable_without_exposing_provider_details(self):
        with patch("core.model_probe.ConversationClient._complete_messages", side_effect=ConversationError(
                "连接被重置 WinError 10054 private-test-token")):
            with self.assertRaisesRegex(ModelProbeError, "连接被服务器或网络设备重置") as caught:
                probe_model(self.config())
        self.assertNotIn("private-test-token", str(caught.exception))


class InterfaceRegressionTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        self.app = DesktopPetApp.__new__(DesktopPetApp)
        self.app.root = self.root

    def test_placeholder_is_display_only_and_follows_input(self):
        app = self.app
        app.inline_input_entry = tk.Text(self.root)
        app.inline_input_placeholder = tk.Label(app.inline_input_entry, text="和她聊点什么吧…（Enter 发送）")
        app._sync_inline_input_placeholder()
        self.assertEqual(app.inline_input_placeholder.winfo_manager(), "place")
        self.assertEqual(app._inline_input_text(), "")
        app.inline_input_entry.insert("1.0", "测试")
        app._sync_inline_input_placeholder()
        self.assertEqual(app.inline_input_placeholder.winfo_manager(), "")
        app.inline_input_entry.delete("1.0", "end")
        app._sync_inline_input_placeholder()
        self.assertEqual(app.inline_input_placeholder.winfo_manager(), "place")

    def test_header_fits_both_text_lines_at_multiple_dpi_scales(self):
        app = self.app
        self.root.overrideredirect(True)
        self.root.attributes("-alpha", 0.0)
        self.root.geometry("1100x450+20000+20000")
        self.root.deiconify()
        self.root.update()
        app._control_images = []
        app._character_display_name = lambda: "夜乃樱"
        for scale in (1.0, 1.25, 1.5, 2.0):
            self.root.tk.call("tk", "scaling", 96 / 72 * scale)
            host = tk.Frame(self.root, width=1100, height=450)
            host.pack_propagate(False)
            host.pack()
            header = app._build_control_header(host)
            self.root.update_idletasks()
            for label in (app._control_header_title, app._control_header_subtitle):
                with self.subTest(scale=scale):
                    self.assertGreaterEqual(label.winfo_height(), label.winfo_reqheight())
                    self.assertGreaterEqual(label.winfo_width(), label.winfo_reqwidth())
            self.assertGreater(header.winfo_reqheight(), 60)
            host.destroy()

    def test_empty_notice_has_no_opaque_background_or_reserved_row(self):
        app = self.app
        app.transparent_color = "#010203"
        app.root_frame = tk.Frame(self.root)
        app.inline_input_notice = tk.Label(app.root_frame, text="", bg="black")
        app.inline_input_notice.grid(row=3, column=0)
        app.root_frame.grid_rowconfigure(3, minsize=80)
        app._configure_inline_input_notice_layout = Mock()
        app._sync_inline_input_notice_height()
        self.assertEqual(app.inline_input_notice.winfo_manager(), "")
        self.assertEqual(app.inline_input_notice.cget("bg"), app.transparent_color)
        self.assertEqual(app.root_frame.grid_rowconfigure(3)["minsize"], 0)

    def test_model_button_reads_draft_without_saving_and_ignores_closed_page(self):
        app = self.app
        app.app_config = {"conversation_model": {"base_url": "https://old.test/v1", "model": "old"}}
        before = copy.deepcopy(app.app_config)
        app._control_model_vars = {
            ("conversation_model", "model"): tk.StringVar(self.root, value="unsaved-model"),
        }
        status = tk.Label(self.root)
        status._probe_button = tk.Button(self.root)
        callbacks = []
        app._safe_root_after = lambda delay, callback: callbacks.append(callback)
        with patch("ui.app.threading.Thread") as thread, patch("ui.app.probe_model", return_value={
                "seconds": .1, "reply": "连接正常"}) as probe:
            app._test_model_connection("conversation_model", status)
            app._test_model_connection("conversation_model", status)
            thread.assert_called_once()
            thread.call_args.kwargs["target"]()
            self.assertEqual(probe.call_args.args[0]["model"], "unsaved-model")
            self.assertEqual(app.app_config, before)
            status.destroy()
            callbacks.pop()()

    def test_cancel_and_restart_ignore_old_test_completion(self):
        app = self.app
        app.app_config = {"conversation_model": {"base_url": "https://example.test/v1", "model": "draft"}}
        app._control_model_vars = {}
        status = tk.Label(self.root)
        status._probe_button = tk.Button(self.root)
        callbacks = []
        app._safe_root_after = lambda delay, callback: callbacks.append(callback)
        with patch("ui.app.threading.Thread") as thread, patch("ui.app.probe_model", return_value={
                "seconds": .1, "reply": "old reply"}):
            app._test_model_connection("conversation_model", status)
            old_worker = thread.call_args.kwargs["target"]
            app._test_model_connection("conversation_model", status)
            self.assertEqual(status.cget("text"), "测试已取消")
            app._test_model_connection("conversation_model", status)
            self.assertEqual(status._probe_button.cget("text"), "取消测试")
            old_worker()
            callbacks.pop()()
            self.assertEqual(status.cget("text"), "正在等待回复…")
            status.destroy()


if __name__ == "__main__":
    unittest.main()
