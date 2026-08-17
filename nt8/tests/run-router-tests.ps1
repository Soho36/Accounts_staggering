[CmdletBinding()]
param()

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

    throw 'Could not find the Visual Studio 2022 Roslyn csc.exe compiler.'
}

$testsDirectory = $PSScriptRoot
$routerSource = [System.IO.Path]::GetFullPath((Join-Path $testsDirectory '..\PropRouter.cs'))
$stubSource = Join-Path $testsDirectory 'NinjaTraderStubs.cs'
$testSource = Join-Path $testsDirectory 'PropRouterRegressionTests.cs'
$tempRoot = Join-Path ([System.IO.Path]::GetTempPath()) `
    ('PropRouterRegression-' + [Guid]::NewGuid().ToString('N'))
$executable = Join-Path $tempRoot 'PropRouterRegressionTests.exe'
$testData = Join-Path $tempRoot 'UserData'
$exitCode = 1

try {
    if (-not (Test-Path -LiteralPath $routerSource)) {
        throw "Router source not found: $routerSource"
    }

    New-Item -ItemType Directory -Path $tempRoot | Out-Null
    New-Item -ItemType Directory -Path $testData | Out-Null
    $compiler = Find-RoslynCompiler

    Write-Host "Compiler: $compiler"
    Write-Host "Building real router source in temporary directory: $tempRoot"

    $compilerArguments = @(
        '/nologo'
        '/target:exe'
        '/langversion:latest'
        "/out:$executable"
        $stubSource
        $routerSource
        $testSource
    )
    & $compiler @compilerArguments
    if ($LASTEXITCODE -ne 0) {
        throw "Compilation failed with exit code $LASTEXITCODE"
    }

    & $executable $testData
    if ($LASTEXITCODE -ne 0) {
        throw "Regression executable failed with exit code $LASTEXITCODE"
    }

    $exitCode = 0
}
catch {
    [Console]::Error.WriteLine($_.Exception.Message)
    $exitCode = 1
}
finally {
    if (Test-Path -LiteralPath $tempRoot) {
        Remove-Item -LiteralPath $tempRoot -Recurse -Force
    }
}

exit $exitCode
