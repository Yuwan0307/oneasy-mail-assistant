$ErrorActionPreference='Stop'
$demoRoot=Split-Path $PSScriptRoot -Parent
& "$demoRoot\runtime\python.exe" -m app demo --data "$demoRoot\demo-data" --port 18766
