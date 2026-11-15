"""Space plans, cache ownership, cancellation and error transport regressions."""
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import tkinter as tk
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from core import installer, install_location, voice_service_repair
from core.config import ROOT
from scripts.test_setup_installation import READY


class SetupRecoveryTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(dir=ROOT/'temp')
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.cache = self.root/'cache'
        self.cache.mkdir()
        p = patch.object(installer, 'ROOT', self.root)
        p.start(); self.addCleanup(p.stop)

    def test_same_volume_retains_32_gib_with_readable_shortfall(self):
        with patch.object(installer.shutil, 'disk_usage', return_value=Mock(free=19.74*installer.GIB)):
            with self.assertRaisesRegex(installer.SetupError, r'需要 32 GiB.*19.7 GiB.*12.3 GiB'):
                installer.preflight_space(cache_dir=self.cache)

    def split_parents(self):
        install = Mock(stat=lambda: SimpleNamespace(st_dev=1))
        cache = Mock(stat=lambda: SimpleNamespace(st_dev=2))
        return install, cache

    def test_split_volume_checks_both_drives(self):
        parents = self.split_parents()
        with patch.object(installer, '_existing_parent', side_effect=[*parents,*parents]), \
             patch.object(installer.shutil, 'disk_usage', side_effect=[Mock(free=21*installer.GIB),Mock(free=10*installer.GIB)]):
            plan = installer.preflight_space(self.root,self.cache,8*installer.GIB)
        self.assertFalse(plan['same_volume'])

    def test_split_volume_rejects_insufficient_cache(self):
        parents = self.split_parents()
        with patch.object(installer, '_existing_parent', side_effect=[*parents,*parents]), \
             patch.object(installer.shutil, 'disk_usage', side_effect=[Mock(free=21*installer.GIB),Mock(free=8*installer.GIB)]):
            with self.assertRaisesRegex(installer.SetupError, '需要 9 GiB'):
                installer.preflight_space(self.root,self.cache,8*installer.GIB)

    def test_split_volume_does_not_promise_install_on_19_gib(self):
        parents = self.split_parents()
        with patch.object(installer, '_existing_parent', side_effect=[*parents,parents[0]]), \
             patch.object(installer.shutil, 'disk_usage', return_value=Mock(free=19.7*installer.GIB)):
            with self.assertRaisesRegex(installer.SetupError, '需要 21 GiB'):
                installer.preflight_space(self.root,self.cache)

    def test_invalid_relative_cache_is_rejected(self):
        with self.assertRaisesRegex(installer.SetupError,'绝对路径'):
            installer.preflight_space(cache_dir='relative')

    def test_cache_download_uses_selected_directory(self):
        package = {'filename':'fixture.7z','size':7,'sha256':'a'*64,'urls':['http://localhost/fixture']}
        with patch.object(installer,'download_verified',return_value=self.cache/'setup_downloads/fixture.7z') as download:
            installer._obtain_package(package,None,None,cache_dir=self.cache)
        self.assertEqual(download.call_args.args[1],self.cache/'setup_downloads/fixture.7z')

    def deploy(self, keep=False, fail=False, cancel=False, offline=False):
        archive = (self.root/'setup_components' if offline else self.cache/'setup_downloads')/'fixture.7z'
        archive.parent.mkdir(parents=True,exist_ok=True);archive.write_bytes(b'fixture')
        other = archive.parent/'unrelated.7z';other.write_bytes(b'preserve')
        event = threading.Event()
        def extract(archive,stage,*args):
            engine=stage/'fixture';engine.mkdir()
            (engine/'api_v2.py').write_text('#fixture')
            return engine
        def validate(*args,**kwargs):
            if cancel: event.set()
            if fail: raise installer.SetupError('真实失败原因')
        with patch.object(installer,'preflight_space'), \
             patch.object(installer,'detect_environment',side_effect=[{},READY]), \
             patch.object(installer,'voice_package',return_value={'filename':'fixture.7z','size':7}), \
             patch.object(installer,'_obtain_package',return_value=archive), \
             patch.object(installer,'_unpack_voice',side_effect=extract), \
             patch.object(installer,'_prepare_engine'),patch.object(installer,'install_lgpl_tools'), \
             patch.object(installer,'obtain_lgpl_archive'), \
             patch.object(installer,'_validate_engine',side_effect=validate):
            if fail or cancel:
                with self.assertRaises(installer.SetupError):
                    installer.install_profile('full',cache_dir=self.cache,keep_cache=keep,cancel_event=event)
            else:
                installer.install_profile('full',cache_dir=self.cache,keep_cache=keep)
        self.assertTrue(other.exists())
        self.assertFalse(any((self.root/'temp/setup_staging').glob('voice_*')))
        return archive

    def test_success_cleans_only_current_download(self):
        tools=self.root/'temp/setup_downloads'/installer.FFMPEG_PACKAGE['filename']
        tools.parent.mkdir(parents=True,exist_ok=True);tools.write_bytes(b'cached tools')
        original=self.root/'setup_components'/installer.FFMPEG_PACKAGE['filename']
        original.parent.mkdir(parents=True,exist_ok=True);original.write_bytes(b'offline original')
        self.assertFalse(self.deploy().exists())
        self.assertFalse(tools.exists())
        self.assertTrue(original.exists())

    def test_keep_cache_retains_successful_download(self):
        self.assertTrue(self.deploy(keep=True).exists())

    def test_offline_original_is_never_deleted(self):
        self.assertTrue(self.deploy(offline=True).exists())

    def test_failure_preserves_download_and_cleans_staging(self):
        self.assertTrue(self.deploy(fail=True).exists())

    def test_cancel_preserves_download_and_cleans_staging(self):
        self.assertTrue(self.deploy(cancel=True).exists())

    def test_failure_status_persists_until_environment_changes(self):
        client=SimpleNamespace(host='127.0.0.1')
        with patch.object(voice_service_repair,'environment_fingerprint',return_value=('old',)):
            failure=voice_service_repair.installation_failure('需要 32 GiB')
            self.assertIs(voice_service_repair.local_environment_status(client,failure),failure)
        with patch.object(voice_service_repair,'environment_fingerprint',return_value=('new',)), \
             patch('core.gpt_sovits.runtime_ready',return_value=False), \
             patch('core.gpt_sovits.ENGINE_DIR',self.root/'missing'):
            self.assertEqual(voice_service_repair.local_environment_status(client,failure)['state'],'missing')

    def test_worker_writes_complete_error_event(self):
        (self.root/'temp').mkdir()
        path=self.root/'temp/error.jsonl'
        with patch.object(install_location,'ROOT',self.root), \
             patch.object(installer,'install_profile',side_effect=installer.SetupError('需要 32 GiB，当前 19.7 GiB')):
            self.assertEqual(install_location.run_setup_worker('full',path),1)
        events=[json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
        self.assertEqual(events[0]['kind'],'error')
        self.assertIn('19.7',events[0]['error'])
        self.assertFalse(events[-1]['success'])

    def test_parent_receives_error_event_even_without_result(self):
        (self.root/'config').mkdir()
        (self.root/'config/app_config.json').write_text('{}')
        exe=self.root/'app.exe';exe.write_bytes(b'fixture')
        target=self.root/'target'
        def spawn(command,**kwargs):
            Path(command[-1]).write_text(json.dumps({'kind':'error','error':'缓存盘空间不足'})+'\n',encoding='utf-8')
            return SimpleNamespace(poll=lambda:1,wait=lambda **kwargs:1,returncode=1)
        with patch.object(install_location,'ROOT',self.root), \
             patch.object(install_location.sys,'frozen',True,create=True), \
             patch.object(install_location.sys,'executable',str(exe)), \
             patch.object(install_location.subprocess,'Popen',side_effect=spawn):
            with self.assertRaisesRegex(installer.SetupError,'缓存盘空间不足'):
                install_location.install_in_directory(target,'desktop',cache_dir=self.cache,keep_cache=True)
        settings=json.loads((target/'data/settings.json').read_text(encoding='utf-8'))
        self.assertEqual(settings['setup_cache_dir'],str(self.cache))
        self.assertTrue(settings['keep_setup_cache'])


class SetupRecoveryUITests(unittest.TestCase):
    def setUp(self):
        self.root=tk.Tk();self.root.withdraw()
        self.addCleanup(self.root.destroy)

    def wizard(self):
        from ui.setup_wizard import SetupWizard
        w=SetupWizard(self.root,{},on_dismissed=Mock())
        w.show().withdraw()
        return w

    def test_close_only_dismisses_current_wizard(self):
        w=self.wizard();w.cancel()
        w.on_dismissed.assert_called_once()
        self.assertIsNone(w.dialog)

    def test_cancel_disables_buttons_then_dismisses_when_worker_finishes(self):
        w=self.wizard();w.worker=SimpleNamespace(is_alive=lambda:True)
        w.cancel()
        for widget in (w.close_button,w.install_button,w.back_button):
            self.assertEqual(str(widget.cget('state')),'disabled')
        self.assertEqual(w.status_var.get(),'正在取消，请稍候…')
        self.assertIsNotNone(w.dialog)
        w.finish_install('full',False,'已取消')
        self.assertIsNone(w.dialog)

    def test_preflight_failure_starts_no_worker(self):
        w=self.wizard();w.profile_var.set('full')
        with patch('ui.setup_wizard.preflight_space',side_effect=installer.SetupError('需要 32 GiB')):
            w.start_install()
        self.assertIsNone(w.worker)
        self.assertEqual(w.status_var.get(),'需要 32 GiB')
        self.assertEqual(str(w.install_button.cget('state')),'normal')
        w.cancel()

    def test_manual_install_preflight_precedes_confirmation(self):
        from ui.app import DesktopPetApp
        from core.gpt_sovits import GPTSoVITSClient
        app=DesktopPetApp.__new__(DesktopPetApp)
        app.root=self.root;app.store=SimpleNamespace(settings={})
        app.gpt_sovits_client=GPTSoVITSClient({'host':'127.0.0.1'})
        app._start_tts_service=Mock()
        with patch.object(voice_service_repair,'local_environment_status',return_value={'action':'install'}), \
             patch.object(installer,'preflight_space',side_effect=installer.SetupError('需要 32 GiB，当前 19.7 GiB')), \
             patch('ui.app.messagebox.askyesno') as ask,patch('ui.app.messagebox.showerror') as show:
            app._start_tts_service_manually()
        ask.assert_not_called();app._start_tts_service.assert_not_called()
        self.assertIn('19.7',show.call_args.args[1])
        self.assertEqual(app._tts_service_status['state'],'failed')

    def test_failed_install_status_survives_actual_panel_probe(self):
        from ui.app import BRAND_COLORS
        from core.gpt_sovits import GPTSoVITSClient
        from ui.voice_service_status import VoiceServicePanel
        status=voice_service_repair.installation_failure('磁盘空间不足，需要 32 GiB')
        client=GPTSoVITSClient({'host':'127.0.0.1'})
        client._request=Mock()
        panel=VoiceServicePanel(self.root,colors=BRAND_COLORS,client=client,project_root=ROOT,
            get_status=lambda:status,set_status=Mock(),on_start=Mock(),monitor=False)
        panel._probe(status)
        self.assertIs(panel._probe_result[1],status)
        client._request.assert_not_called();panel.destroy()

    def test_reopen_clears_skipped_marker_and_shows_wizard(self):
        from ui.app import DesktopPetApp
        app=DesktopPetApp.__new__(DesktopPetApp);app.root=self.root
        app.store=SimpleNamespace(save_settings=Mock())
        app._safe_root_after=lambda delay,callback:callback()
        app._show_setup_wizard=Mock()
        threads=[]
        real_thread=threading.Thread
        def thread(**kwargs):
            t=real_thread(**kwargs);threads.append(t);return t
        with patch('ui.app.detect_environment',return_value={'components':{}}), \
             patch('ui.app.threading.Thread',side_effect=thread):
            app._reopen_setup_wizard();threads[0].join(2)
        app.store.save_settings.assert_called_once_with({'setup_wizard_dismissed':False})
        app._show_setup_wizard.assert_called_once_with({'components':{}})


if __name__ == '__main__':
    unittest.main()
