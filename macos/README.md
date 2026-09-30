# Folder Link for macOS

Folder Link 的原生 macOS 客户端使用 SwiftUI / AppKit 毛玻璃窗口，内置 Python SSH/SFTP 引擎，可独立运行。

完整的功能、架构和构建安装说明见[项目 README](../README.md)。

## 常用操作

- **新建连接**：顶部 `+` 或 `⌘T` 新建 SSH 标签；每个标签独立填写账号、密码与目录。
- **选择远程目录**：点击“浏览远程”，通过主目录、上一级或路径输入框导航，再选择当前文件夹。隐藏项目可通过复选框显示。
- **拉取远程**：仅下载到本地，不上传、不删除。遇到不同版本时可选择保留本地或远程内容。
- **双向同步**：“同步一次”执行一轮，“开始自动同步”持续同步新增与修改。
- **管理任务**：切换标签不中断同步；停止或关闭标签只影响该任务。关闭窗口后继续运行，退出 App 则结束全部任务。

非密码配置会自动恢复，密码需要每次重新输入。双向同步不联动删除；单端删除的文件会从另一端恢复。

## 从源码构建

在仓库根目录执行：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
./macos/build.sh
```

输出 `dist/Folder Link.app`。当前脚本仅构建 Apple Silicon arm64，编译目标 macOS 14.0+；需要合适的 Xcode SDK，实际最低运行系统受 Python 和二进制依赖影响。App 使用本机 ad-hoc 签名，跨电脑分发需自行签名、公证并验证兼容性。
