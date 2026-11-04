# install/install.ps1
# 하우스캐치 블로그 도구를 이 PC에 설치합니다. Windows PowerShell 5.1에서 돕니다.
# 원본은 이 파일이 든 install 폴더의 부모 폴더이고, 원본과 설치 폴더(-Target)가 같으면 복사를 건너뜁니다.
# 레포 맨 위 install.ps1(irm | iex)이 받은 판을 임시 폴더에 풀어 이 파일을 부르고, 압축 파일의 설치하기.cmd도 이 파일을 부릅니다.
# 이 파일은 UTF-8(BOM)로 저장합니다. BOM이 없으면 Windows PowerShell 5.1이 파일 속 한글을 잘못 읽습니다.
#
# 매개변수(-SkipTools, -NoShortcuts와 경로 셋은 시험용입니다)
#   -Target      설치 폴더. 기본은 %USERPROFILE%\housecatch이고, 그 경로에 공백이나 특수 문자가 있으면 C:\housecatch입니다.
#   -SkillDir    /housecatch 스킬 폴더. 기본은 %USERPROFILE%\.claude\skills\housecatch입니다.
#   -DesktopDir  바로가기를 만들 폴더. 기본은 바탕 화면입니다(OneDrive로 옮긴 바탕 화면도 따라갑니다).
#   -SkipTools   Claude Code CLI 설치와 사용자 PATH 고침을 건너뜁니다.
#   -NoShortcuts 바탕 화면 바로가기를 만들지 않습니다.
#   -Version     설치 폴더의 .housecatch-version에 적을 판 번호. 맨 위 install.ps1이 넘깁니다.
param(
    [string]$Target = '',
    [string]$SkillDir = '',
    [string]$DesktopDir = '',
    [switch]$SkipTools,
    [switch]$NoShortcuts,
    [string]$Version = ''
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
try { [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false } catch { }
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

# 설치 때 받는 파이썬. SHA256은 python.org가 함께 올린 sigstore 묶음의 값과 같습니다. 판을 바꾸면 값도 함께 바꿉니다.
$PythonVersion = '3.13.17'
$PythonSha256 = '9e35319bffedab8b36ad44d8f377cb4f1733bcec257a0b6d04d94f01486ddb38'

# 복사 규칙. install/update.py의 KEEP, MIRROR와 같아야 합니다.
#   보존: 설치 폴더에 이미 있으면 덮지 않습니다.
#   미러: 원본에 없는 파일을 설치 폴더에서 지웁니다(__pycache__는 둡니다).
#   나머지: 덮어씁니다.
$KeepPaths = @('블로그', 'python', 'durumi\runs', 'durumi\brands\housecatch\topics.json', 'durumi\brands\housecatch\topics-done.json', '.housecatch-version')
$MirrorDirs = @('durumi\tools', 'durumi\nodes', 'durumi\rules', 'durumi\context', 'install', '.claude\hooks')

function Write-Step([string]$Text) { Write-Host ''; Write-Host $Text -ForegroundColor Cyan }
function Write-Note([string]$Text) { Write-Host "  $Text" }
function Write-Caution([string]$Text) { Write-Host "  $Text" -ForegroundColor Yellow }

function Resolve-FullPath([string]$Path) {
    $full = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Path)
    return [IO.Path]::GetFullPath($full).TrimEnd('\')
}

# /housecatch 명령은 설치 경로를 따옴표 없이 씁니다. 공백이나 특수 문자가 있으면 명령이 깨집니다.
function Test-SafePath([string]$Path) {
    return $Path -match '^[A-Za-z]:(\\[\p{L}\p{Nd}_.\-]+)+$'
}

function Write-Utf8NoBom([string]$Path, [string]$Text) {
    [IO.File]::WriteAllText($Path, $Text, (New-Object System.Text.UTF8Encoding $false))
}

function Test-Under([string]$Rel, [string]$Root) {
    return ($Rel -ieq $Root) -or $Rel.StartsWith($Root + '\', [StringComparison]::OrdinalIgnoreCase)
}

function Test-InCache([string]$Rel) {
    foreach ($part in $Rel.Split('\')) { if ($part -ieq '__pycache__') { return $true } }
    return $false
}

function Clear-ReadOnly([string]$Path) {
    $attr = [IO.File]::GetAttributes($Path)
    if ($attr -band [IO.FileAttributes]::ReadOnly) {
        [IO.File]::SetAttributes($Path, $attr -bxor [IO.FileAttributes]::ReadOnly)
    }
}

# 원본을 복사 규칙대로 설치 폴더에 놓습니다. 놓은 파일 수, 지운 파일 수, 보존한 항목을 돌려줍니다.
function Copy-ByRules([string]$Src, [string]$Dst) {
    # 보존 여부는 복사 전에 한 번만 봅니다. 처음 설치에서 블로그\의 첫 파일을 놓은 뒤 나머지를 건너뛰지 않게 하려는 것입니다.
    $kept = @($KeepPaths | Where-Object { Test-Path -LiteralPath (Join-Path $Dst $_) })
    $copied = 0
    foreach ($file in [IO.Directory]::EnumerateFiles($Src, '*', [IO.SearchOption]::AllDirectories)) {
        $rel = $file.Substring($Src.Length + 1)
        if (Test-InCache $rel) { continue }
        if ($rel.Split('\')[0] -ieq 'dist') { continue }
        $skip = $false
        foreach ($k in $kept) { if (Test-Under $rel $k) { $skip = $true; break } }
        if ($skip) { continue }
        $dest = Join-Path $Dst $rel
        [void][IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($dest))
        if ([IO.File]::Exists($dest)) { Clear-ReadOnly $dest }
        [IO.File]::Copy($file, $dest, $true)
        $copied++
    }
    $removed = 0
    foreach ($m in $MirrorDirs) {
        $dm = Join-Path $Dst $m
        if (-not [IO.Directory]::Exists($dm)) { continue }
        foreach ($file in @([IO.Directory]::GetFiles($dm, '*', [IO.SearchOption]::AllDirectories))) {
            $rel = $file.Substring($Dst.Length + 1)
            if (Test-InCache $rel) { continue }
            if (-not [IO.File]::Exists((Join-Path $Src $rel))) {
                Clear-ReadOnly $file
                [IO.File]::Delete($file)
                $removed++
            }
        }
        # 원본에 없는 빈 폴더를 깊은 것부터 지웁니다. 미러 폴더 자체도 원본에 없고 비었으면 지웁니다.
        $dirs = @([IO.Directory]::GetDirectories($dm, '*', [IO.SearchOption]::AllDirectories)) + @($dm) | Sort-Object -Property Length -Descending
        foreach ($d in $dirs) {
            $rel = $d.Substring($Dst.Length + 1)
            if (Test-InCache $rel) { continue }
            if ([IO.Directory]::Exists((Join-Path $Src $rel))) { continue }
            if (@([IO.Directory]::GetFileSystemEntries($d)).Count -eq 0) { [IO.Directory]::Delete($d) }
        }
    }
    return @($copied, $removed, $kept)
}

# claude 실행 파일을 찾습니다. 공식 설치 위치(.local\bin)를 먼저 보고, 없으면 PATH에서 찾습니다.
function Find-Claude {
    $native = Join-Path $env:USERPROFILE '.local\bin\claude.exe'
    if ([IO.File]::Exists($native)) { return $native }
    $found = Get-Command claude -CommandType Application, ExternalScript -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($found) {
        $path = $found.Source
        # npm으로 깐 claude.ps1은 실행 정책에 막힐 수 있어 같은 자리의 claude.cmd를 씁니다.
        if ($path -like '*.ps1') {
            $cmd = [IO.Path]::ChangeExtension($path, '.cmd')
            if ([IO.File]::Exists($cmd)) { $path = $cmd }
        }
        return $path
    }
    return $null
}

# 사용자 PATH(레지스트리 HKCU\Environment)에 폴더를 더합니다. 이미 있으면 그대로 둡니다.
# [Environment]::SetEnvironmentVariable은 %VAR%가 든 값을 풀어서 REG_SZ로 바꿔 써 버리므로 레지스트리를 직접 씁니다.
#   -SubKey와 -Quiet는 시험용입니다(다른 키에 쓰고, 바뀜 알림을 보내지 않음).
function Add-UserPath([string]$Dir, [string]$SubKey = 'Environment', [switch]$Quiet) {
    $key = [Microsoft.Win32.Registry]::CurrentUser.CreateSubKey($SubKey)
    try {
        $raw = [string]$key.GetValue('Path', '', [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
        foreach ($entry in $raw.Split(';')) {
            if ($entry -and ([Environment]::ExpandEnvironmentVariables($entry).TrimEnd('\') -ieq $Dir.TrimEnd('\'))) { return $false }
        }
        if ($raw) { $kind = $key.GetValueKind('Path') } else { $kind = [Microsoft.Win32.RegistryValueKind]::ExpandString }
        if ($raw) { $value = $raw.TrimEnd(';') + ';' + $Dir } else { $value = $Dir }
        $key.SetValue('Path', $value, $kind)
    } finally {
        $key.Close()
    }
    # 탐색기와 새로 여는 창이 바뀐 PATH를 읽도록 WM_SETTINGCHANGE를 알립니다.
    if ($Quiet) { return $true }
    try {
        if (-not ('Housecatch.Native' -as [type])) {
            Add-Type -Namespace Housecatch -Name Native -MemberDefinition '[DllImport("user32.dll", CharSet = CharSet.Unicode, SetLastError = true)] public static extern IntPtr SendMessageTimeout(IntPtr hWnd, uint Msg, UIntPtr wParam, string lParam, uint fuFlags, uint uTimeout, out UIntPtr lpdwResult);'
        }
        $ignored = [UIntPtr]::Zero
        [void][Housecatch.Native]::SendMessageTimeout([IntPtr]0xffff, 0x001A, [UIntPtr]::Zero, 'Environment', 0x0002, 5000, [ref]$ignored)
    } catch { }
    return $true
}

function Install-Python([string]$PyDir) {
    $url = "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-embed-amd64.zip"
    $tmp = Join-Path ([IO.Path]::GetTempPath()) ('housecatch-python-' + [guid]::NewGuid().ToString('N').Substring(0, 8))
    [void][IO.Directory]::CreateDirectory($tmp)
    try {
        $zip = Join-Path $tmp 'python.zip'
        try {
            Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile $zip
        } catch {
            throw "python.org에서 파이썬을 받지 못했습니다. 인터넷 연결을 확인한 뒤 다시 실행해 주세요. ($($_.Exception.Message))"
        }
        $hash = (Get-FileHash -LiteralPath $zip -Algorithm SHA256).Hash
        if ($hash -ine $PythonSha256) {
            throw "받은 파이썬 파일이 python.org가 공개한 값과 다릅니다. 다시 실행해 주세요. (SHA256 $hash)"
        }
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        $unzipped = Join-Path $tmp 'x'
        [IO.Compression.ZipFile]::ExtractToDirectory($zip, $unzipped)
        [void][IO.Directory]::CreateDirectory($PyDir)
        foreach ($file in [IO.Directory]::GetFiles($unzipped, '*', [IO.SearchOption]::AllDirectories)) {
            $dest = Join-Path $PyDir $file.Substring($unzipped.Length + 1)
            [void][IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($dest))
            [IO.File]::Copy($file, $dest, $true)
        }
    } finally {
        Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
    }
}

try {
    $Source = Resolve-FullPath (Split-Path -Parent $PSScriptRoot)
    if (-not $Target) {
        $Target = Join-Path $env:USERPROFILE 'housecatch'
        if (-not (Test-SafePath $Target)) {
            Write-Caution "사용자 폴더 경로($env:USERPROFILE)에 공백이나 특수 문자가 있어, 설치 폴더를 아래로 바꿉니다."
            $Target = Join-Path $env:SystemDrive 'housecatch'
        }
    }
    $Target = Resolve-FullPath $Target
    if (-not (Test-SafePath $Target)) {
        throw "설치 폴더 경로($Target)에 공백이나 특수 문자가 있습니다. /housecatch 명령이 깨지니, 공백과 특수 문자가 없는 경로를 -Target으로 주세요."
    }
    if (-not $SkillDir) { $SkillDir = Join-Path $env:USERPROFILE '.claude\skills\housecatch' }
    if (-not $DesktopDir) { $DesktopDir = [Environment]::GetFolderPath('Desktop') }
    $SkillDir = Resolve-FullPath $SkillDir
    $DesktopDir = Resolve-FullPath $DesktopDir
    $blog = Join-Path $Target '블로그'

    Write-Host '하우스캐치 블로그 도구를 설치합니다.'
    Write-Note $Target

    # 두루미는 Claude Code CLI(claude -p)로 글을 씁니다. Claude 앱만 깐 PC에는 CLI가 없으니 공식 설치기로 깝니다.
    Write-Step '[1/6] Claude Code CLI를 확인합니다.'
    $localBin = Join-Path $env:USERPROFILE '.local\bin'
    if ($SkipTools) {
        Write-Note '건너뜁니다(-SkipTools).'
    } else {
        if (Find-Claude) {
            Write-Note 'Claude Code CLI가 있습니다.'
        } else {
            Write-Note 'Claude Code CLI가 없어 공식 설치기(https://claude.ai/install.ps1)로 설치합니다.'
            # 공식 설치기는 실패하면 exit로 끝나므로, 이 설치기가 함께 끝나지 않게 따로 띄웁니다.
            $ErrorActionPreference = 'Continue'
            & (Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe') -NoProfile -ExecutionPolicy Bypass -Command '[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; irm https://claude.ai/install.ps1 | iex'
            $ErrorActionPreference = 'Stop'
            if (-not (Find-Claude)) {
                throw 'Claude Code CLI를 설치하지 못했습니다. 인터넷 연결을 확인한 뒤 다시 실행해 주세요.'
            }
            Write-Note 'Claude Code CLI를 설치했습니다.'
        }
        # 공식 설치기는 PATH를 고치지 않습니다. 새로 여는 창에서 claude가 보이도록 사용자 PATH에 더합니다.
        if (Add-UserPath $localBin) {
            Write-Note "사용자 PATH에 $localBin 을 더했습니다."
        }
        if (-not (($env:Path -split ';') | Where-Object { $_.TrimEnd('\') -ieq $localBin })) {
            $env:Path = $localBin + ';' + $env:Path
        }
    }

    Write-Step '[2/6] 파일을 설치 폴더에 놓습니다.'
    if ($Source -ieq $Target) {
        Write-Note '설치 폴더에서 실행해 복사는 건너뜁니다.'
    } else {
        [void][IO.Directory]::CreateDirectory($Target)
        $result = Copy-ByRules $Source $Target
        Write-Note ('파일 {0}개를 놓았고, 새 판에 없는 파일 {1}개를 지웠습니다.' -f $result[0], $result[1])
        if (@($result[2]).Count -gt 0) {
            Write-Note ('이미 있던 것은 그대로 두었습니다. ' + (@($result[2]) -join ', '))
        }
    }
    $versionFile = Join-Path $Target '.housecatch-version'
    if ($Version) {
        Write-Utf8NoBom $versionFile $Version
    } elseif (($Source -ine $Target) -and [IO.File]::Exists((Join-Path $Source '.housecatch-version'))) {
        [IO.File]::Copy((Join-Path $Source '.housecatch-version'), $versionFile, $true)
    }
    # 인터넷에서 받은 압축을 탐색기로 풀면 파일마다 차단 표시가 붙습니다. 설치 폴더 전체에서 지웁니다.
    Get-ChildItem -LiteralPath $Target -Recurse -File -Force -ErrorAction SilentlyContinue | Unblock-File -ErrorAction SilentlyContinue

    Write-Step '[3/6] 블로그 폴더를 마련합니다.'
    foreach ($name in @('발행 전', '발행 완료')) {
        [void][IO.Directory]::CreateDirectory((Join-Path $blog $name))
    }
    Write-Note $blog

    Write-Step '[4/6] 파이썬을 확인합니다.'
    $pyDir = Join-Path $Target 'python'
    $py = Join-Path $pyDir 'python.exe'
    if (-not [IO.File]::Exists($py)) {
        Write-Note "python.org에서 파이썬($PythonVersion)을 받습니다. 11MB쯤 됩니다."
        Install-Python $pyDir
        Write-Note '파이썬을 설치 폴더에 놓았습니다.'
    }
    $engine = Join-Path $Target 'durumi\tools\durumi.py'
    if (-not [IO.File]::Exists($engine)) {
        Write-Caution '두루미 엔진(durumi\tools\durumi.py)이 없어 시험하지 못했습니다.'
    } else {
        $ErrorActionPreference = 'Continue'
        & $py -X utf8 $engine --help *> $null
        $code = $LASTEXITCODE
        $ErrorActionPreference = 'Stop'
        if ($code -eq 0) {
            Write-Note '파이썬과 두루미 엔진이 돌아갑니다.'
        } else {
            Write-Caution "두루미 엔진 시험이 실패했습니다(종료 코드 $code). 설치는 이어서 합니다."
        }
    }

    Write-Step '[5/6] /housecatch 명령을 등록합니다.'
    $template = Join-Path $Target 'install\skill\SKILL.md'
    if (-not [IO.File]::Exists($template)) {
        throw "스킬 틀(install\skill\SKILL.md)이 없습니다."
    }
    $text = [IO.File]::ReadAllText($template, [Text.Encoding]::UTF8)
    $text = $text.Replace('{{ROOT}}', $Target.Replace('\', '/'))
    [void][IO.Directory]::CreateDirectory($SkillDir)
    $skillFile = Join-Path $SkillDir 'SKILL.md'
    Write-Utf8NoBom $skillFile $text
    Write-Note $skillFile

    Write-Step '[6/6] 바탕 화면 바로가기를 만듭니다.'
    if ($NoShortcuts) {
        Write-Note '건너뜁니다(-NoShortcuts).'
    } else {
        [void][IO.Directory]::CreateDirectory($DesktopDir)
        $shell = New-Object -ComObject WScript.Shell
        $folderLink = $shell.CreateShortcut((Join-Path $DesktopDir '하우스캐치 블로그.lnk'))
        $folderLink.TargetPath = $blog
        $folderLink.Description = '하우스캐치 블로그 글이 쌓이는 폴더'
        $folderLink.Save()
        # PATH에 기대지 않도록 claude를 설치 때 찾은 절대 경로로 부릅니다. 경로는 작은따옴표로 감쌉니다.
        $claudePath = Find-Claude
        if (-not $claudePath) { $claudePath = Join-Path $localBin 'claude.exe' }
        $quotedTarget = $Target.Replace("'", "''")
        $quotedClaude = $claudePath.Replace("'", "''")
        $command = "Set-Location -LiteralPath '$quotedTarget'; & '$quotedClaude' '/housecatch'"
        $writeLink = $shell.CreateShortcut((Join-Path $DesktopDir '하우스캐치 블로그 쓰기.lnk'))
        $writeLink.TargetPath = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
        $writeLink.Arguments = "-NoExit -Command `"$command`""
        $writeLink.WorkingDirectory = $Target
        $writeLink.Description = '하우스캐치 블로그 글을 씁니다'
        $writeLink.Save()
        Write-Note '하우스캐치 블로그, 하우스캐치 블로그 쓰기를 만들었습니다.'
    }

    Write-Host ''
    Write-Host '설치를 마쳤습니다.' -ForegroundColor Green
    Write-Host ' - Claude Code를 완전히 닫았다가 다시 열어야 /housecatch가 보입니다.'
    Write-Host ' - 처음 한 번은 바탕 화면의 하우스캐치 블로그 쓰기를 열어 Claude 로그인을 마쳐 주세요. Claude 앱에서 /housecatch를 쓰시더라도 이 로그인은 한 번 필요합니다. 폴더 신뢰 확인도 처음 한 번 나옵니다.'
    Write-Host " - 그다음부터는 '하우스캐치 블로그 쓰기'를 두 번 누르면 블로그 다섯 편을 한꺼번에 씁니다. 아무 폴더에서나 Claude Code를 열고 /housecatch를 쳐도 됩니다."
    Write-Host " - 글은 바탕 화면 '하우스캐치 블로그' 폴더의 '발행 전'에 쌓입니다. 다 올린 글은 '발행 완료'로 옮겨 두면 됩니다."
    Write-Host "   $blog\발행 전"
    exit 0
} catch {
    Write-Host ''
    Write-Host '설치를 마치지 못했습니다.' -ForegroundColor Red
    Write-Host ('  ' + $_.Exception.Message) -ForegroundColor Red
    exit 1
}
