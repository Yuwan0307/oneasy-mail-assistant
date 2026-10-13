# Compatibility entry point; use the same offline installer.
& "$PSScriptRoot\deploy.ps1" @args
exit $LASTEXITCODE
