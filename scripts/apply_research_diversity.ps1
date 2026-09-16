# 기본 실행은 검토만 한다. 학생 사용이 없는 시간에 관리자 PowerShell에서 -Apply로 적용한다.
param([switch]$Apply)
$ErrorActionPreference = 'Stop'
$liveRoot = 'C:\Users\bon505\Desktop\mochang-bot'
$targetBranch = 'research-diversity-20260916'

function Git-Value([string[]]$GitArgs) {
    $result = & git -C $liveRoot @GitArgs
    if ($LASTEXITCODE -ne 0) { throw "git failed: $GitArgs" }
    return $result
}

function Assert-Ready([string]$Target) {
    $branch = Git-Value @('branch', '--show-current')
    if ($branch -ne 'llama-switch-and-diagnostics') { throw "Unexpected live branch: $branch" }
    $dirty = Git-Value @('status', '--porcelain')
    if ($dirty) { throw 'Live worktree has changes. Review them before deploying.' }
    & git -C $liveRoot merge-base --is-ancestor HEAD $Target
    if ($LASTEXITCODE -ne 0) { throw 'Fast-forward is not possible. Review and integrate in the development worktree first.' }
}

$target = Git-Value @('rev-parse', '--verify', "$targetBranch^{commit}")
Assert-Ready $target
$before = Git-Value @('rev-parse', 'HEAD')
Write-Host "Live: $before"
Write-Host "Target: $target"
& git -C $liveRoot diff --stat $before $target
if (-not $Apply) {
    Write-Host 'Preview only. Use -Apply in an administrator PowerShell when students are not using the service.'
    exit 0
}
if ($before -eq $target) { Write-Host 'Already applied.'; exit 0 }
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Run -Apply in an administrator PowerShell.'
}
$null = Get-Command nssm -ErrorAction Stop
# 기존 배포 창 확인 도구를 운영 경로에서 실행한다. 대기 실패 시 운영 변경 없이 종료한다.
& "$liveRoot\.venv\Scripts\python.exe" "$liveRoot\scripts\deploy_window.py"
if ($LASTEXITCODE -ne 0) { throw 'No deployment window. Service was not changed.' }
Assert-Ready $target
if ((Git-Value @('rev-parse', 'HEAD')) -ne $before) { throw 'Live HEAD changed during the wait.' }
# 프롬프트가 요청마다 로드되므로 정지 후 파일을 바꾸고 재기동한다.
& nssm stop mochang-api
if ($LASTEXITCODE -ne 0) { throw 'Could not stop mochang-api. No files were changed.' }
try {
    (Get-Service mochang-api).WaitForStatus('Stopped', [TimeSpan]::FromSeconds(30))
    & git -C $liveRoot merge --ff-only $target
    if ($LASTEXITCODE -ne 0) { throw 'Merge failed. The service will be started again.' }
}
finally {
    & nssm start mochang-api
    if ($LASTEXITCODE -ne 0) { Write-Error 'Service start failed. Check: nssm status mochang-api' }
}
for ($attempt = 0; $attempt -lt 15; $attempt++) {
    Start-Sleep -Seconds 2
    try {
        $health = Invoke-RestMethod 'http://127.0.0.1:8000/health' -TimeoutSec 5
        if ($health.ok) {
            $health | Select-Object ok, llm_reachable
            Write-Host "Applied: $target (previous: $before)"
            exit 0
        }
    } catch { }
}
throw 'Health check did not succeed. Check mochang-api service logs.'
