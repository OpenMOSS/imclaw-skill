# Bridge 检活脚本 (Windows PowerShell) — 计划任务兜底守护
#
# 用法: 通过 Windows 计划任务（Task Scheduler）每分钟运行
#   schtasks /create /tn "IMClaw Bridge Check" /tr "powershell -File check_bridge.ps1" /sc minute /mo 1
#
# 检查逻辑:
#   1. bridge.state 是否为 enabled（或不存在，默认 enabled）
#   2. bridge.pid 是否存在且进程存活
#   3. 如果应该运行但没运行，启动 bridge_wrapper.py

$SKILL_DIR = Split-Path -Parent $MyInvocation.MyCommand.Path
$STATE_FILE = Join-Path $SKILL_DIR "bridge.state"
$PID_FILE = Join-Path $SKILL_DIR "bridge.pid"
$LOG_FILE = Join-Path $SKILL_DIR "bridge.log"
$WATCHDOG_LOG = Join-Path $SKILL_DIR "bridge_watchdog.log"
$VENV_PYTHON = Join-Path $SKILL_DIR "venv\Scripts\python.exe"

if (Test-Path $STATE_FILE) {
    $state = (Get-Content $STATE_FILE).Trim()
    if ($state -eq "disabled") { exit 0 }
}

if (Test-Path $PID_FILE) {
    $pid = [int](Get-Content $PID_FILE).Trim()
    $proc = Get-Process -Id $pid -ErrorAction SilentlyContinue
    if ($proc) { exit 0 }
}

$wrapperProc = Get-Process -Name "python*" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like "*bridge_wrapper.py*" }
if ($wrapperProc) { exit 0 }

Add-Content $WATCHDOG_LOG "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') Bridge not running, auto-starting wrapper"
Set-Location $SKILL_DIR
Start-Process -NoNewWindow -FilePath $VENV_PYTHON -ArgumentList "bridge_wrapper.py" -RedirectStandardOutput $LOG_FILE
