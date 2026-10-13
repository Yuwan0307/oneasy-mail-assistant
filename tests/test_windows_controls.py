"""Exercise the Windows handler without touching real services, windows or registry."""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


@unittest.skipUnless(os.name == 'nt', 'Windows PowerShell required')
class WindowsControlsTest(unittest.TestCase):
    def run_powershell(self, script):
        package = Path(__file__).resolve().parents[1]
        helper = package / 'scripts' / 'desktop-control.ps1'
        self.assertTrue(helper.exists(), 'Windows desktop control handler is missing')
        scratch = package / '.test-temp'
        scratch.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as temporary:
            root = Path(temporary) / '中文 空格 $目录'
            root.mkdir()
            (root / 'deployment.json').write_text(json.dumps({
                'service_name': 'OneasyMailAssistantTest', 'port': 18765}), encoding='utf-8-sig')
            harness = Path(temporary) / 'harness.ps1'
            prelude = """
$ErrorActionPreference = 'Stop'
. $env:ONEASY_TEST_HELPER
$fixtureRoot = $env:ONEASY_TEST_ROOT
$env:LOCALAPPDATA = Join-Path $fixtureRoot 'Local App'
$script:events = New-Object Collections.Generic.List[string]
$script:response = @{ action = 'hide' }
$script:service = [pscustomobject]@{ Status = 'Running'; ServiceName = 'OneasyMailAssistantTest' }
$script:serviceInfo = [pscustomobject]@{ Name = 'OneasyMailAssistantTest'; PathName = (Join-Path $fixtureRoot 'runtime\\pythonservice.exe'); ProcessId = 501 }
$script:listeners = @([pscustomobject]@{ OwningProcess = 501 })
$script:service | Add-Member ScriptMethod WaitForStatus {
    param($status, $timeout)
    $script:events.Add('wait:' + $status)
    if ($this.Status -ne $status) { throw 'service did not stop' }
}
function Show-OneasyDesktopNotice { param([string]$Message) $script:events.Add('notice:' + $Message) }
function Invoke-RestMethod {
    param($Uri, $Method, $ContentType, $Body, $TimeoutSec)
    $script:request = @{ uri = $Uri; method = $Method; content_type = $ContentType; body = ($Body | ConvertFrom-Json) }
    $script:events.Add('redeem')
    return $script:response
}
function Get-Service { param($Name) if ($Name -cne 'OneasyMailAssistantTest') { throw 'foreign service' }; return $script:service }
function Stop-Service {
    param($Name, $InputObject)
    $actualName = if ($InputObject) { $InputObject.ServiceName } else { $Name }
    if ($actualName -cne 'OneasyMailAssistantTest') { throw 'foreign service stop' }
    $script:events.Add('stop:' + $actualName)
    $script:service.Status = 'Stopped'
}
function Get-CimInstance {
    param($ClassName, $Filter)
    if ($ClassName -eq 'Win32_Service') { return $script:serviceInfo }
    if ($Filter -match '^ProcessId=(\\d+)$') { return $script:processes | Where-Object { $_.ProcessId -eq [int]$Matches[1] } }
    return $script:processes
}
function Get-NetTCPConnection { param($LocalPort, $State) return $script:listeners }
function Start-Sleep { param($Milliseconds, $Seconds) }
$script:closed = @{}
function Get-OneasyWindowHandles { param([int]$ProcessId) if (-not $script:closed[$ProcessId]) { return [IntPtr]($ProcessId + 10000) } }
function Send-OneasyWindowClose {
    param([IntPtr]$Handle, [int]$ProcessId)
    $browserId = $Handle.ToInt32() - 10000
    if ($ProcessId -ne $browserId) { throw 'window owner was not verified' }
    $script:closed[$browserId] = $true
    $script:events.Add('wmclose:' + $browserId)
}
function Get-Process {
    param([int]$Id)
    $process = [pscustomobject]@{ Id = $Id; MainWindowHandle = [IntPtr]($Id + 10000) }
    $process | Add-Member ScriptMethod CloseMainWindow { $script:events.Add('close:' + $this.Id); return $true }
    return $process
}
$profile = Join-Path $env:LOCALAPPDATA 'Oneasy\\MailAssistant-18765'
$script:processes = @(
    [pscustomobject]@{ Name = 'msedge.exe'; ProcessId = 201; CommandLine = ('"C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe" --app=http://127.0.0.1:18765/ --user-data-dir="' + $profile + '"') },
    [pscustomobject]@{ Name = 'msedge.exe'; ProcessId = 202; CommandLine = ('msedge.exe --app=http://127.0.0.1:18765/ --user-data-dir="' + $profile + '-other"') },
    [pscustomobject]@{ Name = 'msedge.exe'; ProcessId = 203; CommandLine = ('msedge.exe --app=http://127.0.0.1:18765/ --user-data-dir="' + $profile + '" --type=renderer') },
    [pscustomobject]@{ Name = 'msedge.exe'; ProcessId = 204; CommandLine = ('msedge.exe --user-data-dir="' + $profile + '" http://127.0.0.1:18765/') },
    [pscustomobject]@{ Name = 'msedge.exe'; ProcessId = 205; CommandLine = ('msedge.exe --app=http://127.0.0.1:18766/ --user-data-dir="' + $profile + '"') },
    [pscustomobject]@{ Name = 'chrome.exe'; ProcessId = 206; CommandLine = ('chrome.exe --app=http://127.0.0.1:18765/ --user-data-dir="' + $profile + '"') }
)
"""
            harness.write_text(prelude + '\n' + script, encoding='utf-8-sig')
            environment = os.environ.copy()
            environment.update(ONEASY_TEST_HELPER=str(helper), ONEASY_TEST_ROOT=str(root))
            result = subprocess.run([
                'powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                '-File', str(harness)], capture_output=True, timeout=40, env=environment)
            self.assertEqual(result.returncode, 0, result.stdout.decode('utf-8', 'replace') + result.stderr.decode('utf-8', 'replace'))
            return json.loads(result.stdout.decode('utf-8-sig').strip())

    def test_hide_redeems_token_and_closes_only_the_dedicated_edge_root(self):
        result = self.run_powershell("""
Invoke-OneasyDesktopControl -ControlUrl 'oneasy-mail-assistant://control/18765/Abc_123-xyz' -AppRoot $fixtureRoot
@{ events = @($script:events); request = $script:request } | ConvertTo-Json -Depth 8 -Compress
""")
        self.assertEqual(result['events'], ['redeem', 'close:201', 'wmclose:201'])
        self.assertEqual(result['request'], {
            'uri': 'http://127.0.0.1:18765/api/desktop/command', 'method': 'Post',
            'content_type': 'application/json', 'body': {'token': 'Abc_123-xyz'}})

    def test_control_uri_rejects_foreign_ports_urls_and_unsafe_tokens_before_network(self):
        result = self.run_powershell("""
$invalid = @(
    'oneasy-mail-assistant://control/18766/abc', 'oneasy-mail-assistant://control/018765/abc',
    'http://127.0.0.1:18765/abc', 'oneasy-mail-assistant://control/18765/a?command=exit',
    'oneasy-mail-assistant://control/18765/a#exit', 'oneasy-mail-assistant://control/18765/a/b',
    'oneasy-mail-assistant://control/18765/a%22', 'oneasy-mail-assistant://control/18765/',
    'oneasy-mail-assistant://control/18765/' + ('a' * 101),
    'oneasy-mail-assistant://control/18765/a" -Command whoami',
    'ONEASY-MAIL-ASSISTANT://control/18765/a'
)
$rejected = 0
foreach ($candidate in $invalid) {
    try { Invoke-OneasyDesktopControl -ControlUrl $candidate -AppRoot $fixtureRoot }
    catch { $rejected++ }
}
@{ rejected = $rejected; total = $invalid.Count; events = @($script:events) } | ConvertTo-Json -Compress
""")
        self.assertEqual(result['rejected'], result['total'])
        self.assertEqual(result['events'], [])

    def test_invalid_installation_settings_never_redeem_or_stop_a_service(self):
        result = self.run_powershell("""
$invalid = @(@{ port = '18765'; service_name = 'OneasyMailAssistantTest' },
    @{ port = 18765; service_name = 'OtherService' }, @{ port = 18765.5; service_name = 'OneasyMailAssistantTest' })
$rejected = 0
foreach ($candidate in $invalid) {
    $candidate | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $fixtureRoot 'deployment.json') -Encoding UTF8
    try { Invoke-OneasyDesktopControl -ControlUrl 'oneasy-mail-assistant://control/18765/abc' -AppRoot $fixtureRoot }
    catch { $rejected++ }
}
@{ rejected = $rejected; events = @($script:events) } | ConvertTo-Json -Compress
""")
        self.assertEqual(result, {'rejected': 3, 'events': []})

    def test_exit_stops_and_waits_for_the_own_service_before_closing_window(self):
        result = self.run_powershell("""
$script:response = @{ action = 'exit' }
Invoke-OneasyDesktopControl -ControlUrl 'oneasy-mail-assistant://control/18765/exit-token' -AppRoot $fixtureRoot
@{ events = @($script:events); state = $script:service.Status } | ConvertTo-Json -Compress
""")
        self.assertEqual(result['events'], ['redeem', 'stop:OneasyMailAssistantTest', 'wait:Stopped', 'close:201', 'wmclose:201'])
        self.assertEqual(result['state'], 'Stopped')

    def test_exit_rejects_a_foreign_service_binary_or_port_owner(self):
        result = self.run_powershell("""
$script:response = @{ action = 'exit' }
$rejected = 0
$script:serviceInfo.PathName = 'C:\\Other App\\pythonservice.exe'
try { Invoke-OneasyDesktopControl -ControlUrl 'oneasy-mail-assistant://control/18765/token' -AppRoot $fixtureRoot }
catch { $rejected++ }
$script:serviceInfo.PathName = Join-Path $fixtureRoot 'runtime\\pythonservice.exe'
$script:listeners = @([pscustomobject]@{ OwningProcess = 999 })
try { Invoke-OneasyDesktopControl -ControlUrl 'oneasy-mail-assistant://control/18765/token' -AppRoot $fixtureRoot }
catch { $rejected++ }
@{ rejected = $rejected; events = @($script:events) } | ConvertTo-Json -Compress
""")
        self.assertEqual(result, {'rejected': 2, 'events': ['redeem', 'redeem']})

    def test_reused_browser_process_id_never_closes_the_replacement_process(self):
        result = self.run_powershell("""
function Get-CimInstance {
    param($ClassName, $Filter)
    if ($Filter -match '^ProcessId=') {
        return [pscustomobject]@{ Name = 'msedge.exe'; ProcessId = 201; CommandLine = 'msedge.exe https://example.com/' }
    }
    return $script:processes
}
Invoke-OneasyDesktopControl -ControlUrl 'oneasy-mail-assistant://control/18765/token' -AppRoot $fixtureRoot
@{ events = @($script:events) } | ConvertTo-Json -Compress
""")
        self.assertEqual(result['events'][0], 'redeem')
        self.assertFalse(any(event.startswith(('close:', 'wmclose:')) for event in result['events']))

    def test_launcher_refuses_a_foreign_service_or_port_before_startup(self):
        result = self.run_powershell("""
# Run the actual launcher's try body while replacing only its final message box.
# Its startup errors are caught here, so no interactive UI can block the test.
$launchPath = Join-Path (Split-Path $env:ONEASY_TEST_HELPER) 'launch.ps1'
$tokens = $null; $parseErrors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile($launchPath, [ref]$tokens, [ref]$parseErrors)
$outerTry = $ast.EndBlock.Statements | Where-Object { $_ -is [Management.Automation.Language.TryStatementAst] } | Select-Object -First 1
$bodyText = $outerTry.Body.Extent.Text
$body = [ScriptBlock]::Create($bodyText.Substring(1, $bodyText.Length - 2))
Copy-Item -LiteralPath $env:ONEASY_TEST_HELPER -Destination (Join-Path $PSScriptRoot 'desktop-control.ps1')
$appRoot = $fixtureRoot
function Start-Process { $script:events.Add('unexpected-start') }
$rejected = 0
$script:serviceInfo.PathName = 'C:\\Other App\\pythonservice.exe'
try { . $body } catch { $rejected++ }
$script:serviceInfo.PathName = Join-Path $fixtureRoot 'runtime\\pythonservice.exe'
$script:listeners = @([pscustomobject]@{ OwningProcess = 999 })
try { . $body } catch { $rejected++ }
@{ rejected = $rejected; events = @($script:events) } | ConvertTo-Json -Compress
""")
        self.assertEqual(result, {'rejected': 2, 'events': []})

    def test_exit_uac_cancellation_keeps_the_window_and_reports_failure(self):
        result = self.run_powershell("""
$script:response = @{ action = 'exit' }
function Stop-Service { param($Name, $InputObject) throw [UnauthorizedAccessException]::new('Access denied') }
function Start-Process { param($FilePath, $WindowStyle, $Verb, $ArgumentList, [switch]$Wait, [switch]$PassThru) $script:events.Add('uac:' + $Verb); throw [ComponentModel.Win32Exception]::new(1223) }
$failed = $false
try { Invoke-OneasyDesktopControl -ControlUrl 'oneasy-mail-assistant://control/18765/token' -AppRoot $fixtureRoot }
catch { $failed = $true; $message = $_.Exception.Message }
@{ failed = $failed; message = $message; events = @($script:events); state = $script:service.Status } | ConvertTo-Json -Compress
""")
        self.assertTrue(result['failed'])
        self.assertEqual(result['events'], ['redeem', 'uac:RunAs'])
        self.assertEqual(result['state'], 'Running')
        self.assertIn('授权', result['message'])

    def test_elevation_success_is_verified_before_closing_window(self):
        result = self.run_powershell("""
$script:response = @{ action = 'exit' }
function Stop-Service { param($Name, $InputObject) throw [UnauthorizedAccessException]::new('Access denied') }
function Start-Process {
    param($FilePath, $WindowStyle, $Verb, $ArgumentList, [switch]$Wait, [switch]$PassThru)
    $script:events.Add('uac:' + $Verb)
    $script:elevation = @{ hidden = $WindowStyle; waited = $Wait.IsPresent; arguments = $ArgumentList }
    return [pscustomobject]@{ ExitCode = 0 }
}
$failed = $false
try { Invoke-OneasyDesktopControl -ControlUrl 'oneasy-mail-assistant://control/18765/token' -AppRoot $fixtureRoot }
catch { $failed = $true }
@{ failed = $failed; events = @($script:events); elevation = $script:elevation } | ConvertTo-Json -Depth 5 -Compress
""")
        self.assertTrue(result['failed'], 'An exit code alone must not claim the service stopped')
        self.assertEqual(result['events'], ['redeem', 'uac:RunAs', 'wait:Stopped'])
        self.assertEqual(result['elevation']['hidden'], 'Hidden')
        self.assertTrue(result['elevation']['waited'])

    def test_unknown_or_replayed_commands_never_close_or_stop(self):
        result = self.run_powershell("""
$rejected = 0
foreach ($action in @('restart', 'EXIT', '', 'https://example.com')) {
    $script:response = @{ action = $action }
    try { Invoke-OneasyDesktopControl -ControlUrl 'oneasy-mail-assistant://control/18765/token' -AppRoot $fixtureRoot }
    catch { $rejected++ }
}
function Invoke-RestMethod { throw 'expired or already used' }
try { Invoke-OneasyDesktopControl -ControlUrl 'oneasy-mail-assistant://control/18765/token' -AppRoot $fixtureRoot }
catch { $rejected++ }
@{ rejected = $rejected; events = @($script:events) } | ConvertTo-Json -Compress
""")
        self.assertEqual(result, {'rejected': 5, 'events': ['redeem'] * 4})

    def test_default_browser_fallback_prompts_for_manual_close(self):
        result = self.run_powershell("""
$script:processes = @()
Invoke-OneasyDesktopControl -ControlUrl 'oneasy-mail-assistant://control/18765/token' -AppRoot $fixtureRoot
@{ events = @($script:events) } | ConvertTo-Json -Compress
""")
        self.assertEqual(result['events'][0], 'redeem')
        self.assertIn('关闭', result['events'][1])
        self.assertIn('后台', result['events'][1])

    def test_an_already_closed_main_window_is_not_closed_again(self):
        result = self.run_powershell("""
function Get-Process {
    param([int]$Id)
    $process = [pscustomobject]@{ Id = $Id; MainWindowHandle = [IntPtr]($Id + 10000) }
    $process | Add-Member ScriptMethod CloseMainWindow {
        $script:events.Add('close:' + $this.Id)
        $script:closed[$this.Id] = $true
        return $true
    }
    return $process
}
function Send-OneasyWindowClose { throw 'the main window was already closed' }
Invoke-OneasyDesktopControl -ControlUrl 'oneasy-mail-assistant://control/18765/token' -AppRoot $fixtureRoot
@{ events = @($script:events) } | ConvertTo-Json -Compress
""")
        self.assertEqual(result, {'events': ['redeem', 'close:201']})

    def test_normal_launcher_restarts_a_stopped_service_and_reopens_the_app_window(self):
        result = self.run_powershell("""
$scripts = Join-Path $fixtureRoot 'scripts'
New-Item -Path $scripts -ItemType Directory | Out-Null
Copy-Item -LiteralPath (Join-Path (Split-Path $env:ONEASY_TEST_HELPER) 'launch.ps1') -Destination $scripts
Copy-Item -LiteralPath $env:ONEASY_TEST_HELPER -Destination $scripts
$global:launcherEvents = New-Object Collections.Generic.List[string]
$global:launcherService = [pscustomobject]@{ Status = 'Stopped'; ServiceName = 'OneasyMailAssistantTest' }
$global:launcherServiceInfo = [pscustomobject]@{ Name = 'OneasyMailAssistantTest'; PathName = (Join-Path $fixtureRoot 'runtime\\pythonservice.exe'); ProcessId = 0 }
$global:launcherListeners = @()
function Get-Service { param($Name) return $global:launcherService }
function Get-CimInstance { param($ClassName, $Filter) return $global:launcherServiceInfo }
function Get-NetTCPConnection { param($LocalPort, $State) return $global:launcherListeners }
function Invoke-RestMethod { param($Uri, $TimeoutSec) return @{ app = 'oneasy-mail-assistant' } }
function Test-Path { param($LiteralPath) return $LiteralPath -like '*\\Microsoft\\Edge\\Application\\msedge.exe' }
function Start-Process {
    param($FilePath, $ArgumentList, $WindowStyle, $Verb, [switch]$Wait, [switch]$PassThru)
    if ($Verb -eq 'RunAs') {
        $global:launcherEvents.Add('start-service')
        $global:launcherService.Status = 'Running'
        $global:launcherServiceInfo.ProcessId = 501
        $global:launcherListeners = @([pscustomobject]@{ OwningProcess = 501 })
        return [pscustomobject]@{ ExitCode = 0 }
    }
    $global:launcherArguments = @(Get-OneasyBrowserArguments -CommandLine ('msedge.exe ' + $ArgumentList))
    $global:launcherEvents.Add('open-window')
}
& (Join-Path $scripts 'launch.ps1')
@{ events = @($global:launcherEvents); arguments = $global:launcherArguments } | ConvertTo-Json -Compress
""")
        self.assertEqual(result['events'], ['start-service', 'open-window'])
        self.assertIn('--app=http://127.0.0.1:18765/', result['arguments'])
        self.assertTrue(any(value.startswith('--user-data-dir=') and value.endswith('MailAssistant-18765')
                            for value in result['arguments']))

    def test_protocol_command_quotes_installation_path_and_literal_uri_argument(self):
        result = self.run_powershell("""
$command = Get-OneasyDesktopProtocolCommand -InstallRoot $fixtureRoot
$arguments = @(Get-OneasyBrowserArguments -CommandLine $command)
@{ arguments = $arguments; script = (Join-Path $fixtureRoot 'scripts\\launch.ps1') } | ConvertTo-Json -Compress
""")
        arguments = result['arguments']
        self.assertTrue(arguments[0].lower().endswith('\\powershell.exe'))
        self.assertEqual(arguments[arguments.index('-File') + 1], result['script'])
        self.assertEqual(arguments[arguments.index('-ControlUrl') + 1], '%1')
        self.assertEqual(arguments[arguments.index('-WindowStyle') + 1], 'Hidden')

    def test_protocol_backup_and_restore_preserve_preexisting_registration(self):
        result = self.run_powershell("""
$registryRoot = 'Registry::HKEY_LOCAL_MACHINE\\SOFTWARE\\Classes\\oneasy-mail-assistant'
$script:registry = @{ '(default)' = 'Old handler'; 'URL Protocol' = ''; 'extra\\nested' = 'keep me' }
function Test-Path { param($LiteralPath, $PathType) if ($LiteralPath -eq $registryRoot) { return $null -ne $script:registry }; return Microsoft.PowerShell.Management\\Test-Path -LiteralPath $LiteralPath }
function Invoke-OneasyRegistry {
    param([string[]]$Arguments)
    if ($Arguments[0] -eq 'export') { $script:registry | ConvertTo-Json | Set-Content -LiteralPath $Arguments[2] -Encoding UTF8 }
    elseif ($Arguments[0] -eq 'import') { $script:registry = Get-Content -LiteralPath $Arguments[1] -Raw -Encoding UTF8 | ConvertFrom-Json }
    else { throw 'unexpected registry operation' }
}
function New-Item { param($Path, [switch]$Force) if ($null -eq $script:registry) { $script:registry = @{} } }
function Set-Item { param($LiteralPath, $Value) $script:registry[$LiteralPath] = $Value }
function New-ItemProperty { param($LiteralPath, $Name, $PropertyType, $Value, [switch]$Force) $script:registry[$Name] = $Value }
function Remove-Item { param($LiteralPath, [switch]$Recurse, [switch]$Force) if ($LiteralPath -ne $registryRoot) { throw 'foreign registry deletion' }; $script:registry = $null }
$backupPath = Join-Path $fixtureRoot 'protocol.reg'
$hadProtocol = Save-OneasyDesktopProtocol -BackupPath $backupPath
Set-OneasyDesktopProtocol -InstallRoot $fixtureRoot
$installedCommand = $script:registry[$registryRoot + '\\shell\\open\\command']
Restore-OneasyDesktopProtocol -HadProtocol $hadProtocol -BackupPath $backupPath
@{ had = $hadProtocol; restored = $script:registry; command = $installedCommand } | ConvertTo-Json -Depth 6 -Compress
""")
        self.assertTrue(result['had'])
        self.assertEqual(result['restored'], {'(default)': 'Old handler', 'URL Protocol': '', 'extra\\nested': 'keep me'})
        self.assertIn('-ControlUrl "%1"', result['command'])

    def test_isolated_installation_skips_global_protocol_registration(self):
        result = self.run_powershell("""
function Save-OneasyDesktopProtocol { throw 'global protocol must not be read or changed' }
function Set-OneasyDesktopProtocol { throw 'global protocol must not be changed' }
$state = Install-OneasyDesktopProtocol -InstallRoot $fixtureRoot -BackupPath (Join-Path $fixtureRoot 'protocol.reg') -NoShortcuts
@{ skipped = ($null -eq $state) } | ConvertTo-Json -Compress
""")
        self.assertEqual(result, {'skipped': True})

    def test_upgrade_reuses_matching_protocol_without_registry_writes(self):
        result = self.run_powershell("""
$script:expectedCommand = Get-OneasyDesktopProtocolCommand -InstallRoot $fixtureRoot
function Get-Item {
    param($LiteralPath)
    $key = [pscustomobject]@{}
    $key | Add-Member ScriptMethod GetValueNames { return @('URL Protocol') }
    $key | Add-Member ScriptMethod GetValue { param($name) return $script:expectedCommand }
    $key | Add-Member ScriptMethod Dispose { $script:events.Add('dispose') }
    return $key
}
function Save-OneasyDesktopProtocol { throw 'unchanged handler must not be rewritten' }
function Set-OneasyDesktopProtocol { throw 'unchanged handler must not be rewritten' }
function Remove-Item { throw 'unchanged handler must not be deleted' }
$state = Install-OneasyDesktopProtocol -InstallRoot $fixtureRoot -BackupPath (Join-Path $fixtureRoot 'protocol.reg')
@{ reused = ($null -eq $state); events = @($script:events) } | ConvertTo-Json -Compress
""")
        self.assertEqual(result, {'reused': True, 'events': ['dispose','dispose']})

    def test_upgrade_does_not_reuse_a_different_protocol_handler(self):
        result = self.run_powershell("""
function Get-Item {
    param($LiteralPath)
    $key = [pscustomobject]@{}
    $key | Add-Member ScriptMethod GetValueNames { return @('URL Protocol') }
    $key | Add-Member ScriptMethod GetValue { param($name) return 'different command' }
    $key | Add-Member ScriptMethod Dispose { $script:events.Add('dispose') }
    return $key
}
function Save-OneasyDesktopProtocol { param($BackupPath) $script:events.Add('backup'); return $true }
function Set-OneasyDesktopProtocol { param($InstallRoot) $script:events.Add('register') }
$state = Install-OneasyDesktopProtocol -InstallRoot $fixtureRoot -BackupPath (Join-Path $fixtureRoot 'protocol.reg')
@{ reused = ($null -eq $state); events = @($script:events) } | ConvertTo-Json -Compress
""")
        self.assertEqual(result, {'reused': False, 'events': ['dispose','dispose','backup','register']})

    def test_partial_protocol_registration_failure_restores_previous_handler(self):
        result = self.run_powershell("""
function Save-OneasyDesktopProtocol { param($BackupPath) $script:events.Add('backup'); return $true }
function Set-OneasyDesktopProtocol { param($InstallRoot) $script:events.Add('register'); throw 'permission error during registration' }
function Restore-OneasyDesktopProtocol {
    param($HadProtocol, $BackupPath)
    if (-not $HadProtocol -or $BackupPath -ne (Join-Path $fixtureRoot 'protocol.reg')) { throw 'wrong backup' }
    $script:events.Add('restore')
}
$failed = $false
try { Install-OneasyDesktopProtocol -InstallRoot $fixtureRoot -BackupPath (Join-Path $fixtureRoot 'protocol.reg') }
catch { $failed = $true }
@{ failed = $failed; events = @($script:events) } | ConvertTo-Json -Compress
""")
        self.assertEqual(result, {'failed': True, 'events': ['backup', 'register', 'restore']})


if __name__ == '__main__':
    unittest.main()
