import os
import shutil
import socket
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import certifi
from app import config
from app.smtp import SMTPSender
from tests.smtp_support import SMTP_CONFIG, SMTPServer


class PortableTest(unittest.TestCase):
    def setUp(self):
        scratch = Path.cwd() / '.test-temp'
        scratch.mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_occupied_port_does_not_initialize_data_or_start_worker(self):
        from app.service_runner import run
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            listener.listen()
            with patch('app.service_runner.initialize_config') as initialize:
                with self.assertRaises(OSError):
                    run(self.root / 'data', listener.getsockname()[1])
                initialize.assert_not_called()

    def test_daemon_binds_loopback_before_creating_worker(self):
        from app.service_runner import run
        captured = []

        class Server:
            def __init__(self, settings):
                pass

            def run(self, sockets):
                captured.append(sockets[0].getsockname())

        with patch('app.service_runner.uvicorn.Server', Server), patch('app.service_runner.create_app', return_value='app') as create:
            run(self.root / 'data', 0)
        self.assertEqual(captured[0][0], '127.0.0.1')
        self.assertGreater(captured[0][1], 0)
        self.assertTrue(create.call_args.kwargs['run_worker'])
        self.assertTrue((self.root / 'data/config.json').exists())

    def test_smtp_uses_bundled_trust_store_for_ssl_and_starttls(self):
        server = SMTPServer()
        import ssl
        real_context = ssl.create_default_context
        with patch('app.smtp.ssl.create_default_context', wraps=real_context) as context:
            with patch('smtplib.SMTP_SSL', side_effect=server.connect):
                SMTPSender(SMTP_CONFIG).send_text('owner@example.com', 'test')
            with patch('smtplib.SMTP', side_effect=server.connect):
                SMTPSender({**SMTP_CONFIG, 'security': 'starttls'}).send_text('owner@example.com', 'test')
        self.assertEqual(context.call_count, 2)
        for call in context.call_args_list:
            self.assertEqual(call.kwargs['cafile'], certifi.where())

    def test_mac_encryption_roundtrip_and_backup_require_local_key(self):
        # Use the actual Fernet implementation without changing pathlib's host platform.
        posix = SimpleNamespace(name='posix', open=os.open, fdopen=os.fdopen, chmod=os.chmod,
                                O_WRONLY=os.O_WRONLY, O_CREAT=os.O_CREAT, O_EXCL=os.O_EXCL)
        path = self.root / 'config.json'
        value = {'alimail': {'client_secret': 'fake-secret-only'}}
        with patch.object(config, 'os', posix):
            config.save_config(path, value)
        self.assertEqual(config.load_config(path), value)
        self.assertNotIn('fake-secret-only', path.read_text())
        copy = self.root / 'backup/config.json'
        copy.parent.mkdir()
        shutil.copy2(path, copy)
        with self.assertRaises(FileNotFoundError):
            config.load_config(copy)
        shutil.copy2(path.with_suffix('.key'), copy.with_suffix('.key'))
        self.assertEqual(config.load_config(copy), value)

    def test_windows_configuration_has_clear_macos_migration_error(self):
        path = self.root / 'config.json'
        path.write_text('{"format":"dpapi-machine","data":"AA=="}')
        with patch.object(config, 'os', SimpleNamespace(name='posix')):
            with self.assertRaisesRegex(ValueError, 'Windows.*重新配置'):
                config.load_config(path)
