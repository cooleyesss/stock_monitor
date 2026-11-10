﻿# unified starter: monitor + sim trading (dedup + hidden background)
# 2026-11-05 重构进程识别: 本机 CIM 查询 CommandLine 为空, 旧正则匹配失效
# (导致 15:15 stop 杀不掉监控进程, 残留至次日会双进程重复推送)
# 新机制: Start-Process -PassThru 直接拿 PID 落盘 data\pids\*.pid(格式 PID|启动时间),
#         存活校验 = 进程存在 + 名字python + 启动时间一致(防PID复用误判)
# 另: 子进程统一 PYTHONIOENCODING=utf-8, 修复量化runner盘中gbk编码异常
$env:PYTHONIOENCODING = 'utf-8'
$root = 'D:\trae_projects\stock_monitor'
$pidDir = Join-Path $root 'data\pids'
New-Item -ItemType Directory -Force -Path $pidDir | Out-Null

# 残留进程清理(2026-11-05): 本工具/盘外卡死的旧监控进程无法被低权限会话杀死时,
# 把 PID|启动时间 写入 data\pids\stale_kill.txt, 由本脚本(计划任务高权限)在启动前清理
$staleFile = Join-Path $pidDir 'stale_kill.txt'
if (Test-Path $staleFile) {
    foreach ($line in (Get-Content $staleFile | Where-Object { $_.Trim() })) {
        $parts = $line.Trim() -split '\|'
        if ($parts.Count -lt 2) { continue }
        $proc = Get-Process -Id $parts[0] -ErrorAction SilentlyContinue
        if ($proc -and $proc.ProcessName -eq 'python') {
            $ok = $false
            try { $ok = ($proc.StartTime.ToString('yyyy-MM-dd HH:mm:ss') -eq $parts[1]) } catch {}
            if ($ok) {
                try { Stop-Process -Id $proc.Id -Force -ErrorAction Stop; Write-Host "Stale killed (PID: $($proc.Id))" -ForegroundColor Yellow } catch { Write-Host "Stale kill failed (PID: $($proc.Id)): $_" -ForegroundColor Red }
            }
        }
    }
    Remove-Item $staleFile -Force -ErrorAction SilentlyContinue
}

function Get-AliveProc([string]$pidFile) {
    if (-not (Test-Path $pidFile)) { return $null }
    $parts = (Get-Content $pidFile -Raw).Trim() -split '\|'
    if ($parts.Count -lt 2) { return $null }
    $proc = Get-Process -Id $parts[0] -ErrorAction SilentlyContinue
    if (-not $proc -or $proc.ProcessName -ne 'python') { return $null }
    try {
        if ($proc.StartTime.ToString('yyyy-MM-dd HH:mm:ss') -ne $parts[1]) { return $null }
    } catch { return $null }
    return $proc
}

$scriptPath = 'D:\trae_projects\stock_monitor\ttrade\main.py'
$logOut = 'D:\trae_projects\stock_monitor\monitor.log'
$logErr = 'D:\trae_projects\stock_monitor\monitor_err.log'
$monPidFile = Join-Path $pidDir 'ttrade_monitor.pid'
$mon = Get-AliveProc $monPidFile
if ($mon) {
    Write-Host "Already running (PID: $($mon.Id))" -ForegroundColor Yellow
} else {
    # 兜底: CommandLine 可读的环境里按旧正则清历史遗留(本机为空, 不会误杀)
    $legacy = Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -match 'stock_monitor\\(stock_monitor|sim_trade)\.py' }
    if ($legacy) {
        $legacy | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
        Write-Host "Legacy processes stopped" -ForegroundColor Yellow
    }
    $p = Start-Process python -ArgumentList '-u', $scriptPath -RedirectStandardOutput $logOut -RedirectStandardError $logErr -WindowStyle Hidden -PassThru
    Start-Sleep -Seconds 2
    if ($p -and -not $p.HasExited) {
        "$($p.Id)|$($p.StartTime.ToString('yyyy-MM-dd HH:mm:ss'))" | Out-File -FilePath $monPidFile -Encoding ascii
        Write-Host "Started OK (PID: $($p.Id))" -ForegroundColor Green
    } else {
        Write-Host "Start FAILED, check monitor_err.log" -ForegroundColor Red
    }
}

# 量化盘中双窗口runner(9:25拉起: 竞价确认+9:30~10:00/14:30~14:55择机执行, 15:00自行退出)
$qPath = 'D:\trae_projects\stock_monitor\quant_runner.py'
$qOut = 'D:\trae_projects\stock_monitor\data\quant\runner_out.log'
$qErr = 'D:\trae_projects\stock_monitor\data\quant\runner_err.log'
$qDir = 'D:\trae_projects\stock_monitor\data\quant'
New-Item -ItemType Directory -Force -Path $qDir | Out-Null
$qPidFile = Join-Path $pidDir 'quant_runner.pid'
$q = Get-AliveProc $qPidFile
if ($q) {
    Write-Host "Quant runner already running (PID: $($q.Id))" -ForegroundColor Yellow
} else {
    $pq = Start-Process python -ArgumentList '-u', $qPath -RedirectStandardOutput $qOut -RedirectStandardError $qErr -WindowStyle Hidden -WorkingDirectory 'D:\trae_projects\stock_monitor' -PassThru
    Start-Sleep -Seconds 2
    if ($pq -and -not $pq.HasExited) {
        "$($pq.Id)|$($pq.StartTime.ToString('yyyy-MM-dd HH:mm:ss'))" | Out-File -FilePath $qPidFile -Encoding ascii
        Write-Host "Quant runner started OK (PID: $($pq.Id))" -ForegroundColor Green
    } else {
        Write-Host "Quant runner start FAILED, check data\quant\runner_err.log" -ForegroundColor Red
    }
}
