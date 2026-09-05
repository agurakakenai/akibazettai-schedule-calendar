[CmdletBinding()]
param(
    [ValidateSet('Install', 'Uninstall', 'Run', 'Stop', 'Status', 'Plan', 'Functions')]
    [string]$Action = 'Status',
    [string]$RepoRoot,
    [string]$PythonwPath,
    [ValidateSet('Hourly', 'TwiceDaily')]
    [string]$Cadence
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$script:CollectorOwner = 'akibazettai-schedule-calendar:scheduled-collector:v1'
$script:CollectorToolsRoot = $PSScriptRoot
if (-not $RepoRoot) { $RepoRoot = Split-Path -Parent $PSScriptRoot }

function Get-CollectorIdentity {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    if ($principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'Use a non-elevated PowerShell process. Elevation is not supported.'
    }
    [pscustomobject]@{ UserSid = $identity.User.Value; UserName = $identity.Name }
}

function Get-CollectorPaths {
    param([string]$UserSid)
    $hash = [Security.Cryptography.SHA256]::Create()
    try {
        $suffix = -join ($hash.ComputeHash([Text.Encoding]::UTF8.GetBytes(
            "$script:CollectorOwner|$UserSid"))[0..5] |
            ForEach-Object { $_.ToString('x2') })
    } finally {
        $hash.Dispose()
    }
    $base = Join-Path $env:LOCALAPPDATA "AkibazettaiScheduleCollector\$suffix"
    [pscustomobject]@{
        DataRoot = $base
        ConfigPath = Join-Path $base 'collector-config.json'
        LauncherPath = Join-Path $base 'collector-launcher.pyw'
        TaskName = "AkibazettaiScheduleCollector-$suffix"
    }
}

function Get-CollectorArguments {
    param([object]$Paths)
    return ('"{0}" --config "{1}"' -f $Paths.LauncherPath, $Paths.ConfigPath)
}

function Get-CollectorDescription {
    param([object]$Config)
    return "Owner=$script:CollectorOwner; UserSid=$($Config.userSid); InstallationId=$($Config.installationId); Managed by tools\setup-collector-schedule.ps1"
}

function Read-CollectorConfig {
    param([object]$Paths, [string]$UserSid)
    if (-not (Test-Path -LiteralPath $Paths.ConfigPath -PathType Leaf)) {
        if ((Test-Path -LiteralPath $Paths.DataRoot) -and
            @(Get-ChildItem -LiteralPath $Paths.DataRoot -Force).Count -gt 0) {
            throw 'Unowned nonempty collector directory; refusing to modify it.'
        }
        return $null
    }
    $config = Get-Content -LiteralPath $Paths.ConfigPath -Raw -Encoding UTF8 |
        ConvertFrom-Json
    if ($config.owner -cne $script:CollectorOwner -or
        $config.schemaVersion -ne 1 -or $config.userSid -cne $UserSid -or
        -not $config.installationId -or
        $config.dataRoot -ine $Paths.DataRoot -or
        $config.taskName -cne $Paths.TaskName -or
        $config.launcherPath -ine $Paths.LauncherPath) {
        throw 'Unowned or inconsistent collector configuration; refusing to modify it.'
    }
    foreach ($entry in @{
        snapshotPath = 'observed-shifts.json'; reportPath = 'last-run.json'
        statusPath = 'launcher-status.json'; logPath = 'collector.log'
    }.GetEnumerator()) {
        if ($config.($entry.Key) -ine (Join-Path $Paths.DataRoot $entry.Value)) {
            throw 'Inconsistent collector data paths; refusing to modify them.'
        }
    }
    if ($config.publishPath -ine (Join-Path $config.repoRoot 'data\observed-shifts.json')) {
        throw 'Inconsistent collector publish path; refusing to modify it.'
    }
    return $config
}

function Get-CollectorTask {
    param([object]$Paths)
    # Enumerating the root avoids suppressing access-denied errors as "not found".
    $tasks = @(Get-ScheduledTask -TaskPath '\' |
        Where-Object { $_.TaskName -ieq $Paths.TaskName })
    if ($tasks.Count -gt 1) { throw 'Ambiguous collector task.' }
    if ($tasks.Count -eq 1) { return $tasks[0] }
    return $null
}

function Assert-CollectorTaskOwned {
    param([object]$Task, [object]$Config, [object]$Paths)
    if ($null -eq $Task) { return }
    if ($null -eq $Config) { throw 'Existing task has no owned configuration.' }
    $taskSid = [string]$Task.Principal.UserId
    if ($taskSid -notlike 'S-1-*') {
        try {
            $account = New-Object Security.Principal.NTAccount($taskSid)
            $taskSid = $account.Translate(
                [Security.Principal.SecurityIdentifier]).Value
        } catch {
            throw 'Cannot verify task principal ownership.'
        }
    }
    $actions = @($Task.Actions)
    if ($Task.TaskPath -cne '\' -or
        $Task.Description -cne (Get-CollectorDescription $Config) -or
        $taskSid -cne $Config.userSid -or
        [string]$Task.Principal.LogonType -cne 'Interactive' -or
        [string]$Task.Principal.RunLevel -cne 'Limited' -or
        $actions.Count -ne 1 -or
        $actions[0].Execute -ine $Config.pythonwPath -or
        $actions[0].Arguments -cne (Get-CollectorArguments $Paths) -or
        $actions[0].WorkingDirectory -ine $Paths.DataRoot) {
        throw 'Existing task description, principal, or action is not owned; refusing.'
    }
}

function Resolve-CollectorPythonw {
    param([string]$RequestedPath)
    if ($RequestedPath) {
        $candidate = $RequestedPath
    } else {
        $py = Get-Command py.exe -ErrorAction Stop
        $start = New-Object Diagnostics.ProcessStartInfo
        $start.FileName = $py.Source
        $start.Arguments = '-c "import sys; print(sys.executable)"'
        $start.UseShellExecute = $false
        $start.CreateNoWindow = $true
        $start.RedirectStandardOutput = $true
        $start.RedirectStandardError = $true
        $process = New-Object Diagnostics.Process
        $process.StartInfo = $start
        try {
            [void]$process.Start()
            $output = $process.StandardOutput.ReadToEnd().Trim()
            $errorText = $process.StandardError.ReadToEnd()
            $process.WaitForExit()
            if ($process.ExitCode -ne 0 -or -not $output) {
                throw "Cannot resolve Python executable: $errorText"
            }
            $candidate = Join-Path (Split-Path -Parent $output) 'pythonw.exe'
        } finally {
            $process.Dispose()
        }
    }
    $candidate = [IO.Path]::GetFullPath($candidate)
    if (-not (Test-Path -LiteralPath $candidate -PathType Leaf) -or
        [IO.Path]::GetFileName($candidate) -ine 'pythonw.exe') {
        throw 'An existing pythonw.exe is required; console executables are not allowed.'
    }
    return $candidate
}

function Resolve-CollectorCadence {
    param([string]$Requested, [object]$Previous)
    if ($Requested) {
        if ($Requested -notin @('Hourly', 'TwiceDaily')) {
            throw 'Cadence must be Hourly or TwiceDaily.'
        }
        return $Requested
    }
    if ($null -ne $Previous) {
        if ($null -ne $Previous.PSObject.Properties['scheduleCadence']) {
            if ($Previous.scheduleCadence -notin @('Hourly', 'TwiceDaily')) {
                throw 'Invalid saved collector cadence.'
            }
            return $Previous.scheduleCadence
        }
        return 'TwiceDaily'
    }
    return 'Hourly'
}

function New-CollectorConfig {
    param([object]$Paths, [object]$Identity, [string]$Root,
          [string]$Pythonw, [object]$Previous, [string]$RequestedCadence)
    $installationId = [guid]::NewGuid().ToString()
    if ($null -ne $Previous) { $installationId = $Previous.installationId }
    $effectiveCadence = Resolve-CollectorCadence $RequestedCadence $Previous
    $times = @('13:30', '19:30')
    if ($effectiveCadence -eq 'Hourly') {
        $times = @(0..23 | ForEach-Object { '{0:00}:00' -f $_ })
    }
    [pscustomobject]@{
        schemaVersion = 1
        owner = $script:CollectorOwner
        installationId = $installationId
        userSid = $Identity.UserSid
        taskName = $Paths.TaskName
        dataRoot = $Paths.DataRoot
        launcherPath = $Paths.LauncherPath
        pythonwPath = $Pythonw
        repoRoot = $Root
        snapshotPath = Join-Path $Paths.DataRoot 'observed-shifts.json'
        publishPath = Join-Path $Root 'data\observed-shifts.json'
        reportPath = Join-Path $Paths.DataRoot 'last-run.json'
        statusPath = Join-Path $Paths.DataRoot 'launcher-status.json'
        logPath = Join-Path $Paths.DataRoot 'collector.log'
        scheduleTimeZone = 'UTC+09:00'
        scheduleCadence = $effectiveCadence
        scheduleTimes = $times
    }
}

function New-CollectorTaskXml {
    param([object]$Config, [object]$Paths,
          [datetimeoffset]$Now = [datetimeoffset]::Now)
    $jst = $Now.ToOffset([timespan]::FromHours(9))
    $date = $jst.ToString('yyyy-MM-dd')
    $escape = { param($Value) [Security.SecurityElement]::Escape([string]$Value) }
    $description = & $escape (Get-CollectorDescription $Config)
    $sid = & $escape $Config.userSid
    $exe = & $escape $Config.pythonwPath
    $arguments = & $escape (Get-CollectorArguments $Paths)
    $directory = & $escape $Paths.DataRoot
    $effectiveCadence = Resolve-CollectorCadence '' $Config
    if ($effectiveCadence -eq 'Hourly') {
        $nextHour = $jst.AddTicks(-($jst.Ticks % [timespan]::TicksPerHour)).AddHours(1)
        $boundary = $nextHour.ToString("yyyy-MM-dd'T'HH:mm:sszzz")
        # Omit Duration to repeat indefinitely without overlapping daily boundaries.
        $triggers = "<TimeTrigger><Repetition><Interval>PT1H</Interval><StopAtDurationEnd>false</StopAtDurationEnd></Repetition><StartBoundary>$boundary</StartBoundary><Enabled>true</Enabled></TimeTrigger>"
    } else {
        $triggers = @"
    <CalendarTrigger><StartBoundary>${date}T13:30:00+09:00</StartBoundary><Enabled>true</Enabled><ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay></CalendarTrigger>
    <CalendarTrigger><StartBoundary>${date}T19:30:00+09:00</StartBoundary><Enabled>true</Enabled><ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay></CalendarTrigger>
"@
    }
    return @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>$description</Description></RegistrationInfo>
  <Triggers>
$triggers
  </Triggers>
  <Principals><Principal id="CurrentUser"><UserId>$sid</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>true</RunOnlyIfNetworkAvailable>
    <IdleSettings><StopOnIdleEnd>false</StopOnIdleEnd><RestartOnIdle>false</RestartOnIdle></IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT10M</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="CurrentUser"><Exec><Command>$exe</Command><Arguments>$arguments</Arguments><WorkingDirectory>$directory</WorkingDirectory></Exec></Actions>
</Task>
"@
}

function Write-CollectorFile {
    param([string]$Path, [string]$Content)
    $staging = "$Path.$([guid]::NewGuid().ToString('N')).writing"
    try {
        [IO.File]::WriteAllText($staging, $Content, (New-Object Text.UTF8Encoding($false)))
        if (Test-Path -LiteralPath $Path) {
            [IO.File]::Replace($staging, $Path, [NullString]::Value)
        } else {
            [IO.File]::Move($staging, $Path)
        }
    } finally {
        if (Test-Path -LiteralPath $staging) {
            Remove-Item -LiteralPath $staging
        }
    }
}

function Assert-CollectorReady {
    param([object]$Config)
    $source = Join-Path $Config.repoRoot 'tools\collect-shifts.py'
    if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
        throw 'Collector source is missing. Install again with -RepoRoot pointing to its new location.'
    }
    $text = Get-Content -LiteralPath $source -Raw -Encoding UTF8
    foreach ($option in @('--once', '--snapshot', '--publish')) {
        if ($text -notmatch [regex]::Escape($option)) {
            throw "Collector CLI is not ready: missing $option. Do not start the task yet."
        }
    }
}

