import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import core.backup as backup
import core.config as config
from ui.app import DesktopPetApp


class CharacterPackageTests(unittest.TestCase):
    def make_character(self, root, voice):
        source = root / "characters" / "sample"
        (source / "voice").mkdir(parents=True)
        (source / "character.json").write_text(
            json.dumps({"id": "sample", "display_name": "Package test"}), encoding="utf-8")
        (source / "voice/voice.json").write_text(json.dumps(voice), encoding="utf-8")
        return source

    def test_all_reference_types_survive_move_without_changing_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "external/one/same.wav"
            second = root / "external/two/same.wav"
            for path, content in ((first, b"first"), (second, b"second")):
                path.parent.mkdir(parents=True)
                path.write_bytes(content)
            voice = {
                "reference": {"default_audio": str(first), "text": "Keep text"},
                "language_references": {"zh": {"audio": str(first)},
                                        "ja": {"audio": str(second), "use_prompt_text": False}},
                "emotion_reference_audio_groups": {"happy": [str(first), str(second)]},
                "model": {"ckpt_file": "models/voice.ckpt"},
            }
            source = self.make_character(root, voice)
            (source / "voice/models").mkdir()
            (source / "voice/models/voice.ckpt").write_bytes(b"gpt")
            (source / "voice/models/voice.pth").write_bytes(b"sovits")
            voice["model"]["pth_file"] = "models/voice.pth"
            (source / "voice/voice.json").write_text(json.dumps(voice), encoding="utf-8")
            original = (source / "voice/voice.json").read_bytes()
            with patch.object(config, "CHARACTERS_DIR", source.parent):
                package = backup.export_character("sample", root / "export.zip")
                first.unlink()
                second.unlink()
                with zipfile.ZipFile(package) as archive:
                    self.assertEqual(len([n for n in archive.namelist() if n.endswith(".wav")]), 2)
                imported_id, _ = backup.import_character(package)
            imported = source.parent / imported_id
            saved = json.loads((imported / "voice/voice.json").read_text(encoding="utf-8"))
            app = DesktopPetApp.__new__(DesktopPetApp)
            app.character_dir = imported
            values = [saved["reference"]["default_audio"],
                      saved["language_references"]["zh"]["audio"],
                      saved["language_references"]["ja"]["audio"],
                      *saved["emotion_reference_audio_groups"]["happy"]]
            self.assertEqual([app._resolve_reference_audio_path(v).read_bytes() for v in values],
                             [b"first", b"first", b"second", b"first", b"second"])
            self.assertTrue(all(not Path(value).is_absolute() for value in values))
            self.assertFalse(saved["language_references"]["ja"]["use_prompt_text"])
            self.assertEqual(saved["model"], voice["model"])
            self.assertEqual((source / "voice/voice.json").read_bytes(), original)

    def test_external_weights_are_portable_and_runtime_resolves_import(self):
        from core import gpt_sovits
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            weights = root / "external"
            weights.mkdir()
            gpt, sovits = weights / "sample.ckpt", weights / "sample.pth"
            gpt.write_bytes(b"gpt weights")
            sovits.write_bytes(b"sovits weights")
            source = self.make_character(root, {"model": {"ckpt_file": str(gpt), "pth_file": str(sovits)}})
            original = (source / "voice/voice.json").read_bytes()
            with patch.object(config, "CHARACTERS_DIR", source.parent):
                package = backup.export_character("sample", root / "export.zip")
                gpt.unlink()
                sovits.unlink()
                new_id, _ = backup.import_character(package)
            with patch.object(gpt_sovits, "ROOT", root), patch.object(gpt_sovits, "load_gpt_config", return_value={}):
                _, first, second = gpt_sovits.character_weights(new_id)
            self.assertEqual(first.read_bytes(), b"gpt weights")
            self.assertEqual(second.read_bytes(), b"sovits weights")
            self.assertEqual((source / "voice/voice.json").read_bytes(), original)
            with zipfile.ZipFile(package) as archive:
                info = json.loads(archive.read("share_info.json"))
                self.assertTrue(info["models_included"])
                self.assertEqual(info["version"], 2)

    def test_missing_weight_preserves_existing_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.make_character(root, {"model": {"ckpt_file": "missing.ckpt", "pth_file": "missing.pth"}})
            destination = root / "export.zip"
            destination.write_bytes(b"previous")
            with patch.object(config, "CHARACTERS_DIR", source.parent):
                with self.assertRaises(FileNotFoundError):
                    backup.export_character("sample", destination)
            self.assertEqual(destination.read_bytes(), b"previous")

    def test_damaged_model_package_does_not_create_character(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive_path = root / "broken.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("character.json", '{"display_name": "Broken"}')
                archive.writestr("voice/voice.json", json.dumps({"model": {"ckpt_file": "models/a.ckpt", "pth_file": "models/b.pth"}}))
                archive.writestr("share_info.json", json.dumps({"version": 2, "models_included": True,
                    "model_files": [{"path": "voice/models/a.ckpt", "size": 1}, {"path": "voice/models/b.pth", "size": 1}]}))
            characters = root / "characters"
            characters.mkdir()
            with patch.object(config, "CHARACTERS_DIR", characters):
                with self.assertRaisesRegex(ValueError, "缺失或不完整"):
                    backup.import_character(archive_path)
            self.assertEqual(list(characters.iterdir()), [])

    def test_existing_relative_paths_and_empty_references(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.make_character(root, {
                "reference": {"default_audio": "references/local.wav"},
                "language_references": {"zh": {"audio": ""}},
                "emotion_reference_audio_groups": {"idle": []},
            })
            (source / "voice/references").mkdir()
            (source / "voice/references/local.wav").write_bytes(b"local")
            with patch.object(config, "CHARACTERS_DIR", source.parent):
                package = backup.export_character("sample", root / "export.zip")
            with zipfile.ZipFile(package) as archive:
                saved = json.loads(archive.read("voice/voice.json"))
                self.assertEqual(archive.read("voice/" + saved["reference"]["default_audio"]), b"local")
                self.assertEqual(saved["language_references"]["zh"]["audio"], "")
                self.assertEqual(saved["emotion_reference_audio_groups"]["idle"], [])

    def test_missing_reference_preserves_previous_export(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.make_character(root, {"reference": {"default_audio": "missing.wav"}})
            destination = root / "export.zip"
            destination.write_bytes(b"previous")
            with patch.object(config, "CHARACTERS_DIR", source.parent):
                with self.assertRaisesRegex(FileNotFoundError, "missing.wav"):
                    backup.export_character("sample", destination)
            self.assertEqual(destination.read_bytes(), b"previous")

    def test_export_inside_source_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            source = self.make_character(Path(directory), {})
            with patch.object(config, "CHARACTERS_DIR", source.parent):
                with self.assertRaises(ValueError):
                    backup.export_character("sample", source / "export.zip")


if __name__ == "__main__":
    unittest.main()
