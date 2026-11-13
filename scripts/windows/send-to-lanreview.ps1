param([Parameter(Mandatory=$true)][string]$FilePath)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Net.Http
$base = 'http://172.16.19.158:18765'
$extensions = @('.md','.markdown','.html','.htm','.png','.jpg','.jpeg','.webp','.gif','.avif','.mp3','.wav','.m4a','.ogg','.flac','.mp4','.m4v','.webm','.mov')
try {
    $file = Get-Item -LiteralPath $FilePath -ErrorAction Stop
    if ($file.PSIsContainer -or $extensions -notcontains $file.Extension.ToLowerInvariant()) { throw '请选择支持的 Markdown、HTML、图片或视频文件。' }
    $handler = New-Object System.Net.Http.HttpClientHandler
    $handler.UseProxy = $false
    $client = New-Object System.Net.Http.HttpClient($handler)
    $client.Timeout = [TimeSpan]::FromMinutes(30)
    $form = New-Object System.Net.Http.MultipartFormDataContent
    try {
        $stream = [System.IO.File]::OpenRead($file.FullName)
        $content = New-Object System.Net.Http.StreamContent($stream)
        $content.Headers.ContentType = [System.Net.Http.Headers.MediaTypeHeaderValue]::Parse('application/octet-stream')
        $form.Add($content, 'file', $file.Name)
        $response = $client.PostAsync($base + '/api/reviews', $form).GetAwaiter().GetResult()
        $body = $response.Content.ReadAsStringAsync().GetAwaiter().GetResult()
        if (-not $response.IsSuccessStatusCode) {
            $detail = try { (ConvertFrom-Json $body).error } catch { $null }
            if (-not $detail) { $detail = '服务器返回 HTTP ' + [int]$response.StatusCode }
            throw $detail
        }
        $result = ConvertFrom-Json $body
        if ($result.id -notmatch '^[A-Za-z0-9_-]{32}$') { throw '评审已创建，但返回地址无效。' }
        Start-Process ($base + '/review/' + $result.id)
    } finally {
        if ($form) { $form.Dispose() }
        if ($client) { $client.Dispose() }
    }
} catch {
    [System.Windows.Forms.MessageBox]::Show($_.Exception.Message, 'LanReview 发送失败', 'OK', 'Error') | Out-Null
    exit 1
}
