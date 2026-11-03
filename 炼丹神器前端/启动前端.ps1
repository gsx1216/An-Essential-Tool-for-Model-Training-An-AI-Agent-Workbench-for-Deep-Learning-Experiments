param([int]$Port = 5173)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$runtime = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies'
if (-not (Get-Command node -ErrorAction SilentlyContinue)) {
    $nodeBin = Join-Path $runtime 'node\bin'
    if (Test-Path -LiteralPath (Join-Path $nodeBin 'node.exe')) { $env:Path = "$nodeBin;$env:Path" }
}
if (-not (Get-Command pnpm -ErrorAction SilentlyContinue)) {
    $pnpmBin = Join-Path $runtime 'bin\fallback'
    if (Test-Path -LiteralPath (Join-Path $pnpmBin 'pnpm.cmd')) { $env:Path = "$pnpmBin;$env:Path" }
}
if (-not (Get-Command pnpm -ErrorAction SilentlyContinue)) { throw '请先准备 Node.js 和 pnpm 11.25.0，详见 README.md。' }
if (-not (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'node_modules'))) {
    & pnpm install --frozen-lockfile
    if ($LASTEXITCODE -ne 0) { throw '依赖安装未完成。' }
}
& pnpm dev --port $Port
