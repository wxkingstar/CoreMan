#!/bin/sh
set -eu
umask 077
command -v python3 >/dev/null 2>&1 || { echo '需要 Python 3.10+；请安装后重新运行此命令。' >&2; exit 1; }
python3 - "$@" <<'PY'
import base64, fcntl, hashlib, importlib.util, io, json, os, pathlib, platform, secrets, shlex, shutil, ssl, subprocess, sys, tarfile, tempfile, time, urllib.request
if sys.version_info < (3, 10):
    raise SystemExit('需要 Python 3.10 或以上版本')
if importlib.util.find_spec('venv') is None or importlib.util.find_spec('ensurepip') is None:
    raise SystemExit('当前 Python 缺少 venv/ensurepip；Debian/Ubuntu 请先安装 python3-venv 后重新运行此命令。')
# curl … | sh -s -- --replace
if any(arg != '--replace' for arg in sys.argv[1:]):
    raise SystemExit('不支持的参数：' + ' '.join(sys.argv[1:]) + '。唯一可用的参数是 --replace（替换本机已安装的 Runtime）')
replace = '--replace' in sys.argv[1:]
cfg = json.loads(base64.b64decode('__COREMAN_CONFIG__'))
system = platform.system().lower()
arch = {'x86_64': 'amd64', 'aarch64': 'arm64', 'arm64': 'arm64'}.get(platform.machine())
if system not in ('linux', 'darwin') or not arch:
    raise SystemExit('目前支持 Linux/macOS 的 amd64 与 arm64，不支持 Windows')
root = pathlib.Path(cfg['workspace_root'])
if not root.is_absolute() or str(root) == '/' or '..' in root.parts:
    raise SystemExit('项目主目录无效')
root.mkdir(parents=True, exist_ok=True)
if root.resolve() != root:
    raise SystemExit('项目主目录不能是符号链接；请填写实际路径')
with tempfile.TemporaryFile(dir=root): pass
# /home -> /data/home style links would otherwise fail every extracted-path check.
home = pathlib.Path(os.path.realpath(pathlib.Path.home()))
print('运行用户 UID:', os.getuid(), 'HOME:', home, '\n项目主目录:', root, flush=True)
install = home / '.local/share/coreman-runtime'
if install.is_symlink(): raise SystemExit('安装目录不能是符号链接')
install.mkdir(parents=True, exist_ok=True, mode=0o700)
os.chmod(install, 0o700)
lock = open(install / 'install.lock', 'a')
try:
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    raise SystemExit('另一个安装、升级或卸载操作正在进行，请稍后重试')
config_path = install / 'config.json'
ca_path = install / 'ca.pem'
ca_pem = cfg.pop('ca_pem', '') or ''
# The file name matches CA_FILE_HINT in the command the runtime management page shows.
rerun = ('curl ' + ('--cacert coreman-ca.pem ' if ca_pem else '') + '-fsSL '
         + shlex.quote(cfg['api_url'].rstrip('/') + '/api/runtime/install/' + cfg['install_token'] + '/install.sh')
         + ' | sh -s -- --replace')
existing = None
if config_path.exists():
    try:
        existing = json.loads(config_path.read_text())
    except (OSError, ValueError):
        pass
    existing = existing if isinstance(existing, dict) else {}


def installed_tool(current):
    """The installed runtime's own management command, and whether it accepts --register."""
    manage = install / 'bin/coreman-runtime'
    if manage.is_file():
        return shlex.quote(str(manage)), True
    # Installed before bin/coreman-runtime existed: runtime_daemon imports only from inside the release.
    release = pathlib.Path(str(current.get('release') or '/nonexistent'))
    if (release / 'runtime_daemon/install_service.py').is_file():
        return 'cd ' + shlex.quote(str(release)) + ' && .venv/bin/python -m runtime_daemon.install_service', False
    return None, False


if existing is not None:
    current_url = str(existing.get('api_url') or '未知平台')
    tool, modern = installed_tool(existing)
