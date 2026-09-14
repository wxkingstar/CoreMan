#!/bin/sh
set -eu
umask 077
command -v python3 >/dev/null 2>&1 || { echo '需要 Python 3.10+；请安装后重新运行此命令。' >&2; exit 1; }
python3 - <<'PY'
import base64, hashlib, io, json, os, pathlib, platform, shutil, subprocess, sys, tarfile, tempfile, urllib.request
if sys.version_info < (3, 10):
    raise SystemExit('需要 Python 3.10 或以上版本')
cfg = json.loads(base64.b64decode('__COREMAN_CONFIG__'))
system = platform.system().lower()
arch = {'x86_64': 'amd64', 'aarch64': 'arm64', 'arm64': 'arm64'}.get(platform.machine())
if system not in ('linux', 'darwin') or not arch:
    raise SystemExit('目前支持 Linux/macOS 的 amd64 与 arm64')
root = pathlib.Path(cfg['workspace_root'])
if not root.is_absolute() or str(root) == '/' or '..' in root.parts:
    raise SystemExit('项目主目录无效')
root.mkdir(parents=True, exist_ok=True)
if root.resolve() != root:
    raise SystemExit('项目主目录不能是符号链接；请填写实际路径')
with tempfile.TemporaryFile(dir=root): pass
home = pathlib.Path.home()
print('运行用户 UID:', os.getuid(), 'HOME:', home, '\n项目主目录:', root, flush=True)
install = home / '.local/share/coreman-runtime'
if install.is_symlink(): raise SystemExit('安装目录不能是符号链接')
install.mkdir(parents=True, exist_ok=True, mode=0o700)
os.chmod(install, 0o700)
config_path = install / 'config.json'
if config_path.exists():
    raise SystemExit('此用户环境已有 Runtime。请使用现有运行时服务，避免覆盖身份。')
url = cfg['api_url'].rstrip('/') + '/api/runtime/install/' + cfg['install_token'] + '/' + system + '/' + arch + '/bundle'
try:
    proxy = cfg.get("control_proxy")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy, "https": proxy} if proxy else {}))
    with opener.open(url, timeout=120) as response:
        checksum = response.headers.get('X-SHA256', '')
        bundle = response.read(150 * 1024 * 1024 + 1)
except Exception as exc:
    raise SystemExit('安装包下载失败（' + type(exc).__name__ + '）；请检查平台地址、链接有效期与发布包。')
if len(bundle) > 150 * 1024 * 1024 or hashlib.sha256(bundle).hexdigest() != checksum:
    raise SystemExit('安装包大小或校验值无效')
with tempfile.TemporaryDirectory(prefix='stage-', dir=install) as stage:
    release = pathlib.Path(stage) / 'release'
    release.mkdir()
    with tarfile.open(fileobj=io.BytesIO(bundle), mode='r:gz') as archive:
        members = archive.getmembers()
        if sum(m.size for m in members) > 400 * 1024 * 1024:
            raise SystemExit('安装包解压大小超过上限')
        for member in members:
            target = release / member.name
            if not target.resolve().is_relative_to(release) or not (member.isfile() or member.isdir()):
                raise SystemExit('安装包包含无效文件')
        archive.extractall(release)
    # Create the virtual environment only at its permanent absolute location.
    # A failed pre-enrollment attempt can be retried without overwriting an identity.
    destination = pathlib.Path(tempfile.mkdtemp(prefix='release-' + checksum[:16] + '-', dir=install))
    shutil.copytree(release, destination, dirs_exist_ok=True)
    subprocess.run([sys.executable, '-m', 'venv', str(destination / '.venv')], check=True)
    python = destination / '.venv/bin/python'
    subprocess.run([str(python), '-m', 'pip', 'install', '--disable-pip-version-check', '--no-index',
                    '--find-links', str(destination / 'wheels'), '-r', str(destination / 'runtime_daemon/requirements.txt')], check=True)
    cfg['path'] = os.environ.get('PATH', '')
    cfg['release'] = str(destination)
    fd = os.open(config_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as f: json.dump(cfg, f)
    subprocess.run([str(python), '-m', 'runtime_daemon.install_service', '--config', str(config_path)], cwd=destination, check=True)
PY
