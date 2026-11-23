# 幕言

Windows 系统声音实时字幕。抓取扬声器正在播放的声音，识别后显示在置顶悬浮窗；不录麦克风。字幕会一直累积，可以往上翻、调字号、改窗口大小。

## 下载

[Releases](../../releases) 提供两个压缩包：

| 压缩包 | 内容 | 前提 |
|---|---|---|
| 幕言-Windows-免安装版.zip | 单个 exe，双击即用；云知声、百炼、硅基流动等联网识别可用 | 不用装 Python；不含本地离线模型 |
| 幕言-源码版.zip | 完整 Python 源码，含本地 faster-whisper 离线识别 | 电脑已装 Python 3.10+ |

两个版本首次启动都会要求设置：选服务商、填 API Key，部分服务商还需要填 Base URL。Key 明文保存在 `%APPDATA%\MuYan\config.json`，不要分享这个文件。

## 源码运行

```powershell
python -m pip install -r requirements.txt
python -m muyan
```

也可以双击 `启动.bat`。只支持 Windows，依赖 WASAPI 环回采集。

`build.ps1` 可把源码打包成单文件 exe（需要 PyInstaller）。功能细节、弹窗操作和排错见 [docs/详细文档.md](docs/详细文档.md)。

## 许可

MIT
