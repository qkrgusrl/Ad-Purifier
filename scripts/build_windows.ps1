param([string]$Version = '0.2.0-preview.1', [switch]$SingleInstaller, [string]$LayaZip,
      [string]$Python, [switch]$SkipUnitTests, [string]$LayaModelDir)
# CI(GitHub Actions, pwsh 7)와 일반 Windows PC(Windows PowerShell 5.1) 양쪽에서 실행한다.
# 로컬 예: powershell -ExecutionPolicy Bypass -File scripts\build_windows.ps1 -Python .venv-build\Scripts\python.exe -LayaZip <Advanced ZIP>
#          (-SkipUnitTests, -LayaModelDir <Laya 모델 폴더> 는 로컬 전용)
# (docs/BUILD.md '로컬 Windows PC 빌드'. 이 파일은 5.1이 한글을 읽도록 UTF-8 BOM으로 저장한다.)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$Diagnostics = Join-Path $Root 'build-diagnostics'
New-Item -ItemType Directory -Force $Diagnostics | Out-Null
Start-Transcript -Path (Join-Path $Diagnostics 'build.log')
$Packaged = $false
$ValidationStatus = 'not_passed'
$Smoke = $null
function Invoke-Checked {
    param([string]$Program, [string[]]$Arguments)
    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Program exited with code $LASTEXITCODE" }
}
# Windows PowerShell 5.1의 Set-Content -Encoding utf8은 BOM을 붙인다. pwsh 7과 같은 BOM 없는 UTF-8로 쓴다.
function Write-Utf8 {
    param([string]$Path, [Parameter(ValueFromPipeline)][string]$Text)
    [IO.File]::WriteAllText([IO.Path]::Combine((Get-Location).Path, $Path), $Text + [Environment]::NewLine, [Text.UTF8Encoding]::new($false))
}
try {
    if ([Environment]::OSVersion.Platform -ne 'Win32NT') { throw 'Windows runner required' }
    $CI = $env:GITHUB_ACTIONS -eq 'true'
    if ($SkipUnitTests -and $CI) { throw 'CI에서는 유닛 테스트를 건너뛸 수 없음' }
    # 로컬 전용: Advanced ZIP 대신 로컬 모델 폴더를 같은 설치 위치(assets.lock.json의 Laya 경로)에 넣는다.
    if ($LayaModelDir -and $CI) { throw 'CI에서는 잠금된 Advanced 모델 ZIP만 사용' }
    if ($LayaModelDir) { $LayaModelDir = (Resolve-Path -LiteralPath $LayaModelDir).Path }
    # 로컬 실행: CI가 주는 값을 같은 의미의 로컬 값으로 채운다. CI에서는 기존 값을 그대로 쓴다.
    $Temp = $env:RUNNER_TEMP
    if (-not $Temp) {
        $Temp = Join-Path ([IO.Path]::GetTempPath()) 'adpurifier-build'
        New-Item -ItemType Directory -Force $Temp | Out-Null
    }
    if (-not $env:GITHUB_SHA) {
        # release_manifest와 validation-status가 소스 commit으로 기록한다.
        $env:GITHUB_SHA = & git rev-parse HEAD
        if ($LASTEXITCODE -ne 0) { throw 'git commit 확인 실패' }
        if (& git status --porcelain --untracked-files=no) { Write-Warning "커밋되지 않은 변경이 있음: 기록되는 commit $env:GITHUB_SHA 과 빌드 소스가 다름" }
    }
    if (-not $env:PYTHONUTF8) { $env:PYTHONUTF8 = '1' }
    if (Test-Path 'release') { Remove-Item 'release' -Recurse -Force }
    $Drive = Get-PSDrive -Name ([IO.Path]::GetPathRoot($Root).Substring(0,1))
    if ($Drive.Free -lt 14GB) { throw '빌드 시작 시 14 GiB 이상 여유 공간 필요' }
    if ($Python) { $Python = (Resolve-Path $Python).Path } else { $Python = (Get-Command python).Source }
    if ($Python.StartsWith((Join-Path $Root 'build') + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw '빌드 중 build 폴더를 삭제하므로 Python 환경을 build 폴더 밖에 두어야 함'
    }
    # 단일 설치도 현재 소스에서 새로 빌드한다. 이전 설치본 재포장 금지.
    $PayloadSource = (& git rev-parse HEAD).Trim()
    if (-not $LayaModelDir) {
    if (-not $LayaZip -and $env:LAYA_ADVANCED_ZIP_URL) {
        $LayaZip = Join-Path $Temp 'laya-advanced.zip'
        Invoke-WebRequest -UseBasicParsing -Uri $env:LAYA_ADVANCED_ZIP_URL -OutFile $LayaZip
    }
    if (-not $LayaZip -or -not (Test-Path $LayaZip -PathType Leaf)) { throw '-LayaZip 또는 빌드 전용 LAYA_ADVANCED_ZIP_URL에 Advanced 원본 ZIP을 지정하세요' }
    $LayaZip = (Resolve-Path $LayaZip).Path
    }
    # DOM 독립 테스트가 PATH=0을 강제한다. 동일한 잠금 브라우저로 검사한 뒤 이동한다.
    $TestBrowsers = & $Python -c "import pathlib,playwright; print(pathlib.Path(playwright.__file__).parent / 'driver/package/.local-browsers')"
    if ($LASTEXITCODE -ne 0) { throw 'Playwright package path lookup failed' }
    Invoke-Checked $Python @('-m', 'scripts.assets', 'browsers', '--root', $TestBrowsers, '--manifest', 'assets.lock.json')
    if ($SkipUnitTests) {
        Write-Warning '로컬 빌드: 유닛 테스트를 건너뜀 (-SkipUnitTests). CI 빌드는 항상 실행한다.'
    } else {
        Invoke-Checked $Python @('-m', 'unittest', 'discover', '-s', 'tests', '-v')
    }
    $BrowserStage = Join-Path $Temp ('adpurifier-browsers-' + [guid]::NewGuid().ToString())
    Move-Item $TestBrowsers $BrowserStage
    Invoke-Checked $Python @('-m', 'scripts.collect_notices')
    Invoke-Checked $Python @('-m', 'pip', 'freeze', '--all')
    Invoke-Checked $Python @('-m', 'PyInstaller', '--noconfirm', '--clean', 'packaging/AdPurifier.spec')
    New-Item -ItemType Directory -Force 'dist/AdPurifier/runtime' | Out-Null
    Move-Item $BrowserStage 'dist/AdPurifier/runtime/chromium'
    # 대형 자산은 완성된 dist에 직접 기록한다. 별도 모델 staging 사본을 만들지 않는다.
    if ($LayaModelDir) {
        Write-Warning "로컬 Laya 모델 폴더 사용: $LayaModelDir (Advanced ZIP 대신)"
        Invoke-Checked $Python @('-m', 'scripts.assets', 'prepare', '--root', 'dist/AdPurifier',
                                '--manifest', 'assets.lock.json', '--laya-dir', $LayaModelDir)
    } else {
    Invoke-Checked $Python @('-m', 'scripts.assets', 'prepare', '--root', 'dist/AdPurifier',
                            '--manifest', 'assets.lock.json', '--laya-zip', $LayaZip)
    }
    # VS 설치의 한 release x64 CRT family를 선택한다. System32/다른 버전에서 보충하지 않는다.
    $VsWhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio/Installer/vswhere.exe'
    if (-not (Test-Path $VsWhere)) { throw 'VS 설치 탐색 도구 누락' }
    # VS 2022(CI runner) 또는 이후 버전. VS 2026도 Redist 아래에 VC143 family를 함께 설치한다.
    $VsRoot = & $VsWhere -latest -products '*' -version '[17.0,)' -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
    if ($LASTEXITCODE -ne 0 -or -not $VsRoot) { throw 'VS 2022 이상 C++ release redist 설치를 찾지 못함' }
    $Redist = Join-Path $VsRoot 'VC/Redist/MSVC'
    $CrtSource = $null
    $Versions = @(Get-ChildItem $Redist -Directory | Where-Object { $_.Name -match '^\d+\.\d+\.\d+(\.\d+)?$' } | Sort-Object { [version]$_.Name } -Descending)
    foreach ($CandidateVersion in $Versions) {
        $Candidate = Join-Path $CandidateVersion.FullName 'x64/Microsoft.VC143.CRT'
        if (Test-Path $Candidate -PathType Container) { $CrtSource = $Candidate; break }
    }
    if (-not $CrtSource) { throw '단일 x64 Microsoft.VC143.CRT release family를 찾지 못함' }
    Invoke-Checked $Python @('-m', 'scripts.assets', 'crt', '--root', 'dist/AdPurifier',
                            '--source-dir', $CrtSource, '--manifest', 'dist/AdPurifier/crt-manifest.json')
    # -AsHashtable은 pwsh 7 전용이다. PSCustomObject에 속성을 추가한다.
    $CrtMetadata = Get-Content -Raw -Encoding UTF8 'dist/AdPurifier/crt-manifest.json' | ConvertFrom-Json
    $FileVersions = [ordered]@{}
    foreach ($Dll in @(Get-ChildItem $CrtSource -File -Filter '*.dll' | Sort-Object Name)) {
        $FileVersions[$Dll.Name] = $Dll.VersionInfo.FileVersion
    }
    $CrtMetadata.source_family | Add-Member -Force -NotePropertyName 'file_versions' -NotePropertyValue $FileVersions
    $CrtMetadata.source_family | Add-Member -Force -NotePropertyName 'selection_policy' -NotePropertyValue 'VS2022+ installed latest numeric VC143 release family; recorded at build time, not source-pinned'
    $CrtMetadata | ConvertTo-Json -Depth 20 | Write-Utf8 'dist/AdPurifier/crt-manifest.json'
    Copy-Item 'dist/AdPurifier/crt-manifest.json' $Diagnostics
    Invoke-Checked $Python @('-m', 'scripts.assets', 'generate', '--root', 'dist/AdPurifier',
                            '--manifest', 'dist/AdPurifier/installed-assets.json')
    Copy-Item 'dist/AdPurifier/installed-assets.json' $Diagnostics
    Copy-Item 'dist/AdPurifier/assets.lock.json', 'requirements-windows.lock.txt' $Diagnostics
    # 수집 완료 후 PyInstaller 작업 캐시를 제거한다.
    Remove-Item 'build' -Recurse -Force
    [ordered]@{ source_sha = $PayloadSource; payload_source_commit = $PayloadSource;
        packaging_commit = $env:GITHUB_SHA; validation_status = 'passed'; mode = 'fresh-build' } |
        ConvertTo-Json | Set-Content (Join-Path $Diagnostics 'payload-source.json') -Encoding utf8
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
        if (-not (Get-Command 'choco' -ErrorAction SilentlyContinue)) { throw 'Inno Setup 6 설치 필요 (https://jrsoftware.org/isdl.php)' }
        Invoke-Checked 'choco' @('install', 'innosetup', '--yes', '--no-progress', '--limit-output')
        $Iscc = Find-Inno
    }
    if (-not $Iscc) { throw '공식 Inno Setup compiler를 찾지 못함' }
    (Get-Item $Iscc).VersionInfo | Format-List | Out-File (Join-Path $Diagnostics 'inno-version.txt')
    $InnoArgs = @('packaging/installer.iss')
    if ($SingleInstaller) {
        $InnoArgs = @('/DSingleInstaller', "/DAppVersion=$Version", 'packaging/installer.iss')
    }
    Invoke-Checked $Iscc $InnoArgs
    $ManifestArgs = @('-m', 'scripts.release_manifest', '--root', 'release', '--version', $Version)
    if ($SingleInstaller) { $ManifestArgs += @('--single', '--payload-source', $PayloadSource) }
    Invoke-Checked $Python $ManifestArgs
    if ($SingleInstaller) {
        Copy-Item 'dist/AdPurifier/THIRD_PARTY_NOTICES.txt' 'release/'
    } else {
        Copy-Item 'requirements-windows.lock.txt', 'THIRD_PARTY_NOTICES.txt' 'release/'
    }
    Copy-Item (Join-Path $Diagnostics 'assets.lock.json') 'release/'
    Copy-Item (Join-Path $Diagnostics 'installed-assets.json') 'release/'
    Copy-Item (Join-Path $Diagnostics 'crt-manifest.json') 'release/'
    $Packaged = $true
    # 설치 시험 전 dist 삭제: 모델 두 사본 + 설치압축을 동시에 보유하지 않는다.
    Remove-Item 'dist/AdPurifier' -Recurse -Force
    $Install = Join-Path $env:LOCALAPPDATA '설치 시험/광고청정기'
    $Setup = (Resolve-Path "release/AdPurifier-$Version-Setup.exe").Path
    $InstallLog = Join-Path $Diagnostics 'install.log'
    $Installer = Start-Process -FilePath $Setup -ArgumentList @('/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART',
                         "/DIR=`"$Install`"", "/LOG=`"$InstallLog`"") -PassThru -Wait
    if ($Installer.ExitCode -ne 0) { throw "설치 실패: $($Installer.ExitCode)" }
    $Elsewhere = Join-Path $Temp '다른 작업 폴더'
    New-Item -ItemType Directory -Force $Elsewhere | Out-Null
    $EmptyCache = Join-Path $Temp ([guid]::NewGuid().ToString())
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
    if (Test-Path $Smoke) { Remove-Item $Smoke }
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
        $Report = Get-Content -Raw -Encoding UTF8 $Smoke | ConvertFrom-Json
        if ($Report.status -ne 'ok' -or -not $Report.frozen) { throw 'frozen self-test 증거 불일치' }
        # 사용자 PATH를 보존한 실제 환경도 검사한다. 깨끗한 PATH 시험만으로는
        # Scoop junction 등 사용자 경로에 의한 Gemma 시작 실패를 발견하지 못한다.
        $env:PATH = $OldPath
        $NormalSmoke = Join-Path $Diagnostics 'self-test-user-path.json'
        $Normal = Start-Process -FilePath $Exe -WorkingDirectory $Elsewhere -ArgumentList @('--self-test',
                    '--no-browser', '--self-test-output', "`"$NormalSmoke`"") -PassThru
        if (-not $Normal.WaitForExit(1200000)) {
            & "$env:SystemRoot/System32/taskkill.exe" /PID $Normal.Id /T /F
            throw '사용자 PATH 설치 스모크 시간 초과'
        }
        $Normal.Refresh()
        if ($Normal.ExitCode -ne 0 -or (Get-Content -Raw $NormalSmoke | ConvertFrom-Json).status -ne 'ok') {
            throw '사용자 PATH에서 설치 self-test 실패'
        }
        # 별도의 실제 UI 시작 검사. --no-browser로 기본 브라우저를 열지 않는다.
        $Listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, 0)
        $Listener.Start(); $Port = $Listener.LocalEndpoint.Port; $Listener.Stop()
        $UI = Start-Process -FilePath $Exe -WorkingDirectory $Elsewhere -ArgumentList @('--no-browser', '--port', "$Port") -PassThru
        $NormalExit = $false
        try {
            $Ready = $false
            for ($i = 0; $i -lt 60; $i++) {
                if ($UI.HasExited) { throw 'UI 시작 중 종료' }
                try {
                    $Status = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/status" -TimeoutSec 2
                    # -UseBasicParsing: Windows PowerShell 5.1은 없으면 IE 엔진을 찾다가 실패한다. pwsh 7에서는 무시된다.
                    $Html = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$Port/" -TimeoutSec 2
                    if ($Html.StatusCode -eq 200 -and $Status.model_available -and $Status.out_dir -eq $Install) {
                        $Status | ConvertTo-Json -Depth 20 | Write-Utf8 (Join-Path $Diagnostics 'ui-smoke.json')
                        $Ready = $true; break
                    }
                } catch { Start-Sleep -Milliseconds 500 }
            }
            if (-not $Ready) { throw 'UI HTTP smoke 실패' }
            $ShutdownResponse = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/shutdown" -Method Post `
                                -ContentType 'application/json' -Body '{}' -TimeoutSec 10
            $Shutdown = [ordered]@{ status = 'waiting'; response = $ShutdownResponse; pid = $UI.Id; exit_code = $null }
            $Shutdown | ConvertTo-Json -Depth 20 | Write-Utf8 (Join-Path $Diagnostics 'ui-shutdown.json')
            if (-not $UI.WaitForExit(30000)) { throw 'UI 정상 종료 30초 초과' }
            $UI.Refresh()
            $Shutdown.exit_code = $UI.ExitCode
            $Shutdown.status = if ($UI.ExitCode -eq 0) { 'ok' } else { 'failed' }
            $Shutdown | ConvertTo-Json -Depth 20 | Write-Utf8 (Join-Path $Diagnostics 'ui-shutdown.json')
            if ($UI.ExitCode -ne 0) { throw "UI 정상 종료 코드 불일치: $($UI.ExitCode)" }
            $NormalExit = $true
        } finally {
            # 성공 판정은 HTTP 종료 요청 + 실제 exit code 0으로만 한다.
            if (-not $NormalExit -and -not $UI.HasExited) {
                Stop-Process -Id $UI.Id -Force -ErrorAction Continue
            }
        }
    } finally { $env:PATH = $OldPath }
    $ValidationStatus = 'passed'
} finally {
    if ($Packaged) {
        [ordered]@{ source_commit = $env:GITHUB_SHA; validation_status = $ValidationStatus } |
            ConvertTo-Json | Write-Utf8 'release/validation-status.json'
        if ($Smoke -and (Test-Path $Smoke)) { Copy-Item $Smoke 'release/self-test.json' }
    }
    # 세션 키와 토큰은 진단물에 포함하지 않는다.
    $RuntimeLogs = Join-Path $env:LOCALAPPDATA 'AdPurifier/logs'
    if (Test-Path $RuntimeLogs) {
        Get-ChildItem $RuntimeLogs -Recurse -File -Filter '*.log' | ForEach-Object {
            Copy-Item $_.FullName (Join-Path $Diagnostics ($_.Directory.Name + '-' + $_.Name))
        }
    }
    Stop-Transcript
}
