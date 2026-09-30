开源 SSH / SFTP 文件夹同步工具，提供源码与各平台可执行程序。

| 平台 | 下载文件 |
| --- | --- |
| macOS Apple Silicon | `Folder-Link-macos-arm64.zip` |
| macOS Intel | `Folder-Link-macos-x64.zip` |
| Windows x64 | `Folder-Link-windows-x64.zip` |
| Linux x64 | `Folder-Link-linux-x64.tar.gz` |

macOS 为原生 App；Windows / Linux 为独立程序，启动后打开本地浏览器界面。无需安装 Python。多 SSH 标签、远程目录浏览与单独拉取目前在 macOS 原生 App 中提供。

编译方法见 README。macOS 使用本机签名、未公证；Windows 未做代码签名。此发布只完成构建，运行测试由使用者进行。Linux 包构建于 Ubuntu 22.04（glibc 2.35）。
