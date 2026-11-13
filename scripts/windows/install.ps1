$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
try {
    $source = Join-Path $PSScriptRoot 'send-to-lanreview.ps1'
    $directory = Join-Path $env:LOCALAPPDATA 'LanReview'
    New-Item -ItemType Directory -Force -Path $directory | Out-Null
    $target = Join-Path $directory 'send-to-lanreview.ps1'
    Copy-Item -LiteralPath $source -Destination $target -Force
    $key = [Microsoft.Win32.Registry]::CurrentUser.CreateSubKey('Software\Classes\*\shell\LanReview')
    $powershell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    try {
        $key.SetValue('', '发送到 LanReview')
        $key.SetValue('Icon', 'shell32.dll,167')
        $command = $key.CreateSubKey('command')
        try { $command.SetValue('', ('"{0}" -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{1}" "%1"' -f $powershell, $target)) }
        finally { $command.Dispose() }
    } finally { $key.Dispose() }
    [System.Windows.Forms.MessageBox]::Show('安装完成。在文件上右键，选择“发送到 LanReview”。Windows 11 可能需要先点击“显示更多选项”。', 'LanReview', 'OK', 'Information') | Out-Null
} catch {
    [System.Windows.Forms.MessageBox]::Show($_.Exception.Message, 'LanReview 安装失败', 'OK', 'Error') | Out-Null
    exit 1
}
