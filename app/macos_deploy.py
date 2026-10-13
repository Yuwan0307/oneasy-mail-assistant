"""macOS deployment helpers. Uses only the standard library for bootstrap."""
import hashlib
import json
import os
import platform
import plistlib
import re
import shlex
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
from contextlib import closing, contextmanager
from datetime import datetime
from pathlib import Path
from urllib.request import ProxyHandler, Request, build_opener

VERSION = '1.6.4'
LABEL = 'com.oneasy.mailassistant'
PORT = 8765
ROOT = Path('/Library/Application Support/Oneasy/MailAssistant')
PLIST = Path('/Library/LaunchDaemons/com.oneasy.mailassistant.plist')
APPLICATION = Path('/Applications/Oneasy 邮件助手.app')
PYTHON = '/Library/Frameworks/Python.framework/Versions/3.13/bin/python3.13'
CODE_NAMES = ('app', 'scripts', '.venv', 'deployment.json', 'requirements.txt',
              'runtime-packages-macos.txt', 'runtime-packages-macos-arm64.txt',
              'runtime-packages-macos-x86_64.txt', 'manifest.json')


@contextmanager
def deployment_lock():
    import fcntl
    descriptor = os.open('/var/run/com.oneasy.mailassistant.deploy.lock',
                         os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        if os.fstat(descriptor).st_uid != 0:
            raise ValueError('部署锁文件的所有者不正确，请联系管理员。')
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('另一项部署正在进行，请等待完成后再试。') from None
        yield
    finally:
        os.close(descriptor)


def verify_manifest(source):
    source = Path(source).resolve()
    manifest = json.loads((source / 'manifest.json').read_text(encoding='utf-8-sig'))
    if not manifest.get('files'):
        raise ValueError('交付包校验清单为空，请重新解压。')
    for entry in manifest['files']:
        relative = Path(entry['path'])
        path = source / relative
        if relative.is_absolute() or '..' in relative.parts or path.is_symlink() or not path.resolve().is_relative_to(source):
            raise ValueError('交付包文件路径无效。')
        if not path.is_file():
            raise ValueError('交付包缺少文件：' + entry['path'])
        with path.open('rb') as file:
            digest = hashlib.file_digest(file, 'sha256').hexdigest()
        if digest != entry['sha256'].lower():
            raise ValueError('交付包文件校验失败，请重新解压：' + entry['path'])


def daemon_config(root, user, home):
    root, home = Path(root), Path(home)
    return {'Label': LABEL, 'UserName': user,
            'ProgramArguments': [(root / '.venv/bin/python').as_posix(), '-m', 'app.service_runner',
                                 '--data', (root / 'data').as_posix(), '--port', str(PORT)],
            'WorkingDirectory': root.as_posix(), 'RunAtLoad': True, 'KeepAlive': True,
            'ThrottleInterval': 10, 'ExitTimeOut': 45, 'Umask': 0o077,
            'StandardOutPath': str(root / 'logs/service.log'),
            'StandardErrorPath': str(root / 'logs/service-error.log'),
            'EnvironmentVariables': {'HOME': home.as_posix(), 'TMPDIR': '/tmp',
                'PATH': '/usr/bin:/bin:/usr/sbin:/sbin',
                'PYTHONNOUSERSITE': '1', 'PYTHONDONTWRITEBYTECODE': '1'}}


def validate_existing(root, plist_path, user, listeners, daemon_pid):
    root, plist_path = Path(root), Path(plist_path)
    if root.is_symlink() or plist_path.is_symlink():
        raise ValueError('安装目录和后台配置不能是符号链接。')
    metadata_path = root / 'deployment.json'
    existing = metadata_path.exists()
    if root.exists() and any(root.iterdir()) and not existing:
        raise ValueError('安装目录已有其他文件，不能覆盖。')
    if existing:
        metadata = json.loads(metadata_path.read_text(encoding='utf-8-sig'))
        if (metadata.get('service_name'), metadata.get('port'), metadata.get('user')) != (LABEL, PORT, user):
            raise ValueError('原有安装的服务、端口或运行用户不一致，请使用原安装用户更新。')
    if plist_path.exists():
        config = plistlib.loads(plist_path.read_bytes())
        expected = daemon_config(root, user, Path('/'))
        if not existing or any(config.get(key) != expected[key] for key in ('Label', 'UserName', 'ProgramArguments')):
            raise ValueError('同名后台属于其他安装，不能覆盖。')
    if daemon_pid and not plist_path.exists():
        raise ValueError('同名后台缺少可核验的配置，请先检查原安装。')
    if any(pid != daemon_pid for pid in listeners) or (listeners and not existing):
        raise ValueError('本机端口 8765 已被其他程序占用；部署不会结束其他进程。')
    return existing


def prepare_root(root):
    root = Path(root)
    if root.is_symlink() or root.parent.is_symlink():
        raise ValueError('Oneasy 安装目录不能是符号链接。')
    # Only the known brand directory is created; the system Application Support stays unchanged.
    root.parent.mkdir(mode=0o755, exist_ok=True)
    root.parent.chmod(0o755)
    root.mkdir(mode=0o755, exist_ok=True)
    root.chmod(0o755)


def snapshot_data(source, destination):
    source, destination = Path(source), Path(destination)
    if destination.exists():
        raise ValueError('备份目录已存在。')
    if source.is_symlink() or any(path.is_symlink() for path in source.rglob('*')):
        raise ValueError('数据目录不能包含符号链接。')
    shutil.copytree(source, destination, ignore=shutil.ignore_patterns('main.db', 'main.db-wal', 'main.db-shm'))
    database = source / 'main.db'
    if database.exists():
        with closing(sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True)) as original:
            with closing(sqlite3.connect(destination / 'main.db')) as backup:
                original.backup(backup)
                if backup.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise ValueError('数据库备份校验失败。')


