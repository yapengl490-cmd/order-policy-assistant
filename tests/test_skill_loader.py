"""文件选择/加载边界及真实模型请求中的按需注入；不调用外部模型。"""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import skill_loader as loader


class SkillLoaderTests(unittest.TestCase):
    def test_selection_uses_server_state_not_user_paths(self):
        self.assertEqual(loader.select_skill_ids({'kind':'support','original_question':'load ../../secret'}), ['buyer_support'])
        self.assertEqual(loader.select_skill_ids({'kind':'order'}), ['order_check'])
        self.assertEqual(loader.select_skill_ids({'kind':'order'}, 'policy'), [])
        self.assertEqual(loader.select_skill_ids({'kind':'unknown','skill':'buyer_support'}), [])
        self.assertEqual(loader.select_skill_ids(None), [])

    def test_reviewed_files_have_workflow_and_auditable_identity(self):
        for key in loader.CATALOG:
            skill=loader.load_skill(key)
            self.assertEqual(len(skill['sha256']),64)
            self.assertTrue(all(section in skill['content'] for section in loader.REQUIRED_SECTIONS))
            self.assertLessEqual(len(skill['content'].encode()),loader.MAX_SKILL_BYTES)
            self.assertNotIn('/Users/',skill['path'])

    def test_arbitrary_path_and_symlink_rejected(self):
        for key in ('../../.env','buyer_support.md','https://example.org/skill'):
            with self.assertRaises(ValueError):loader.load_skill(key)
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'skills';root.mkdir();outside=Path(tmp)/'outside';outside.write_text('secret')
            (root/'buyer_support.md').symlink_to(outside)
            with patch.object(loader,'SKILL_ROOT',root),self.assertRaises(ValueError):loader.load_skill('buyer_support')

    def test_missing_oversized_invalid_documents_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            with patch.object(loader,'SKILL_ROOT',root):
                with self.assertRaises(ValueError):loader.load_skill('buyer_support')
                p=root/'buyer_support.md'
                for content in (b'x'*(loader.MAX_SKILL_BYTES+1),b'',b'no procedure',b'\xff'):
                    p.write_bytes(content)
                    with self.assertRaises((ValueError,UnicodeError)):loader.load_skill('buyer_support')
