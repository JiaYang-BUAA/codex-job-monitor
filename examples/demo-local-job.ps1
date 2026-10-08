param(
    [Parameter(Mandatory=$true)][string]$JobDirectory,
    [ValidateRange(1,3600)][int]$Seconds = 30,
    [switch]$Fail
)
# Disposable example: runs only a timed wait; does not start a solver.
$ErrorActionPreference = 'Stop'
$path = [IO.Path]::GetFullPath($JobDirectory)
if (Test-Path -LiteralPath $path) { throw 'Choose a new directory for each run; existing evidence is never overwritten.' }
New-Item -ItemType Directory -Path $path | Out-Null
function Write-JsonAtomic([string]$Name, $Value) {
    $file = Join-Path $path $Name
    $temp = $file + '.tmp'
    ConvertTo-Json -InputObject $Value -Depth 8 | Set-Content -LiteralPath $temp -Encoding utf8
    Move-Item -LiteralPath $temp -Destination $file -Force
}
$started = [DateTimeOffset]::UtcNow.ToString('o')
$self = Get-Process -Id $PID
$identity = @{pid=$PID;start_ticks=$self.StartTime.ToUniversalTime().Ticks}
Write-JsonAtomic 'owned-processes.json' @($identity)
Write-JsonAtomic 'wrapper-status.json' @{state='running';started_utc=$started}
Start-Sleep -Seconds $Seconds
$code = if ($Fail) { 1 } else { 0 }
$state = if ($Fail) { 'program_failed' } else { 'program_completed' }
Write-JsonAtomic 'wrapper-status.json' @{
    state=$state; started_utc=$started; finished_utc=[DateTimeOffset]::UtcNow.ToString('o')
    child_exit_code=$code; wrapper_exit_code=$code
}
exit $code
