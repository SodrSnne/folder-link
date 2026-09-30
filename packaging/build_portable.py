"""Build a standalone launcher for the local browser interface on the host OS."""
import platform
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
platform_name = {'Windows': 'windows', 'Linux': 'linux'}[platform.system()]
architecture = {'AMD64': 'x64', 'x86_64': 'x64', 'aarch64': 'arm64', 'arm64': 'arm64'}[platform.machine()]
name = f'Folder-Link-{platform_name}-{architecture}'
staging = ROOT / 'build' / 'portable' / name
staging.mkdir(parents=True, exist_ok=True)
subprocess.run([
    sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean', '--onefile',
    '--name', 'FolderLink', '--console',
    '--add-data', f'{ROOT / "templates"}:templates',
    '--add-data', f'{ROOT / "static"}:static',
    '--distpath', str(ROOT / 'build' / 'portable-bin'),
    '--workpath', str(ROOT / 'build' / 'portable-work'),
    '--specpath', str(ROOT / 'build'), str(ROOT / 'app.py'),
], check=True, cwd=ROOT)
executable = 'FolderLink.exe' if platform_name == 'windows' else 'FolderLink'
shutil.copy2(ROOT / 'build' / 'portable-bin' / executable, staging / executable)
shutil.copy2(ROOT / 'LICENSE', staging / 'LICENSE')
(staging / '使用说明.txt').write_text(
    'Folder Link\n\n'
    'Windows：解压后双击 FolderLink.exe。Linux：运行 ./FolderLink。\n'
    '程序会启动本机服务并打开默认浏览器，无需安装 Python。\n'
    '默认地址：http://127.0.0.1:8765；保留终端窗口以维持同步。\n'
    '结束前请在页面停止同步，再在终端按 Ctrl+C 退出。\n'
    '端口占用时使用 --port 8766；无桌面环境时使用 --no-browser。\n\n'
    '此版本提供单连接双向同步和冲突处理。多 SSH 标签、远程目录浏览\n'
    '和独立拉取按钮目前在 macOS 原生 App 中提供。\n'
    '双向同步不联动删除：单端删除会从另一端恢复。\n\n'
    '文档与源码：https://github.com/SodrSnne/folder-link\n', encoding='utf-8')
output = ROOT / 'release-assets'
output.mkdir(exist_ok=True)
archive_format = 'zip' if platform_name == 'windows' else 'gztar'
archive = shutil.make_archive(str(output / name), archive_format, staging.parent, staging.name)
print(archive)
