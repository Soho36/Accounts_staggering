[CmdletBinding()]
param(
    # NinjaTrader's user folder on THIS machine (the trading PC).
    [string]$NinjaTraderDir = (Join-Path ([Environment]::GetFolderPath('MyDocuments')) 'NinjaTrader 8'),
    # Show what would change without copying anything.
    [switch]$DryRun,
    # Skip the GitHub comparison when the trading PC is offline. The clone must
    # still be a clean master.
    [switch]$Offline
)

# Deploys the routed NinjaScript from this clone into NinjaTrader on the trading PC.
#
#   git pull                        # in the clone, on master
#   .\nt8\deploy.ps1 -DryRun        # see what would change
#   .\nt8\deploy.ps1                # copy, then compile in the NinjaScript Editor
#
# Development happens on the analysis PC; this clone is a read-only consumer of
# master. The script refuses to deploy anything else: a dirty tree, another
# branch, or a commit GitHub does not have.
#
# Each file replaces whichever deployed file already defines the same class, so
# a renamed deployed copy is updated instead of duplicated (a duplicate class
# stops NinjaTrader from compiling anything). Replaced files are backed up
# OUTSIDE bin\Custom - a backup inside it would be compiled as a duplicate too.
#
# Peak files are not touched; install them with make_peak_file.py --install
# while NinjaTrader is stopped.

$ErrorActionPreference = 'Stop'
$nt8 = $PSScriptRoot
$repo = Split-Path -Parent $nt8

$deployments = @(
    @{ Source = 'PropRouter.cs';   Folder = 'AddOns'; Class = 'PropRouter' },
    @{ Source = 'PropRouterGG.cs'; Folder = 'AddOns'; Class = 'PropRouterGG' },
    @{ Source = 'RR_m_w_r_stoplimits_and_tplimit_InstanceID_WindowRR_Offsets_SafeExits_Routed_MaxRedRun.cs'
       Folder = 'Strategies'; Class = 'RRLongTimeWinStopLimitTPlimitGAPWindowRROffsetsSafeExitsRoutedMaxRedRun' },
    @{ Source = 'GG_m_w_r_stoplimits_and_tplimit_InstanceID_WindowRR_Offsets_SafeExits_Routed_MaxGreenRun.cs'
       Folder = 'Strategies'; Class = 'GGLongTimeWinStopLimitTPlimitGAPWindowRROffsetsSafeExitsRoutedGreenRun' }
)

function Invoke-Git([string[]]$Arguments) {
    # schannel: antivirus HTTPS scanning re-signs GitHub with a root only the
    # Windows certificate store trusts.
    $output = & git -C $repo -c http.sslBackend=schannel @Arguments 2>&1
    if ($LASTEXITCODE -ne 0) { throw "git $($Arguments -join ' ') failed: $output" }
    return ($output | Out-String).Trim()
}

function Get-ClassPattern([string]$Class) {
    # \b after the name: PropRouter must not match PropRouterGG.
    return '\bclass\s+' + [regex]::Escape($Class) + '\b'
}

# --- 1. Only a clean, published master is deployable -------------------------
$branch = Invoke-Git @('rev-parse', '--abbrev-ref', 'HEAD')
if ($branch -ne 'master') { throw "The clone is on '$branch'. Deploy only master: git checkout master; git pull" }
$dirty = Invoke-Git @('status', '--porcelain')
if ($dirty) { throw "The clone has local changes. Never edit on the trading PC; discard them or move them to the analysis PC:`n$dirty" }
$head = Invoke-Git @('rev-parse', 'HEAD')
if (-not $Offline) {
    [void](Invoke-Git @('fetch', '--quiet', 'origin', 'master'))
    $remote = Invoke-Git @('rev-parse', 'origin/master')
    if ($head -ne $remote) { throw "The clone ($($head.Substring(0,7))) differs from GitHub master ($($remote.Substring(0,7))). Run: git pull" }
}
$subject = Invoke-Git @('log', '-1', '--format=%h %s')
Write-Host "Deploying master $subject"