def remove_child(root, path):
    root, path = Path(root).resolve(), Path(path)
    if not path.parent.resolve().is_relative_to(root) or path == root or not path.resolve().is_relative_to(root):
        raise ValueError('拒绝操作安装目录之外的路径。')
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


def restore_install(root, backup):
    root, backup = Path(root), Path(backup)
    if not backup.resolve().is_relative_to((root / 'backups').resolve()):
        raise ValueError('恢复源必须位于本安装的备份目录。')
    for name in (*CODE_NAMES, 'data'):
        saved, target = backup / name, root / name
        remove_child(root, target)
        if saved.is_dir():
            shutil.copytree(saved, target, symlinks=True)
        elif saved.exists():
            shutil.copy2(saved, target)


def run(command, **kwargs):
    return subprocess.run([str(value) for value in command], check=True, text=True, **kwargs)


def daemon_pid():
    result = subprocess.run(['/bin/launchctl', 'print', 'system/' + LABEL], capture_output=True, text=True)
    if result.returncode:
        return None
    found = re.search(r'^\s*pid = (\d+)', result.stdout, re.MULTILINE)
    return int(found.group(1)) if found else None


def listener_pids():
    result = subprocess.run(['/usr/sbin/lsof', '-nP', '-iTCP:' + str(PORT), '-sTCP:LISTEN', '-t'],
                            capture_output=True, text=True)
    if result.returncode not in (0, 1):
        raise ValueError('无法检查端口占用情况。')
    return [int(line) for line in result.stdout.splitlines() if line.strip()]


def stop_daemon():
    result = subprocess.run(['/bin/launchctl', 'bootout', 'system/' + LABEL], capture_output=True, text=True)
    if result.returncode and daemon_pid():
        raise ValueError('旧后台未能停止，已取消部署。')
    for _ in range(50):
        if not daemon_pid():
            return
        time.sleep(1)
    raise ValueError('等待旧后台停止超时，已取消部署。')


