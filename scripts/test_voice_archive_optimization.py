"""Real extraction tests for inference-only selection and path validation."""
import os
from pathlib import Path
import tempfile
import unittest
import zipfile
from unittest.mock import patch
from core import installer


class VoiceArchiveOptimizationTests(unittest.TestCase):
    def test_listing_checks_paths_and_rejects_links(self):
        with self.assertRaises(installer.SetupError):
            installer._voice_archive_names('header\n----------\nPath = ../escape\n')
        with self.assertRaises(installer.SetupError):
            installer._voice_archive_names('header\n----------\nPath = file\nSymbolic Link = target\n')
        self.assertEqual(installer._voice_archive_names('Path = header.zip\n----------\nPath = 引擎/api_v2.py\n'),['引擎/api_v2.py'])

    def test_selects_only_utility_models_and_rebuildable_bytecode(self):
        files=['engine/api_v2.py','engine/runtime/pkg/source.py',
               'engine/runtime/pkg/__pycache__/source.cpython-39.pyc',
               'engine/runtime/pkg/__pycache__/no_source.cpython-39.pyc',
               'engine/runtime/pkg/sourceless.pyc',
               'engine/tools/asr/models/model.pt','engine/tools/uvr5/uvr5_weights/model.pth',
               'engine/tools/AP_BWE_main/model.pth','engine/GPT_SoVITS/pretrained_models/s2Gv3.pth']
        self.assertEqual(installer._voice_archive_exclusions(files),[files[2],files[5],files[6]])

    @unittest.skipUnless(os.name=='nt','Bundled Windows extractor')
    def test_real_archive_selection_preserves_inference_and_license(self):
        with tempfile.TemporaryDirectory(dir=installer.ROOT/'temp') as name:
            root=Path(name);archive=root/'fixture.zip'
            files={'示例/api_v2.py':'# api','示例/GPT_SoVITS/marker':'model',
                   '示例/runtime/pkg/module.py':'# source',
                   '示例/runtime/pkg/__pycache__/module.cpython-39.pyc':'bytecode',
                   '示例/runtime/sourceless.pyc':'keep',
                   '示例/tools/asr/models/model.pt':'omit','示例/tools/uvr5/uvr5_weights/model.pth':'omit',
                   '示例/tools/AP_BWE_main/model.pth':'keep','示例/LICENSE':'license'}
            with zipfile.ZipFile(archive,'w') as handle:
                for path,content in files.items():handle.writestr(path,content)
            events=[]
            result=installer._unpack_voice(archive,root/'output',events.append,None)
            self.assertTrue((result/'runtime/pkg/module.py').is_file())
            self.assertTrue((result/'runtime/sourceless.pyc').is_file())
            self.assertTrue((result/'tools/AP_BWE_main/model.pth').is_file())
            self.assertTrue((result/'LICENSE').is_file())
            self.assertFalse((result/'runtime/pkg/__pycache__/module.cpython-39.pyc').exists())
            self.assertFalse((result/'tools/asr/models/model.pt').exists())
            self.assertFalse((result/'tools/uvr5/uvr5_weights/model.pth').exists())
            self.assertTrue(any(e['component']=='gpt_sovits_runtime' for e in events))


if __name__=='__main__':unittest.main()