if existing is not None and not replace:
    node = str(existing.get('node_id') or '')[:8]
    lines = ['本机已安装 CoreMan Runtime，本次安装未做任何改动。',
             '  现有 Runtime：' + current_url + ('（节点 ' + node + '）' if node else ''),
             '  本次安装链接：' + cfg['api_url'], '']
    if existing.get('service_status') == 'uninstalled' and tool:
        lines += ['现有 Runtime 的服务已卸载，节点身份仍然保留。恢复原节点：', '    ' + tool + (' --register' if modern else ''), '']
    elif current_url.rstrip('/') == cfg['api_url'].rstrip('/'):
        lines += ['两者是同一平台，通常无需重装；服务异常时先查看 ' + str(install / 'runtime.log') + '。', '']
    lines += ['改用本次链接重新安装（现有 Runtime 会先停止，节点身份、会话与日志整体备份，不会删除）：', '    ' + rerun]
    if tool:
        lines += ['', '只升级版本、保留节点身份：',
                  '    ' + tool + ' --upgrade ' + ('<发布包路径>' if modern else '<发布包的绝对路径>')]
    raise SystemExit('\n'.join(lines))
backup = None
if existing is None:
    # Without config.json no release is in use: remove what interrupted attempts left behind.
    for leftover in sorted(install.glob('release-*')) + sorted(install.glob('stage-*')) + [install / 'bin']:
        if leftover.is_dir() and not leftover.is_symlink():
            shutil.rmtree(leftover, ignore_errors=True)
    ca_path.unlink(missing_ok=True)
else:
    stamp = time.strftime('%Y%m%d-%H%M%S')
    backup = install.with_name(install.name + '.bak-' + stamp)
    suffix = 1
    while backup.exists():
        backup = install.with_name(install.name + '.bak-' + stamp + '-' + str(suffix))
        suffix += 1
    restore = 'rm -rf ' + shlex.quote(str(install)) + ' && mv ' + shlex.quote(str(backup)) + ' ' + shlex.quote(str(install))
    if tool:
        restore += ' && ' + tool + (' --register' if modern else '')
    print('将替换现有 Runtime（' + current_url + '）。先下载并校验新安装包，这一步失败不会改动现有 Runtime。', flush=True)
# Same sources the daemon trusts: system roots, certifi when present and the private CA.
context = ssl.create_default_context()
try:
    import certifi
    context.load_verify_locations(cafile=certifi.where())
except Exception:
    pass
if ca_pem:
    try:
        context.load_verify_locations(cadata=ca_pem)
    except (ssl.SSLError, ValueError):
        raise SystemExit('私有 CA 证书无效：需要 PEM 格式的 CA 证书')
url = cfg['api_url'].rstrip('/') + '/api/runtime/install/' + cfg['install_token'] + '/' + system + '/' + arch + '/bundle'
try:
    proxy = cfg.get("control_proxy")
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy, "https": proxy} if proxy else {}),
        urllib.request.HTTPSHandler(context=context),
    )
    with opener.open(url, timeout=120) as response:
        checksum = response.headers.get('X-SHA256', '')
        bundle = response.read(150 * 1024 * 1024 + 1)
except Exception as exc:
    tls = isinstance(getattr(exc, 'reason', exc), ssl.SSLError)
    raise SystemExit('安装包下载失败（' + type(exc).__name__ + '）；请检查平台地址、链接有效期与发布包'
                     + ('；平台使用私有 CA 时请在安装链接中提供 CA 证书' if tls else '') + '。')
if len(bundle) > 150 * 1024 * 1024 or hashlib.sha256(bundle).hexdigest() != checksum:
    raise SystemExit('安装包大小或校验值无效')
destination = None
retired = False


def restore_note():
    if not retired:
        return ''
    return ('\n原 Runtime 已停止，节点身份、会话与日志完整保存在 ' + str(backup)
            + '。修复问题后重新运行安装命令即可；要恢复原 Runtime，执行：\n    ' + restore)


