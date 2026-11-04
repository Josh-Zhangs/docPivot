# ============================================================
#  docPivot 开发环境一键启动
#  用法：
#    .\dev.ps1            启动（若端口已被占用则提示后退出）
#    .\dev.ps1 -Restart   先清掉占用 8000/5173 的旧进程，再启动
#    .\dev.ps1 -Chrome    服务就绪后，用 Chrome 应用窗口打开（无地址栏，适合看效果）
# ============================================================

param(
    [switch]$Restart,
    [switch]$Chrome
)

$ErrorActionPreference = 'Stop'
$root     = Split-Path -Parent $MyInvocation.MyCommand.Path
$backend  = Join-Path $root 'backend'
$frontend = Join-Path $root 'frontend'

# uvicorn 启动前确认虚拟环境存在，避免报一堆看不懂的 ModuleNotFoundError
$venvPython = Join-Path $backend '.venv\Scripts\python.exe'
if (-not (Test-Path $venvPython)) {
    Write-Host "[X] 找不到后端虚拟环境: $venvPython" -ForegroundColor Red
    Write-Host "    请先在 backend 目录下创建 .venv 并安装依赖。" -ForegroundColor Yellow
    exit 1
}

# 返回占用指定端口的进程 PID 列表；没有占用则返回空
function Get-PortOwners([int]$port) {
    $conns = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    if ($null -eq $conns) { return @() }
    return @($conns | Select-Object -ExpandProperty OwningProcess -Unique)
}

# 端口被占时：默认提示退出，-Restart 则强制清理
function Resolve-Port([int]$port, [string]$label) {
    $owners = Get-PortOwners $port
    if ($owners.Count -eq 0) { return }

    $pids = $owners -join ','
    if (-not $Restart) {
        Write-Host "[!] 端口 $port ($label) 已被 PID $pids 占用。" -ForegroundColor Yellow
        Write-Host "    若要强制重启，请执行: .\dev.ps1 -Restart" -ForegroundColor Yellow
        exit 1
    }

    Write-Host "[*] 清理端口 $port 上的旧进程 (PID $pids) ..." -ForegroundColor Cyan
    foreach ($p in $owners) {
        try {
            Stop-Process -Id $p -Force -ErrorAction Stop
        } catch {
            Write-Host "    无法结束 PID $p : $($_.Exception.Message)" -ForegroundColor Red
        }
    }
    # 给 TCP 一点时间释放，否则紧接着 bind 会失败
    Start-Sleep -Milliseconds 800
}

Resolve-Port 8000 'backend'
Resolve-Port 5173 'frontend'

Write-Host "[*] 启动后端 FastAPI  -> http://127.0.0.1:8000" -ForegroundColor Cyan
# 注意：故意不加 --reload。在本机后台任务方式下 --reload 偶发静默卡死，
# 改代码后请用 -Restart 硬重启，比等 reload 更可控。
Start-Process -FilePath 'cmd.exe' `
    -ArgumentList '/k', '.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000' `
    -WorkingDirectory $backend

Write-Host "[*] 启动前端 Vite     -> http://127.0.0.1:5173" -ForegroundColor Cyan
Start-Process -FilePath 'cmd.exe' `
    -ArgumentList '/k', 'npm run dev' `
    -WorkingDirectory $frontend

# 等 Vite 起来再开浏览器，否则首屏是"无法访问"
Write-Host "[*] 等待服务就绪 ..." -ForegroundColor DarkGray
$ready = $false
for ($i = 0; $i -lt 20; $i++) {
    Start-Sleep -Milliseconds 500
    try {
        $r = Invoke-WebRequest -Uri 'http://127.0.0.1:5173/' -UseBasicParsing -TimeoutSec 2
        if ($r.StatusCode -eq 200) { $ready = $true; break }
    } catch {
        # 还没起来，继续等
    }
}

$url = 'http://127.0.0.1:5173/'
if ($ready) {
    Write-Host "[*] 页面已就绪，打开浏览器" -ForegroundColor Cyan
} else {
    Write-Host "[!] 等待超时，页面可能还没起来，先打开浏览器看看" -ForegroundColor Yellow
}

if ($Chrome) {
    # --app 模式：去掉地址栏/标签栏，看起来就是一个独立桌面程序，
    # 演示或截图时效果更干净。F5 依然可以刷新。
    # 注意：变量不能叫 $chrome，会和 -Chrome 开关生成的 $Chrome 撞名（PS 变量名不区分大小写）
    $chromePath = 'C:\Program Files\Google\Chrome\Application\chrome.exe'
    if (Test-Path $chromePath) {
        Start-Process -FilePath $chromePath -ArgumentList "--app=$url"
    } else {
        Write-Host "[!] 未找到 Chrome，改用默认浏览器" -ForegroundColor Yellow
        Start-Process $url
    }
} else {
    Start-Process $url
}

Write-Host ""
Write-Host "前端 http://127.0.0.1:5173   后端 http://127.0.0.1:8000/docs" -ForegroundColor Green
Write-Host "两个服务分别跑在独立窗口里，关闭对应窗口即停止该服务。" -ForegroundColor DarkGray