def wait_ready():
    opener = build_opener(ProxyHandler({}))
    for _ in range(45):
        try:
            with opener.open('http://127.0.0.1:8765/api/health', timeout=2) as response:
                health = json.load(response)
            if health.get('app') == 'oneasy-mail-assistant' and health.get('version') == VERSION:
                return
        except (OSError, ValueError):
            pass
        time.sleep(1)
    raise ValueError('后台启动超时，请查看安装目录 logs 中的日志。')


def secure_data(root, uid, gid):
    for name in ('data', 'logs'):
        folder = root / name
        folder.mkdir(exist_ok=True)
        for path in [folder, *folder.rglob('*')]:
            if path.is_symlink():
                raise ValueError('数据和日志目录不能包含符号链接。')
            os.chown(path, uid, gid)
            path.chmod(0o700 if path.is_dir() else 0o600)
    (root / 'backups').chmod(0o700)


def install_runtime(root, source, user, home):
    architecture = platform.machine()
    if architecture not in ('arm64', 'x86_64'):
        raise ValueError('仅支持 Apple Silicon 和 Intel Mac。')
    run([PYTHON, '-m', 'venv', root / '.venv'])
    python = root / '.venv/bin/python'
    requirements = source / ('runtime-packages-macos-' + architecture + '.txt')
    if not requirements.exists():
        requirements = source / 'runtime-packages-macos.txt'
    run([python, '-m', 'pip', 'install', '--no-index', '--only-binary=:all:', '--no-cache-dir',
         '--find-links', source / 'vendor/wheels' / architecture, '-r', requirements])
    for path in [root / '.venv', *(root / '.venv').rglob('*')]:
        if not path.is_symlink():
            path.chmod(0o755 if path.is_dir() or path.parent == root / '.venv/bin' else 0o644)
    # Import as the daemon user, rather than accidentally relying on root's environment.
    run(['/usr/bin/sudo', '-u', user, '/usr/bin/env', 'HOME=' + str(home),
         'PYTHONNOUSERSITE=1', 'PYTHONDONTWRITEBYTECODE=1', python, '-c',
         'import ssl, certifi, app.service_runner; ssl.create_default_context(cafile=certifi.where())'], cwd=root)


def install_application(root, destination):
    if destination.exists():
        info = destination / 'Contents/Info.plist'
        if destination.is_symlink() or not info.is_file() or plistlib.loads(info.read_bytes()).get('CFBundleIdentifier') != LABEL + '.desktop':
            raise ValueError('应用程序中已存在同名的其他应用，不能覆盖。')
    temporary = destination.with_name('.oneasy-mailassistant-install.app')
    if temporary.exists():
        raise ValueError('应用程序中存在未处理的临时安装目录，请管理员检查。')
    command = '/bin/bash ' + shlex.quote(str(root / 'scripts/macos-launch.sh'))
    escaped = command.replace('\\', '\\\\').replace('"', '\\"')
    script = ('on run\n    do shell script "' + escaped + '"\nend run\n'
              'on open location controlURI\n    do shell script "' + escaped +
              ' " & quoted form of controlURI\nend open location')
    run(['/usr/bin/osacompile', '-o', temporary, '-e', script])
    info = temporary / 'Contents/Info.plist'
    metadata = plistlib.loads(info.read_bytes())
    metadata.update({'CFBundleIdentifier': LABEL + '.desktop', 'CFBundleName': 'Oneasy 邮件助手',
                     'CFBundleShortVersionString': VERSION,
                     'CFBundleURLTypes': [{'CFBundleURLName': LABEL + '.control',
                                           'CFBundleURLSchemes': ['oneasy-mail-assistant']}]})
    info.write_bytes(plistlib.dumps(metadata))
    for path in [temporary, *temporary.rglob('*')]:
        path.chmod(0o755 if path.is_dir() or path.stat().st_mode & 0o111 else 0o644)
    if destination.exists():
        shutil.rmtree(destination)  # Exact application path and identity were verified above.
    temporary.rename(destination)


