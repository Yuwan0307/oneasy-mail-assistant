param([string]$ControlUrl)
$ErrorActionPreference = 'Stop'
$appRoot = Split-Path $PSScriptRoot -Parent
Add-Type -AssemblyName System.Windows.Forms
try {
    . "$PSScriptRoot\desktop-control.ps1"
    if ($PSBoundParameters.ContainsKey('ControlUrl')) {
        Invoke-OneasyDesktopControl -ControlUrl $ControlUrl -AppRoot $appRoot
        exit 0
    }
    $settings = Get-Content "$appRoot\deployment.json" -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($settings.service_name -notmatch '^OneasyMailAssistant[A-Za-z0-9_-]*$' -or $settings.port -lt 1024 -or $settings.port -gt 65535) { throw '安装信息无效，请重新运行部署脚本。' }
    $url = "http://127.0.0.1:$($settings.port)/"
    Assert-OneasyServiceOwnership -ServiceName $settings.service_name -AppRoot $appRoot -Port $settings.port
    $service = Get-Service $settings.service_name -ErrorAction Stop
    if ($service.Status -eq 'Stopped') {
        $command = "Start-Service -Name '$($settings.service_name)'"
        $process = Start-Process powershell.exe -WindowStyle Hidden -Verb RunAs -ArgumentList "-NoProfile -Command `"$command`"" -Wait -PassThru
        if ($process.ExitCode -ne 0) { throw '后台服务未能启动，请查看安装目录中的 logs\service.log。' }
    }
    $ready = $false
    for ($attempt=0; $attempt -lt 30; $attempt++) {
        try {
            $health = Invoke-RestMethod ($url+'api/health') -TimeoutSec 2
            if ($health.app -eq 'oneasy-mail-assistant') { $ready = $true; break }
        } catch { }
        Start-Sleep -Seconds 1
    }
    if (-not $ready) { throw '后台暂时不可用，请稍后重试，或查看安装目录中的 logs\service.log。' }
    Assert-OneasyServiceOwnership -ServiceName $settings.service_name -AppRoot $appRoot -Port $settings.port -RequireListener
    $edge = @("${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe", "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe") | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
    if ($edge) {
        $profile = "$env:LOCALAPPDATA\Oneasy\MailAssistant-$($settings.port)"
        Start-Process -FilePath $edge -ArgumentList "--app=$url --user-data-dir=`"$profile`" --no-first-run --window-size=1280,900"
    } else {
        [Windows.Forms.MessageBox]::Show('未找到 Microsoft Edge，将使用默认浏览器打开。安装 Edge 后即可使用独立桌面窗口。','Oneasy 邮件助手') | Out-Null
        Start-Process $url
    }
} catch { [Windows.Forms.MessageBox]::Show($_.Exception.Message,'Oneasy 邮件助手') | Out-Null; exit 1 }
