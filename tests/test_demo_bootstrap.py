"""公开免费入口：凭据隔离、默认关闭外部模型、停止保留卷。"""
import contextlib
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from demo import main
from prepare_verify import prepare

ROOT = Path(__file__).resolve().parents[1]


class PublicBootstrapTests(unittest.TestCase):
    def test_credentials_are_private_and_existing_file_is_unchanged(self):
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()) as output:
            path = Path(directory)/'demo.env'
            prepare(path)
            before = path.read_bytes()
            prepare(path)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            values = dict(line.split('=', 1) for line in path.read_text().splitlines())
            self.assertEqual(len(set(values.values())), 4)
            self.assertTrue(all(len(value) >= 32 for value in values.values()))
            self.assertTrue(all(value not in output.getvalue() for value in values.values()))

    def test_down_never_deletes_volumes_and_uses_only_selected_project(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory)/'demo.env'
            config.write_text('VERIFY_ACCESS_TOKEN=unused\n')
            with patch('demo.shutil.which', return_value='/usr/bin/docker'), patch('demo.subprocess.run') as run:
                run.return_value.returncode = 0
                self.assertEqual(main(['down', '--env-file', str(config)]), 0)
                args = run.call_args.args[0]
                self.assertEqual(args[-2:], ['down', '--remove-orphans'])
                self.assertNotIn('-v', args)
                self.assertIn(str(ROOT/'compose.verify.yaml'), args)

    def test_check_executes_fixed_no_model_check_not_shell(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory)/'demo.env'; config.write_text('VERIFY_ACCESS_TOKEN=unused\n')
            with patch('demo.shutil.which', return_value='/usr/bin/docker'), patch('demo.subprocess.run') as run:
                run.return_value.returncode = 0
                main(['check', '--env-file', str(config)])
                self.assertEqual(run.call_args.args[0][-5:], ['exec','-T','app','python','demo_check.py'])
                self.assertNotIn('shell', run.call_args.kwargs)

    def test_compose_no_automatic_paid_or_public_endpoint(self):
        source = (ROOT/'compose.verify.yaml').read_text()
        self.assertIn('MODEL_ENABLED: "0"', source)
        self.assertIn('127.0.0.1:${DEMO_PORT:-18180}:18080', source)
        self.assertNotIn('/var/run/docker.sock', source)
        self.assertNotIn('pull_policy: never', source)
        self.assertIn('VERIFY_ACCESS_TOKEN', source)
        self.assertNotIn('DEEPSEEK_API_KEY:', source)