def desktop_alert(message):
    escaped = str(message).replace('\\', '\\\\').replace('"', '\\"')
    subprocess.run(['/usr/bin/osascript', '-e',
                    'display alert "Oneasy 邮件助手" message "' + escaped + '" as warning'],
                   capture_output=True, text=True)


def desktop_admin(command, failure_message='管理员授权已取消或操作失败。后台将继续运行，窗口保持打开。'):
    escaped = command.replace('\\', '\\\\').replace('"', '\\"')
    try:
        result = subprocess.run(['/usr/bin/osascript', '-e',
                                 'do shell script "' + escaped + '" with administrator privileges'],
                                capture_output=True, text=True)
    except (OSError, subprocess.SubprocessError):
        raise ValueError(failure_message) from None
    if result.returncode:
        raise ValueError(failure_message)


def desktop_browser_pids(output, home, uid):
    home = Path(home)
    profile = (home / 'Library/Application Support/Oneasy/Desktop').as_posix()
    executables = [(base / (browser + '.app') / 'Contents/MacOS' / browser).as_posix()
                   for browser in ('Microsoft Edge', 'Google Chrome')
                   for base in (Path('/Applications'), home / 'Applications')]
    found = []
    for line in output.splitlines():
        fields = line.strip().split(None, 2)
        if len(fields) != 3 or not fields[0].isdigit() or fields[1] != str(uid):
            continue
        command = fields[2]
        if not any(command.startswith(executable + ' ') for executable in executables):
            continue
        # ps preserves spaces inside argv values. Require the whole dedicated profile
        # followed by another switch or end of command, never a similarly named profile.
        if (re.search(r'(?:^| )--user-data-dir=' + re.escape(profile) + r'(?= --|$)', command)
                and re.search(r'(?:^| )--app=http://127\.0\.0\.1:8765/(?= --|$)', command)
                and not re.search(r'(?:^| )--type=', command)):
            found.append(int(fields[0]))
    return found


def close_desktop_windows(home):
    uid = os.getuid()
    result = subprocess.run(['/bin/ps', '-axww', '-o', 'pid=,uid=,command='],
                            capture_output=True, text=True, check=True)
    closed = 0
    for pid in desktop_browser_pids(result.stdout, home, uid):
        # Recheck immediately before signalling so a reused PID is not mistaken
        # for the browser from the inventory. Only the dedicated browser is closed.
        current = subprocess.run(['/bin/ps', '-p', str(pid), '-ww', '-o', 'pid=,uid=,command='],
                                 capture_output=True, text=True)
        if current.returncode or pid not in desktop_browser_pids(current.stdout, home, uid):
            continue
        try:
            os.kill(pid, signal.SIGTERM)
            closed += 1
        except ProcessLookupError:
            pass
    return closed


def desktop_control(uri, home=None):
    match = re.fullmatch(r'oneasy-mail-assistant://control/8765/([A-Za-z0-9_-]{1,100})', uri)
    if not match:
        raise ValueError('助手控制链接无效，请从助手页面重新操作。')
    request = Request('http://127.0.0.1:8765/api/desktop/command',
                      data=json.dumps({'token': match.group(1)}).encode('utf-8'),
                      headers={'Content-Type': 'application/json'}, method='POST')
    try:
        with build_opener(ProxyHandler({})).open(request, timeout=5) as response:
            payload = json.loads(response.read(4096))
        action = payload.get('action') if isinstance(payload, dict) else None
    except (OSError, ValueError):
        raise ValueError('控制请求已失效或后台未响应；后台状态未更改，请重新打开助手再试。') from None
    if action not in ('hide', 'exit'):
        raise ValueError('控制请求无效；后台状态未更改，请重新操作。')
    if action == 'exit':
        # A KeepAlive LaunchDaemon must be unloaded, rather than killing its PID.
        # osascript runs in the desktop user's session and presents native approval.
        desktop_admin('/bin/launchctl bootout system/' + LABEL)
    home = Path.home() if home is None else Path(home)
    try:
        closed = close_desktop_windows(home)
    except (OSError, subprocess.SubprocessError):
        closed = 0
    if not closed:
        status = '后台已完全退出。' if action == 'exit' else '后台保持当前监控状态。'
        desktop_alert(status + '请手动点击窗口左上角的关闭按钮，关闭浏览器窗口。')


