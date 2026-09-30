# Folder Link for macOS

SwiftUI / AppKit 原生毛玻璃界面，支持多个 SSH 标签、远程目录浏览、双向同步与远程拉取。

在仓库根目录编译：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-build.txt
./macos/build.sh
```

产物为 `dist/Folder Link.app`，架构与编译用的 Mac 相同。需要 Xcode 工具链和 Python 3.10+，编译目标 macOS 14+。

其他平台见[项目 README](../README.md)。
