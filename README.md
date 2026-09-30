# Folder Link

开源 SSH / SFTP 文件夹同步工具，在本地电脑与远程服务器之间同步文件。支持 macOS、Windows 和 Linux。

## 功能

- 双向同步：手动同步一次，或持续同步新增、修改的文件。
- 冲突处理：两端内容冲突时，由用户选择保留的版本。
- 安全连接：SSH 加密传输、服务器指纹校验，密码不保存到磁盘。
- macOS 原生版：毛玻璃界面、多 SSH 标签、远程目录浏览与单向拉取。

macOS 使用 SwiftUI / AppKit 原生界面；Windows、Linux 使用独立可执行程序启动本地浏览器界面。底层共用 Python + Paramiko 同步引擎，远程服务器只需支持 SSH / SFTP。

> Windows / Linux 当前提供单连接双向同步。双向同步不联动删除，单端删除的文件会从另一端恢复。

## 下载

可执行程序见 [GitHub Releases](https://github.com/SodrSnne/folder-link/releases)。也可以按以下步骤从源码编译，无需连接真实服务器。

## 编译

先下载源码：

```bash
git clone https://github.com/SodrSnne/folder-link.git
cd folder-link
```

### macOS

需要 Xcode 工具链和 Python 3.10+。在 Apple Silicon 或 Intel Mac 上分别生成对应架构的 App，编译目标为 macOS 14+。

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-build.txt
./macos/build.sh
```

产物：`dist/Folder Link.app`。拖入“应用程序”即可使用。

### Windows

需要 Python 3.10+。在 PowerShell 中运行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-build.txt
.\.venv\Scripts\python.exe packaging/build_portable.py
```

产物：`release-assets/Folder-Link-windows-x64.zip`（x64 构建）。解压后双击 `FolderLink.exe`，自动打开本地操作页面。

### Linux

需要 Python 3.10+ 和对应的 `venv` 支持，在目标架构的 Linux 环境中编译：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-build.txt
.venv/bin/python packaging/build_portable.py
```

产物：`release-assets/Folder-Link-linux-x64.tar.gz`（x64 构建）。解压后运行 `./FolderLink`。Linux 运行环境的 glibc 版本需兼容构建环境。

构建产物内置 Python，使用者无需安装 Python。macOS 为本机签名、未公证；Windows 未做代码签名。发布构建不等同于完成运行测试。

## 许可证

[MIT License](LICENSE)
