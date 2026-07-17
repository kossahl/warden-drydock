from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from warden_drydock.core.generator import init_campaign
from warden_drydock.core.validation import validate_campaign
from warden_drydock.core.context import build_context

class GeneratorTest(unittest.TestCase):
    def test_mothership_campaign_generation(self):
        with TemporaryDirectory() as tmp:
            root=Path(tmp)/'campaign'
            init_campaign(root,name='Test Campaign',adapter='mothership')
            self.assertTrue((root/'.drydock.json').exists())
            self.assertIn('Test Campaign',(root/'README.md').read_text())
            self.assertTrue((root/'templates'/'npc.md').exists())
            self.assertEqual(validate_campaign(root),0)
            self.assertTrue(build_context(root).exists())

if __name__=='__main__': unittest.main()
