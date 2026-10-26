<#
.SYNOPSIS
  Ghi lại console MỚI (conhost + chuỗi tiến trình cha) và cửa sổ console/Windows Terminal HIỆN ra trong N giây.

.DESCRIPTION
  Vỏ PowerShell của scripts/win_console_watch.py (phần đo viết bằng Python + ctypes để chạy được cả từ Git Bash và test):
  Toolhelp32 + dòng lệnh từ PEB cho tiến trình mới, AttachConsole/GetConsoleWindow qua một tiến trình phụ DETACHED để biết
  console có cửa sổ hiện hay không (lớp PseudoConsoleWindow = tab/cửa sổ Windows Terminal). Chỉ đọc, không đụng tiến trình nào.
  Xem docs/dev-notes/windows.md §6.

.EXAMPLE
  pwsh scripts/win_console_watch.ps1 -Seconds 40
  pwsh scripts/win_console_watch.ps1 -Seconds 60 -Match cfharness -Probe
#>
param(
    [double]$Seconds = 40,
    [string]$Match = "",
    [switch]$Probe  # in kết quả hỏi console của từng tiến trình mới (-v)
)
$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$py = Join-Path $repo '.venv\Scripts\python.exe'
if (-not (Test-Path $py)) { $py = 'python' }
$pyArgs = @((Join-Path $PSScriptRoot 'win_console_watch.py'), $Seconds.ToString([cultureinfo]::InvariantCulture))
if ($Match) { $pyArgs += @('--match', $Match) }
if ($Probe) { $pyArgs += '-v' }
& $py @pyArgs
exit $LASTEXITCODE
