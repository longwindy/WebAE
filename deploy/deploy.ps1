<#
Deploys what is already pushed to GitHub onto the server, in one command:
    .\deploy\deploy.ps1            # server = the "aeweb" entry in ~/.ssh/config
    .\deploy\deploy.ps1 -Server root@1.2.3.4
It refuses to run while local commits are not pushed (the server could not see them),
then copies update.sh to the server and runs it: git pull, uv sync when the
dependencies changed, and a restart only when backend code changed.
#>
param([string]$Server = "aeweb")

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8  # the server prints Chinese
Set-Location (Split-Path $PSScriptRoot)

$dirty = git status --porcelain --untracked-files=no
if ($dirty) { Write-Warning "本地有未提交的改动，不会被部署：`n$($dirty -join "`n")" }

git fetch --quiet
if ($LASTEXITCODE -ne 0) { throw "git fetch 失败" }
$ahead = [int](git rev-list --count "@{u}..HEAD")
if ($ahead -gt 0) {
    Write-Host "本地还有 $ahead 个提交没有 push，服务器拉不到。先 git push 再部署。" -ForegroundColor Red
    exit 1
}

Write-Host "部署到 $Server ..."
scp -q "$PSScriptRoot\update.sh" "${Server}:/tmp/webae-update.sh"
if ($LASTEXITCODE -ne 0) { throw "上传 update.sh 失败" }
ssh $Server "sudo bash /tmp/webae-update.sh"
if ($LASTEXITCODE -ne 0) {
    Write-Host "部署失败（退出码 $LASTEXITCODE），看上面的输出。" -ForegroundColor Red
    exit $LASTEXITCODE
}
Write-Host "部署完成。" -ForegroundColor Green