function Get-CollectorStatus {
    param([object]$Paths, [object]$Config, [object]$Task)
    $info = $null
    $xml = $null
    if ($null -ne $Task) {
        $info = Get-ScheduledTaskInfo -TaskName $Paths.TaskName -TaskPath '\'
        $xml = Export-ScheduledTask -TaskName $Paths.TaskName -TaskPath '\'
    }
    [pscustomobject]@{
        TaskName = $Paths.TaskName
        Installed = ($null -ne $Task)
        State = $(if ($null -ne $Task) { [string]$Task.State } else { 'NotInstalled' })
        TimeZone = (Get-TimeZone).Id
        NextRunTime = $(if ($null -ne $info) { $info.NextRunTime.ToString('o') } else { $null })
        LastRunTime = $(if ($null -ne $info) { $info.LastRunTime.ToString('o') } else { $null })
        LastTaskResult = $(if ($null -ne $info) { $info.LastTaskResult } else { $null })
        Cadence = $(if ($null -ne $Config) { Resolve-CollectorCadence '' $Config } else { $null })
        Principal = $(if ($null -ne $Task) {
            [pscustomobject]@{
                UserId = [string]$Task.Principal.UserId
                LogonType = [string]$Task.Principal.LogonType
                RunLevel = [string]$Task.Principal.RunLevel
            }
        } else { $null })
        ConfigPath = $Paths.ConfigPath
        Config = $Config
        TaskXml = $xml
    }
}

