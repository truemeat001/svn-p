param(
    [Parameter(Mandatory=$true)][string]$WebView2PackagePath,
    [Parameter(Mandatory=$true)][string]$OutDir
)
$ErrorActionPreference = 'Stop'
$compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
if (!(Test-Path $compiler)) { throw 'Existing .NET Framework C# compiler not found. No installation was attempted.' }
if (Test-Path $OutDir) { throw 'OutDir already exists. Choose a new isolated build directory; no existing files will be overwritten.' }
$diagnostics = $OutDir.TrimEnd('\','/') + '.diagnostics'
if (Test-Path $diagnostics) { throw 'Diagnostics directory already exists. Choose a fresh build directory.' }
$package = (Resolve-Path $WebView2PackagePath).Path
$nuspecPath = Join-Path $package 'Microsoft.Web.WebView2.nuspec'
[xml]$nuspec = Get-Content -Raw -LiteralPath $nuspecPath
$id = $nuspec.SelectSingleNode('/*[local-name()="package"]/*[local-name()="metadata"]/*[local-name()="id"]').InnerText
$version = $nuspec.SelectSingleNode('/*[local-name()="package"]/*[local-name()="metadata"]/*[local-name()="version"]').InnerText
if ($id -cne 'Microsoft.Web.WebView2' -or $version -cne '1.0.3537.50') { throw 'Expected official Microsoft.Web.WebView2 NuGet package 1.0.3537.50. Metadata is not a cryptographic signature; retrieve only from the official NuGet source.' }
$core = Join-Path $package 'lib\net462\Microsoft.Web.WebView2.Core.dll'
$forms = Join-Path $package 'lib\net462\Microsoft.Web.WebView2.WinForms.dll'
$loader = Join-Path $package 'runtimes\win-x64\native\WebView2Loader.dll'
foreach ($file in @($core,$forms,$loader)) { if (!(Test-Path $file)) { throw "Required official WebView2 package file missing: $file" } }
# Only explicit top-level candidates from the pinned package; never recurse over
# a supplied dependency directory or copy arbitrary nuspec-directed paths.
$licenseCandidates = @('LICENSE.txt','LICENSE','LICENSE.md')
$noticeCandidates = @('THIRD-PARTY-NOTICES.txt','ThirdPartyNotices.txt','NOTICE.txt','NOTICE')
$licenseInputs = @()
$noticeInputs = @()
foreach ($name in ($licenseCandidates + $noticeCandidates)) {
    $path = Join-Path $package $name
    if (Test-Path -LiteralPath $path -PathType Leaf) {
        $entry = Get-Item -LiteralPath $path
        if (($entry.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or $entry.Length -gt 4MB) { throw 'License/notice input must be a bounded regular file.' }
        if ($licenseCandidates -contains $name) { $licenseInputs += $path } else { $noticeInputs += $path }
    }
}
if ($licenseInputs.Count -eq 0) { throw 'Pinned WebView2 package license is absent from known filenames. Stop redistribution and inspect the official package.' }
$missingNotices = $noticeInputs.Count -eq 0
if ($missingNotices) { Write-Warning 'No separate WebView2 third-party notice found under the explicit known names; this absence is recorded in the build manifest. Review the official package before redistribution.' }
New-Item -ItemType Directory -Path $OutDir | Out-Null
New-Item -ItemType Directory -Path $diagnostics | Out-Null
$out = (Resolve-Path $OutDir).Path
$diag = (Resolve-Path $diagnostics).Path
$common = @('/nologo','/langversion:5','/platform:x64','/optimize+','/r:System.dll','/r:System.Core.dll','/r:System.Web.Extensions.dll')
function Invoke-Compiler([string]$Name, [string[]]$Arguments) {
    $output = & $compiler @Arguments 2>&1
    $code = $LASTEXITCODE
    $output | Set-Content -LiteralPath (Join-Path $diag "$Name.compiler.log") -Encoding UTF8
    $output | Write-Output
    if ($code -ne 0) { throw "$Name compilation failed; see diagnostics." }
}
Invoke-Compiler 'ProtocolTests' ($common + @('/target:exe',"/out:$diag\ProtocolTests.exe","$PSScriptRoot\Protocol.cs","$PSScriptRoot\ProfilePolicy.cs","$PSScriptRoot\ProtocolTests.cs"))
$testOutput = & "$diag\ProtocolTests.exe" 2>&1
$testExit = $LASTEXITCODE
$testOutput | Set-Content -LiteralPath "$diag\ProtocolTests.log" -Encoding UTF8
$testOutput | Write-Output
if ($testExit -ne 0) { throw 'Protocol checks failed' }
Invoke-Compiler 'ProfilePolicyTests' ($common + @('/target:exe',"/out:$diag\ProfilePolicyTests.exe","$PSScriptRoot\ProfilePolicy.cs","$PSScriptRoot\ProfilePolicyTests.cs"))
$profileOutput = & "$diag\ProfilePolicyTests.exe" $diag 2>&1
$profileExit = $LASTEXITCODE
$profileOutput | Set-Content -LiteralPath "$diag\ProfilePolicyTests.log" -Encoding UTF8
$profileOutput | Write-Output
if ($profileExit -ne 0) { throw 'Windows profile-policy checks failed' }
Invoke-Compiler 'SVNDesk' ($common + @('/target:winexe','/r:System.Drawing.dll','/r:System.Windows.Forms.dll',"/r:$core","/r:$forms","/out:$out\SVNDesk.exe","$PSScriptRoot\Protocol.cs","$PSScriptRoot\ProfilePolicy.cs","$PSScriptRoot\Program.cs"))
foreach ($file in @($core,$forms,$loader)) { Copy-Item -LiteralPath $file -Destination $out }
Copy-Item -LiteralPath "$PSScriptRoot\SVNDesk.exe.config" -Destination $out
$licenseOut = Join-Path $out 'Licenses'
New-Item -ItemType Directory -Path $licenseOut | Out-Null
$legalOutputs = @()
foreach ($file in ($licenseInputs + $noticeInputs)) {
    $destination = Join-Path $licenseOut ([IO.Path]::GetFileName($file))
    Copy-Item -LiteralPath $file -Destination $destination
    $legalOutputs += $destination
}
$hashes = @()
foreach ($file in @($compiler,$nuspecPath,"$PSScriptRoot\Protocol.cs","$PSScriptRoot\ProfilePolicy.cs","$PSScriptRoot\Program.cs","$PSScriptRoot\ProtocolTests.cs","$PSScriptRoot\ProfilePolicyTests.cs","$PSScriptRoot\build.ps1") + @(Get-ChildItem -File -LiteralPath $out | ForEach-Object FullName) + $legalOutputs) {
    $hashes += [ordered]@{ name = [IO.Path]::GetFileName($file); sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $file).Hash.ToLowerInvariant() }
}
[ordered]@{ schema = 'svn-desk-native-build/1'; utc = [DateTime]::UtcNow.ToString('o'); architecture = 'x64'; compilerVersion = (Get-Item $compiler).VersionInfo.FileVersion; webView2Package = "$id/$version"; protocolTests = 'passed'; profilePolicyTests = 'passed'; nativeGuiTest = 'not_run'; separateWebView2NoticeAbsent = $missingNotices; redistributedLegalFiles = @($legalOutputs | ForEach-Object { 'Licenses/' + [IO.Path]::GetFileName($_) }); hashes = $hashes } | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath "$diag\build-manifest.json" -Encoding UTF8
Write-Output "Compiled host and passed protocol checks at $out. Diagnostics: $diag. GUI/lifecycle tests are separate."
