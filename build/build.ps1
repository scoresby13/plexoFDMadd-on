# Packs the add-on into plexo-multi-network.fda (a zip with manifest.json at its root), which
# FDM installs from its Add-ons menu. Run from PowerShell: .\build\build.ps1
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$dist = Join-Path $root 'dist'
$out = Join-Path $dist 'plexo-multi-network.fda'
New-Item -ItemType Directory -Force -Path $dist | Out-Null
if (Test-Path $out) { Remove-Item $out }
Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem
$addon = (Resolve-Path (Join-Path $root 'addon')).Path
$zip = [System.IO.Compression.ZipFile]::Open($out, 'Create')
try {
  Get-ChildItem -Path $addon -Recurse -File |
    Where-Object { $_.FullName -notmatch '__pycache__' -and $_.Extension -ne '.pyc' } |
    ForEach-Object {
      # Forward slashes: zip entry names must not use backslashes.
      $name = $_.FullName.Substring($addon.Length + 1).Replace('\', '/')
      [System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile($zip, $_.FullName, $name, 'Optimal') | Out-Null
    }
} finally {
  $zip.Dispose()
}
Write-Host "Built $out"