# --- 2. GG must still be exactly what the RTL sources generate ---------------
$python = Get-Command python -ErrorAction SilentlyContinue
if ($python) {
    $sync = & $python.Source (Join-Path $nt8 'gg_sync.py') 2>&1
    if ($LASTEXITCODE -ne 0) { $sync | ForEach-Object { Write-Host $_ }; throw 'gg_sync.py reports drift; fix it on the analysis PC first.' }
    Write-Host 'GG files match their RTL sources.'
}
else {
    Write-Warning 'Python not found; skipping the gg_sync.py drift check.'
}

# --- 3. Resolve each target --------------------------------------------------
$custom = Join-Path $NinjaTraderDir 'bin\Custom'
if (-not (Test-Path -LiteralPath $custom)) { throw "NinjaTrader Custom folder not found: $custom (pass -NinjaTraderDir)" }

$plan = @()
foreach ($d in $deployments) {
    $source = Join-Path $nt8 $d.Source
    $pattern = Get-ClassPattern $d.Class
    if (-not (Select-String -LiteralPath $source -Pattern $pattern -Quiet)) {
        throw "$($d.Source) does not define class $($d.Class); update deploy.ps1."
    }
    $folder = Join-Path $custom $d.Folder
    $existing = @(Get-ChildItem -LiteralPath $folder -Filter '*.cs' -File |
        Where-Object { Select-String -LiteralPath $_.FullName -Pattern $pattern -Quiet })
    if ($existing.Count -gt 1) {
        throw "Class $($d.Class) is defined in $($existing.Count) files in $folder ($($existing.Name -join ', ')). Remove the extra copies first."
    }
    $target = if ($existing.Count -eq 1) { $existing[0].FullName } else { Join-Path $folder ($d.Class + '.cs') }
    $state = if ($existing.Count -eq 0) { 'NEW' }
             elseif ((Get-FileHash -LiteralPath $source).Hash -eq (Get-FileHash -LiteralPath $target).Hash) { 'unchanged' }
             else { 'UPDATE' }
    $plan += [pscustomobject]@{ State = $state; Source = $source; Target = $target; Folder = $d.Folder }
}

foreach ($p in $plan) { Write-Host ('  {0,-9} {1}\{2}' -f $p.State, $p.Folder, (Split-Path -Leaf $p.Target)) }
$changes = @($plan | Where-Object { $_.State -ne 'unchanged' })
if ($changes.Count -eq 0) { Write-Host 'Nothing to deploy; NinjaTrader already has this revision.'; exit 0 }
if ($DryRun) { Write-Host "Dry run: $($changes.Count) file(s) would be copied."; exit 0 }

# --- 4. Back up, then copy ---------------------------------------------------
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$backupRoot = Join-Path $NinjaTraderDir "deploy_backups\$stamp"
foreach ($p in $changes) {
    if ($p.State -eq 'UPDATE') {
        $dir = Join-Path $backupRoot $p.Folder
        New-Item -ItemType Directory -Force -Path $dir | Out-Null
        Copy-Item -LiteralPath $p.Target -Destination $dir
    }
    Copy-Item -LiteralPath $p.Source -Destination $p.Target -Force
}
New-Item -ItemType Directory -Force -Path (Join-Path $NinjaTraderDir 'deploy_backups') | Out-Null
Add-Content -LiteralPath (Join-Path $NinjaTraderDir 'deploy_backups\deploy_log.txt') `
    -Value ("{0}  {1}  {2} file(s)" -f (Get-Date -Format 's'), $subject, $changes.Count)

Write-Host ''
Write-Host "Copied $($changes.Count) file(s). Previous versions: $backupRoot"
Write-Host 'Next:'
Write-Host '  1. Open the NinjaScript Editor and compile (F5).'
Write-Host '  2. Fully restart NinjaTrader: running strategies keep the old code until then.'
