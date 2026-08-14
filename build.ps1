# 一键构建脚本：安装依赖 -> 生成图标 -> 打包 exe
# 用法：powershell -ExecutionPolicy Bypass -File build.ps1
$ErrorActionPreference = "Stop"
$here = $PSScriptRoot
$py = Join-Path $here "python-tools\python.exe"

if (-not (Test-Path $py)) {
    Write-Host "未找到 $py，请先安装便携版 Python 到 python-tools 目录"
    exit 1
}

$mirror = "http://mirrors.huaweicloud.com/repository/pypi/simple"
$trusted = "mirrors.huaweicloud.com"

Write-Host "[1/3] 安装依赖..."
& $py -m pip install --no-cache-dir --disable-pip-version-check -q `
    --index-url $mirror --trusted-host $trusted `
    -r (Join-Path $here "requirements.txt")
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "[2/3] 生成图标..."
& $py (Join-Path $here "build_icon.py")
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "[3/3] PyInstaller 打包..."
& $py -m PyInstaller --noconfirm --clean --onefile --windowed `
    --name "在线时长助手" `
    --hidden-import pystray._win32 `
    --icon (Join-Path $here "icon.ico") `
    (Join-Path $here "main.py")
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host ""
Write-Host "打包完成：$here\dist\在线时长助手.exe"
