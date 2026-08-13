# Light DevOps API 连通性监测器：按固定间隔探测健康端点并记录可审计 JSONL。

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$Uri,

    [Parameter(Mandatory = $true)]
    [ValidateRange(1, 86400)]
    [int]$IntervalSeconds,

    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$Deadline,

    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$LogPath,

    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$StopFile,

    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$PidFile
)

$ErrorActionPreference = "Stop"

function Write-MonitorRecord {
    <# 将单次探测或监测生命周期事件追加为一行 JSON，便于后续统计。 #>
    param(
        [Parameter(Mandatory = $true)]
        [hashtable]$Record,

        [Parameter(Mandatory = $true)]
        [System.IO.StreamWriter]$Writer
    )

    $Record["timestamp"] = [DateTimeOffset]::Now.ToString("o")
    $Writer.WriteLine(($Record | ConvertTo-Json -Compress -Depth 8))
}

function Test-HealthResponse {
    <# 判断响应是否同时满足 HTTP 2xx 和健康载荷 status=ok。 #>
    param(
        [Parameter(Mandatory = $true)]
        [int]$StatusCode,

        [Parameter(Mandatory = $true)]
        [AllowEmptyString()]
        [string]$Body
    )

    if ($StatusCode -lt 200 -or $StatusCode -ge 300) {
        return $false
    }

    try {
        $payload = $Body | ConvertFrom-Json
        return [string]$payload.status -eq "ok"
    } catch {
        return $false
    }
}

function Get-ShortError {
    <# 截断异常文本，避免单条日志异常膨胀。 #>
    param(
        [Parameter(Mandatory = $true)]
        [System.Management.Automation.ErrorRecord]$ErrorRecord
    )

    $message = "{0}: {1}" -f $ErrorRecord.Exception.GetType().Name, $ErrorRecord.Exception.Message
    if ($message.Length -gt 500) {
        return $message.Substring(0, 500)
    }
    return $message
}

$resolvedLogPath = [System.IO.Path]::GetFullPath($LogPath)
$resolvedStopFile = [System.IO.Path]::GetFullPath($StopFile)
$resolvedPidFile = [System.IO.Path]::GetFullPath($PidFile)
$logDirectory = [System.IO.Path]::GetDirectoryName($resolvedLogPath)
$stopDirectory = [System.IO.Path]::GetDirectoryName($resolvedStopFile)
$pidDirectory = [System.IO.Path]::GetDirectoryName($resolvedPidFile)

foreach ($directory in @($logDirectory, $stopDirectory, $pidDirectory) | Sort-Object -Unique) {
    if (-not [string]::IsNullOrEmpty($directory)) {
        New-Item -ItemType Directory -Path $directory -Force | Out-Null
    }
}

$deadlineAt = [DateTimeOffset]::Parse($Deadline)
$writer = [System.IO.StreamWriter]::new(
    $resolvedLogPath,
    $true,
    [System.Text.UTF8Encoding]::new($false)
)
$writer.AutoFlush = $true

try {
    # PID 文件只用于人工中断定位，不承担任务状态；监测结果始终以 JSONL 为准。
    [System.IO.File]::WriteAllText($resolvedPidFile, [string]$PID, [System.Text.UTF8Encoding]::new($false))
    $total = 0
    $successes = 0
    $failures = 0
    $consecutiveFailures = 0
    $maxConsecutiveFailures = 0

    Write-MonitorRecord -Writer $writer -Record @{
        event = "started"
        uri = $Uri
        interval_seconds = $IntervalSeconds
        deadline = $deadlineAt.ToString("o")
        pid = $PID
    }

    while ([DateTimeOffset]::Now -lt $deadlineAt) {
        if (Test-Path -LiteralPath $resolvedStopFile) {
            Write-MonitorRecord -Writer $writer -Record @{
                event = "stopped"
                reason = "manual_stop"
                probes = $total
                successes = $successes
                failures = $failures
            }
            break
        }

        $total++
        $stopwatch = [System.Diagnostics.Stopwatch]::StartNew()
        $ok = $false
        $statusCode = $null
        $detail = $null

        try {
            $response = Invoke-WebRequest -Uri $Uri -Method Get -TimeoutSec 10 -UseBasicParsing
            $statusCode = [int]$response.StatusCode
            $body = [string]$response.Content
            $ok = Test-HealthResponse -StatusCode $statusCode -Body $body
            if ($ok) {
                $successes++
                $consecutiveFailures = 0
                $detail = "healthy"
            } else {
                $failures++
                $consecutiveFailures++
                $detail = "unexpected_health_response"
            }
        } catch {
            $failures++
            $consecutiveFailures++
            $detail = Get-ShortError -ErrorRecord $_
        } finally {
            $stopwatch.Stop()
        }

        if ($consecutiveFailures -gt $maxConsecutiveFailures) {
            $maxConsecutiveFailures = $consecutiveFailures
        }

        Write-MonitorRecord -Writer $writer -Record @{
            event = "probe"
            probe = $total
            result = if ($ok) { "ok" } else { "fail" }
            status_code = $statusCode
            duration_ms = [Math]::Round($stopwatch.Elapsed.TotalMilliseconds, 2)
            successes = $successes
            failures = $failures
            availability_percent = [Math]::Round(($successes / $total) * 100, 2)
            consecutive_failures = $consecutiveFailures
            max_consecutive_failures = $maxConsecutiveFailures
            detail = $detail
        }

        # 用 1 秒粒度轮询停止标记，正式探测仍严格保持 IntervalSeconds 周期。
        $sleepUntil = [DateTimeOffset]::Now.AddSeconds($IntervalSeconds)
        while ([DateTimeOffset]::Now -lt $sleepUntil -and [DateTimeOffset]::Now -lt $deadlineAt) {
            if (Test-Path -LiteralPath $resolvedStopFile) {
                break
            }
            Start-Sleep -Milliseconds 1000
        }
    }

    if ([DateTimeOffset]::Now -ge $deadlineAt) {
        Write-MonitorRecord -Writer $writer -Record @{
            event = "finished"
            reason = "deadline"
            probes = $total
            successes = $successes
            failures = $failures
            availability_percent = if ($total -gt 0) { [Math]::Round(($successes / $total) * 100, 2) } else { 0 }
            max_consecutive_failures = $maxConsecutiveFailures
        }
    }
} finally {
    $writer.Dispose()
    if (Test-Path -LiteralPath $resolvedPidFile) {
        Remove-Item -LiteralPath $resolvedPidFile -Force -ErrorAction SilentlyContinue
    }
}