try:
    with tempfile.TemporaryDirectory(prefix='stage-', dir=install) as stage:
        release = pathlib.Path(stage) / 'release'
        release.mkdir()
        base = release.resolve()
        with tarfile.open(fileobj=io.BytesIO(bundle), mode='r:gz') as archive:
            members = archive.getmembers()
            if sum(m.size for m in members) > 400 * 1024 * 1024:
                raise SystemExit('安装包解压大小超过上限')
            for member in members:
                target = base / member.name
                if not target.resolve().is_relative_to(base) or not (member.isfile() or member.isdir()):
                    raise SystemExit('安装包包含无效文件')
            archive.extractall(base, **({'filter': 'data'} if hasattr(tarfile, 'data_filter') else {}))
        if backup is not None:
            # The new bundle's tool stops the service, so an old or broken release does not matter.
            print('正在停止现有 Runtime，并把原安装目录的内容移到 ' + str(backup) + ' …', flush=True)
            if subprocess.run([sys.executable, '-m', 'runtime_daemon.install_service', '--config', str(config_path),
                               '--retire', str(backup)], cwd=base).returncode != 0:
                raise SystemExit('未能停止现有 Runtime，本次安装已停止；按上面的提示处理后重新运行本命令。')
            retired = True
        # A venv embeds its absolute path, so it is built at the permanent location and
        # removed again below if any later step fails.
        destination = install / ('release-' + checksum[:16] + '-' + secrets.token_hex(4))
        destination.mkdir(mode=0o700)
        shutil.copytree(release, destination, dirs_exist_ok=True)
        subprocess.run([sys.executable, '-m', 'venv', str(destination / '.venv')], check=True)
        python = destination / '.venv/bin/python'
        subprocess.run([str(python), '-m', 'pip', 'install', '--disable-pip-version-check', '--no-index',
                        '--find-links', str(destination / 'wheels'), '-r', str(destination / 'runtime_daemon/requirements.txt')], check=True)
        if ca_pem:
            fd = os.open(ca_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, 'w') as f: f.write(ca_pem)
            cfg['ca_file'] = str(ca_path)
        cfg['path'] = os.environ.get('PATH', '')
        cfg['release'] = str(destination)
        staged = pathlib.Path(stage) / 'config.json'
        fd = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as f: json.dump(cfg, f)
        # install_service creates config.json only once the service manager accepted the
        # definition, and deletes it again if registration or enrollment fails.
        code = subprocess.run([str(python), '-m', 'runtime_daemon.install_service', '--config', str(config_path),
                               '--staged-config', str(staged)], cwd=destination).returncode
except BaseException as exc:
    if not config_path.exists():
        if destination is not None:
            shutil.rmtree(destination, ignore_errors=True)
        ca_path.unlink(missing_ok=True)
    if isinstance(exc, subprocess.CalledProcessError):
        raise SystemExit('安装准备失败（' + str(exc.cmd[2] if len(exc.cmd) > 2 else exc.cmd) + ' 退出码 '
                         + str(exc.returncode) + '），已清理本次安装；修复后可直接重新运行安装命令。' + restore_note())
    if retired:
        print(restore_note().strip(), file=sys.stderr, flush=True)
    raise
if code != 0:
    if config_path.exists():
        raise SystemExit('服务已注册但尚未确认上线，安装已保留；按上面的提示修复后重启服务即可，无需重新运行安装命令。'
                         + ('\n原 Runtime 的备份在 ' + str(backup) + '。' if retired else ''))
    shutil.rmtree(destination, ignore_errors=True)
    ca_path.unlink(missing_ok=True)
    raise SystemExit('服务注册失败，已清理本次安装。修复上面的问题后可直接重新运行安装命令；'
                     '若提示链接无效或已过期，请在「运行时管理」重新生成链接。' + restore_note())
if retired:
    print('已替换原 Runtime。原节点身份、会话与日志备份在 ' + str(backup) + '，确认新 Runtime 正常后可删除；'
          '原节点请在 ' + current_url + ' 的「运行时管理」中撤销。')
PY
