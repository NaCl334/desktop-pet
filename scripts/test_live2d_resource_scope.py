import unittest
from core.live2d_models import model_resource_scope

class ResourceScopeTests(unittest.TestCase):
    def test_separate_folders_and_file_types(self):
        files=['表情/happy.exp3.json','动作/wave.motion3.json','outside/sad.exp3.json','表情/wrong.motion3.json']
        self.assertEqual(model_resource_scope(files,'expression')['paths'],['表情/happy.exp3.json'])
        self.assertEqual(model_resource_scope(files,'motion')['paths'],['动作/wave.motion3.json'])
    def test_english_names_remain_supported(self):
        self.assertEqual(model_resource_scope(['Expressions/happy.EXP3.JSON'],'expression')['paths'],['Expressions/happy.EXP3.JSON'])
    def test_no_conventional_folder_uses_extension_fallback(self):
        self.assertEqual(model_resource_scope(['custom/happy.exp3.json','custom/wave.motion3.json'],'expression')['paths'],['custom/happy.exp3.json'])
if __name__=='__main__':unittest.main()