function Invoke-CollectorSchedule {
    param([string]$Operation, [string]$Root, [string]$RequestedPythonw,
          [string]$RequestedCadence)
    if ($RequestedCadence -and $Operation -notin @('Install', 'Plan')) {
        throw '-Cadence is only valid for Install or Plan.'
    }
    $identity = Get-CollectorIdentity
    $paths = Get-CollectorPaths $identity.UserSid
    $config = Read-CollectorConfig $paths $identity.UserSid
    $task = Get-CollectorTask $paths
    Assert-CollectorTaskOwned $task $config $paths
    switch ($Operation) {
        { $_ -in 'Install', 'Plan' } {
            if ($null -ne $task -and [string]$task.State -eq 'Running') {
                throw 'Owned task is running. Wait for it to finish before installing.'
            }
            $rootPath = [IO.Path]::GetFullPath($Root).TrimEnd('\')
            if (-not (Test-Path -LiteralPath (Join-Path $rootPath 'tools\collect-shifts.py') -PathType Leaf)) {
                throw 'RepoRoot must contain tools\collect-shifts.py.'
            }
            $pythonw = Resolve-CollectorPythonw $RequestedPythonw
            $newConfig = New-CollectorConfig $paths $identity $rootPath $pythonw $config $RequestedCadence
            $xml = New-CollectorTaskXml $newConfig $paths
            if ($Operation -eq 'Plan') {
                return [pscustomobject]@{ Config = $newConfig; TaskXml = $xml; TimeZone = (Get-TimeZone).Id }
            }
            $launcherSource = Join-Path $script:CollectorToolsRoot 'collector-launcher.pyw'
            if (-not (Test-Path -LiteralPath $launcherSource -PathType Leaf)) {
                throw 'The companion collector-launcher.pyw is missing.'
            }
            [void][IO.Directory]::CreateDirectory($paths.DataRoot)
            Write-CollectorFile $paths.LauncherPath (
                Get-Content -LiteralPath $launcherSource -Raw -Encoding UTF8)
            Write-CollectorFile $paths.ConfigPath ($newConfig | ConvertTo-Json -Depth 5)
            # Register only after validating the old registration, never with credentials.
            try {
                Register-ScheduledTask -TaskName $paths.TaskName -TaskPath '\' -Xml $xml -Force | Out-Null
            } catch {
                if ($null -ne $config) {
                    Write-CollectorFile $paths.ConfigPath ($config | ConvertTo-Json -Depth 5)
                }
                throw
            }
            $config = $newConfig
            $task = Get-CollectorTask $paths
            if ($null -eq $task) { throw 'Registration returned but the task is missing.' }
            Assert-CollectorTaskOwned $task $config $paths
        }
        'Uninstall' {
            if ($null -ne $task) {
                if ([string]$task.State -eq 'Running') {
                    throw 'Owned task is running. Stop it explicitly before uninstalling.'
                }
                Unregister-ScheduledTask -TaskName $paths.TaskName -TaskPath '\' -Confirm:$false
                $task = Get-CollectorTask $paths
                if ($null -ne $task) { throw 'Task registration still exists.' }
            }
        }
        'Run' {
            if ($null -eq $task) { throw 'Owned task is not installed.' }
            Assert-CollectorReady $config
            Start-ScheduledTask -TaskName $paths.TaskName -TaskPath '\'
            $task = Get-CollectorTask $paths
        }
        'Stop' {
            if ($null -eq $task) { throw 'Owned task is not installed.' }
            Stop-ScheduledTask -TaskName $paths.TaskName -TaskPath '\'
            $task = Get-CollectorTask $paths
        }
        'Status' {}
        default { throw 'Unsupported schedule operation.' }
    }
    Get-CollectorStatus $paths $config $task
}

if ($Action -ne 'Functions') {
    Invoke-CollectorSchedule $Action $RepoRoot $PythonwPath $Cadence | ConvertTo-Json -Depth 8
}
