$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
try {
    $parent = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey('Software\Classes\*\shell', $true)
    if ($parent) { try { $parent.DeleteSubKeyTree('LanReview', $false) } finally { $parent.Dispose() } }
    Remove-Item -LiteralPath (Join-Path $env:LOCALAPPDATA 'LanReview\send-to-lanreview.ps1') -Force -ErrorAction SilentlyContinue
    [System.Windows.Forms.MessageBox]::Show('右键入口已移除。', 'LanReview', 'OK', 'Information') | Out-Null
} catch {
    [System.Windows.Forms.MessageBox]::Show($_.Exception.Message, 'LanReview 卸载失败', 'OK', 'Error') | Out-Null
    exit 1
}
