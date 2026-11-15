"""Isolated Windows processes; never uninstall the project or touch its registry key."""
import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import tkinter as tk
import unittest
import uuid
from unittest.mock import Mock, patch

from core.config import ROOT
from core import uninstaller as u, install_location as loc


class UninstallRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='uninstall_test_', dir=ROOT/'temp')
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.target = self.base/'release'
        self.target.mkdir()
        (self.target/'config').mkdir()
        (self.target/'config/app_config.json').write_text('{"release_template":true}')
        (self.target/'发布清单.txt').write_text('fixture', encoding='utf-8')
        (self.target/'我的桌宠.exe').write_bytes(b'MZfixture')
        self.env = patch.dict(os.environ, {'LOCALAPPDATA':str(self.base/'state')})
        self.env.start(); self.addCleanup(self.env.stop)
        self.state = u._state_dir()

    def test_cwd_is_outside_even_when_temp_is_inside(self):
        inside = self.target/'temp'; inside.mkdir()
        with patch.object(u.tempfile, 'gettempdir', return_value=str(inside)), \
                patch.object(u.subprocess, 'Popen') as spawn:
            script = u.schedule_release_uninstall(self.target, 0)
        self.assertFalse(u._is_relative_to(Path(spawn.call_args.kwargs['cwd']), self.target))
        self.assertFalse(u._is_relative_to(script, self.target))

    def test_developer_directory_and_forged_failure_rejected(self):
        (self.target/'main.py').write_text('fixture')
        with self.assertRaisesRegex(RuntimeError,'开发'):
            u.schedule_release_uninstall(self.target,0)
        self.state.mkdir(parents=True)
        (self.state/'uninstall_failed.json').write_text(json.dumps({'target':str(ROOT),'token':uuid.uuid4().hex}))
        self.assertIsNone(u.pending_uninstall())
        with patch('tkinter.messagebox.askyesno') as ask:
            u.prompt_failed_uninstall(None,ROOT,Mock())
        ask.assert_not_called()

    def test_valid_retry_prompt_decline_clears_marker(self):
        with patch.object(u.subprocess,'Popen'):
            u.schedule_release_uninstall(self.target,0)
        owner=json.loads((self.target/u.OWNER_FILE).read_text())
        (self.state/'uninstall_failed.json').write_text(json.dumps(owner))
        self.assertEqual(u.pending_uninstall(),owner)
        with patch('tkinter.messagebox.askyesno',return_value=False) as ask:
            u.prompt_failed_uninstall(None,ROOT,Mock())
            u.prompt_failed_uninstall(None,ROOT,Mock())
        ask.assert_called_once()
        self.assertFalse((self.state/'uninstall_failed.json').exists())

    def test_retry_current_install_schedules_then_changes_cwd_and_exits(self):
        record={'target':str(self.target),'token':uuid.uuid4().hex}
        exit_app=Mock()
        with patch.object(u,'pending_uninstall',return_value=record), \
                patch('tkinter.messagebox.askyesno',return_value=True), \
                patch.object(u,'schedule_release_uninstall') as schedule, \
                patch.object(u.os,'chdir') as chdir:
            u.prompt_failed_uninstall(None,self.target,exit_app)
        schedule.assert_called_once_with(self.target,os.getpid(),retry=record)
        chdir.assert_called_once();exit_app.assert_called_once()

    def test_startup_retry_precedes_application_windows(self):
        with patch.object(u,'pending_uninstall',return_value={'fixture':True}), \
                patch.object(u,'prompt_failed_uninstall',side_effect=lambda parent,root,exit_app:exit_app()):
            self.assertTrue(u.check_startup_uninstall(self.target))
        with patch.object(u,'pending_uninstall',return_value=None),patch('tkinter.Tk') as create:
            self.assertFalse(u.check_startup_uninstall(ROOT))
        create.assert_not_called()

    def test_settings_uninstall_chdir_after_schedule_even_if_chdir_fails(self):
        from types import SimpleNamespace
        from ui.app import DesktopPetApp
        for fails in (False,True):
            app=DesktopPetApp.__new__(DesktopPetApp)
            app.app_config={};app._runtime_errors=[];app._control_status=Mock()
            app._set_autostart_enabled=Mock();app.quit_app=Mock()
            order=[]
            app.quit_app.side_effect=lambda:order.append('quit')
            def chdir(path):
                order.append('chdir')
                if fails:raise OSError('fixture')
            with patch('ui.app.ROOT',self.target),patch('ui.app.messagebox.askyesno',return_value=True), \
                    patch('ui.app.save_app_config'),patch('ui.app.os.chdir',side_effect=chdir), \
                    patch('ui.app.schedule_release_uninstall',side_effect=lambda *a:order.append('schedule')):
                app._uninstall_from_settings()
            self.assertEqual(order,['schedule','chdir','quit'])

    @unittest.skipUnless(os.name=='nt','Windows deletion semantics')
    def test_real_locked_file_partial_delete_retry_and_registry(self):
        import winreg
        keyname='Software\\MyDesktopPetTests\\'+uuid.uuid4().hex
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER,keyname) as key:
            winreg.SetValueEx(key,'InstallPath',0,winreg.REG_SZ,str(self.target/'我的桌宠.exe'))
        self.addCleanup(lambda:winreg.DeleteKey(winreg.HKEY_CURRENT_USER,keyname))
        locked=self.target/'config/settings.json';locked.write_text('locked')
        outside=self.base/'outside';outside.mkdir();(outside/'keep.txt').write_text('keep')
        import _winapi
        _winapi.CreateJunction(str(outside),str(self.target/'external_link'))
        create=ctypes.windll.kernel32.CreateFileW
        create.argtypes=[ctypes.c_wchar_p,ctypes.c_uint,ctypes.c_uint,ctypes.c_void_p,ctypes.c_uint,ctypes.c_uint,ctypes.c_void_p]
        create.restype=ctypes.c_void_p
        close=ctypes.windll.kernel32.CloseHandle;close.argtypes=[ctypes.c_void_p]
        handle=create(str(locked),0x80000000,1,None,3,0,None)
        self.assertNotEqual(handle,ctypes.c_void_p(-1).value)
        real_popen=subprocess.Popen
        processes=[]
        def spawn(command,**kwargs):
            script=Path(command[command.index('-File')+1])
            content=script.read_text(encoding='utf-8-sig')
            script.write_text(content.replace("'Software\\我的桌宠'",repr(keyname).replace('\\\\','\\')),encoding='utf-8-sig')
            process=real_popen(command,**kwargs);processes.append(process);return process
        try:
            with patch.object(u.subprocess,'Popen',side_effect=spawn):
                u.schedule_release_uninstall(self.target,0)
                self.assertEqual(processes[-1].wait(timeout=25),0)
            self.assertTrue(locked.exists())
            self.assertFalse((self.target/'我的桌宠.exe').exists())
            self.assertFalse((self.target/'发布清单.txt').exists())
            self.assertEqual((outside/'keep.txt').read_text(),'keep')
            failure=u._read_json(self.state/'uninstall_failed.json')
            self.assertTrue(any(item['path']==str(locked) for item in failure['failures']),failure)
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,keyname) as key:
                self.assertEqual(winreg.QueryValueEx(key,'InstallPath')[0],str(self.target/'我的桌宠.exe'))
        finally:
            close(handle)
        with patch.object(u.subprocess,'Popen',side_effect=spawn):
            u.schedule_release_uninstall(self.target,0,retry=u.pending_uninstall())
            self.assertEqual(processes[-1].wait(timeout=25),0)
        self.assertFalse(self.target.exists())
        self.assertFalse((self.state/'uninstall_failed.json').exists())
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,keyname) as key:
            with self.assertRaises(OSError):winreg.QueryValueEx(key,'InstallPath')
        log=(self.state/'uninstall.log').read_text(encoding='utf-8-sig')
        self.assertIn('INCOMPLETE',log);self.assertIn('SUCCESS',log)
        self.assertEqual((outside/'keep.txt').read_text(),'keep')

    @unittest.skipUnless(os.name=='nt','Windows junction semantics')
    def test_root_junction_cannot_be_uninstalled(self):
        import _winapi
        link=self.base/'linked_release'
        _winapi.CreateJunction(str(self.target),str(link))
        self.addCleanup(lambda: os.rmdir(link) if link.exists() else None)
        with self.assertRaisesRegex(RuntimeError,'目录链接'):
            u.schedule_release_uninstall(link,0)
        self.assertTrue((self.target/'我的桌宠.exe').exists())

    @unittest.skipUnless(os.name=='nt','Windows parent cwd')
    def test_real_parent_exit_then_delete_and_same_path_reinstall(self):
        child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(1)'],cwd=self.target,
                               creationflags=subprocess.CREATE_NO_WINDOW)
        real_popen=subprocess.Popen;processes=[]
        def spawn(*args,**kwargs):
            process=real_popen(*args,**kwargs);processes.append(process);return process
        with patch.object(u.subprocess,'Popen',side_effect=spawn):
            u.schedule_release_uninstall(self.target,child.pid)
        self.assertEqual(processes[0].wait(timeout=25),0)
        child.wait(timeout=5)
        self.assertFalse(self.target.exists())
        self.target.mkdir()
        (self.target/'fresh.txt').write_text('reinstalled')
        self.assertEqual((self.target/'fresh.txt').read_text(),'reinstalled')


