param(
    [string]$BackendRoot = 'E:\code\LangChian_New',
    [string]$LogDirectory = '',
    [int]$FileIndex = -1,
    [switch]$Once
)
$ErrorActionPreference = 'Stop'
if (-not $LogDirectory) {
    if ($env:APP_LOG_DIR) { $LogDirectory = $env:APP_LOG_DIR }
    else { $LogDirectory = Join-Path $BackendRoot 'logs' }
}
if (-not (Test-Path -LiteralPath $LogDirectory -PathType Container)) {
    throw "日志目录不存在：$LogDirectory。请确认后端路径或使用 -LogDirectory 指定实际目录。"
}
$files = @(Get-ChildItem -LiteralPath $LogDirectory -Recurse -File -Filter 'app-*.log' |
    Sort-Object LastWriteTime -Descending | Select-Object -First 20)
if ($files.Count -eq 0) { throw '暂未找到日志。请先启动后端并访问一次接口。' }
if ($FileIndex -lt 0) {
    Write-Host '请选择日志文件；日期和进程号用于区分服务与测试进程：'
    for ($i = 0; $i -lt $files.Count; $i++) {
        Write-Host ("[{0}] {1}  最后更新 {2}" -f $i, $files[$i].FullName, $files[$i].LastWriteTime)
    }
    $choice = Read-Host '输入编号，直接回车选择 0'
    if (-not $choice) { $FileIndex = 0 }
    elseif (-not [int]::TryParse($choice, [ref]$FileIndex)) { throw '请输入文件编号。' }
}
if ($FileIndex -lt 0 -or $FileIndex -ge $files.Count) { throw '文件编号不在列表范围内。' }
$selected = $files[$FileIndex].FullName
Write-Host "正在查看：$selected"
Write-Host 'ERROR 表示错误，WARNING 表示警告。可用页面追踪号在该文件中定位请求。'
if ($Once) { Get-Content -LiteralPath $selected -Encoding UTF8 -Tail 100 }
else {
    Write-Host '实时显示末尾 100 行及新增内容；按 Ctrl+C 退出。后端重启后请重新选文件。'
    Get-Content -LiteralPath $selected -Encoding UTF8 -Tail 100 -Wait
}
