# Windows 桌面工作台：首批 GitHub 改造与实机验收

本改造只处理 Windows 客户端界面承载方式，**没有修改生成与播放任务、GPU 调度、音频缓存、Runtime 服务协议**。

## 已改
- `windows_launcher.py` 不再自动调用系统浏览器：Runtime 就绪后启动桌面工作台。
- `desktop_shell.py` 使用 pywebview 的 Edge Chromium（WebView2）窗口承载本地 Text Studio。
- 关闭桌面窗口隐藏到系统托盘，不向 Runtime 发送停止请求。
- 托盘提供「打开工作台」和「退出程序（停止后台服务）→确认退出并停止后台」。
- 明确退出时先要求 Runtime 停止；如果停止失败，保留托盘和窗口实例，不假装退出成功。
- 打包脚本加入桌面依赖，PyInstaller 增加 WebView 与托盘后端引用。
- `tests/test_windows_desktop_shell.py` 包含不需要 Windows GUI 的静态回归约束。

## Windows 实机必须验收
1. 在 Windows 执行 `build/windows/build.ps1`，检查 pywebview、pystray、pythonnet、WebView2 Edge Runtime 和 PyInstaller 依赖是否正确打包。
2. 双击 AI-Live-Studio.exe，确认业务界面直接在桌面窗口展示，不打开 Chrome/Edge 浏览器。
3. 点击窗口 X，确认仅隐藏至托盘；音频生成和直播播放任务仍继续。
4. 托盘点击「打开工作台」，确认窗口恢复且运行任务状态未丢失。
5. 托盘选择退出 → 确认退出，确认 Runtime 关闭、窗口与托盘消失；当 Runtime 停止失败时不应强行报告退出成功。
6. 在正在直播、空闲和模型启动中三种状态各重复以上流程。
7. 检查运行中再次双击程序是否启动重复 UI 实例。**目前未实现严格 UI 单实例与前台唤醒：必须单独完成后才可判定整体客户端目标通过。**
8. 检查 Windows 10 无 WebView2 Runtime、权限受限、打包安装与升级路径。缺少运行依赖时应明确报告，而不是悄然退回外部浏览器。

## 尚未完成
- 用户确认的「音频生成」与「直播播放」功能拆分：当前仅改桌面载体，不改变业务状态或控制按钮。
- Runtime Host 单宿主进程、Job Object、GPU 调度、音频队列替换。
- Windows 自动化构建及真实 GUI、WinMM、Vulkan 测试。

## 回滚
桌面壳以独立模块接入，回滚相关提交即可恢复旧启动器。请先完成 Windows 实机验收，再开展音频生成与播放拆分。

## 2026-10-19 审阅修复验证

- 关闭回调返回 `False` 取消关闭，确认退出成功后返回 `True`。已核对 pywebview 6.2.1 wheel 的 `webview/event.py` 与 Windows `winforms.py`：回调返回 False 才设置取消关闭。
- 页面心跳不再触发 Runtime 停止；旧 guardian 入口直接退出，新启动只更新会话标记以使旧 guardian 失效。隐藏、页面崩溃或无页面均保留后台，必须显式退出。
- Python 3.11 的桌面行为测试覆盖隐藏、托盘恢复、停止失败保留窗口及成功退出；库存/回放测试桩改为带时长的 ProcessingResult。
- Studio JSON POST 统一检查 Content-Type 和 Origin（存在时只允许同端口本机 HTTP 来源），拒绝 cross-site 请求；无 Origin 的本机脚本调用仍兼容。真实 HTTP 回归覆盖生成、播放、回放和库存删除。
- Windows GUI 实机验收仍待执行：直播或回放中关闭窗口，等待至少 120 秒，确认任务持续运行，再从托盘恢复并确认退出。
