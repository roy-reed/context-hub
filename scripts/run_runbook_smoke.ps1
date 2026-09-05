[CmdletBinding()]
param(
    [string]$Output = ".context-hub-test-data/runbook-smoke-report.json",
    [string]$ContextHubCommand = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$Utf8NoBom = [Text.UTF8Encoding]::new($false)
$OutputEncoding = $Utf8NoBom
[Console]::InputEncoding = $Utf8NoBom
[Console]::OutputEncoding = $Utf8NoBom
$env:PYTHONUTF8 = "1"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$TestRoot = Join-Path $RepoRoot ".context-hub-test-data"
$RunRoot = Join-Path $TestRoot ("runbook-smoke-" + [Guid]::NewGuid().ToString("N"))
$DataRoot = Join-Path $RunRoot "data"
$ProjectRoot = Join-Path $RunRoot "project"
$BackupRoot = Join-Path $RunRoot "backups"
$RestoreRoot = Join-Path $RunRoot "restored"
$OutputPath = if ([IO.Path]::IsPathRooted($Output)) {
    [IO.Path]::GetFullPath($Output)
} else {
    [IO.Path]::GetFullPath((Join-Path $RepoRoot $Output))
}
$Checks = [ordered]@{}
$Stopwatch = [Diagnostics.Stopwatch]::StartNew()

function Assert-Check {
    param(
        [Parameter(Mandatory)] [bool]$Condition,
        [Parameter(Mandatory)] [string]$Name
    )
    if (-not $Condition) {
        throw "runbook smoke check failed: $Name"
    }
    $Checks[$Name] = $true
}

function Resolve-ContextHubCommand {
    param([string]$Candidate)

    if (-not [string]::IsNullOrWhiteSpace($Candidate)) {
        if (Test-Path -LiteralPath $Candidate -PathType Leaf) {
            return (Resolve-Path -LiteralPath $Candidate).Path
        }
        $ExplicitCommand = Get-Command $Candidate -CommandType Application -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if ($null -ne $ExplicitCommand) {
            return [string]$ExplicitCommand.Source
        }
        throw "context-hub command was not found: $Candidate"
    }

    $RepoVenvHub = Join-Path $RepoRoot ".venv/Scripts/context-hub.exe"
    if (Test-Path -LiteralPath $RepoVenvHub -PathType Leaf) {
        return (Resolve-Path -LiteralPath $RepoVenvHub).Path
    }

    $InstalledCommand = Get-Command "context-hub" -CommandType Application -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($null -ne $InstalledCommand) {
        return [string]$InstalledCommand.Source
    }

    throw "missing CLI executable; install the project or pass -ContextHubCommand"
}

$HubExe = Resolve-ContextHubCommand -Candidate $ContextHubCommand

function Invoke-ContextHub {
    param(
        [Parameter(Mandatory)] [string[]]$CliArgs,
        [string]$DataDirectory = $DataRoot
    )

    $Raw = & $HubExe --data-dir $DataDirectory @CliArgs 2>&1
    $ExitCode = $LASTEXITCODE
    $Text = (($Raw | ForEach-Object { $_.ToString() }) -join [Environment]::NewLine).Trim()
    if ($ExitCode -ne 0) {
        throw "context-hub exited with ${ExitCode}: $Text"
    }
    try {
        return $Text | ConvertFrom-Json
    } catch {
        throw "context-hub returned invalid JSON: $Text"
    }
}

New-Item -ItemType Directory -Path (Join-Path $ProjectRoot "context") -Force | Out-Null
New-Item -ItemType Directory -Path ([IO.Path]::GetDirectoryName($OutputPath)) -Force | Out-Null

$Marker = "runbooksmokeanchor"
$WriteMarker = "runbooksmokewrite"
$Padding = (1..80 | ForEach-Object { "synthetic-token-$($_.ToString('D3'))" }) -join " "
$AgentsText = "# Synthetic Runbook Project`n`n$Marker proves deterministic project search. $Padding`n"
$NotesText = "# Synthetic Notes`n`nThis file is generated only for the runbook smoke test.`n"
[IO.File]::WriteAllText((Join-Path $ProjectRoot "AGENTS.md"), $AgentsText, $Utf8NoBom)
[IO.File]::WriteAllText((Join-Path $ProjectRoot "context/notes.md"), $NotesText, $Utf8NoBom)

try {
    $Init = Invoke-ContextHub -CliArgs @("init")
    $InitAgain = Invoke-ContextHub -CliArgs @("init")
    Assert-Check ($Init.ok -and -not $Init.write_enabled) "initializes_read_only"
    Assert-Check ($InitAgain.ok -and -not $InitAgain.write_enabled) "repeat_init_is_idempotent"

    $Registered = Invoke-ContextHub -CliArgs @("register-project", "demo", $ProjectRoot)
    Assert-Check ($Registered.ok -and $Registered.project_id -eq "demo") "registers_synthetic_project"
    Assert-Check ((@($Registered.files) -join ",") -eq "AGENTS.md,context/notes.md") "registers_only_default_allowlist"

    $Manifest = Invoke-ContextHub -CliArgs @("manifest")
    Assert-Check (@($Manifest.projects).Count -eq 1) "manifest_has_one_project"
    Assert-Check ($Manifest.projects[0].project_id -eq "demo") "manifest_project_id_matches"

    $Search = Invoke-ContextHub -CliArgs @("search", $Marker, "--project-id", "demo")
    Assert-Check (@($Search.items).Count -ge 1) "search_finds_synthetic_marker"
    $Ref = [string]$Search.items[0].ref
    Assert-Check ($Ref.StartsWith("file:demo:", [StringComparison]::Ordinal)) "search_returns_project_scoped_file_ref"

    $Chunks = [Collections.Generic.List[string]]::new()
    $ExpectedOffset = 0
    $Cursor = $null
    $ReadHash = $null
    do {
        $Page = if ($null -eq $Cursor) {
            Invoke-ContextHub -CliArgs @("read", $Ref, "--max-chars", "200")
        } else {
            Invoke-ContextHub -CliArgs @("read", "--cursor", $Cursor, "--max-chars", "200")
        }
        Assert-Check ([int]$Page.char_offset -eq $ExpectedOffset) ("read_page_offset_" + $ExpectedOffset)
        Assert-Check ([string]$Page.ref -eq $Ref) ("read_page_ref_" + $ExpectedOffset)
        if ($null -eq $ReadHash) {
            $ReadHash = [string]$Page.sha256
        }
        Assert-Check ([string]$Page.sha256 -eq $ReadHash) ("read_page_hash_" + $ExpectedOffset)
        $Chunk = [string]$Page.content
        $Chunks.Add($Chunk)
        $ExpectedOffset += $Chunk.Length
        if ([bool]$Page.truncated) {
            Assert-Check (-not [string]::IsNullOrWhiteSpace([string]$Page.next_cursor)) ("read_page_cursor_" + $ExpectedOffset)
            $Cursor = [string]$Page.next_cursor
        } else {
            $Cursor = $null
        }
    } while ($null -ne $Cursor)

    $Reconstructed = $Chunks -join ""
    $ComputedHash = [Convert]::ToHexString(
        [Security.Cryptography.SHA256]::HashData([Text.Encoding]::UTF8.GetBytes($Reconstructed))
    ).ToLowerInvariant()
    Assert-Check ($Reconstructed -eq $AgentsText) "pagination_reconstructs_exact_content"
    Assert-Check ($ExpectedOffset -eq [int]$Page.total_chars) "pagination_reaches_total_chars"
    Assert-Check ($ComputedHash -eq $ReadHash) "pagination_sha256_matches_content"

    $WritesEnabled = Invoke-ContextHub -CliArgs @("init", "--enable-writes")
    Assert-Check ($WritesEnabled.write_enabled) "explicitly_enables_writes"
    $Put = Invoke-ContextHub -CliArgs @(
        "put", "--confirm-write", "--action", "append", "--kind", "fact",
        "--content", $WriteMarker, "--project-id", "demo", "--source-ref", "synthetic:runbook-smoke"
    )
    Assert-Check ($Put.status -eq "created" -and $Put.index_state -eq "current") "confirmed_put_is_indexed"
    $EventSearch = Invoke-ContextHub -CliArgs @("search", $WriteMarker, "--project-id", "demo", "--kind", "fact")
    Assert-Check (@($EventSearch.items).Count -eq 1) "kind_filtered_search_finds_written_event"
    Assert-Check ([string]$EventSearch.items[0].ref -eq ("event:" + [string]$Put.event_id)) "written_event_ref_matches"

    $Doctor = Invoke-ContextHub -CliArgs @("doctor")
    Assert-Check ($Doctor.ok) "doctor_is_clean"
    Assert-Check ([int]$Doctor.counts.projects -eq 1) "doctor_counts_one_project"
    Assert-Check ([int]$Doctor.counts.events -eq 1 -and [int]$Doctor.counts.indexed_events -eq 1) "doctor_event_counts_match"
    Assert-Check ([int]$Doctor.counts.sections -eq 2 -and [int]$Doctor.counts.section_fts -eq 2) "doctor_section_counts_match"

    $IndexPath = [IO.Path]::GetFullPath((Join-Path $DataRoot "index/context.sqlite3"))
    $SafeDataPrefix = [IO.Path]::GetFullPath($DataRoot).TrimEnd([IO.Path]::DirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
    Assert-Check ($IndexPath.StartsWith($SafeDataPrefix, [StringComparison]::OrdinalIgnoreCase)) "index_delete_target_is_scoped"
    Assert-Check (Test-Path -LiteralPath $IndexPath -PathType Leaf) "derived_index_exists_before_delete"
    Remove-Item -LiteralPath $IndexPath -Force

    $Reindex = Invoke-ContextHub -CliArgs @("reindex")
    Assert-Check ($Reindex.ok -and [int]$Reindex.events -eq 1 -and [int]$Reindex.sections -eq 2) "reindex_restores_exact_counts"
    $SearchAfterReindex = Invoke-ContextHub -CliArgs @("search", $Marker, "--project-id", "demo")
    $ReadAfterReindex = Invoke-ContextHub -CliArgs @("read", ([string]$SearchAfterReindex.items[0].ref), "--max-chars", "200")
    Assert-Check ([string]$SearchAfterReindex.items[0].ref -eq $Ref) "reindex_preserves_stable_ref"
    Assert-Check ([string]$ReadAfterReindex.sha256 -eq $ReadHash) "reindex_preserves_content_hash"

    $Backup = Invoke-ContextHub -CliArgs @("backup", "--destination", $BackupRoot)
    Assert-Check ($Backup.ok -and (Test-Path -LiteralPath ([string]$Backup.backup) -PathType Leaf)) "backup_zip_is_created"
    Assert-Check ([string]$Backup.sha256 -match "^[0-9a-f]{64}$") "backup_has_sha256"
    $BackupEntries = @($Backup.entries | ForEach-Object { [string]$_.path })
    Assert-Check (($BackupEntries -join ",") -eq "config.toml,manifest.json,memory/events.jsonl") "backup_contains_only_authoritative_files"

    $Restored = Invoke-ContextHub -CliArgs @(
        "restore", [string]$Backup.backup, "--destination", $RestoreRoot
    )
    Assert-Check ($Restored.ok -and $Restored.reindexed) "restore_verifies_and_reindexes"
    Assert-Check ([string]$Restored.destination -eq [IO.Path]::GetFullPath($RestoreRoot)) "restore_targets_new_data_root"
    $RestoreDoctor = Invoke-ContextHub -DataDirectory $RestoreRoot -CliArgs @("doctor")
    Assert-Check ($RestoreDoctor.ok) "restored_doctor_is_clean"
    Assert-Check (
        [int]$RestoreDoctor.counts.projects -eq 1 -and
        [int]$RestoreDoctor.counts.events -eq 1 -and
        [int]$RestoreDoctor.counts.sections -eq 2
    ) "restored_counts_match_source"
    $RestoreSearch = Invoke-ContextHub -DataDirectory $RestoreRoot -CliArgs @("search", $Marker, "--project-id", "demo")
    $RestoreRead = Invoke-ContextHub -DataDirectory $RestoreRoot -CliArgs @(
        "read", ([string]$RestoreSearch.items[0].ref), "--max-chars", "200"
    )
    Assert-Check ([string]$RestoreSearch.items[0].ref -eq $Ref) "restore_preserves_stable_ref"
    Assert-Check ([string]$RestoreRead.sha256 -eq $ReadHash) "restore_preserves_content_hash"

    $Stopwatch.Stop()
    $Report = [ordered]@{
        ok = $true
        synthetic_only = $true
        external_fact_inputs = 0
        surface = "context-hub CLI"
        elapsed_ms = [Math]::Round($Stopwatch.Elapsed.TotalMilliseconds, 3)
        checks = $Checks
        doctor_counts = $Doctor.counts
        backup_entries = $BackupEntries
        restored_doctor_counts = $RestoreDoctor.counts
        notes = @(
            "All fact inputs were generated inside one unique ignored test directory.",
            "The unique fixture directory was removed after validation; this report was retained.",
            "Backup restore was verified into a new data root and rebuilt its derived SQLite index.",
            "Local MCP STDIO and Streamable HTTP are validated separately by the acceptance and unit suites."
        )
    }
    $Json = $Report | ConvertTo-Json -Depth 8
    [IO.File]::WriteAllText($OutputPath, $Json + [Environment]::NewLine, $Utf8NoBom)
    $Json
} finally {
    $ResolvedTestRoot = [IO.Path]::GetFullPath($TestRoot).TrimEnd([IO.Path]::DirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
    $ResolvedRunRoot = [IO.Path]::GetFullPath($RunRoot)
    if (-not $ResolvedRunRoot.StartsWith($ResolvedTestRoot, [StringComparison]::OrdinalIgnoreCase)) {
        throw "refusing to remove out-of-scope smoke directory: $ResolvedRunRoot"
    }
    if (Test-Path -LiteralPath $ResolvedRunRoot) {
        Remove-Item -LiteralPath $ResolvedRunRoot -Recurse -Force
    }
}
