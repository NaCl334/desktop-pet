"""Hidden checks for the simplified installer and native window controls."""
import ctypes
import json
import os
from pathlib import Path
import tempfile
import threading
import tkinter as tk
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from core import installer
from core.config import ROOT
from ui.app import DesktopPetApp
from ui.setup_wizard import SetupWizard


class SimplifiedWizardTests(unittest.TestCase):
    def setUp(self):
        self.root=tk.Tk();self.root.withdraw();self.addCleanup(self.root.destroy)
        self.w=SetupWizard(self.root,{})
        self.w.show().withdraw();self.addCleanup(self.w._close_after_success)

    def test_welcome_options_and_completion_have_no_removed_controls(self):
        def texts(widget):
            result=[]
            try:result.append(str(widget.cget('text')))
            except tk.TclError:pass
            for child in widget.winfo_children():result.extend(texts(child))
            return result
        w=self.w
        for page in (0,1,2):
            w._show_page(page)
            content='\n'.join(texts(w.dialog))
            for unwanted in ('缓存位置','保留下载包','跳过设置'):
                self.assertNotIn(unwanted,content)
        self.assertEqual(w.close_button.cget('text'),'关闭向导')
        self.assertEqual(str(w.dialog.transient()),'')

    @unittest.skipUnless(os.name=='nt','Native Windows styles')
    def test_native_window_has_minimize_box_and_taskbar_eligibility(self):
        self.root.update_idletasks()
        get_ancestor=ctypes.windll.user32.GetAncestor
        get_ancestor.argtypes=[ctypes.c_void_p,ctypes.c_uint];get_ancestor.restype=ctypes.c_void_p
        wrapper=get_ancestor(self.w.dialog.winfo_id(),2)
        get_long=ctypes.windll.user32.GetWindowLongPtrW
        get_long.argtypes=[ctypes.c_void_p,ctypes.c_int];get_long.restype=ctypes.c_ssize_t
        get_window=ctypes.windll.user32.GetWindow
        get_window.argtypes=[ctypes.c_void_p,ctypes.c_uint];get_window.restype=ctypes.c_void_p
        style=get_long(wrapper,-16);extended=get_long(wrapper,-20)
        self.assertTrue(style & 0x00020000,'Missing WS_MINIMIZEBOX')
        self.assertFalse(extended & 0x80,'WS_EX_TOOLWINDOW hides taskbar entry')
        self.assertFalse(get_window(wrapper,4),'Owned windows are hidden from taskbar by default')
        self.assertFalse(self.w.dialog.attributes('-toolwindow'))

    def test_selected_install_uses_its_own_temp_and_cleans_success_cache(self):
        w=self.w
        directory=ROOT/'temp'/('wizard_destination_'+str(os.getpid()))
        w.location_var.set(str(directory));w.profile_var.set('full');w._show_page(1)
        with patch('ui.setup_wizard.preflight_space') as preflight, \
                patch('ui.setup_wizard.install_in_directory',return_value={}) as install:
            w.start_install()
            self.assertEqual(w.close_button.cget('text'),'取消配置')
            w.worker.join(2);w._poll_events()
        preflight.assert_called_once_with(directory.resolve(),directory.resolve()/'temp')
        self.assertEqual(install.call_args.kwargs['cache_dir'],directory.resolve()/'temp')
        self.assertIs(install.call_args.kwargs['keep_cache'],False)
        self.assertEqual(w._page,2)

    def test_legacy_cache_and_keep_settings_are_no_longer_applied(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'temp') as name:
            root=Path(name);(root/'data').mkdir()
            (root/'data/settings.json').write_text(json.dumps({
                'setup_cache_dir':'Z:/old_cache','keep_setup_cache':True}))
            self.assertEqual(installer.setup_options(root),{'cache_dir':str(root/'temp'),'keep_cache':False})

    def test_old_skip_flag_no_longer_suppresses_incomplete_setup(self):
        app=DesktopPetApp.__new__(DesktopPetApp)
        app.store=SimpleNamespace(settings={'setup_wizard_completed':False,'setup_wizard_dismissed':True})
        app._show_setup_wizard=Mock()
        app._safe_root_after=lambda delay,callback:callback()
        threads=[];real_thread=threading.Thread
        def start_thread(**kwargs):
            thread=real_thread(**kwargs);threads.append(thread);return thread
        with patch('ui.app.detect_environment',return_value={'components':{}}), \
                patch('ui.app.profile_ready',return_value=False), \
                patch('ui.app.threading.Thread',side_effect=start_thread):
            app._check_startup_environment();threads[0].join(2)
        app._show_setup_wizard.assert_called_once()

    def app_wizard(self, completed=False):
        app=DesktopPetApp.__new__(DesktopPetApp);app.root=self.root
        settings={'setup_wizard_completed':completed}
        app.store=SimpleNamespace(settings=settings,save_settings=Mock(side_effect=settings.update));app.quit_app=Mock()
        app._safe_root_after=Mock()
        app.show_control_page=Mock()
        with patch('ui.app.list_characters',return_value=[]):
            app._show_setup_wizard({})
        app._setup_wizard.show().withdraw()
        self.addCleanup(app._setup_wizard._close_after_success)
        return app

    def test_closing_incomplete_wizard_quits_desktop_pet(self):
        app=self.app_wizard()
        app._setup_wizard.cancel()
        app.quit_app.assert_called_once()
        self.assertIsNone(app._setup_wizard.dialog)
        app.store.save_settings.assert_called_once_with({'setup_wizard_dismissed':False})

    def test_cancel_waits_for_worker_before_quitting(self):
        app=self.app_wizard();w=app._setup_wizard
        w.worker=SimpleNamespace(is_alive=lambda:True)
        w.cancel();app.quit_app.assert_not_called()
        w.finish_install('full',False,'已取消')
        app.quit_app.assert_called_once()
        w._dismiss();app.quit_app.assert_called_once()

    def test_completed_install_and_minimize_do_not_quit(self):
        app=self.app_wizard();w=app._setup_wizard
        w.dialog.withdraw();app.quit_app.assert_not_called()
        w.finish_install('desktop',True,'')
        with patch('ui.app.list_characters',return_value=[]):
            w._complete_setup()
        app.quit_app.assert_not_called()
        app.show_control_page.assert_called_once_with('characters')
        self.assertTrue(app.store.settings['setup_wizard_completed'])

    def test_closing_reopened_wizard_preserves_installed_pet(self):
        app=self.app_wizard(completed=True)
        app._setup_wizard.cancel();app.quit_app.assert_not_called()

    def test_failed_first_install_then_close_quits(self):
        app=self.app_wizard();w=app._setup_wizard
        w.finish_install('full',False,'下载中断')
        app.quit_app.assert_not_called()
        w.cancel();app.quit_app.assert_called_once()

    def test_dismiss_callback_can_destroy_root(self):
        root=tk.Tk();root.withdraw()
        w=SetupWizard(root,{},on_dismissed=root.destroy)
        w.show().withdraw()
        w.cancel()
        self.assertIsNone(w.dialog)

    def test_live2d_detail_hidden_without_runtime_for_both_profiles(self):
        for available in (False,True):
            w=SetupWizard(self.root,{'components':{
                'live2d':{'available':available},'python':{'available':True}}})
            w.show().withdraw();self.addCleanup(w._close_after_success)
            for profile in ('desktop','full'):
                w.profile_var.set(profile);w.refresh_details()
                self.assertEqual('live2d' in w.component_status_labels,available)
                self.assertIn('python',w.component_status_labels)

    def test_worker_progress_continues_when_window_is_minimized(self):
        # _poll_events checks existence, not viewability. A withdrawn window
        # provides the same non-viewable state without occupying the desktop.
        w=self.w
        self.assertFalse(w.dialog.winfo_viewable())
        w._events.put(('progress',{'text':'正在解压','percent':55}))
        w._poll_events()
        self.assertEqual(w.status_var.get(),'正在解压')
        self.assertEqual(float(w.progress.cget('value')),55)

    def test_component_details_follow_worker_then_verified_report(self):
        components={key:{'available':False} for key in ('torch','ffmpeg','gpt_sovits_runtime','gpt_sovits_model')}
        w=SetupWizard(self.root,{'components':components})
        w.show().withdraw();self.addCleanup(w._close_after_success)
        w.profile_var.set('full');w.refresh_details()
        w._events.put(('progress',{'text':'正在解压','component':'gpt_sovits_runtime','state':'解压中 59%','percent':83}))
        w._events.put(('progress',{'text':'检查通过','component':'ffmpeg','state':'已就绪'}))
        w._poll_events()
        self.assertEqual(w.component_status_labels['gpt_sovits_runtime'].cget('text'),'解压中 59%')
        self.assertEqual(w.component_status_labels['ffmpeg'].cget('text'),'已就绪')
        w.finish_install('full',False,'测试失败')
        self.assertEqual(w.component_status_labels['gpt_sovits_runtime'].cget('text'),'未完成')
        w._events.put(('report',{'components':{key:{'available':True} for key in components}}))
        w._poll_events()
        self.assertEqual(w.component_status_labels['gpt_sovits_runtime'].cget('text'),'已就绪')
        self.assertFalse(w._component_states)

    def test_completion_directory_button_uses_wizard_colors(self):
        button=self.w.original_directory_button
        self.assertIsInstance(button,tk.Button)
        self.assertEqual(button.cget('bg'),'white')
        self.assertEqual(button.cget('fg'),'#2175bf')

    def test_tray_has_no_reopen_wizard_entry(self):
        app=DesktopPetApp.__new__(DesktopPetApp)
        app.store=SimpleNamespace(settings={})
        self.assertNotIn('重新运行配置向导',[label for label,callback in app._get_tray_actions()])


if __name__=='__main__':unittest.main()
