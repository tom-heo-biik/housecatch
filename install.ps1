# install.ps1
# 하우스캐치 블로그 도구 설치의 첫 단계입니다. PowerShell에서 아래 한 줄로 돌립니다.
#   irm https://raw.githubusercontent.com/tom-heo-biik/housecatch/main/install.ps1 | iex
# GitHub main의 최신 판을 받아 임시 폴더에 풀고, 그 안의 install\install.ps1에 나머지를 맡깁니다.
# 복사 규칙, 판 기록(.housecatch-version), 파이썬, /housecatch 등록, 바탕 화면 바로가기가 모두 그쪽에 있습니다.
#
# 이 파일은 BOM 없이 저장합니다. Windows PowerShell 5.1의 irm | iex는 BOM이 붙은 첫 줄을 명령 이름으로 읽어 멈춥니다.
# 파일로 시험할 때는 UTF-8로 읽어서 돌립니다(-File로 돌리면 5.1이 한글을 잘못 읽습니다).
#   & ([scriptblock]::Create([IO.File]::ReadAllText('install.ps1', [Text.Encoding]::UTF8))) -ZipPath 받은.zip -Sha 판
#
# 매개변수는 모두 시험용입니다. -Target, -SkillDir, -DesktopDir, -SkipTools, -NoShortcuts는 install\install.ps1에 그대로 넘기고,
# -ZipPath는 GitHub에서 받는 대신 쓸 압축 파일, -Sha는 그 판 번호입니다(주면 GitHub API를 부르지 않습니다).
param(
    [string]$Target = '',
    [string]$SkillDir = '',
    [string]$DesktopDir = '',
    [switch]$SkipTools,
    [switch]$NoShortcuts,
    [string]$ZipPath = '',
    [string]$Sha = ''
)

# irm | iex는 지금 세션에서 돌므로, 설정 바꿈이 사용자 세션에 남지 않게 블록 안에서 돌립니다.
& {
    $ErrorActionPreference = 'Stop'
    $ProgressPreference = 'SilentlyContinue'
    try { [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false } catch { }
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

    $repo = 'tom-heo-biik/housecatch'
    $agent = 'housecatch-install'

    # /housecatch 명령은 설치 경로를 따옴표 없이 씁니다. 공백이나 특수 문자가 있으면 명령이 깨집니다.
    if (-not $Target) {
        $Target = Join-Path $env:USERPROFILE 'housecatch'
        if ($Target -notmatch '^[A-Za-z]:(\\[\p{L}\p{Nd}_.\-]+)+$') {
            Write-Host "사용자 폴더 경로($env:USERPROFILE)에 공백이나 특수 문자가 있어, 설치 폴더를 아래로 바꿉니다." -ForegroundColor Yellow
            $Target = Join-Path $env:SystemDrive 'housecatch'
            Write-Host "  $Target"
        }
    }
    $Target = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Target)

    Write-Host '하우스캐치 블로그 도구의 최신 판을 받습니다.'
    if (-not $Sha) {
        if ($ZipPath) {
            $Sha = 'local'
        } else {
            try {
                $commit = Invoke-RestMethod -UseBasicParsing -UserAgent $agent -Uri "https://api.github.com/repos/$repo/commits/main"
                $Sha = [string]$commit.sha
            } catch {
                throw "GitHub에서 최신 판 정보를 받지 못했습니다. 인터넷 연결을 확인한 뒤 다시 실행해 주세요. ($($_.Exception.Message))"
            }
            if ($Sha -notmatch '^[0-9a-f]{40}$') {
                throw "GitHub이 알려 준 판 번호를 읽지 못했습니다. ($Sha)"
            }
        }
    }

    $work = Join-Path ([IO.Path]::GetTempPath()) ('housecatch-install-' + [guid]::NewGuid().ToString('N').Substring(0, 8))
    [void][IO.Directory]::CreateDirectory($work)
    try {
        if ($ZipPath) {
            $zip = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($ZipPath)
        } else {
            $zip = Join-Path $work 'housecatch.zip'
            try {
                Invoke-WebRequest -UseBasicParsing -UserAgent $agent -Uri "https://codeload.github.com/$repo/zip/$Sha" -OutFile $zip
            } catch {
                throw "GitHub에서 판을 받지 못했습니다. 인터넷 연결을 확인한 뒤 다시 실행해 주세요. ($($_.Exception.Message))"
            }
        }
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        $unzipped = Join-Path $work 'x'
        [IO.Compression.ZipFile]::ExtractToDirectory($zip, $unzipped)
        # GitHub 압축은 맨 위에 housecatch-<판> 폴더가 하나 있습니다. 그 한 겹을 벗깁니다.
        $top = @(Get-ChildItem -LiteralPath $unzipped -Force)
        if ($top.Count -eq 1 -and $top[0].PSIsContainer) { $source = $top[0].FullName } else { $source = $unzipped }
        $inner = Join-Path $source 'install\install.ps1'
        if (-not (Test-Path -LiteralPath $inner)) {
            throw '받은 판에 install\install.ps1이 없습니다.'
        }
        Write-Host ('최신 판({0})을 받아 풀었습니다.' -f $Sha.Substring(0, [Math]::Min(7, $Sha.Length)))

        # 실행 정책이 막지 않도록 새 PowerShell에서 Bypass로 돌립니다.
        $arguments = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $inner, '-Target', $Target, '-Version', $Sha)
        if ($SkillDir) { $arguments += @('-SkillDir', $SkillDir) }
        if ($DesktopDir) { $arguments += @('-DesktopDir', $DesktopDir) }
        if ($SkipTools) { $arguments += '-SkipTools' }
        if ($NoShortcuts) { $arguments += '-NoShortcuts' }
        $powershell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
        $ErrorActionPreference = 'Continue'
        & $powershell @arguments
        $code = $LASTEXITCODE
        $ErrorActionPreference = 'Stop'
        if ($code -ne 0) {
            throw "설치를 마치지 못했습니다(종료 코드 $code). 위의 안내를 확인해 주세요."
        }
    } finally {
        Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue
    }
}
