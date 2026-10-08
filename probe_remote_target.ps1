param(
    [Parameter(Mandatory=$true)][string]$JobDirectory,
    [string]$OwnedProcessesFile = 'owned-processes.json',
    [string]$SummaryFile = 'output\summary.json',
    [string]$LaunchFile = 'launch.json',
    [string]$DriverExitFile = 'driver-exit.json',
    [string]$HostAlias = 'compute-server',
    [string]$JobId,
    [string]$JobName
)
# Execute on the target server. Inspect exactly this run; never enumerate runs.
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$observedAt = [DateTimeOffset]::UtcNow.ToString('o')
$job = [ordered]@{
    id=$JobId; name=$JobName; valid=$false; state=$null
    started_utc=$null; finished_utc=$null; child_exit_code=$null; wrapper_exit_code=$null
    scientific_status=$null; processes_known=$false; processes=@()
}

function Resolve-RunFile([string]$RelativePath) {
    if (-not $RelativePath -or [IO.Path]::IsPathRooted($RelativePath)) {
        throw 'Run evidence filenames must be nonempty relative paths'
    }
    $resolved = [IO.Path]::GetFullPath((Join-Path $path $RelativePath))
    if (-not $resolved.StartsWith($path + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Run evidence must remain inside JobDirectory'
    }
    return $resolved
}

function Read-RunJson([string]$RelativePath, [bool]$Required=$false) {
    $filename = Resolve-RunFile $RelativePath
    try { $raw = [IO.File]::ReadAllText($filename, [Text.Encoding]::UTF8) }
    catch [IO.FileNotFoundException] {
        if ($Required) { throw }
        return $null
    }
    catch [IO.DirectoryNotFoundException] {
        if ($Required) { throw }
        return $null
    }
    # Invalid JSON and access errors remain invalid observations, never exits.
    $data = ConvertFrom-Json -InputObject $raw
    if ($null -eq $data -or $data -is [Array] -or $data -isnot [pscustomobject]) {
        throw "Invalid evidence object: $RelativePath"
    }
    return $data
}

function Read-Integer($Value, [string]$Label, [bool]$Positive=$true) {
    if ($Value -is [bool] -or $Value -is [double] -or $Value -is [single] -or $Value -is [decimal]) {
        throw "Invalid integer $Label"
    }
    $parsed = 0L
    if (-not [long]::TryParse([string]$Value, [ref]$parsed) -or ($Positive -and $parsed -le 0)) {
        throw "Invalid integer $Label"
    }
    return $parsed
}

function Observe-Identity($Entry) {
    $processId = Read-Integer $Entry.pid 'pid'
    $ticks = Read-Integer $Entry.start_ticks 'start_ticks'
    if ($processId -gt [int]::MaxValue) { throw 'PID is out of range' }
    $processErrors = @()
    $process = Get-Process -Id ([int]$processId) -ErrorAction SilentlyContinue -ErrorVariable processErrors
    foreach ($processError in $processErrors) {
        if ($processError.FullyQualifiedErrorId -notlike 'NoProcessFoundForGivenId*') {
            throw $processError
        }
    }
    $alive = $false
    if ($process) {
        # Reading StartTime may fail with access denied or a race. Let it fail
        # closed, so an unknown identity cannot generate an exit event.
        $actualTicks = $process.StartTime.ToUniversalTime().Ticks
        $alive = ($actualTicks -eq $ticks)
    }
    return [pscustomobject]@{ identity=('{0}:{1}' -f $processId,$ticks); alive=[bool]$alive }
}

try {
    # IsPathFullyQualified is unavailable on the old server's .NET Framework.
    if ($JobDirectory -notmatch '^(?:[A-Za-z]:[\\/]|\\\\[^\\]+\\[^\\]+)') {
        throw 'Absolute JobDirectory required'
    }
    $path = [IO.Path]::GetFullPath($JobDirectory).TrimEnd('\')
    if (-not $JobId) { $job.id = 'remote:' + $HostAlias + ':' + $path }
    if (-not $JobName) { $job.name = [IO.Path]::GetFileName($path) }
    $launch = Read-RunJson $LaunchFile $true
    if (-not $launch.started_utc) { throw 'Launch started_utc missing' }
    $job.started_utc = [DateTimeOffset]::Parse($launch.started_utc).ToUniversalTime().ToString('o')
    $launchIdentity = Observe-Identity $launch
    $observations = @($launchIdentity)
    $owned = Read-RunJson $OwnedProcessesFile
    $known = $false
    if ($null -ne $owned) {
        $guardId = Read-Integer $owned.guard_pid 'guard_pid'
        if ($owned.processes -isnot [Array] -or $owned.processes.Count -eq 0) {
            throw 'Owned process identities missing'
        }
        $guardRecorded = $false
        foreach ($entry in $owned.processes) {
            $identity = Observe-Identity $entry
            if ([long]$entry.pid -eq $guardId) { $guardRecorded = $true }
            if ($observations.identity -notcontains $identity.identity) { $observations += $identity }
        }
        if (-not $guardRecorded) { throw 'guard_pid has no recorded process identity' }
        $known = $true
    }
    $summary = Read-RunJson $SummaryFile
    if ($null -ne $summary) {
        if (-not $summary.status -or $summary.status -isnot [string]) { throw 'Summary status missing' }
        $job.scientific_status = $summary.status
        $job.actual_iteration = $summary.actual_iteration
        $job.scientific_accepted = $summary.accepted
        $job.wall_deadline_hit = $summary.wall_deadline_hit
        $job.last_checkpoint = $summary.last_checkpoint
    }
    $driverExit = Read-RunJson $DriverExitFile
    if ($null -ne $driverExit) {
        if ((Read-Integer $driverExit.pid 'exit pid') -ne [long]$launch.pid) {
            throw 'Driver exit PID does not match launch'
        }
        $job.child_exit_code = Read-Integer $driverExit.exit_code 'exit_code' $false
        if (-not $driverExit.exited_utc) { throw 'Driver exit time missing' }
        $job.finished_utc = [DateTimeOffset]::Parse($driverExit.exited_utc).ToUniversalTime().ToString('o')
    }
    $job.processes = @($observations)
    $job.processes_known = $known
    $anyAlive = @($observations | Where-Object { $_.alive }).Count -gt 0
    # Summary FINISHED and exit 0 do not certify that all descendants exited.
    $job.state = if ($known -and -not $anyAlive) { 'processes_exited' } else { 'running' }
    $job.valid = $true
} catch {
    $job.valid = $false
    $job.state = $null
    $job.processes_known = $false
    $job.processes = @()
    $job.error = $_.Exception.Message
}
[pscustomobject]@{ observed_at=$observedAt; jobs=@([pscustomobject]$job) } | ConvertTo-Json -Depth 8 -Compress
