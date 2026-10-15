[CmdletBinding()]
param(
    # NinjaScript sources to compile together as one assembly, exactly as the
    # NinjaScript Editor does. Default: both routers and both routed strategies,
    # which also proves the RTL and GG files can coexist in one Custom folder.
    [string[]]$Sources,
    [string]$NinjaTraderBin = 'C:\Program Files\NinjaTrader 8\bin'
)

# Real compile, not a syntax filter: uses the Roslyn build that ships with
# NinjaTrader and references the installed NinjaTrader assemblies, so missing
# members, wrong signatures and duplicate type names all fail here instead of in
# the NinjaScript Editor. It does not run NinjaTrader's order engine or a broker.
#
# The stock Custom\Backup\NinjaTrader.Custom.dll supplies the Strategy base class
# without the user's own compiled scripts, so the sources here cannot collide
# with an older deployed copy of themselves.

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'NinjaTraderRoslyn.ps1')

if (-not $Sources) {
    $nt8 = Split-Path -Parent $PSScriptRoot
    $Sources = @(
        (Join-Path $nt8 'PropRouter.cs'),
        (Join-Path $nt8 'RR_m_w_r_stoplimits_and_tplimit_InstanceID_WindowRR_Offsets_SafeExits_Routed_MaxRedRun.cs'),
        (Join-Path $nt8 'PropRouterGG.cs'),
        (Join-Path $nt8 'GG_m_w_r_stoplimits_and_tplimit_InstanceID_WindowRR_Offsets_SafeExits_Routed_MaxGreenRun.cs')
    )
}

$framework = 'C:\Windows\Microsoft.NET\Framework64\v4.0.30319'
$references = @(
    "$framework\mscorlib.dll", "$framework\System.dll", "$framework\System.Core.dll",
    "$framework\System.Xml.dll", "$framework\System.ComponentModel.DataAnnotations.dll",
    "$framework\WPF\WindowsBase.dll", "$framework\WPF\PresentationCore.dll",
    "$framework\WPF\PresentationFramework.dll", "$framework\System.Xaml.dll",
    "$NinjaTraderBin\NinjaTrader.Core.dll", "$NinjaTraderBin\NinjaTrader.Gui.dll",
    "$NinjaTraderBin\Custom\Backup\NinjaTrader.Custom.dll",
    "$NinjaTraderBin\SharpDX.dll", "$NinjaTraderBin\SharpDX.Direct2D1.dll"
)

$result = Invoke-NinjaTraderRoslyn -Sources $Sources -References $references -NinjaTraderBin $NinjaTraderBin
foreach ($path in $Sources) { Write-Host ('  ' + [System.IO.Path]::GetFileName($path)) }
$result.Errors | Select-Object -First 25 | ForEach-Object { Write-Host $_ }
# Warnings raised inside the compiled sources, by id and file. Reference-binding
# noise (CS1701/CS1702) is excluded; it comes from NinjaTrader's own assemblies.
$result.Warnings | Group-Object | Sort-Object Name |
    ForEach-Object { Write-Host ('  warning {0}  x{1}' -f $_.Name, $_.Count) }
Write-Host ("Result: {0} error(s), {1} source warning(s)" -f $result.Errors.Count, $result.Warnings.Count)
if ($result.Success) { exit 0 } else { exit 1 }
