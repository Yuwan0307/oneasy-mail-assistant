"""Run the actual .cmd through Windows, with a harmless installation stub."""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


@unittest.skipUnless(os.name == 'nt', 'Windows command interpreter required')
class LauncherTest(unittest.TestCase):
    def run_launcher(self, exit_code):
        package = Path(__file__).resolve().parents[1]
        scratch = Path.cwd() / '.test-temp'
        scratch.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as temporary:
            root = Path(temporary) / '中文 空格目录'
            (root / 'scripts').mkdir(parents=True)
            entry = root / '部署邮件助手.cmd'
            entry.write_bytes((package / entry.name).read_bytes())
            (root / 'scripts/deploy.ps1').write_text(
                "Write-Output 'ONEASY_INSTALLER_REACHED'\nexit " + str(exit_code) + '\n',
                encoding='utf-8-sig')
            # EOF permits a failure pause to end without installing or sending any messages.
            return subprocess.run([os.environ['COMSPEC'], '/d', '/c', str(entry)],
                                  input=b'', capture_output=True, timeout=25)

    def test_cmd_starts_powershell_from_a_chinese_path_with_spaces(self):
        result = self.run_launcher(0)
        self.assertIn(b'ONEASY_INSTALLER_REACHED', result.stdout)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stderr, b'')

    def test_cmd_shows_failure_and_preserves_installer_exit_code(self):
        result = self.run_launcher(23)
        self.assertIn(b'ONEASY_INSTALLER_REACHED', result.stdout)
        self.assertIn('部署未完成'.encode('utf-8'), result.stdout)
        self.assertEqual(result.returncode, 23)
        self.assertEqual(result.stderr, b'')


if __name__ == '__main__':
    unittest.main()