class InstallPathTests(unittest.TestCase):
    def test_reject_root_system_program_files_long_and_relative(self):
        cases=[(Path(ROOT.anchor),'根目录'),('relative','绝对'),
               (Path(os.environ['SystemRoot'])/'pet','系统目录'),
               (Path(os.environ['ProgramFiles'])/'pet','Program Files'),
               (str(ROOT/'temp')+'x'*181,'过长')]
        for path,expected in cases:
            with self.subTest(path=str(path)):
                self.assertIn(expected,loc.validate_install_dir(path))

    def test_backend_rejects_before_copy_or_launch(self):
        with patch.object(loc.subprocess,'Popen') as spawn:
            with self.assertRaisesRegex(loc.SetupError,'根目录'):
                loc.install_in_directory(ROOT.anchor,'desktop')
        spawn.assert_not_called()

    def test_valid_missing_directory_not_created_and_write_failure(self):
        target=ROOT/'temp'/('uncreated_'+uuid.uuid4().hex)/'pet'
        self.assertIsNone(loc.validate_install_dir(target))
        self.assertFalse(target.parent.exists())
        with patch.object(loc.tempfile,'TemporaryFile',side_effect=PermissionError()):
            self.assertIn('写入权限',loc.validate_install_dir(target))

    def test_removable_and_network_rejected(self):
        for drive_type in (2,4):
            with patch('ctypes.windll.kernel32.GetDriveTypeW',return_value=drive_type):
                self.assertIn('移动磁盘或网络',loc.validate_install_dir(ROOT/'temp/pet'))
        self.assertIn('网络',loc.validate_install_dir(r'\\server\share\pet'))


