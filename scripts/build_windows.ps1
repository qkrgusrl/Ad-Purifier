param([string]$Version = '0.2.0-preview.1')
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$Diagnostics = Join-Path $Root 'build-diagnostics'
New-Item -ItemType Directory -Force $Diagnostics | Out-Null
Start-Transcript -Path (Join-Path $Diagnostics 'build.log')
function Invoke-Checked {
    param([string]$Program, [string[]]$Arguments)
    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Program exited with code $LASTEXITCODE" }
}
try {
    if (-not $IsWindows) { throw 'Windows runner required' }
    $Drive = Get-PSDrive -Name ([IO.Path]::GetPathRoot($Root).Substring(0,1))
    if ($Drive.Free -lt 14GB) { throw '빌드 시작 시 14 GiB 이상 여유 공간 필요' }
    $Python = (Get-Command python).Source
    # DOM 독립 테스트가 PATH=0을 강제한다. 동일한 잠금 브라우저로 검사한 뒤 이동한다.
    $TestBrowsers = & $Python -c "import pathlib,playwright; print(pathlib.Path(playwright.__file__).parent / 'driver/package/.local-browsers')"
    if ($LASTEXITCODE -ne 0) { throw 'Playwright package path lookup failed' }
    Invoke-Checked $Python @('-m', 'scripts.assets', 'browsers', '--root', $TestBrowsers, '--manifest', 'assets.lock.json')
    Invoke-Checked $Python @('-m', 'unittest', 'discover', '-s', 'tests', '-v')
    $BrowserStage = Join-Path $env:RUNNER_TEMP ('adpurifier-browsers-' + [guid]::NewGuid().ToString())
    Move-Item $TestBrowsers $BrowserStage
    Invoke-Checked $Python @('-m', 'scripts.collect_notices')
    Invoke-Checked $Python @('-m', 'pip', 'freeze', '--all')
    Invoke-Checked $Python @('-m', 'PyInstaller', '--noconfirm', '--clean', 'packaging/AdPurifier.spec')
    New-Item -ItemType Directory -Force 'dist/AdPurifier/runtime' | Out-Null
    Move-Item $BrowserStage 'dist/AdPurifier/runtime/chromium'
    # 대형 자산은 완성된 dist에 직접 기록한다. 별도 모델 staging 사본을 만들지 않는다.
    $LayaZip = Join-Path $env:RUNNER_TEMP 'laya-long2-model.zip'
    if (Test-Path $LayaZip) { Remove-Item $LayaZip }
    Invoke-Checked 'gh' @('release', 'download', 'build-inputs-long2-20261018', '--repo', $env:GITHUB_REPOSITORY,
                          '--pattern', 'laya-long2-model.zip', '--dir', $env:RUNNER_TEMP)
    Invoke-Checked $Python @('-m', 'scripts.assets', 'prepare', '--root', 'dist/AdPurifier',
                            '--manifest', 'assets.lock.json', '--laya-zip', $LayaZip)
    Invoke-Checked $Python @('-m', 'scripts.assets', 'generate', '--root', 'dist/AdPurifier',
                            '--manifest', 'dist/AdPurifier/installed-assets.json')
    Copy-Item 'dist/AdPurifier/installed-assets.json' $Diagnostics
    Copy-Item 'dist/AdPurifier/assets.lock.json', 'requirements-windows.lock.txt' $Diagnostics
    # 수집 완료 후 PyInstaller 작업 캐시를 제거한다.
    Remove-Item 'build' -Recurse -Force
    function Find-Inno {
        $Command = Get-Command 'iscc.exe' -ErrorAction SilentlyContinue
        if ($Command) { return $Command.Source }
        foreach ($Base in @(${env:ProgramFiles(x86)}, $env:ProgramFiles)) {
            if (-not $Base) { continue }
            foreach ($Folder in @(Get-ChildItem $Base -Directory -Filter 'Inno Setup*' -ErrorAction SilentlyContinue | Sort-Object Name -Descending)) {
                $Candidate = Join-Path $Folder.FullName 'ISCC.exe'
                if (Test-Path $Candidate) { return $Candidate }
            }
        }
        return $null
    }
    $Iscc = Find-Inno
    if (-not $Iscc) {
        Invoke-Checked 'choco' @('install', 'innosetup', '--yes', '--no-progress', '--limit-output')
        $Iscc = Find-Inno
    }
    if (-not $Iscc) { throw '공식 Inno Setup compiler를 찾지 못함' }
    (Get-Item $Iscc).VersionInfo | Format-List | Out-File (Join-Path $Diagnostics 'inno-version.txt')
    Invoke-Checked $Iscc @('packaging/installer.iss')
    # 설치 시험 전 dist 삭제: 모델 두 사본 + 설치압축을 동시에 보유하지 않는다.
    Remove-Item 'dist/AdPurifier' -Recurse -Force
    $Install = Join-Path $env:LOCALAPPDATA '설치 시험/광고청정기'
    $Setup = (Resolve-Path "release/AdPurifier-$Version-Setup.exe").Path
    $InstallLog = Join-Path $Diagnostics 'install.log'
    $Installer = Start-Process -FilePath $Setup -ArgumentList @('/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART',
                         "/DIR=`"$Install`"", "/LOG=`"$InstallLog`"") -PassThru -Wait
    if ($Installer.ExitCode -ne 0) { throw "설치 실패: $($Installer.ExitCode)" }
    $Elsewhere = Join-Path $env:RUNNER_TEMP '다른 작업 폴더'
    New-Item -ItemType Directory -Force $Elsewhere | Out-Null
    $EmptyCache = Join-Path $env:RUNNER_TEMP ([guid]::NewGuid().ToString())
    New-Item -ItemType Directory $EmptyCache | Out-Null
    $env:HF_HOME = $EmptyCache
    $env:HF_HUB_CACHE = Join-Path $EmptyCache 'hub'
    $env:TRANSFORMERS_CACHE = Join-Path $EmptyCache 'transformers'
    $env:HF_HUB_OFFLINE = '1'
    $env:TRANSFORMERS_OFFLINE = '1'
    $env:PYTHONHOME = ''
    $env:PYTHONPATH = ''
    $OldPath = $env:PATH
    # 사용자 Python을 PATH에서 제거한 frozen 실행. 동봉된 Python DLL만 사용한다.
    $env:PATH = "$env:SystemRoot\System32;$env:SystemRoot"
    $Smoke = Join-Path $Diagnostics 'self-test.json'
    try {
        $Exe = Join-Path $Install 'AdPurifier.exe'
        $Process = Start-Process -FilePath $Exe -WorkingDirectory $Elsewhere -ArgumentList @('--self-test',
                    '--no-browser', '--self-test-output', "`"$Smoke`"") -PassThru
        if (-not $Process.WaitForExit(1200000)) {
            & "$env:SystemRoot/System32/taskkill.exe" /PID $Process.Id /T /F
            throw '설치 스모크 20분 초과'
        }
        $Process.Refresh()
        if ($Process.ExitCode -ne 0) { throw "설치 self-test 실패: $($Process.ExitCode)" }
        $Report = Get-Content -Raw $Smoke | ConvertFrom-Json
        if ($Report.status -ne 'ok' -or -not $Report.frozen) { throw 'frozen self-test 증거 불일치' }
        # 별도의 실제 UI 시작 검사. --no-browser로 기본 브라우저를 열지 않는다.
        $Listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, 0)
        $Listener.Start(); $Port = $Listener.LocalEndpoint.Port; $Listener.Stop()
        $UI = Start-Process -FilePath $Exe -WorkingDirectory $Elsewhere -ArgumentList @('--no-browser', '--port', "$Port") -PassThru
        try {
            $Ready = $false
            for ($i = 0; $i -lt 60; $i++) {
                if ($UI.HasExited) { throw 'UI 시작 중 종료' }
                try {
                    $Status = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/status" -TimeoutSec 2
                    $Html = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/" -TimeoutSec 2
                    if ($Html.StatusCode -eq 200 -and $Status.model_available -and $Status.out_dir -eq $Install) {
                        $Status | ConvertTo-Json -Depth 20 | Out-File (Join-Path $Diagnostics 'ui-smoke.json') -Encoding utf8
                        $Ready = $true; break
                    }
                } catch { Start-Sleep -Milliseconds 500 }
            }
            if (-not $Ready) { throw 'UI HTTP smoke 실패' }
        } finally {
            if (-not $UI.HasExited) { & "$env:SystemRoot/System32/taskkill.exe" /PID $UI.Id /T /F }
        }
    } finally { $env:PATH = $OldPath }
    Invoke-Checked $Python @('-m', 'scripts.release_manifest', '--root', 'release', '--version', $Version)
    Copy-Item 'requirements-windows.lock.txt', 'THIRD_PARTY_NOTICES.txt' 'release/'
    Copy-Item (Join-Path $Diagnostics 'assets.lock.json') 'release/'
    Copy-Item (Join-Path $Diagnostics 'installed-assets.json') 'release/'
    Copy-Item $Smoke 'release/self-test.json'
} finally {
    # 세션 키와 토큰은 진단물에 포함하지 않는다.
    $RuntimeLogs = Join-Path $env:LOCALAPPDATA 'AdPurifier/logs'
    if (Test-Path $RuntimeLogs) {
        Get-ChildItem $RuntimeLogs -Recurse -File -Filter '*.log' | ForEach-Object {
            Copy-Item $_.FullName (Join-Path $Diagnostics ($_.Directory.Name + '-' + $_.Name))
        }
    }
    Stop-Transcript
}