def open_desktop(home):
    home = Path(home)
    profile = home / 'Library/Application Support/Oneasy/Desktop'
    profile.mkdir(parents=True, exist_ok=True)
    url = 'http://127.0.0.1:8765/'
    for browser in ('Microsoft Edge', 'Google Chrome'):
        for base in (Path('/Applications'), home / 'Applications'):
            application = base / (browser + '.app')
            if application.is_dir():
                run(['/usr/bin/open', '-na', application, '--args', '--app=' + url,
                     '--user-data-dir=' + profile.as_posix(), '--no-first-run', '--window-size=1280,900'])
                return
    run(['/usr/bin/open', url])


def launch_desktop(root=ROOT, plist_path=PLIST, home=None, user=None):
    root, plist_path = Path(root), Path(plist_path)
    home = Path.home() if home is None else Path(home)
    if user is None:
        import pwd
        user = pwd.getpwuid(os.getuid()).pw_name
    # Only the verified installation may request privileged service startup.
    validate_existing(root, plist_path, user, [], None)
    if not (root / 'deployment.json').is_file() or not plist_path.is_file():
        raise ValueError('助手尚未正确安装，请联系管理员重新部署。')
    result = subprocess.run(['/bin/launchctl', 'print', 'system/' + LABEL],
                            capture_output=True, text=True)
    loaded = result.returncode == 0
    if not loaded and not ('Could not find service' in result.stderr and LABEL in result.stderr):
        raise ValueError('无法确认助手后台状态，请联系管理员检查。')
    found = re.search(r'^\s*pid = (\d+)', result.stdout, re.MULTILINE)
    pid = int(found.group(1)) if found else None
    if any(listener != pid for listener in listener_pids()):
        raise ValueError('本机端口 8765 已被其他程序占用；请联系管理员，助手不会结束其他程序。')
    if not loaded:
        desktop_admin('/bin/launchctl bootstrap system ' + shlex.quote(plist_path.as_posix()),
                      '管理员授权已取消或启动失败。后台尚未恢复运行，请重新打开助手并允许启动。')
    wait_ready()
    open_desktop(home)


