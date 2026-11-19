# unified stopper (量化盘中双窗口runner)
# $PSScriptRoot在某些调用方式下为空(2026-10-29实测), 兜底固定路径防Join-Path崩溃丢T侧日跑
# 2026-11-05: 进程识别优先按 data\pids\*.pid(start.ps1 -PassThru 落盘, PID|启动时间);
#   本机 CIM 查询 CommandLine 为空导致旧正则失效(15:15任务杀不掉监控), 正则保留作兼容兜底
# 2026-11-16: T侧监控进程改为"过夜存活"(stop不再杀它): 次日8:45自动醒跑新闻雷达
#   隔夜汇总+9:00 SVM日频重训(runner.seconds_to_next_window重排醒来时点);
#   仅量化runner照旧15:15退出(次日9:25由start.ps1重新拉起)。
$root = if ($PSScriptRoot) { $PSScriptRoot } else { 'D:\trae_projects\stock_monitor' }
$pidDir = Join-Path $root 'data\pids'
$killed = @()

foreach ($f in @('quant_runner.pid')) {
    $pf = Join-Path $pidDir $f
    if (-not (Test-Path $pf)) { continue }
    $parts = (Get-Content $pf -Raw).Trim() -split '\|'
    if ($parts.Count -lt 2) { continue }
    $proc = Get-Process -Id $parts[0] -ErrorAction SilentlyContinue
    if ($proc -and $proc.ProcessName -eq 'python') {
        $ok = $false
        try { $ok = ($proc.StartTime.ToString('yyyy-MM-dd HH:mm:ss') -eq $parts[1]) } catch {}
        if ($ok) {
            Stop-Process -Id $proc.Id -Force
            $killed += $proc.Id
        }
    }
    Remove-Item $pf -Force -ErrorAction SilentlyContinue
}

$existing = Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -match 'stock_monitor\\quant_runner\.py' }
if ($existing) {
    $existing | ForEach-Object {
        if ($killed -notcontains $_.ProcessId) { Stop-Process -Id $_.ProcessId -Force; $killed += $_.ProcessId }
    }
}
if ($killed) {
    Write-Host "Stopped (PID: $($killed -join ', '))" -ForegroundColor Green
} else {
    Write-Host "Not running" -ForegroundColor Yellow
}

$env:PYTHONIOENCODING = 'utf-8'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

# 收盘后日内T日跑(15:15, 幂等): 收盘战报推送 + dashboard_ttrade.html(含history归档)
#   + 双模次日信号; 与监控进程是否存活无关, 保证每天必有T侧日报
$tLogDir = Join-Path $root 'data\ttrade'
New-Item -ItemType Directory -Force -Path $tLogDir | Out-Null
Write-Host "Running ttrade daily (ttrade\main.py --eod)..." -ForegroundColor Cyan
$stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
$out = & python -u "$root\ttrade\main.py" --eod 2>&1 | Out-String
"`n===== ttrade_daily $stamp =====`n$out" |
    Out-File -FilePath (Join-Path $tLogDir 'ttrade_run.log') -Append -Encoding UTF8
Write-Host $out
if ($LASTEXITCODE -ne 0) {
    Write-Host "ttrade\main.py --eod 退出码 $LASTEXITCODE" -ForegroundColor Red
}

# 收盘后量化账户日跑: 推进子账户 + 记日志 + 推送复盘日报 + 生成 dashboard_quant.html
# 输出追加到 data\quant\quant_run.log(供无人值守的计划任务留痕), 编码统一UTF-8
$quantLogDir = Join-Path $root 'data\quant'
New-Item -ItemType Directory -Force -Path $quantLogDir | Out-Null
Write-Host "Running quant daily (quant_daily.py)..." -ForegroundColor Cyan
$stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
$out = & python -u "$root\quant_daily.py" 2>&1 | Out-String
"`n===== quant_daily $stamp =====`n$out" |
    Out-File -FilePath (Join-Path $quantLogDir 'quant_run.log') -Append -Encoding UTF8
Write-Host $out
if ($LASTEXITCODE -ne 0) {
    Write-Host "quant_daily.py 退出码 $LASTEXITCODE" -ForegroundColor Red
}
