# 一键打包：生成 成品\幕言.exe（单个 exe，PySide6 全打进去，免安装）
#
# 用法：
#   .\build.ps1                          # 用 PATH 里的 python
#   .\build.ps1 -Python F:\devenv\Python313\python.exe
#
# 注意：为了体积，排除了 faster-whisper / ctranslate2 / av 这些离线识别依赖，
# 成品里没有「本地 faster-whisper」模式，联网模式（云知声、百炼、硅基流动）都正常。

param(
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = Split-Path -Parent $here
$prod = Join-Path $root "成品"
$tmp  = Join-Path $root "build_tmp"

& $Python -c "import PyInstaller" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "没装 PyInstaller，先装一下："
    & $Python -m pip install pyinstaller
}

$env:PYTHONIOENCODING = "utf-8"
$icon = Join-Path $here "assets\幕言.ico"
$iconArg = @()
if (Test-Path -LiteralPath $icon) { $iconArg = @("--icon", $icon) }

& $Python -m PyInstaller --noconfirm --clean --windowed --onefile --name "幕言" `
    --distpath $prod --workpath $tmp --specpath $tmp `
    --collect-all soundcard --collect-submodules muyan `
    --exclude-module faster_whisper --exclude-module ctranslate2 --exclude-module av `
    --exclude-module tokenizers --exclude-module opencc --exclude-module torch --exclude-module tkinter `
    --exclude-module PIL `
    @iconArg (Join-Path $here "muyan_launcher.py")

if ($LASTEXITCODE -ne 0) { throw "打包失败" }

Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
# 旧的「目录版」成品（幕言\ 文件夹 + 启动脚本）清掉，只留单文件 exe
Remove-Item -LiteralPath (Join-Path $prod "幕言") -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath (Join-Path $prod "启动幕言.bat") -Force -ErrorAction SilentlyContinue
$exe = Join-Path $prod "幕言.exe"
$size = "{0:N1} MB" -f ((Get-Item -LiteralPath $exe).Length / 1MB)
Write-Host "打包完成：$exe（$size）"
