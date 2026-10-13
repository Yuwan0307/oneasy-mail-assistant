# Fixed desktop actions only. Dot-sourcing this file has no system side effects.
function Read-OneasyDesktopRequest {
    param([string]$ControlUrl, [string]$AppRoot)
    if ($ControlUrl -cnotmatch '\Aoneasy-mail-assistant://control/(?<port>[0-9]{1,5})/(?<token>[A-Za-z0-9_-]{1,100})\z') {
        throw '桌面操作链接无效，请返回助手重新操作。'
    }
    $requestedPort = $Matches.port
    $token = $Matches.token
    $settings = Get-Content -LiteralPath (Join-Path $AppRoot 'deployment.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    if (($settings.port -isnot [int] -and $settings.port -isnot [long]) -or $settings.port -lt 1024 -or $settings.port -gt 65535 -or
        $settings.service_name -isnot [string] -or $settings.service_name -cnotmatch '\AOneasyMailAssistant[A-Za-z0-9_-]*\z') {
        throw '安装信息无效，请重新运行部署脚本。'
    }
    if ($requestedPort -cne [string]$settings.port) { throw '桌面操作链接不属于此安装，请返回助手重新操作。' }
    return [pscustomobject]@{
        Port = [int]$settings.port
        Token = $token
        ServiceName = $settings.service_name
        AppRoot = [IO.Path]::GetFullPath($AppRoot).TrimEnd('\')
        Url = "http://127.0.0.1:$($settings.port)/"
        Profile = Join-Path $env:LOCALAPPDATA "Oneasy\MailAssistant-$($settings.port)"
    }
}

function Initialize-OneasyNativeMethods {
    if ('OneasyDesktopNative' -as [type]) { return }
    Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
public static class OneasyDesktopNative {
    [DllImport("shell32.dll", SetLastError=true, CharSet=CharSet.Unicode)]
    private static extern IntPtr CommandLineToArgvW(string command, out int count);
    [DllImport("kernel32.dll")] private static extern IntPtr LocalFree(IntPtr pointer);
    private delegate bool EnumWindowsCallback(IntPtr window, IntPtr state);
    [DllImport("user32.dll")] private static extern bool EnumWindows(EnumWindowsCallback callback, IntPtr state);
    [DllImport("user32.dll")] private static extern uint GetWindowThreadProcessId(IntPtr window, out uint processId);
    [DllImport("user32.dll")] private static extern bool IsWindowVisible(IntPtr window);
    [DllImport("user32.dll", SetLastError=true)] private static extern bool PostMessage(IntPtr window, uint message, IntPtr wParam, IntPtr lParam);
    public static string[] Arguments(string command) {
        int count;
        IntPtr pointer = CommandLineToArgvW(command, out count);
        if (pointer == IntPtr.Zero) throw new System.ComponentModel.Win32Exception();
        try {
            string[] result = new string[count];
            for (int i=0; i<count; i++) result[i] = Marshal.PtrToStringUni(Marshal.ReadIntPtr(pointer, i * IntPtr.Size));
            return result;
        } finally { LocalFree(pointer); }
    }
    public static IntPtr[] Windows(int processId) {
        List<IntPtr> result = new List<IntPtr>();
        EnumWindows(delegate(IntPtr window, IntPtr state) {
            uint owner;
            GetWindowThreadProcessId(window, out owner);
            if (owner == processId && IsWindowVisible(window)) result.Add(window);
            return true;
        }, IntPtr.Zero);
        return result.ToArray();
    }
    public static bool Close(IntPtr window, int processId) {
        uint owner;
        GetWindowThreadProcessId(window, out owner);
        // A window may disappear or its handle may be reused between enumeration
        // and posting. Never send WM_CLOSE to a different process.
        if (owner != processId || owner == 0) return true;
        return PostMessage(window, 0x0010, IntPtr.Zero, IntPtr.Zero);
    }
}
'@
}

function Get-OneasyBrowserArguments {
    param([string]$CommandLine)
    Initialize-OneasyNativeMethods
    return [OneasyDesktopNative]::Arguments($CommandLine)
}

function Test-OneasyDesktopBrowser {
    param($Process, $Request)
    if ($Process.Name -ine 'msedge.exe' -or [string]::IsNullOrWhiteSpace($Process.CommandLine)) { return $false }
    $arguments = @(Get-OneasyBrowserArguments -CommandLine $Process.CommandLine)
    $profiles = @()
    $apps = @()
    for ($index = 1; $index -lt $arguments.Count; $index++) {
        $argument = $arguments[$index]
        if ($argument -match '^--type(?:=|$)') { return $false }
        if ($argument -cmatch '^--user-data-dir=(.*)$') { $profiles += $Matches[1] }
        elseif ($argument -ceq '--user-data-dir' -and $index + 1 -lt $arguments.Count) { $index++; $profiles += $arguments[$index] }
        elseif ($argument -cmatch '^--app=(.*)$') { $apps += $Matches[1] }
        elseif ($argument -ceq '--app' -and $index + 1 -lt $arguments.Count) { $index++; $apps += $arguments[$index] }
    }
    if ($profiles.Count -ne 1 -or $apps.Count -ne 1 -or $apps[0] -cne $Request.Url) { return $false }
    try {
        return [IO.Path]::GetFullPath($profiles[0]).TrimEnd('\').Equals(
            [IO.Path]::GetFullPath($Request.Profile).TrimEnd('\'), [StringComparison]::OrdinalIgnoreCase)
    } catch { return $false }
}

function Get-OneasyWindowHandles {
    param([int]$ProcessId)
    Initialize-OneasyNativeMethods
    return [OneasyDesktopNative]::Windows($ProcessId)
}

function Send-OneasyWindowClose {
    param([IntPtr]$Handle, [int]$ProcessId)
    Initialize-OneasyNativeMethods
    if (-not [OneasyDesktopNative]::Close($Handle, $ProcessId)) { throw '关闭桌面窗口失败，请手动关闭窗口。' }
}

function Close-OneasyDesktopWindow {
    param($Request)
    $windowsFound = $false
    $browsers = @(Get-CimInstance Win32_Process -Filter "Name='msedge.exe'" -ErrorAction Stop |
        Where-Object { Test-OneasyDesktopBrowser -Process $_ -Request $Request })
    foreach ($browser in $browsers) {
        $browserId = [int]$browser.ProcessId
        $handles = @(Get-OneasyWindowHandles -ProcessId $browserId)
        if (-not $handles.Count) { continue }
        $current = Get-CimInstance Win32_Process -Filter "ProcessId=$browserId" -ErrorAction Stop
        if (-not $current -or $current.CreationDate -ne $browser.CreationDate -or
            -not (Test-OneasyDesktopBrowser -Process $current -Request $Request)) { continue }
        $windowsFound = $true
        # Edge can have several app windows in one root process. Close all its visible
        # top-level windows; never terminate the browser or an unrelated profile.
        $process = Get-Process -Id $browserId -ErrorAction SilentlyContinue
        if ($process -and $process.MainWindowHandle -ne [IntPtr]::Zero) { [void]$process.CloseMainWindow() }
        $remaining = @(Get-OneasyWindowHandles -ProcessId $browserId)
        if ($remaining.Count) {
            $current = Get-CimInstance Win32_Process -Filter "ProcessId=$browserId" -ErrorAction Stop
            if (-not $current -or $current.CreationDate -ne $browser.CreationDate -or
                -not (Test-OneasyDesktopBrowser -Process $current -Request $Request)) { continue }
            foreach ($handle in $remaining) { Send-OneasyWindowClose -Handle $handle -ProcessId $browserId }
        }
        for ($attempt = 0; $attempt -lt 10; $attempt++) {
            if (@(Get-OneasyWindowHandles -ProcessId $browserId).Count -eq 0) { break }
            Start-Sleep -Milliseconds 500
        }
        if (@(Get-OneasyWindowHandles -ProcessId $browserId).Count -ne 0) { throw '桌面窗口尚未关闭，请手动关闭窗口。' }
    }
    return $windowsFound
}

function Assert-OneasyServiceOwnership {
    param([string]$ServiceName, [string]$AppRoot, [int]$Port, [switch]$RequireListener)
    if ($ServiceName -cnotmatch '\AOneasyMailAssistant[A-Za-z0-9_-]*\z' -or $Port -lt 1024 -or $Port -gt 65535) { throw '安装信息中的后台服务名或端口无效。' }
    $expectedPath = Join-Path ([IO.Path]::GetFullPath($AppRoot).TrimEnd('\')) 'runtime\pythonservice.exe'
    $serviceInfo = Get-CimInstance Win32_Service -Filter "Name='$ServiceName'" -ErrorAction Stop
    if (-not $serviceInfo -or $serviceInfo.PathName.Trim('"') -ine $expectedPath) { throw '后台服务属于另一安装目录，操作已取消。请检查安装信息。' }
    $listeners = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
    if (@($listeners | Where-Object { $_.OwningProcess -ne $serviceInfo.ProcessId }).Count -or
        ($RequireListener -and (-not $listeners.Count -or $serviceInfo.ProcessId -le 0))) {
        throw '助手端口未由此安装的后台服务监听，操作已取消。请稍后重试或检查安装信息。'
    }
}

function Stop-OneasyDesktopService {
    param([string]$ServiceName, [string]$AppRoot, [int]$Port)
    # Validate again at the elevation boundary. No URL/token is ever placed in a command.
    if ($ServiceName -cnotmatch '\AOneasyMailAssistant[A-Za-z0-9_-]*\z') { throw '安装信息中的后台服务名无效。' }
    $service = Get-Service -Name $ServiceName -ErrorAction Stop
    Assert-OneasyServiceOwnership -ServiceName $ServiceName -AppRoot $AppRoot -Port $Port -RequireListener:($service.Status -ne 'Stopped')
    if ($service.Status -eq 'Stopped') { return }
    try {
        Stop-Service -InputObject $service -ErrorAction Stop
    } catch {
        $exception = $_.Exception
        $accessDenied = $false
        while ($exception) {
            if ($exception -is [UnauthorizedAccessException] -or
                ($exception -is [ComponentModel.Win32Exception] -and $exception.NativeErrorCode -eq 5)) { $accessDenied = $true; break }
            $exception = $exception.InnerException
        }
        if (-not $accessDenied -and $_.FullyQualifiedErrorId -notmatch 'CouldNotStopService|ServiceCommandException') {
            throw '后台服务未能停止，窗口保持打开。请重试或查看 logs\service.log。'
        }
        $escapedRoot = [IO.Path]::GetFullPath($AppRoot).TrimEnd('\').Replace("'", "''")
        $command = "& { `$ErrorActionPreference = 'Stop'; try { . '$escapedRoot\scripts\desktop-control.ps1'; `$state = Get-Service -Name '$ServiceName' -ErrorAction Stop; Assert-OneasyServiceOwnership -ServiceName '$ServiceName' -AppRoot '$escapedRoot' -Port $Port -RequireListener:(`$state.Status -ne 'Stopped'); Stop-Service -Name '$ServiceName' -ErrorAction Stop; (Get-Service -Name '$ServiceName' -ErrorAction Stop).WaitForStatus('Stopped', [TimeSpan]::FromSeconds(45)); exit 0 } catch { exit 1 } }"
        $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($command))
        try {
            $process = Start-Process -FilePath "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" -WindowStyle Hidden -Verb RunAs -ArgumentList "-NoProfile -NonInteractive -EncodedCommand $encoded" -Wait -PassThru -ErrorAction Stop
        } catch { throw '未能获得停止后台服务的管理员授权，窗口保持打开。请允许授权后重试。' }
        if ($process.ExitCode -ne 0) { throw '后台服务未能停止，窗口保持打开。请重试或查看 logs\service.log。' }
    }
    try {
        $service = Get-Service -Name $ServiceName -ErrorAction Stop
        $service.WaitForStatus('Stopped', [TimeSpan]::FromSeconds(45))
    } catch { throw '未能确认后台服务已停止，窗口保持打开。请稍后重试或查看 logs\service.log。' }
}

function Show-OneasyDesktopNotice {
    param([string]$Message)
    Add-Type -AssemblyName System.Windows.Forms
    [Windows.Forms.MessageBox]::Show($Message, 'Oneasy 邮件助手') | Out-Null
}

function Invoke-OneasyDesktopControl {
    param([string]$ControlUrl, [string]$AppRoot)
    $request = Read-OneasyDesktopRequest -ControlUrl $ControlUrl -AppRoot $AppRoot
    try {
        $response = Invoke-RestMethod -Uri ($request.Url + 'api/desktop/command') -Method Post -ContentType 'application/json' -Body (@{ token = $request.Token } | ConvertTo-Json -Compress) -TimeoutSec 5
    } catch { throw '桌面操作已过期、已使用或后台暂时不可用。请返回助手重新操作。' }
    if ($response.action -isnot [string] -or $response.action -cnotin @('hide', 'exit')) { throw '后台返回的桌面操作无效，请返回助手重新操作。' }
    if ($response.action -ceq 'exit') { Stop-OneasyDesktopService -ServiceName $request.ServiceName -AppRoot $request.AppRoot -Port $request.Port }
    try { $closed = Close-OneasyDesktopWindow -Request $request }
    catch {
        if ($response.action -ceq 'exit') { throw ('后台服务已停止，但' + $_.Exception.Message) }
        throw ('后台保持当前状态，但' + $_.Exception.Message)
    }
    if (-not $closed) {
        if ($response.action -ceq 'exit') { Show-OneasyDesktopNotice '后台服务已停止。请手动关闭当前浏览器窗口；下次打开助手会重新启动后台。' }
        else { Show-OneasyDesktopNotice '请手动关闭当前浏览器窗口；后台保持当前监控状态。安装 Edge 后可使用自动关闭的独立桌面窗口。' }
    }
}

function Get-OneasyDesktopProtocolCommand {
    param([string]$InstallRoot)
    $root = [IO.Path]::GetFullPath($InstallRoot).TrimEnd('\')
    if ($root.Contains('"')) { throw '安装路径不能包含双引号。' }
    return "`"$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe`" -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$root\scripts\launch.ps1`" -ControlUrl `"%1`""
}

function Invoke-OneasyRegistry {
    param([string[]]$Arguments)
    & "$env:SystemRoot\System32\reg.exe" @Arguments | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "桌面操作协议注册失败，退出码 $LASTEXITCODE。" }
}

function Save-OneasyDesktopProtocol {
    param([string]$BackupPath)
    if (-not (Test-Path -LiteralPath 'Registry::HKEY_LOCAL_MACHINE\SOFTWARE\Classes\oneasy-mail-assistant')) { return $false }
    Invoke-OneasyRegistry -Arguments @('export', 'HKLM\SOFTWARE\Classes\oneasy-mail-assistant', $BackupPath, '/y')
    return $true
}

function Set-OneasyDesktopProtocol {
    param([string]$InstallRoot)
    $root = 'Registry::HKEY_LOCAL_MACHINE\SOFTWARE\Classes\oneasy-mail-assistant'
    $command = Get-OneasyDesktopProtocolCommand -InstallRoot $InstallRoot
    if (Test-Path -LiteralPath $root) { Remove-Item -LiteralPath $root -Recurse -Force -ErrorAction Stop }
    New-Item -Path "$root\shell\open\command" -Force -ErrorAction Stop | Out-Null
    Set-Item -LiteralPath $root -Value 'URL:Oneasy Mail Assistant' -ErrorAction Stop
    New-ItemProperty -LiteralPath $root -Name 'URL Protocol' -PropertyType String -Value '' -Force -ErrorAction Stop | Out-Null
    Set-Item -LiteralPath "$root\shell\open\command" -Value $command -ErrorAction Stop
}

function Restore-OneasyDesktopProtocol {
    param([bool]$HadProtocol, [string]$BackupPath)
    $root = 'Registry::HKEY_LOCAL_MACHINE\SOFTWARE\Classes\oneasy-mail-assistant'
    if ($HadProtocol -and -not (Test-Path -LiteralPath $BackupPath -PathType Leaf)) { throw '桌面操作协议备份缺失，未修改现有注册。' }
    if (Test-Path -LiteralPath $root) { Remove-Item -LiteralPath $root -Recurse -Force -ErrorAction Stop }
    if ($HadProtocol) { Invoke-OneasyRegistry -Arguments @('import', $BackupPath) }
}

function Install-OneasyDesktopProtocol {
    param([string]$InstallRoot, [string]$BackupPath, [switch]$NoShortcuts)
    # Isolated test installs opt out of every global desktop integration.
    if ($NoShortcuts) { return $null }
    # The existing handler already points at the updated launcher on upgrades.
    $key = $null; $commandKey = $null
    try {
        $root = 'Registry::HKEY_LOCAL_MACHINE\SOFTWARE\Classes\oneasy-mail-assistant'
        $key = Get-Item -LiteralPath $root -ErrorAction Stop
        $commandKey = Get-Item -LiteralPath "$root\shell\open\command" -ErrorAction Stop
        if ($key.GetValueNames() -contains 'URL Protocol' -and
            $commandKey.GetValueNames() -notcontains 'DelegateExecute' -and
            $commandKey.GetValue('') -ceq (Get-OneasyDesktopProtocolCommand -InstallRoot $InstallRoot)) { return $null }
    } catch { }
    finally {
        if ($null -ne $commandKey) { $commandKey.Dispose() }
        if ($null -ne $key) { $key.Dispose() }
    }
    $hadProtocol = Save-OneasyDesktopProtocol -BackupPath $BackupPath
    try { Set-OneasyDesktopProtocol -InstallRoot $InstallRoot }
    catch {
        try { Restore-OneasyDesktopProtocol -HadProtocol $hadProtocol -BackupPath $BackupPath }
        catch { throw "桌面操作协议恢复未完成；原始备份保留在 $BackupPath" }
        throw
    }
    return [pscustomobject]@{ HadProtocol = $hadProtocol; BackupPath = $BackupPath }
}
