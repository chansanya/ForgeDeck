# 启动本仓库默认 API 健康监测；停止时在 backend/logs/api-connectivity.stop 创建空文件。

$root = Split-Path -Parent $PSScriptRoot
$logPath = Join-Path $root 'backend\logs\api-connectivity-20260813.jsonl'
$stopPath = Join-Path $root 'backend\logs\api-connectivity.stop'
$pidPath = Join-Path $root 'backend\logs\api-connectivity.pid'

foreach ($path in @($stopPath, $pidPath)) {
    if (Test-Path -LiteralPath $path) {
        Remove-Item -LiteralPath $path -Force
    }
}

& $PSScriptRoot\monitor_api.ps1 `
    -Uri 'http://127.0.0.1:8000/health' `
    -IntervalSeconds 120 `
    -Deadline '2026-08-13T10:00:00+08:00' `
    -LogPath $logPath `
    -StopFile $stopPath `
    -PidFile $pidPath
