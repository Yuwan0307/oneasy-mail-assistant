import hashlib
import json
import plistlib
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import sys
import os
import stat


class MacDeploymentTest(unittest.TestCase):
    def setUp(self):
        scratch = Path.cwd() / '.test-temp'; scratch.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_manifest_detects_changed_files_and_paths_outside_package(self):
        from app.macos_deploy import verify_manifest
        package = self.root / 'package'; package.mkdir()
        (package / 'file.txt').write_bytes(b'original')
        manifest = {'files': [{'path': 'file.txt', 'sha256': hashlib.sha256(b'original').hexdigest()}]}
        (package / 'manifest.json').write_text(json.dumps(manifest))
        verify_manifest(package)
        (package / 'file.txt').write_bytes(b'changed')
        with self.assertRaises(ValueError): verify_manifest(package)
        manifest['files'][0]['path'] = '../file.txt'
        (package / 'manifest.json').write_text(json.dumps(manifest))
        with self.assertRaises(ValueError): verify_manifest(package)

    def test_install_parent_is_traversable_when_private_umask_is_used(self):
        from app.macos_deploy import prepare_root
        installed = self.root / 'Oneasy/MailAssistant'
        previous = os.umask(0o077)
        try:
            prepare_root(installed)
        finally:
            os.umask(previous)
        self.assertTrue(installed.is_dir())
        if os.name != 'nt':
            self.assertEqual(stat.S_IMODE(installed.parent.stat().st_mode), 0o755)
            self.assertEqual(stat.S_IMODE(installed.stat().st_mode), 0o755)

    def test_existing_install_rejects_foreign_files_owner_and_listener(self):
        from app.macos_deploy import validate_existing
        installed = self.root / 'installed'; installed.mkdir()
        plist = self.root / 'daemon.plist'
        self.assertFalse(validate_existing(installed, plist, 'colleague', [], None))
        (installed / 'other.txt').write_text('foreign')
        with self.assertRaises(ValueError): validate_existing(installed, plist, 'colleague', [], None)
        (installed / 'other.txt').unlink()
        metadata = {'service_name': 'com.oneasy.mailassistant', 'port': 8765, 'user': 'colleague'}
        (installed / 'deployment.json').write_text(json.dumps(metadata))
        plist.write_bytes(plistlib.dumps({'Label': 'com.oneasy.mailassistant', 'UserName': 'colleague',
            'ProgramArguments': [(installed / '.venv/bin/python').as_posix(), '-m', 'app.service_runner',
                                 '--data', (installed / 'data').as_posix(), '--port', '8765']}))
        self.assertTrue(validate_existing(installed, plist, 'colleague', [123], 123))
        with self.assertRaises(ValueError): validate_existing(installed, plist, 'another', [], None)
        with self.assertRaises(ValueError): validate_existing(installed, plist, 'colleague', [456], 123)
        plist.write_bytes(plistlib.dumps({'Label': 'com.oneasy.mailassistant', 'UserName': 'colleague',
            'ProgramArguments': ['/other/program']}))
        with self.assertRaises(ValueError): validate_existing(installed, plist, 'colleague', [], None)

    def test_snapshot_preserves_encrypted_files_and_consistent_sqlite(self):
        from app.macos_deploy import snapshot_data
        data = self.root / 'data'; data.mkdir()
        (data / 'config.json').write_bytes(b'opaque encrypted configuration')
        (data / 'config.key').write_bytes(b'private key')
        (data / 'archive').mkdir(); (data / 'archive/item.txt').write_text('audit')
        with closing(sqlite3.connect(data / 'main.db')) as db:
            db.execute('CREATE TABLE state (activation TEXT)')
            db.execute("INSERT INTO state VALUES ('original activation')")
            db.commit()
            destination = self.root / 'snapshot'
            snapshot_data(data, destination)
        self.assertEqual((destination / 'config.json').read_bytes(), b'opaque encrypted configuration')
        self.assertEqual((destination / 'config.key').read_bytes(), b'private key')
        self.assertEqual((destination / 'archive/item.txt').read_text(), 'audit')
        with closing(sqlite3.connect(destination / 'main.db')) as db:
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
        with self.assertRaises(ValueError): snapshot_data(data, destination)

    def test_launchdaemon_runs_as_original_user_with_private_environment(self):
        from app.macos_deploy import daemon_config
        config = daemon_config(Path('/Library/Application Support/Oneasy/MailAssistant'), 'colleague', Path('/Users/colleague'))
        parsed = plistlib.loads(plistlib.dumps(config))
        self.assertEqual(parsed['UserName'], 'colleague')
        self.assertEqual(parsed['ProgramArguments'], ['/Library/Application Support/Oneasy/MailAssistant/.venv/bin/python',
            '-m', 'app.service_runner', '--data', '/Library/Application Support/Oneasy/MailAssistant/data', '--port', '8765'])
        self.assertTrue(parsed['RunAtLoad']); self.assertTrue(parsed['KeepAlive'])
        self.assertEqual(parsed['EnvironmentVariables']['HOME'], '/Users/colleague')
        self.assertEqual(parsed['EnvironmentVariables']['PYTHONNOUSERSITE'], '1')
        self.assertEqual(parsed['EnvironmentVariables']['PYTHONDONTWRITEBYTECODE'], '1')

    def test_restore_removes_new_code_and_restores_previous_data(self):
        from app.macos_deploy import restore_install
        installed = self.root / 'installed'; installed.mkdir()
        backup = installed / 'backups/previous'; backup.mkdir(parents=True)
        for folder in ('app', 'scripts', '.venv', 'data'):
            (backup / folder).mkdir(); (backup / folder / 'old.txt').write_text(folder)
            (installed / folder).mkdir(); (installed / folder / 'new.txt').write_text('changed')
        (backup / 'deployment.json').write_text('{"version":"old"}')
        (installed / 'deployment.json').write_text('{"version":"new"}')
        restore_install(installed, backup)
        self.assertEqual((installed / 'data/old.txt').read_text(), 'data')
        self.assertFalse((installed / 'app/new.txt').exists())
        self.assertEqual(json.loads((installed / 'deployment.json').read_text())['version'], 'old')
        self.assertTrue(backup.exists())
        with self.assertRaises(ValueError): restore_install(installed, self.root / 'outside')

    def test_failed_repeat_deploy_restores_code_config_and_previous_daemon(self):
        from app import macos_deploy as deployer
        package = self.root / 'package'; package.mkdir()
        installed = self.root / 'installed'; installed.mkdir()
        for name in ('app', 'scripts'):
            (package / name).mkdir(); (package / name / 'new.txt').write_text('new code')
            (installed / name).mkdir(); (installed / name / 'old.txt').write_text('old code')
        for name in ('requirements.txt', 'runtime-packages-macos.txt'):
            (package / name).write_text('dependencies')
        manifest = {'files': [{'path': path.relative_to(package).as_posix(),
            'sha256': hashlib.sha256(path.read_bytes()).hexdigest()} for path in package.rglob('*') if path.is_file()]}
        (package / 'manifest.json').write_text(json.dumps(manifest))
        metadata = {'service_name': 'com.oneasy.mailassistant', 'port': 8765, 'user': 'colleague', 'version': 'old'}
        (installed / 'deployment.json').write_text(json.dumps(metadata))
        (installed / 'data').mkdir(); (installed / 'data/config.json').write_bytes(b'old encrypted configuration')
        (installed / '.venv').mkdir(); (installed / '.venv/old.txt').write_text('old runtime')
        plist = self.root / 'daemon.plist'
        old_plist = plistlib.dumps(deployer.daemon_config(installed, 'colleague', self.root))
        plist.write_bytes(old_plist)
        account = SimpleNamespace(pw_uid=501, pw_gid=20, pw_dir=str(self.root))
        fake_pwd = SimpleNamespace(getpwnam=lambda user: account)
        def runtime(root, *args):
            (root / '.venv').mkdir(); (root / '.venv/new.txt').write_text('new runtime')
        with patch.dict(sys.modules, {'pwd': fake_pwd}), patch.object(deployer, 'listener_pids', return_value=[123]), \
             patch.object(deployer, 'daemon_pid', return_value=123), patch.object(deployer, 'stop_daemon'), \
             patch.object(deployer.os, 'chown', create=True), patch.object(deployer, 'run'), \
             patch.object(deployer, 'install_runtime', side_effect=runtime), \
             patch.object(deployer, 'wait_ready', side_effect=ValueError('new daemon failed')):
            with self.assertRaisesRegex(ValueError, 'new daemon failed'):
                deployer.deploy(package, 'colleague', installed, plist, self.root / 'Oneasy.app')
        self.assertEqual((installed / 'app/old.txt').read_text(), 'old code')
        self.assertFalse((installed / 'app/new.txt').exists())
        self.assertEqual((installed / '.venv/old.txt').read_text(), 'old runtime')
        self.assertEqual((installed / 'data/config.json').read_bytes(), b'old encrypted configuration')
        self.assertEqual(plist.read_bytes(), old_plist)

    def test_failed_first_runtime_install_can_be_retried_without_deleting_data(self):
        from app import macos_deploy as deployer
        package = self.root / 'package'; package.mkdir()
        for name in ('app', 'scripts'):
            (package / name).mkdir(); (package / name / 'new.txt').write_text('code')
        for name in ('requirements.txt', 'runtime-packages-macos.txt'):
            (package / name).write_text('dependencies')
        manifest = {'files': [{'path': path.relative_to(package).as_posix(),
            'sha256': hashlib.sha256(path.read_bytes()).hexdigest()} for path in package.rglob('*') if path.is_file()]}
        (package / 'manifest.json').write_text(json.dumps(manifest))
        installed = self.root / 'Oneasy/MailAssistant'
        plist = self.root / 'daemon.plist'
        account = SimpleNamespace(pw_uid=501, pw_gid=20, pw_dir=str(self.root))
        with patch.dict(sys.modules, {'pwd': SimpleNamespace(getpwnam=lambda user: account)}), \
             patch.object(deployer, 'listener_pids', return_value=[]), patch.object(deployer, 'daemon_pid', return_value=None), \
             patch.object(deployer, 'stop_daemon'), patch.object(deployer.os, 'chown', create=True), \
             patch.object(deployer, 'run'), patch.object(deployer, 'wait_ready'), patch.object(deployer, 'install_application'), \
             patch.object(deployer, 'install_runtime', side_effect=ValueError('interrupted install')) as runtime:
            with self.assertRaisesRegex(ValueError, 'interrupted install'):
                deployer.deploy(package, 'colleague', installed, plist, self.root / 'Oneasy.app')
            self.assertTrue(deployer.validate_existing(installed, plist, 'colleague', [], None))
            (installed / 'data/config.json').write_bytes(b'configuration retained')
            runtime.side_effect = None
            deployer.deploy(package, 'colleague', installed, plist, self.root / 'Oneasy.app')
        self.assertEqual((installed / 'data/config.json').read_bytes(), b'configuration retained')
        self.assertEqual(json.loads((installed / 'deployment.json').read_text())['version'], '1.6.12')


if __name__ == '__main__': unittest.main()
