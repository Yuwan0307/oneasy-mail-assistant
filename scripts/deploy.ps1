param(
    [string]$InstallRoot = "$env:ProgramData\Oneasy\MailAssistant",
    [ValidatePattern('^OneasyMailAssistant[A-Za-z0-9_-]*$')][string]$ServiceName = 'OneasyMailAssistant',
    [ValidateRange(1024,65535)][int]$Port = 8765,
    [switch]$NoLaunch,
    [switch]$NoShortcuts
)
$ErrorActionPreference = 'Stop'
$SourceRoot = Split-Path $PSScriptRoot -Parent
. "$PSScriptRoot\desktop-control.ps1"
$InstallRoot = [IO.Path]::GetFullPath($InstallRoot).TrimEnd('\')
if ($InstallRoot.Contains('"') -or $SourceRoot.Contains('"')) { throw '安装路径不能包含双引号。' }
$resultFile = Join-Path $SourceRoot '部署结果.txt'
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    '' | Set-Content -LiteralPath $resultFile -Encoding UTF8
    Write-Host '正在部署，请允许 Windows 管理员授权，并等待完成……'
    $arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`" -InstallRoot `"$InstallRoot`" -ServiceName $ServiceName -Port $Port -NoLaunch"
    if ($NoShortcuts) { $arguments += ' -NoShortcuts' }
    try {
        $process = Start-Process powershell.exe -WindowStyle Hidden -Verb RunAs -ArgumentList $arguments -PassThru -Wait
        if ($process.ExitCode -ne 0) { throw ((Get-Content -LiteralPath $resultFile -Raw -Encoding UTF8) + ' 部署未成功，请查看安装目录中的 logs。') }
        if (-not $NoLaunch) { & "$InstallRoot\scripts\launch.ps1" }
        exit 0
    } catch { Write-Host $_.Exception.Message -ForegroundColor Red; exit 1 }
}
if (-not [Environment]::Is64BitOperatingSystem -or -not [Environment]::Is64BitProcess) { throw '请在 64 位 Windows 10/11 上运行部署脚本。' }
if ([Environment]::OSVersion.Version.Major -lt 10) { throw '需要 Windows 10 或更新系统。' }
if ($InstallRoot -eq $SourceRoot -or $SourceRoot.StartsWith($InstallRoot+'\',[StringComparison]::OrdinalIgnoreCase) -or $InstallRoot.StartsWith($SourceRoot+'\',[StringComparison]::OrdinalIgnoreCase) -or $InstallRoot.StartsWith($env:SystemRoot+'\',[StringComparison]::OrdinalIgnoreCase) -or $InstallRoot -eq [IO.Path]::GetPathRoot($InstallRoot) -or $InstallRoot -eq $env:SystemRoot -or $InstallRoot -eq $env:ProgramData -or $InstallRoot -eq $env:ProgramFiles) {
    throw '请使用独立的安装目录，不能覆盖交付包或系统目录。'
}
function Invoke-Checked([string]$Program, [string[]]$Arguments) {
    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) { throw "操作失败：$([IO.Path]::GetFileName($Program))，退出码 $LASTEXITCODE" }
}
function Wait-Ready {
    for ($attempt = 0; $attempt -lt 45; $attempt++) {
        try {
            $health = Invoke-RestMethod "http://127.0.0.1:$Port/api/health" -TimeoutSec 2
            if ($health.app -eq 'oneasy-mail-assistant' -and $health.version -eq '1.6.7') { return }
        } catch { }
        if ((Get-Service $ServiceName).Status -eq 'Stopped') { throw '后台服务启动失败，请查看 logs\service.log 和 Windows 应用程序事件日志。' }
        Start-Sleep -Seconds 1
    }
    throw '后台启动超时，请查看 logs\service.log。'
}
$mutex = New-Object Threading.Mutex($false, 'Global\OneasyMailAssistantDeploy')
$locked = $false
$transcribing = $false
$changed = $false
$hadService = $false
$backup = $null
$protocolState = $null
$protocolBackup = $null
try {
    try { $locked = $mutex.WaitOne(0) } catch [Threading.AbandonedMutexException] { $locked = $true }
    if (-not $locked) { throw '另一项部署正在运行，请等待它结束。' }
    Write-Host '正在检查交付包完整性……'
    $manifest = Get-Content "$SourceRoot\manifest.json" -Raw -Encoding UTF8 | ConvertFrom-Json
    foreach ($entry in $manifest.files) {
        $file = [IO.Path]::GetFullPath((Join-Path $SourceRoot $entry.path))
        if (-not $file.StartsWith($SourceRoot+'\',[StringComparison]::OrdinalIgnoreCase) -or -not (Test-Path -LiteralPath $file -PathType Leaf)) { throw '交付包不完整，请重新解压。' }
        if ((Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash -ne $entry.sha256) { throw "文件校验失败：$($entry.path)，请重新解压交付包。" }
    }
    if (Test-Path -LiteralPath $InstallRoot) {
        if (-not (Test-Path "$InstallRoot\deployment.json") -and (Get-ChildItem -LiteralPath $InstallRoot -Force | Select-Object -First 1)) { throw '目标目录已有其他文件，请选择空目录。' }
        if ((Get-Item -LiteralPath $InstallRoot).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw '安装目录不能是符号链接。' }
    }
    $existing = Get-CimInstance Win32_Service -Filter "Name='$ServiceName'"
    $hadService = $null -ne $existing
    if ($hadService -and $existing.PathName.Trim('"') -ne "$InstallRoot\runtime\pythonservice.exe") { throw '同名服务属于另一安装目录，请先处理原有安装，避免重复监控。' }
    if (Test-Path "$InstallRoot\deployment.json") {
        $previous = Get-Content "$InstallRoot\deployment.json" -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($previous.service_name -ne $ServiceName -or $previous.port -ne $Port) { throw '再次部署时请沿用原来的服务名和端口。' }
    }
    $listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    if ($listener -and (-not $hadService -or @($listener.OwningProcess | Where-Object { $_ -ne $existing.ProcessId }).Count)) {
        throw "本机端口 $Port 已被其他程序占用。如果旧版助手正在运行，请先停止旧后台；部署脚本不会结束其他进程。"
    }
    New-Item -ItemType Directory -Force -Path "$InstallRoot\logs" | Out-Null
    if (-not (Test-Path "$InstallRoot\deployment.json")) {
        @{service_name=$ServiceName; port=$Port; version='pending'} | ConvertTo-Json | Set-Content "$InstallRoot\deployment.json" -Encoding UTF8
    }
    $log = "$InstallRoot\logs\deploy-$(Get-Date -Format 'yyyyMMdd-HHmmss').log"
    Start-Transcript -Path $log | Out-Null
    $transcribing = $true
    # Only administrators and the service account may change service code or encrypted data.
    Invoke-Checked icacls.exe @($InstallRoot,'/inheritance:r','/grant:r','*S-1-5-18:(OI)(CI)F','*S-1-5-32-544:(OI)(CI)F','*S-1-5-32-545:(OI)(CI)RX')
    $backup = "$InstallRoot\backups\$(Get-Date -Format 'yyyyMMdd-HHmmss-fff')"
    New-Item -ItemType Directory -Force -Path $backup | Out-Null
    $protocolBackup = Join-Path $backup 'desktop-protocol.reg'
    if ($hadService) { Stop-Service $ServiceName; (Get-Service $ServiceName).WaitForStatus('Stopped',[TimeSpan]::FromSeconds(45)) }
    foreach ($name in @('app','runtime','scripts','deployment.json')) {
        if (Test-Path "$InstallRoot\$name") { Copy-Item -LiteralPath "$InstallRoot\$name" -Destination $backup -Recurse -Force }
    }
    $python = "$SourceRoot\runtime\python.exe"
    if (Test-Path "$InstallRoot\data") {
        Invoke-Checked $python @('-m','app.deployment','backup',"$InstallRoot\data","$backup\data")
    } elseif (Test-Path "$SourceRoot\data\config.json") {
        Write-Host '正在迁移本机原有配置和记录……'
        Invoke-Checked $python @('-m','app.deployment','backup',"$SourceRoot\data","$InstallRoot\data")
    }
    $changed = $true
    Write-Host '正在安装运行环境和后台服务……'
    foreach ($name in @('app','runtime','scripts')) { Copy-Item -LiteralPath "$SourceRoot\$name" -Destination $InstallRoot -Recurse -Force }
    @{service_name=$ServiceName; port=$Port; version='1.6.7'} | ConvertTo-Json | Set-Content "$InstallRoot\deployment.json" -Encoding UTF8
    New-Item -ItemType Directory -Force -Path "$InstallRoot\data" | Out-Null
    Invoke-Checked icacls.exe @("$InstallRoot\data",'/inheritance:r','/grant:r','*S-1-5-18:(OI)(CI)F','*S-1-5-32-544:(OI)(CI)F')
    Invoke-Checked icacls.exe @("$InstallRoot\backups",'/inheritance:r','/grant:r','*S-1-5-18:(OI)(CI)F','*S-1-5-32-544:(OI)(CI)F')
    $installedPython = "$InstallRoot\runtime\python.exe"
    $action = if ($hadService) { 'update' } else { 'install' }
    Invoke-Checked $installedPython @('-m','app.windows_service','--startup','delayed',$action)
    Invoke-Checked sc.exe @('failure',$ServiceName,'reset=','86400','actions=','restart/60000/restart/60000/restart/60000')
    Invoke-Checked sc.exe @('failureflag',$ServiceName,'1')
    Start-Service $ServiceName
    Wait-Ready
    $protocolState = Install-OneasyDesktopProtocol -InstallRoot $InstallRoot -BackupPath $protocolBackup -NoShortcuts:$NoShortcuts
    if (-not $NoShortcuts) {
        $shell = New-Object -ComObject WScript.Shell
        foreach ($location in @([Environment]::GetFolderPath('CommonDesktopDirectory'),[Environment]::GetFolderPath('CommonPrograms'))) {
            $shortcut = $shell.CreateShortcut((Join-Path $location 'Oneasy 邮件助手.lnk'))
            $shortcut.TargetPath = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
            $shortcut.Arguments = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$InstallRoot\scripts\launch.ps1`""
            $shortcut.WorkingDirectory = $InstallRoot
            $shortcut.IconLocation = "$InstallRoot\app\static\oneasy.ico"
            $shortcut.Description = 'Oneasy 邮件助手'
            $shortcut.WindowStyle = 7
            $shortcut.Save()
        }
    }
    Write-Host "部署成功。安装目录：$InstallRoot" -ForegroundColor Green
    Write-Host '后台随 Windows 启动；关闭桌面窗口不会停止监控。'
    '部署成功。' | Set-Content -LiteralPath $resultFile -Encoding UTF8
    if (-not $NoLaunch) { & "$InstallRoot\scripts\launch.ps1" }
} catch {
    "部署失败：$($_.Exception.Message)" | Set-Content -LiteralPath $resultFile -Encoding UTF8
    Write-Host "部署失败：$($_.Exception.Message)" -ForegroundColor Red
    if ($protocolState) {
        try { Restore-OneasyDesktopProtocol -HadProtocol $protocolState.HadProtocol -BackupPath $protocolState.BackupPath }
        catch { Write-Host "桌面操作协议恢复未完成；原始备份保留在 $protocolBackup" -ForegroundColor Red }
    }
    if ($changed) {
        try {
            Stop-Service $ServiceName -ErrorAction SilentlyContinue
            $service = Get-Service $ServiceName -ErrorAction SilentlyContinue
            if ($service) { $service.WaitForStatus('Stopped',[TimeSpan]::FromSeconds(45)) }
            if ($hadService) {
                foreach ($name in @('app','runtime','scripts','deployment.json','data')) {
                    if (Test-Path "$backup\$name") { Copy-Item -LiteralPath "$backup\$name" -Destination $InstallRoot -Recurse -Force }
                }
                & "$InstallRoot\runtime\python.exe" -m app.windows_service --startup delayed update
                Start-Service $ServiceName
                Write-Host '已恢复原有程序和配置。'
            } elseif ($service) { & sc.exe delete $ServiceName | Out-Null }
        } catch { Write-Host "自动恢复未完成；原始备份保留在 $backup" -ForegroundColor Red }
    } elseif ($hadService) { Start-Service $ServiceName -ErrorAction SilentlyContinue }
    if ($log) { Write-Host "部署日志：$log" }
    exit 1
} finally {
    if ($transcribing) { Stop-Transcript | Out-Null }
    if ($locked) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}
