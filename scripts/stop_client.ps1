# EtherealFlow 停止器（只结束本项目的进程，不动其它 python 程序）
$ErrorActionPreference = 'SilentlyContinue'
$procs = Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" |
    Where-Object { $_.CommandLine -like '*client.app*' }

if (-not $procs) {
    Write-Host 'EtherealFlow 没有在运行。'
} else {
    # 本环境下 WMI 可能把同一个进程报两次，按 PID 去重后再杀
    $pids = $procs | Select-Object -ExpandProperty ProcessId -Unique
    foreach ($id in $pids) {
        Write-Host "停止 PID $id"
        Stop-Process -Id $id -Force
    }
    Write-Host 'EtherealFlow 已停止。' -ForegroundColor Green
}
Read-Host '按回车关闭本窗口'