def deploy(source, user, root=ROOT, plist_path=PLIST, application=APPLICATION):
    import pwd
    source, root, plist_path, application = map(Path, (source, root, plist_path, application))
    account = pwd.getpwnam(user); home = Path(account.pw_dir)
    if account.pw_uid == 0:
        raise ValueError('请使用同事自己的 Mac 账号部署，不能以 root 作为运行用户。')
    verify_manifest(source)
    if source.resolve().is_relative_to(root.resolve()) or root.resolve().is_relative_to(source.resolve()):
        raise ValueError('交付包和安装目录不能互相包含。')
    existing = validate_existing(root, plist_path, user, listener_pids(), daemon_pid())
    if application.exists():
        info = application / 'Contents/Info.plist'
        if application.is_symlink() or not info.is_file() or plistlib.loads(info.read_bytes()).get('CFBundleIdentifier') != LABEL + '.desktop':
            raise ValueError('应用程序中存在同名的其他应用，已取消部署。')
    prepare_root(root)
    run(['/usr/sbin/chown', 'root:wheel', root.parent, root])
    if not existing:
        (root / 'deployment.json').write_text(json.dumps({'service_name': LABEL, 'port': PORT,
            'user': user, 'version': 'pending'}), encoding='utf-8')
        (root / 'deployment.json').chmod(0o644)
    (root / 'backups').mkdir(exist_ok=True); (root / 'backups').chmod(0o700)
    backup = root / 'backups' / datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    backup.mkdir(mode=0o700)
    previous_plist = plist_path.read_bytes() if plist_path.exists() else None
    if previous_plist:
        (backup / 'launchdaemon.plist').write_bytes(previous_plist)
    for name in CODE_NAMES:
        path = root / name
        if path.is_dir(): shutil.copytree(path, backup / name, symlinks=True)
        elif path.exists(): shutil.copy2(path, backup / name)
    if (root / 'data').exists(): snapshot_data(root / 'data', backup / 'data')
    stopped = False
    changed = False
    try:
        if previous_plist:
            stop_daemon(); stopped = True
            if (backup / 'data').exists(): remove_child(root, backup / 'data')
            if (root / 'data').exists(): snapshot_data(root / 'data', backup / 'data')
        changed = True
        for name in ('app', 'scripts'):
            remove_child(root, root / name)
            shutil.copytree(source / name, root / name, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        for name in ('requirements.txt', 'runtime-packages-macos.txt', 'runtime-packages-macos-arm64.txt',
                     'runtime-packages-macos-x86_64.txt', 'manifest.json'):
            if (source / name).exists(): shutil.copy2(source / name, root / name)
        # Root code is readable by the daemon user and cannot be changed by them.
        for path in [root / 'app', root / 'scripts', *(root / 'app').rglob('*'), *(root / 'scripts').rglob('*')]:
            path.chmod(0o755 if path.is_dir() or path.suffix == '.sh' else 0o644)
        secure_data(root, account.pw_uid, account.pw_gid)
        remove_child(root, root / '.venv')
        install_runtime(root, source, user, home)
        (root / 'deployment.json').write_text(json.dumps({'service_name': LABEL, 'port': PORT,
            'user': user, 'version': VERSION}), encoding='utf-8')
        (root / 'deployment.json').chmod(0o644)
        plist_path.write_bytes(plistlib.dumps(daemon_config(root, user, home))); plist_path.chmod(0o644)
        run(['/usr/sbin/chown', 'root:wheel', plist_path])
        run(['/bin/launchctl', 'bootstrap', 'system', plist_path])
        wait_ready()
        install_application(root, application)
    except Exception:
        if stopped or changed:
            stop_daemon()
            if existing:
                restore_install(root, backup); secure_data(root, account.pw_uid, account.pw_gid)
                if previous_plist:
                    plist_path.write_bytes(previous_plist); plist_path.chmod(0o644)
                    run(['/bin/launchctl', 'bootstrap', 'system', plist_path])
            elif plist_path.exists():
                plist_path.unlink()
        raise
    print('部署完成。后台将开机运行，关闭助手窗口后仍会监控邮件。')


if __name__ == '__main__':
    desktop_mode = len(sys.argv) > 1 and sys.argv[1] in ('desktop-launch', 'desktop-control')
    try:
        os.umask(0o077)
        if desktop_mode and sys.platform == 'darwin' and os.geteuid() != 0:
            if len(sys.argv) == 2 and sys.argv[1] == 'desktop-launch':
                launch_desktop()
            elif len(sys.argv) == 3 and sys.argv[1] == 'desktop-control':
                desktop_control(sys.argv[2])
            else:
                raise ValueError('助手控制参数无效，请从应用程序重新打开助手。')
        elif len(sys.argv) == 3 and sys.argv[1] == 'verify':
            verify_manifest(sys.argv[2]); print('交付包文件校验通过。')
        elif len(sys.argv) == 4 and sys.argv[1] == 'deploy' and sys.platform == 'darwin' and os.geteuid() == 0:
            with deployment_lock():
                deploy(sys.argv[2], sys.argv[3])
        else:
            raise ValueError('请双击交付包中的 部署邮件助手.command 进行安装。')
    except Exception as error:
        if desktop_mode and sys.platform == 'darwin':
            desktop_alert(str(error))
        print(('助手操作失败：' if desktop_mode else '部署失败：') + str(error), file=sys.stderr)
        raise SystemExit(1)
