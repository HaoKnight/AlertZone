# AlertZone Desktop

AlertZone Desktop 是 AlertZone Client 的局域网桌面前端。它直接显示 Client
提供的完整 Web 页面，并额外提供不依赖主窗口的后台报警小窗、弹窗位置与尺寸
记忆、系统默认提示音和自定义音频文件。

## 功能

- 首次启动输入 AlertZone Client 的局域网地址
- 嵌入并完整复用 AlertZone Web 页面
- 关闭主窗口后驻留系统托盘并持续检测报警
- 报警时只显示置顶小窗，不自动弹出主界面
- 可预览、移动、缩放并保存报警小窗的位置和大小
- 支持电脑默认提示音或自定义音频文件
- 可设置报警确认时间、自动关闭时间和连续重新布防
- 支持跟随 Web 页面、固定浅色或固定深色主题

## 运行

先启动项目根目录中的 AlertZone Client，并启用“局域网连接”。再打开终端：

```bash
cd "AlertZone Desktop"
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python start.py
```

Windows PowerShell：

```powershell
Set-Location "AlertZone Desktop"
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python start.py
```

首次进入时填写 Client 显示的地址，例如
`http://192.168.1.20:8765`。只填写 IP 时会自动使用端口 `8765`。

## 后台报警

点击工具栏“后台运行”或直接关闭主窗口后，程序会驻留托盘。报警事件到达时只
显示已设置大小和位置的报警小窗。通过托盘菜单可以重新打开主界面、进入桌面
设置或彻底退出程序。

在“桌面设置”中点击“设置弹窗位置和大小”，拖动预览窗标题栏调整位置，拖动
窗口边缘调整大小，最后点击“确定位置和大小”。

自定义提示音是否支持 MP3、M4A 等格式取决于操作系统的 Qt 多媒体后端；WAV
兼容性最好。
