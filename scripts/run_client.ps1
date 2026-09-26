# CherryVoice 启动器（被「启动 CherryVoice.bat」调用，也可以直接右键用 PowerShell 运行）
#
# 为什么逻辑放在 .ps1 而不是 .bat：cmd 按系统 ANSI 代码页（中文 Windows 是 GBK）
# 解析 .bat，而文件是 UTF-8 —— 中文注释会把整行读断，命令直接失效（实测踩到）。
# PowerShell 脚本用 UTF-8 没问题，所以 .bat 只留一行 ASCII 调用。
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot      # 仓库根目录
$pythonw = Join-Path $root '.venv\Scripts\pythonw.exe'

function Get-AppProcesses {
    # 注意：本环境下同一个 pythonw 进程有时会被 WMI 重复列出（对照实验：
    # 一个只 sleep 的脚本也显示成 2 个），所以**不能靠进程个数判断是否已在运行**，
    # 要看日志里最后一条启动记录。
    Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like '*client.app*' }
}

if (-not (Test-Path $pythonw)) {
    Write-Host '[错误] 找不到 .venv\Scripts\pythonw.exe' -ForegroundColor Red
    Write-Host '       请先在仓库根目录执行：'
    Write-Host '         python -m venv .venv'
    Write-Host '         .venv\Scripts\python.exe -m pip install numpy sounddevice websockets'
    Read-Host '按回车退出'
    exit 1
}

$cfgDir = Join-Path $env:APPDATA 'CherryVoice'
$log = Join-Path $cfgDir 'logs\client.log'

# 判重：日志最后一条「热键就绪」之后，进程是否还有心跳（配置里没有心跳，
# 所以退而求其次看进程是否存在 + 日志新鲜度）
if (Get-AppProcesses) {
    $fresh = $false
    if (Test-Path $log) {
        $age = (New-TimeSpan -Start (Get-Item $log).LastWriteTime -End (Get-Date)).TotalMinutes
        $fresh = $age -lt 240
    }
    Write-Host 'CherryVoice 看起来已经在运行了。' -ForegroundColor Yellow
    Write-Host "  如果按 Ctrl+Win 没反应，先双击「停止 CherryVoice.bat」再启动一次。"
    Read-Host '按回车退出'
    exit 0
}

Start-Process -FilePath $pythonw -ArgumentList '-m', 'client.app' -WorkingDirectory $root
Start-Sleep -Seconds 5

Write-Host ''
Write-Host 'CherryVoice 已启动。' -ForegroundColor Green
Write-Host '  用法：在任意输入框里，按住 Ctrl + Win 说话，说完松开两个键。'
Write-Host '  （Win 键 = 键盘左下角那个 Windows 徽标键，在 Ctrl 和 Alt 中间）'
Write-Host ''
Write-Host "  日志：$log"
Write-Host '  停止：双击「停止 CherryVoice.bat」'
Write-Host ''
Write-Host '  如果什么都没发生，把日志最后几行发给我，我来看是哪一步卡住。'
Read-Host '按回车关闭本窗口'
