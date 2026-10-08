param([Parameter(Mandatory=$true)][string]$JobDirectory)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
if(-not [IO.Path]::IsPathFullyQualified($JobDirectory)){throw 'Absolute JobDirectory required'}
$path = [IO.Path]::GetFullPath($JobDirectory).TrimEnd('\')
$jobs = @()
$file = [IO.FileInfo]::new((Join-Path $path 'wrapper-status.json'))
    try {
        $wrapper = Get-Content -LiteralPath $file.FullName -Raw -Encoding UTF8 | ConvertFrom-Json
        if(-not $wrapper.started_utc -or -not $wrapper.state){throw 'Invalid wrapper status'}
        $ownedPath = Join-Path $path 'owned-processes.json'
        $processes = @()
        $known = Test-Path -LiteralPath $ownedPath
        if($known) {
            $owned = Get-Content -LiteralPath $ownedPath -Raw -Encoding UTF8 | ConvertFrom-Json
            if($owned.Count -eq 0){$known=$false}
            foreach($entry in $owned) {
                if(-not $entry.pid -or -not $entry.start_ticks){$known=$false;continue}
                $identity = '{0}:{1}' -f $entry.pid,$entry.start_ticks
                # Missing PID or PID reused with another creation time means this identity exited.
                $procErrors = @()
                $process = Get-Process -Id ([int]$entry.pid) -ErrorAction SilentlyContinue -ErrorVariable procErrors
                foreach($procError in $procErrors) {
                    if($procError.FullyQualifiedErrorId -notlike 'NoProcessFoundForGivenId*'){throw $procError}
                }
                $alive = $false
                if($process){$alive=($process.StartTime.ToUniversalTime().Ticks -eq [long]$entry.start_ticks)}
                $processes += [pscustomobject]@{identity=$identity;alive=$alive}
            }
        }
        $summaryStatus = $null
        $summaryPath = Join-Path $path 'output\summary.json'
        if(Test-Path -LiteralPath $summaryPath) {
            $summary = Get-Content -LiteralPath $summaryPath -Raw -Encoding UTF8 | ConvertFrom-Json
            $summaryStatus = $summary.status
        }
        $jobs += [pscustomobject]@{
            id=('local:'+$path);name=$file.Directory.Name;valid=$true;state=$wrapper.state
            started_utc=$wrapper.started_utc;finished_utc=$wrapper.finished_utc
            child_exit_code=$wrapper.child_exit_code;wrapper_exit_code=$wrapper.wrapper_exit_code
            scientific_status=$summaryStatus;processes_known=$known;processes=@($processes)
        }
    } catch {
        $jobs += [pscustomobject]@{id=('local:'+$path);name=$file.Directory.Name;valid=$false;error=$_.Exception.Message}
    }
[pscustomobject]@{observed_at=[DateTimeOffset]::UtcNow.ToString('o');jobs=@($jobs)} | ConvertTo-Json -Depth 7 -Compress
