"""演示准备的默认零副作用和重复导入；只用独立内存数据库。"""
from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import patch
import unittest

from sqlalchemy import create_engine, select, func
from policy_kb import metadata, policy_documents, policy_chunks
import prepare_catalog_policy_demo as preparation


class CatalogPreparationTests(unittest.TestCase):
    def test_default_only_validates_without_database_or_model(self):
        with patch('sys.argv', ['prepare_catalog_policy_demo.py']), \
             patch.object(preparation, 'local_admin_engine', side_effect=AssertionError('must not connect')), \
             redirect_stdout(StringIO()) as out:
            self.assertEqual(preparation.main(), 0)
        self.assertIn('checked_only_no_database_connection', out.getvalue())

    def test_apply_is_idempotent_and_retains_policy_source(self):
        engine = create_engine('sqlite+pysqlite:///:memory:').execution_options(schema_translate_map={'public': None})
        self.addCleanup(engine.dispose)
        metadata.create_all(engine)
        first = preparation.prepare_demo(engine)
        second = preparation.prepare_demo(engine)
        self.assertEqual(first['policy_sha256'], second['policy_sha256'])
        self.assertEqual(len(first['import']['imported']), 1)
        self.assertEqual(second['import']['unchanged'], first['import']['imported'])
        with engine.connect() as connection:
            self.assertEqual(connection.scalar(select(func.count()).select_from(policy_documents)), 1)
            self.assertEqual(connection.scalar(select(func.count()).select_from(policy_chunks)), 3)
