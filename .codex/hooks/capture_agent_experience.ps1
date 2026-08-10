$ErrorActionPreference = "Stop"

try {
    $MaxInputBytes = 65536; $MaxEventBytes = 16384; $MaxIdLength = 256
    $MaxFilenameIdLength = 120; $MaxPathLength = 4096; $MaxLabelLength = 256
    $MaxIdentifierLength = 64; $MaxSkills = 16; $MaxCount = 1000000000000
    $AllowedAgents = @("adapter_specialist", "agent_curator", "architect", "core_implementer", "docs_maintainer", "product_strategist", "reviewer", "test_engineer")
    $AllowedSkills = @("improve-drydock-agents", "plan-drydock-change", "verify-drydock-change")
    $rawInput = $input | Out-String
    if ([Text.Encoding]::UTF8.GetByteCount($rawInput) -gt $MaxInputBytes) { throw "hook input exceeds byte limit" }
    $payload = $rawInput | ConvertFrom-Json
    if ($payload -isnot [System.Management.Automation.PSCustomObject]) {
        throw "hook input must be a JSON object"
    }
    $projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
    $queueDir = Join-Path $projectRoot ".agent-experience\pending"
    New-Item -ItemType Directory -Path $queueDir -Force | Out-Null

    function Get-BoundedString([object]$Object, [string]$Name, [int]$Maximum, [bool]$Nonempty = $false) {
        $property = $Object.PSObject.Properties[$Name]
        if ($null -eq $property -or $property.Value -isnot [string] -or $property.Value.Length -gt $Maximum) { return $null }
        if ($Nonempty -and [string]::IsNullOrWhiteSpace($property.Value)) { return $null }
        return $property.Value
    }

    function ConvertTo-SafeId([string]$Value, [string]$Fallback) {
        $cleaned = ($Value -replace '[^A-Za-z0-9._-]+', '-').Trim('-', '.')
        if ([string]::IsNullOrWhiteSpace($cleaned)) { return $Fallback }
        if ($cleaned.Length -gt $MaxFilenameIdLength) { return $cleaned.Substring(0, $MaxFilenameIdLength) }
        return $cleaned
    }

    function Get-Identifier([object]$Object, [string]$Name) {
        $value = Get-BoundedString $Object $Name $MaxIdentifierLength $true
        if ($null -ne $value -and $value -match '^[A-Za-z0-9][A-Za-z0-9._-]*$') { return $value }
        return $null
    }

    function Get-Allowlisted([object]$Object, [string]$Name, [string[]]$Accepted) {
        $value = Get-Identifier $Object $Name
        if ($null -ne $value -and $value -in $Accepted) { return $value }
        return $null
    }

    function Get-OptionalCount([object]$Object, [string]$Name) {
        $property = $Object.PSObject.Properties[$Name]
        if ($null -ne $property -and $property.Value -isnot [bool] -and
            ($property.Value -is [byte] -or $property.Value -is [int16] -or $property.Value -is [int32] -or $property.Value -is [int64]) -and
            $property.Value -ge 0 -and $property.Value -le $MaxCount) {
            return [int64]$property.Value
        }
        return $null
    }

    function Get-OptionalSkills([object]$Object) {
        $property = $Object.PSObject.Properties["skills"]
        if ($null -eq $property -or $property.Value -is [string] -or $property.Value -isnot [System.Collections.IEnumerable]) {
            return @()
        }
        $items = @($property.Value)
        if ($items.Count -gt $MaxSkills) { return @() }
        $result = @()
        foreach ($item in $items) {
            if ($item -isnot [string] -or $item.Length -gt $MaxIdentifierLength -or $item -notin $AllowedSkills) { return @() }
            $result += $item
        }
        return $result
    }

    $sessionValue = Get-BoundedString $payload "session_id" $MaxIdLength $true
    $turnValue = Get-BoundedString $payload "turn_id" $MaxIdLength $true
    $eventId = (ConvertTo-SafeId $sessionValue "unknown-session") + "--" + (ConvertTo-SafeId $turnValue "unknown-turn")
    $destination = Join-Path $queueDir ($eventId + ".json")

    $agent = Get-Allowlisted $payload "agent" $AllowedAgents
    $skills = @(Get-OptionalSkills $payload)
    $outcomeCandidate = Get-BoundedString $payload "outcome" $MaxIdentifierLength $true
    $outcome = if ($outcomeCandidate -in @("completed", "failed", "cancelled", "interrupted")) { $outcomeCandidate } else { $null }
    $testCandidate = Get-BoundedString $payload "test_status" $MaxIdentifierLength $true
    $testStatus = if ($testCandidate -in @("passed", "failed", "partial", "not_run")) { $testCandidate } else { $null }
    $correctionCount = Get-OptionalCount $payload "correction_count"
    $inputTokens = Get-OptionalCount $payload "input_tokens"
    $outputTokens = Get-OptionalCount $payload "output_tokens"
    $filesChangedCount = Get-OptionalCount $payload "files_changed_count"
    $provenance = [ordered]@{}
    $optionalValues = [ordered]@{
        agent = $agent; skills = $skills; outcome = $outcome; test_status = $testStatus
        correction_count = $correctionCount; input_tokens = $inputTokens
        output_tokens = $outputTokens; files_changed_count = $filesChangedCount
    }
    foreach ($entry in $optionalValues.GetEnumerator()) {
        if ($null -ne $entry.Value -and -not ($entry.Value -is [array] -and $entry.Value.Count -eq 0)) {
            $provenance[$entry.Key] = "hook_payload." + $entry.Key
        }
    }

    $event = [ordered]@{
        schema_version = 2
        event_id = $eventId
        captured_at = [DateTimeOffset]::UtcNow.ToString("o")
        session_id = $sessionValue
        turn_id = $turnValue
        hook_event_name = Get-BoundedString $payload "hook_event_name" $MaxIdentifierLength
        transcript_path = Get-BoundedString $payload "transcript_path" $MaxPathLength
        cwd = Get-BoundedString $payload "cwd" $MaxPathLength
        model = Get-BoundedString $payload "model" $MaxLabelLength
        permission_mode = Get-BoundedString $payload "permission_mode" $MaxIdentifierLength
        agent = $agent
        skills = $skills
        outcome = $outcome
        test_status = $testStatus
        correction_count = $correctionCount
        input_tokens = $inputTokens
        output_tokens = $outputTokens
        files_changed_count = $filesChangedCount
        field_provenance = $provenance
    }
    $json = $event | ConvertTo-Json -Depth 3
    if ([Text.Encoding]::UTF8.GetByteCount($json + "`n") -gt $MaxEventBytes) { throw "agent experience event exceeds byte limit" }

    try {
        $stream = [System.IO.File]::Open(
            $destination,
            [System.IO.FileMode]::CreateNew,
            [System.IO.FileAccess]::Write,
            [System.IO.FileShare]::None
        )
        try {
            $writer = New-Object System.IO.StreamWriter($stream, (New-Object System.Text.UTF8Encoding($false)))
            try { $writer.WriteLine($json) } finally { $writer.Dispose() }
        } finally {
            if ($null -ne $stream) { $stream.Dispose() }
        }
    } catch [System.IO.IOException] {
        if (-not ([IO.File]::Exists($destination))) { throw }
        try { $existing = Get-Content -Raw -LiteralPath $destination | ConvertFrom-Json } catch { throw "event identity collides with an invalid existing target" }
        $legacyNames = @("session_id", "turn_id", "hook_event_name", "transcript_path", "cwd", "model", "permission_mode")
        $validExisting = $existing -is [System.Management.Automation.PSCustomObject] -and $existing.schema_version -in @(1, 2) -and $existing.captured_at -is [string]
        foreach ($name in $legacyNames) {
            $property = $existing.PSObject.Properties[$name]
            if ($null -eq $property -or ($null -ne $property.Value -and $property.Value -isnot [string])) { $validExisting = $false }
        }
        if (-not $validExisting -or $existing.session_id -ne $sessionValue -or $existing.turn_id -ne $turnValue -or
            ($existing.schema_version -eq 2 -and $existing.event_id -ne $eventId)) {
            throw "event identity collides with a non-matching existing target"
        }
    }

    exit 0
} catch {
    [Console]::Error.WriteLine("agent experience capture failed: " + $_.Exception.Message)
    exit 1
}
