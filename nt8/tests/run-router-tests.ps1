[CmdletBinding()]
param(
    # RTL tests PropRouter.cs. GG runs the same regression suite against the
    # generated PropRouterGG.cs plus the GG-only cross-router guard tests.
    [ValidateSet('RTL', 'GG')]
    [string]$Router = 'RTL'
)

$ErrorActionPreference = 'Stop'

function Find-RoslynCompiler {
    $candidates = New-Object System.Collections.Generic.List[string]

    $vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
    if (Test-Path -LiteralPath $vswhere) {
        $found = & $vswhere -latest -products '*' -version '[17.0,18.0)' `
            -requires Microsoft.Component.MSBuild `
            -find 'MSBuild\**\Bin\Roslyn\csc.exe'
        foreach ($path in $found) {
            if (-not [string]::IsNullOrWhiteSpace($path)) {
                $candidates.Add($path.Trim())
            }
        }
    }

    foreach ($edition in 'Community', 'Professional', 'Enterprise', 'BuildTools') {
        $candidates.Add((Join-Path $env:ProgramFiles `
            "Microsoft Visual Studio\2022\$edition\MSBuild\Current\Bin\Roslyn\csc.exe"))
        $candidates.Add((Join-Path ${env:ProgramFiles(x86)} `
            "Microsoft Visual Studio\2022\$edition\MSBuild\Current\Bin\Roslyn\csc.exe"))
    }

    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate) {
            return (Resolve-Path -LiteralPath $candidate).Path
        }
    }
    # No Visual Studio: fall back to the Roslyn build inside NinjaTrader.
    return $null
}

function Build-TestExecutable([string[]]$Sources, [string]$Executable, $Compiler) {
    if ($Compiler) {
        $compilerArguments = @('/nologo', '/target:exe', '/langversion:latest', "/out:$Executable") + $Sources
        & $Compiler @compilerArguments
        if ($LASTEXITCODE -ne 0) {
            throw "Compilation failed with exit code $LASTEXITCODE"
        }
        return
    }

    $framework = 'C:\Windows\Microsoft.NET\Framework64\v4.0.30319'
    $result = Invoke-NinjaTraderRoslyn -Sources $Sources -OutputKind ConsoleApplication `
        -OutputPath $Executable `
        -References @("$framework\mscorlib.dll", "$framework\System.dll", "$framework\System.Core.dll")
    if (-not $result.Success) {
        $result.Errors | Select-Object -First 25 | ForEach-Object { Write-Host $_ }
        throw 'Compilation failed'
    }
}

$testsDirectory = $PSScriptRoot
. (Join-Path $testsDirectory 'NinjaTraderRoslyn.ps1')
$nt8Directory = [System.IO.Path]::GetFullPath((Join-Path $testsDirectory '..'))
$stubSource = Join-Path $testsDirectory 'NinjaTraderStubs.cs'

if ($Router -eq 'RTL') {
    $routerSource = Join-Path $nt8Directory 'PropRouter.cs'
    $suites = @(
        @{ Name = 'PropRouterRegressionTests'; Test = (Join-Path $testsDirectory 'PropRouterRegressionTests.cs') }
    )
}
else {
    $routerSource = Join-Path $nt8Directory 'PropRouterGG.cs'
    $suites = @(
        @{ Name = 'PropRouterGGRegressionTests'; Test = (Join-Path $testsDirectory 'PropRouterGGRegressionTests.cs') },
        @{ Name = 'PropRouterGGGuardTests'; Test = (Join-Path $testsDirectory 'PropRouterGGGuardTests.cs') }
    )
}

$tempRoot = Join-Path ([System.IO.Path]::GetTempPath()) `
    ('PropRouterRegression-' + [Guid]::NewGuid().ToString('N'))
$exitCode = 1

try {
    foreach ($path in @($routerSource, $stubSource) + @($suites | ForEach-Object { $_.Test })) {
        if (-not (Test-Path -LiteralPath $path)) {
            throw "Source not found: $path"
        }
    }

    New-Item -ItemType Directory -Path $tempRoot | Out-Null
    $compiler = Find-RoslynCompiler
    Write-Host ("Compiler: {0}" -f $(if ($compiler) { $compiler } else { "NinjaTrader's Roslyn" }))
    Write-Host "Building real router source in temporary directory: $tempRoot"

    $failures = 0
    foreach ($suite in $suites) {
        $executable = Join-Path $tempRoot ($suite.Name + '.exe')
        $testData = Join-Path $tempRoot ($suite.Name + '-UserData')
        New-Item -ItemType Directory -Path $testData | Out-Null

        Write-Host ''
        Write-Host ("== {0} against {1}" -f $suite.Name, [System.IO.Path]::GetFileName($routerSource))
        Build-TestExecutable @($stubSource, $routerSource, $suite.Test) $executable $compiler
        & $executable $testData
        if ($LASTEXITCODE -ne 0) {
            Write-Host ("{0} exited with code {1}" -f $suite.Name, $LASTEXITCODE)
            $failures++
        }
    }

    if ($failures -gt 0) {
        throw "$failures regression suite(s) failed"
    }
    $exitCode = 0
}
catch {
    Write-Host ("ERROR: " + $_.Exception.Message)
    $exitCode = 1
}
finally {
    # Best effort: an antivirus scanner can briefly hold a freshly built test
    # executable (Avast leaves asw-* files). A leftover temp folder is harmless.
    for ($attempt = 1; $attempt -le 5 -and (Test-Path -LiteralPath $tempRoot); $attempt++) {
        try { Remove-Item -LiteralPath $tempRoot -Recurse -Force -ErrorAction Stop }
        catch { Start-Sleep -Milliseconds 500 }
    }
    if (Test-Path -LiteralPath $tempRoot) {
        Write-Host "Note: could not remove $tempRoot (file in use); delete it later."
    }
}

exit $exitCode
