import io
import json
import plistlib
import signal
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app import macos_deploy as mac


class MacDesktopControlsTest(unittest.TestCase):
    def setUp(self):
        scratch = Path.cwd() / '.test-temp'
        scratch.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / 'user-home'
        self.profile = (self.home / 'Library/Application Support/Oneasy/Desktop').as_posix()
        self.requests = []
        self.native = []
        self.killed = []

    def response_opener(self, action):
        def send(request, timeout):
            self.requests.append((request, timeout))
            return io.BytesIO(json.dumps({'action': action}).encode())
        return SimpleNamespace(open=send)

    def process(self, pid=301, profile=None, browser='Google Chrome', uid=501, extra=''):
        profile = self.profile if profile is None else profile
        return (f'{pid} {uid} /Applications/{browser}.app/Contents/MacOS/{browser} '
                f'--app=http://127.0.0.1:8765/ --user-data-dir={profile} '
                f'--no-first-run{extra}\n')

    def native_run(self, command, **kwargs):
        self.native.append(command)
        if command[0] == '/bin/ps':
            processes = [self.process(), self.process(302, browser='Microsoft Edge'),
                         self.process(303, profile=self.profile + '-other'),
                         self.process(304, uid=502),
                         self.process(305, extra=' --type=renderer'),
                         '306 501 /Applications/Other.app/Contents/MacOS/Other --user-data-dir=' + self.profile + '\n']
            if '-p' in command:
                processes = [line for line in processes if line.startswith(command[command.index('-p') + 1] + ' ')]
            return subprocess.CompletedProcess(command, 0, ''.join(processes), '')
        if command[0] == '/usr/bin/osascript':
            return subprocess.CompletedProcess(command, 0, '', '')
        raise AssertionError('Unexpected native operation: ' + repr(command))

    def test_hide_redeems_token_and_closes_only_current_users_dedicated_browser_roots(self):
        with patch.object(mac, 'build_opener', return_value=self.response_opener('hide')), \
             patch.object(mac.subprocess, 'run', side_effect=self.native_run), \
             patch.object(mac.os, 'getuid', return_value=501, create=True), \
             patch.object(mac.os, 'kill', side_effect=lambda pid, signum: self.killed.append((pid, signum))):
            mac.desktop_control('oneasy-mail-assistant://control/8765/a_B-9', self.home)
        request, timeout = self.requests[0]
        self.assertEqual(request.full_url, 'http://127.0.0.1:8765/api/desktop/command')
        self.assertEqual(request.get_method(), 'POST')
        self.assertEqual(json.loads(request.data), {'token': 'a_B-9'})
        self.assertLessEqual(timeout, 10)
        self.assertEqual(self.killed, [(301, signal.SIGTERM), (302, signal.SIGTERM)])
        self.assertFalse(any(command[0] == '/usr/bin/osascript' for command in self.native))

    def test_exit_unloads_keepalive_system_service_before_closing_window(self):
        events = []
        def native(command, **kwargs):
            events.append(command)
            return self.native_run(command, **kwargs)
        with patch.object(mac, 'build_opener', return_value=self.response_opener('exit')), \
             patch.object(mac.subprocess, 'run', side_effect=native), \
             patch.object(mac.os, 'getuid', return_value=501, create=True), \
             patch.object(mac.os, 'kill', side_effect=lambda pid, signum: events.append(('kill', pid, signum))):
            mac.desktop_control('oneasy-mail-assistant://control/8765/token', self.home)
        self.assertEqual(events[0], ['/usr/bin/osascript', '-e',
            'do shell script "/bin/launchctl bootout system/com.oneasy.mailassistant" with administrator privileges'])
        self.assertTrue(any(event[0] == 'kill' for event in events))
        self.assertFalse(any(command[0] in ('pkill', 'killall') for command in self.native))

    def test_cancelled_exit_keeps_window_and_reports_background_still_running(self):
        def cancelled(command, **kwargs):
            self.native.append(command)
            return subprocess.CompletedProcess(command, 1, '', 'User canceled. (-128)')
        with patch.object(mac, 'build_opener', return_value=self.response_opener('exit')), \
             patch.object(mac.subprocess, 'run', side_effect=cancelled), \
             patch.object(mac.os, 'kill') as kill:
            with self.assertRaisesRegex(ValueError, '后台将继续运行'):
                mac.desktop_control('oneasy-mail-assistant://control/8765/token', self.home)
        self.assertFalse(kill.called)
        self.assertEqual(len(self.native), 1)

    def test_native_stop_error_keeps_window_and_reports_background_still_running(self):
        with patch.object(mac, 'build_opener', return_value=self.response_opener('exit')), \
             patch.object(mac.subprocess, 'run', side_effect=OSError('native command unavailable')), \
             patch.object(mac.os, 'kill') as kill:
            with self.assertRaisesRegex(ValueError, '后台将继续运行'):
                mac.desktop_control('oneasy-mail-assistant://control/8765/token', self.home)
        self.assertFalse(kill.called)

    def test_untrusted_uris_are_rejected_before_network_or_native_operations(self):
        invalid = ['', 'http://control/8765/token', 'oneasy-mail-assistant://control/1/token',
                   'oneasy-mail-assistant://control/8765/', 'oneasy-mail-assistant://control/8765/a/b',
                   'oneasy-mail-assistant://control/8765/token?x=y',
                   'oneasy-mail-assistant://control/8765/token#hide',
                   'oneasy-mail-assistant://control/8765/%74oken',
                   'oneasy-mail-assistant://control/8765/a;touch',
                   'oneasy-mail-assistant://control/8765/' + 'x' * 101,
                   'oneasy-mail-assistant://control/8765/token\n']
        with patch.object(mac, 'build_opener') as opener, patch.object(mac.subprocess, 'run') as native:
            for uri in invalid:
                with self.subTest(uri=uri), self.assertRaises(ValueError):
                    mac.desktop_control(uri, self.home)
        self.assertFalse(opener.called)
        self.assertFalse(native.called)

    def test_unrecognized_action_cannot_run_a_native_operation(self):
        with patch.object(mac, 'build_opener', return_value=self.response_opener('reboot')), \
             patch.object(mac.subprocess, 'run') as native:
            with self.assertRaises(ValueError):
                mac.desktop_control('oneasy-mail-assistant://control/8765/token', self.home)
        self.assertFalse(native.called)

    def test_process_reused_between_inventory_and_close_is_not_signalled(self):
        def native(command, **kwargs):
            output = self.process() if '-p' not in command else self.process(profile=self.profile + '-other')
            return subprocess.CompletedProcess(command, 0, output, '')
        with patch.object(mac.subprocess, 'run', side_effect=native), \
             patch.object(mac.os, 'getuid', return_value=501, create=True), patch.object(mac.os, 'kill') as kill:
            self.assertEqual(mac.close_desktop_windows(self.home), 0)
        self.assertFalse(kill.called)

    def install_fixture(self):
        installed = self.root / 'installed'
        installed.mkdir()
        (installed / 'deployment.json').write_text(json.dumps({
            'service_name': 'com.oneasy.mailassistant', 'port': 8765, 'user': 'colleague'}))
        daemon = self.root / 'service.plist'
        daemon.write_bytes(plistlib.dumps(mac.daemon_config(installed, 'colleague', self.home)))
        return installed, daemon

    def test_start_after_exit_bootstraps_existing_service_and_waits_before_opening(self):
        installed, daemon = self.install_fixture()
        events = []
        def native(command, **kwargs):
            events.append(command)
            if command[:3] == ['/bin/launchctl', 'print', 'system/com.oneasy.mailassistant']:
                return subprocess.CompletedProcess(command, 113, '', 'Could not find service "com.oneasy.mailassistant" in domain for system')
            if command[0] == '/usr/sbin/lsof':
                return subprocess.CompletedProcess(command, 1, '', '')
            if command[0] == '/usr/bin/osascript':
                return subprocess.CompletedProcess(command, 0, '', '')
            raise AssertionError(repr(command))
        with patch.object(mac.subprocess, 'run', side_effect=native), \
             patch.object(mac, 'wait_ready', side_effect=lambda: events.append('ready')), \
             patch.object(mac, 'open_desktop', side_effect=lambda home: events.append('open')):
            mac.launch_desktop(installed, daemon, self.home, 'colleague')
        # Check the approved native command rather than assuming a Python monitor was spawned.
        prompt = next(command for command in events if isinstance(command, list) and command[0] == '/usr/bin/osascript')
        self.assertIn('/bin/launchctl bootstrap system ', prompt[-1])
        self.assertIn('with administrator privileges', prompt[-1])
        self.assertLess(events.index(prompt), events.index('ready'))
        self.assertLess(events.index('ready'), events.index('open'))

    def test_cancelled_start_does_not_claim_running_or_open_a_window(self):
        installed, daemon = self.install_fixture()
        def native(command, **kwargs):
            if command[0] == '/bin/launchctl':
                return subprocess.CompletedProcess(command, 113, '', 'Could not find service "com.oneasy.mailassistant" in domain for system')
            if command[0] == '/usr/sbin/lsof':
                return subprocess.CompletedProcess(command, 1, '', '')
            if command[0] == '/usr/bin/osascript':
                return subprocess.CompletedProcess(command, 1, '', 'User canceled. (-128)')
            raise AssertionError(repr(command))
        with patch.object(mac.subprocess, 'run', side_effect=native), patch.object(mac, 'wait_ready') as ready, \
             patch.object(mac, 'open_desktop') as opened:
            with self.assertRaisesRegex(ValueError, '后台尚未恢复运行'):
                mac.launch_desktop(installed, daemon, self.home, 'colleague')
        self.assertFalse(ready.called)
        self.assertFalse(opened.called)

    def test_default_browser_hide_explains_manual_close_without_signalling_browser(self):
        alerts = []
        def native(command, **kwargs):
            if command[0] == '/bin/ps':
                return subprocess.CompletedProcess(command, 0, self.process(profile=self.profile + '-other'), '')
            if command[0] == '/usr/bin/osascript':
                alerts.append(command[-1])
                return subprocess.CompletedProcess(command, 0, '', '')
            raise AssertionError(repr(command))
        with patch.object(mac, 'build_opener', return_value=self.response_opener('hide')), \
             patch.object(mac.subprocess, 'run', side_effect=native), \
             patch.object(mac.os, 'getuid', return_value=501, create=True), patch.object(mac.os, 'kill') as kill:
            mac.desktop_control('oneasy-mail-assistant://control/8765/token', self.home)
        self.assertFalse(kill.called)
        self.assertEqual(len(alerts), 1)
        self.assertIn('后台保持当前监控状态', alerts[0])
        self.assertIn('手动', alerts[0])

    def test_open_desktop_passes_dedicated_profile_to_available_browser_as_single_argument(self):
        application = self.home / 'Applications/Google Chrome.app'
        def available(path):
            return path == application
        with patch.object(Path, 'is_dir', available), patch.object(mac.subprocess, 'run') as native:
            mac.open_desktop(self.home)
        self.assertEqual(native.call_args.args[0], ['/usr/bin/open', '-na', str(application), '--args',
            '--app=http://127.0.0.1:8765/', '--user-data-dir=' + self.profile,
            '--no-first-run', '--window-size=1280,900'])

    def test_open_desktop_falls_back_to_default_browser_when_no_supported_app_exists(self):
        with patch.object(Path, 'is_dir', return_value=False), patch.object(mac.subprocess, 'run') as native:
            mac.open_desktop(self.home)
        self.assertEqual(native.call_args.args[0], ['/usr/bin/open', 'http://127.0.0.1:8765/'])

    def test_running_service_is_reused_without_another_monitor(self):
        installed, daemon = self.install_fixture()
        commands = []
        def native(command, **kwargs):
            commands.append(command)
            if command[0] == '/bin/launchctl':
                return subprocess.CompletedProcess(command, 0, 'state = running\n\tpid = 400\n', '')
            if command[0] == '/usr/sbin/lsof':
                return subprocess.CompletedProcess(command, 0, '400\n', '')
            raise AssertionError(repr(command))
        with patch.object(mac.subprocess, 'run', side_effect=native), patch.object(mac, 'wait_ready'), \
             patch.object(mac, 'open_desktop') as opened:
            mac.launch_desktop(installed, daemon, self.home, 'colleague')
        opened.assert_called_once_with(self.home)
        self.assertEqual(len(commands), 2)

    def test_foreign_port_listener_is_reported_without_bootstrap_or_window(self):
        installed, daemon = self.install_fixture()
        def native(command, **kwargs):
            if command[0] == '/bin/launchctl':
                return subprocess.CompletedProcess(command, 113, '', 'Could not find service "com.oneasy.mailassistant" in domain for system')
            if command[0] == '/usr/sbin/lsof':
                return subprocess.CompletedProcess(command, 0, '999\n', '')
            raise AssertionError(repr(command))
        with patch.object(mac.subprocess, 'run', side_effect=native), patch.object(mac, 'wait_ready') as ready, \
             patch.object(mac, 'open_desktop') as opened:
            with self.assertRaisesRegex(ValueError, '其他程序占用'):
                mac.launch_desktop(installed, daemon, self.home, 'colleague')
        self.assertFalse(ready.called)
        self.assertFalse(opened.called)

    def test_unknown_service_status_is_not_treated_as_stopped(self):
        installed, daemon = self.install_fixture()
        with patch.object(mac.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, '', 'Operation not permitted')) as native, \
             patch.object(mac, 'open_desktop') as opened:
            with self.assertRaisesRegex(ValueError, '状态'):
                mac.launch_desktop(installed, daemon, self.home, 'colleague')
        self.assertEqual(native.call_count, 1)
        self.assertFalse(opened.called)

    def test_foreign_existing_plist_never_prompts_for_admin_or_starts(self):
        installed, daemon = self.install_fixture()
        config = plistlib.loads(daemon.read_bytes())
        config['ProgramArguments'] = ['/some/other/program']
        daemon.write_bytes(plistlib.dumps(config))
        with patch.object(mac.subprocess, 'run') as native:
            with self.assertRaises(ValueError):
                mac.launch_desktop(installed, daemon, self.home, 'colleague')
        self.assertFalse(native.called)

    def test_application_registers_url_scheme_and_compiles_separate_run_and_url_handlers(self):
        application = self.root / 'Oneasy.app'
        compiled = []
        def compile_app(command, **kwargs):
            compiled.append(command)
            temporary = Path(command[command.index('-o') + 1])
            (temporary / 'Contents').mkdir(parents=True)
            (temporary / 'Contents/Info.plist').write_bytes(plistlib.dumps({}))
        with patch.object(mac, 'run', side_effect=compile_app):
            mac.install_application(self.root, application)
        info = plistlib.loads((application / 'Contents/Info.plist').read_bytes())
        self.assertEqual(info['CFBundleURLTypes'][0]['CFBundleURLSchemes'], ['oneasy-mail-assistant'])
        script = compiled[0][compiled[0].index('-e') + 1]
        self.assertIn('on run', script)
        self.assertIn('on open location controlURI', script)
        self.assertIn('quoted form of controlURI', script)
        self.assertIn('end open location', script)


if __name__ == '__main__':
    unittest.main()
