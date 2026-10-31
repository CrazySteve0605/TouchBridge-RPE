# TouchBridge -> RPE 一键启动（Windows）
# =====================================================
# 双击 run.bat 即可（它会调用本脚本）。它会按顺序自动完成：
#   1) 找到 Python 3.10+（py -3 / python / python3 依次尝试）
#   2) 创建虚拟环境并安装依赖（首次稍慢）
#   3) 清理上一次遗留的 app.py 后端进程（避免端口冲突）
#   4) 后台启动服务 + 启动触控板采集器，并打开浏览器制谱网页
#
# 要求：Windows 10/11 + 精确式触控板(Precision Touchpad) + Python 3.10+
# 没有 Python：到 https://www.python.org/downloads/ 装最新版（勾选 Add to PATH）
# =====================================================

$Root = $PSScriptRoot
Set-Location -LiteralPath $Root
$env:PYTHONUTF8 = "1"

function Test-PyVersion([string]$exePath) {
    # 返回 "3.x" 或 $null；微软商店的 python stub 会返回非零退出码，一并排除
    try {
        $out = & $exePath -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
    } catch { return $null }
    if ($LASTEXITCODE -ne 0) { return $null }
    if ($out -match "^3\.(\d+)") {
        $minor = [int]$Matches[1]
        if ($minor -ge 10) { return "3.$minor" }
    }
    return $null
}

# ---- 1) 找 Python 3.10+ ----
$py = $null
foreach ($name in @("py", "python", "python3")) {
    $exe = Get-Command $name -ErrorAction SilentlyContinue
    if (-not $exe) { continue }
    $v = Test-PyVersion $exe.Source
    if ($v) { $py = $exe.Source; break }
}
if (-not $py) {
    Write-Host "[错误] 未找到 Python 3.10+。" -ForegroundColor Red
    Write-Host "       请到 https://www.python.org/downloads/ 安装最新 Python（安装时勾选 Add to PATH），"
    Write-Host "       装好后再双击一次 run.bat。"
    Read-Host "按回车键关闭"
    exit 1
}
Write-Host "[1/5] 使用 Python：$py"

# ---- 2) 虚拟环境 + 依赖 ----
$VenvPy = Join-Path $Root "backend\venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $VenvPy)) {
    Write-Host "[2/5] 首次运行：创建虚拟环境…"
    & $py -m venv (Join-Path $Root "backend\venv") 2>$null
}
if (-not (Test-Path -LiteralPath $VenvPy)) {
    Write-Host "[错误] 虚拟环境创建失败，请把上面的报错截图发到 GitHub Issues。" -ForegroundColor Red
    Read-Host "按回车键关闭"
    exit 1
}
& $VenvPy -m pip install -q -r (Join-Path $Root "requirements.txt") --disable-pip-version-check 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "[错误] 依赖安装失败，请检查网络后重试。" -ForegroundColor Red
    Read-Host "按回车键关闭"
    exit 1
}
Write-Host "[2/5] 依赖就绪"

# ---- 3) 清理上一次遗留的后端进程（只杀我们自己的 app.py，不动别的程序） ----
Write-Host "[3/5] 清理旧实例…"
Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -match "[\\/]app\.py" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Milliseconds 500

# ---- 4) 启动后端（最小化窗口；日志也在窗口里，还原即可看） ----
Write-Host "[4/5] 启动后端 + 触控板采集器…"
Start-Process -FilePath $VenvPy -ArgumentList '"app.py"' -WorkingDirectory (Join-Path $Root "backend") -WindowStyle Minimized

# 触控板采集器（独立窗口；纯标准库，系统 Python 即可）
Start-Process -FilePath $py -ArgumentList ('"{0}"' -f (Join-Path $Root "TouchBridge\collector_win.py")) -WorkingDirectory $Root

# ---- 5) 等端口就绪再开浏览器 ----
Write-Host "[5/5] 等待服务就绪…"
$ok = $false
for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Milliseconds 500
    try {
        $null = Invoke-WebRequest -Uri "http://127.0.0.1:8765/" -UseBasicParsing -TimeoutSec 1
        $ok = $true; break
    } catch { }
}
if ($ok) {
    Start-Process "http://127.0.0.1:8765/"
    Write-Host ""
    Write-Host "✔ 已打开制谱网页 http://127.0.0.1:8765/" -ForegroundColor Green
    Write-Host "  - TouchBridge 采集器窗口应显示「正在发送 → 127.0.0.1:9000」"
    Write-Host "  - 没有精确式触控板时，采集器会退化为「鼠标模拟」（窗口的触控板图内按住拖动＝单指）"
    Write-Host "  - 停止服务：关掉任务栏里那两个 Python 窗口即可"
    Start-Sleep -Seconds 4
} else {
    Write-Host "[警告] 服务启动超时。请还原任务栏里的 Python 窗口查看报错，" -ForegroundColor Yellow
    Write-Host "       截图发到 GitHub Issues。"
    Read-Host "按回车键关闭"
}