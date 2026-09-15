#!/bin/sh
set -eu
umask 077
command -v python3 >/dev/null 2>&1 || { echo '需要 Python 3.10+；请安装后重新运行此命令。' >&2; exit 1; }
python3 - <<'PY'
import base64, fcntl, hashlib, importlib.util, io, json, os, pathlib, platform, secrets, shutil, ssl, subprocess, sys, tarfile, tempfile, urllib.request
if sys.version_info < (3, 10):
    raise SystemExit('需要 Python 3.10 或以上版本')
if importlib.util.find_spec('venv') is None or importlib.util.find_spec('ensurepip') is None:
    raise SystemExit('当前 Python 缺少 venv/ensurepip；Debian/Ubuntu 请先安装 python3-venv 后重新运行此命令。')
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
if config_path.exists():
    try:
        current = json.loads(config_path.read_text()).get('release') or '<release 目录>'
    except (OSError, ValueError):
        current = '<release 目录>'
    command = str(pathlib.Path(current) / '.venv/bin/python') + ' -m runtime_daemon.install_service'
    raise SystemExit('此用户环境已有 Runtime，为避免覆盖身份已停止。升级：' + command + ' --upgrade <发布包>；'
                     '重装：先执行 ' + command + ' --uninstall --purge，再重新生成链接安装。')
# Without config.json no release is in use: remove what interrupted attempts left behind.
for leftover in sorted(install.glob('release-*')) + sorted(install.glob('stage-*')):
    if leftover.is_dir() and not leftover.is_symlink():
        shutil.rmtree(leftover, ignore_errors=True)
ca_path = install / 'ca.pem'
ca_path.unlink(missing_ok=True)
ca_pem = cfg.pop('ca_pem', '') or ''
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
                         + str(exc.returncode) + '），已清理本次安装；修复后可直接重新运行安装命令。')
    raise
if code != 0:
    if config_path.exists():
        raise SystemExit('服务已注册但尚未确认上线，安装已保留；按上面的提示修复后重启服务即可，无需重新运行安装命令。')
    shutil.rmtree(destination, ignore_errors=True)
    ca_path.unlink(missing_ok=True)
    raise SystemExit('服务注册失败，已清理本次安装。修复上面的问题后可直接重新运行安装命令；'
                     '若提示链接无效或已过期，请在「运行时管理」重新生成链接。')
PY
