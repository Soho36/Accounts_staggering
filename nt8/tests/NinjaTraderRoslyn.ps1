# Dot-source this file to get Invoke-NinjaTraderRoslyn: compiles C# with the Roslyn
# build that ships inside NinjaTrader 8, so no Visual Studio installation is needed
# and the compiler is the same one the NinjaScript Editor uses.

function Invoke-NinjaTraderRoslyn {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string[]]$Sources,
        [Parameter(Mandatory = $true)][string[]]$References,
        [ValidateSet('DynamicallyLinkedLibrary', 'ConsoleApplication')]
        [string]$OutputKind = 'DynamicallyLinkedLibrary',
        # Omit to compile in memory only.
        [string]$OutputPath,
        [string]$NinjaTraderBin = 'C:\Program Files\NinjaTrader 8\bin'
    )

    foreach ($required in 'Microsoft.CodeAnalysis.dll', 'Microsoft.CodeAnalysis.CSharp.dll') {
        if (-not (Test-Path -LiteralPath (Join-Path $NinjaTraderBin $required))) {
            throw "NinjaTrader's Roslyn compiler ($required) was not found in $NinjaTraderBin"
        }
    }

    # Roslyn's own dependencies live next to it in NinjaTrader's folder.
    $bin = $NinjaTraderBin
    $resolver = [System.ResolveEventHandler] {
        param($sender, $eventArgs)
        $name = (New-Object System.Reflection.AssemblyName($eventArgs.Name)).Name
        $candidate = Join-Path $bin ($name + '.dll')
        if (Test-Path -LiteralPath $candidate) {
            return [System.Reflection.Assembly]::LoadFrom($candidate)
        }
        return $null
    }.GetNewClosure()

    [System.AppDomain]::CurrentDomain.add_AssemblyResolve($resolver)
    $stream = $null
    try {
        [void][System.Reflection.Assembly]::LoadFrom((Join-Path $NinjaTraderBin 'Microsoft.CodeAnalysis.dll'))
        [void][System.Reflection.Assembly]::LoadFrom((Join-Path $NinjaTraderBin 'Microsoft.CodeAnalysis.CSharp.dll'))

        $metadata = [Microsoft.CodeAnalysis.MetadataReference[]]@(
            foreach ($path in $References) {
                [Microsoft.CodeAnalysis.MetadataReference]::CreateFromFile($path)
            }
        )
        $parseOptions = [Microsoft.CodeAnalysis.CSharp.CSharpParseOptions]::Default
        $trees = [Microsoft.CodeAnalysis.SyntaxTree[]]@(
            foreach ($path in $Sources) {
                $full = [System.IO.Path]::GetFullPath($path)
                [Microsoft.CodeAnalysis.CSharp.CSharpSyntaxTree]::ParseText(
                    [System.IO.File]::ReadAllText($full), $parseOptions, $full)
            }
        )
        $kind = [Microsoft.CodeAnalysis.OutputKind]$OutputKind
        $options = New-Object Microsoft.CodeAnalysis.CSharp.CSharpCompilationOptions($kind)
        $name = if ($OutputPath) { [System.IO.Path]::GetFileNameWithoutExtension($OutputPath) } else { 'NinjaScriptCompileCheck' }
        $compilation = [Microsoft.CodeAnalysis.CSharp.CSharpCompilation]::Create(
            $name, $trees, $metadata, $options)

        $stream = if ($OutputPath) { [System.IO.File]::Create($OutputPath) } else { New-Object System.IO.MemoryStream }
        $result = $compilation.Emit($stream)
        $stream.Dispose()
        $stream = $null
        if (-not $result.Success -and $OutputPath -and (Test-Path -LiteralPath $OutputPath)) {
            Remove-Item -LiteralPath $OutputPath -Force
        }

        $warnings = @($result.Diagnostics | Where-Object {
            $_.Severity -eq 'Warning' -and $_.Location.IsInSource -and $_.Id -ne 'CS1701' -and $_.Id -ne 'CS1702' })
        [pscustomobject]@{
            Success  = $result.Success
            Errors   = @($result.Diagnostics | Where-Object { $_.Severity -eq 'Error' } | ForEach-Object { $_.ToString() })
            Warnings = @($warnings | ForEach-Object {
                $_.Id + '  ' + [System.IO.Path]::GetFileName($_.Location.SourceTree.FilePath) })
        }
    }
    finally {
        if ($stream) { $stream.Dispose() }
        # A script-block resolver left registered can fire during process teardown on a
        # thread without a runspace and crash the host.
        [System.AppDomain]::CurrentDomain.remove_AssemblyResolve($resolver)
    }
}