class RecoveryWizardTests(unittest.TestCase):
    def setUp(self):
        self.root=tk.Tk();self.root.withdraw();self.addCleanup(self.root.destroy)
        from ui.setup_wizard import SetupWizard
        self.w=SetupWizard(self.root,{})
        self.w.show().withdraw();self.addCleanup(self.w._close_after_success)

    def test_manual_input_disables_then_recovers_install_button(self):
        w=self.w;w._show_page(1)
        w.location_var.set(ROOT.anchor)
        self.assertEqual(str(w.install_button.cget('state')),'disabled')
        self.assertIn('根目录',w.location_error.cget('text'))
        w.start_install();self.assertIsNone(w.worker)
        w.location_var.set(str(ROOT))
        self.assertEqual(str(w.install_button.cget('state')),'normal')
        self.assertEqual(w.location_error.cget('text'),'')

    def test_completion_discloses_original_data_before_any_launch(self):
        w=self.w;w.install_directory=ROOT/'temp/new_install'
        with patch('ui.setup_wizard.subprocess.Popen') as spawn,patch('ui.setup_wizard.os.startfile') as start:
            w.finish_install('desktop',True,'')
            spawn.assert_not_called();start.assert_not_called()
            text=w.completion_message.cget('text')
            for value in (str(ROOT),str(w.install_directory),'不会自动迁移','手动删除','characters'):
                self.assertIn(value,text)
            w.original_directory_button.invoke()
            start.assert_called_once_with(str(ROOT.resolve()))

    def test_completion_message_and_button_fit_without_screen_occupation(self):
        w=self.w;w.install_directory=ROOT/'temp/new_install'
        w.finish_install('desktop',True,'')
        w.dialog.attributes('-alpha',0)
        w.dialog.attributes('-toolwindow',True)
        w.dialog.geometry('+10000+10000');w.dialog.deiconify()
        self.root.update()
        button=w.original_directory_button
        footer=w.install_button.master.master
        self.assertLessEqual(button.winfo_rooty()+button.winfo_height(),footer.winfo_rooty())
        w.dialog.withdraw()


if __name__=='__main__':unittest.main()
