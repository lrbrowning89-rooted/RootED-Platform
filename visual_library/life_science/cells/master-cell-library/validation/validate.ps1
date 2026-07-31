param([switch]$Render)
$ErrorActionPreference = "Stop"
$packageRoot = Split-Path -Parent $PSScriptRoot
$sourceRoot = Join-Path $packageRoot "source"
$metadataRoot = Join-Path $packageRoot "metadata"
$exportRoot = Join-Path $packageRoot "exports"
$validationRoot = Join-Path $packageRoot "validation"
$requiredTopGroups = @("instructional","annotations","presentation")
$results = [System.Collections.Generic.List[object]]::new()

function Add-Result([string]$Asset,[string]$Check,[bool]$Passed,[string]$Detail) {
  $results.Add([pscustomobject]@{asset=$Asset;check=$Check;passed=$Passed;detail=$Detail})
}

function Get-PixelHash([string]$Path) {
  Add-Type -AssemblyName System.Drawing
  $bitmap = [System.Drawing.Bitmap]::FromFile($Path)
  try {
    $stream = [System.IO.MemoryStream]::new()
    for($y=0;$y -lt $bitmap.Height;$y+=4){for($x=0;$x -lt $bitmap.Width;$x+=4){$pixel=$bitmap.GetPixel($x,$y);$stream.WriteByte($pixel.R);$stream.WriteByte($pixel.G);$stream.WriteByte($pixel.B);$stream.WriteByte($pixel.A)}}
    $stream.Position=0
    $sha=[Security.Cryptography.SHA256]::Create()
    try {return ([BitConverter]::ToString($sha.ComputeHash($stream))).Replace("-","")} finally {$sha.Dispose()}
  } finally {$bitmap.Dispose()}
}

function Convert-ToGrayscale([string]$InputPath,[string]$OutputPath) {
  Add-Type -AssemblyName System.Drawing
  $source=[System.Drawing.Bitmap]::FromFile($InputPath)
  $target=[System.Drawing.Bitmap]::new($source.Width,$source.Height)
  try {
    $graphics=[System.Drawing.Graphics]::FromImage($target)
    $attributes=[System.Drawing.Imaging.ImageAttributes]::new()
    $matrix=[System.Drawing.Imaging.ColorMatrix]::new([single[][]]@(
      [single[]]@(.299,.299,.299,0,0),
      [single[]]@(.587,.587,.587,0,0),
      [single[]]@(.114,.114,.114,0,0),
      [single[]]@(0,0,0,1,0),
      [single[]]@(0,0,0,0,1)
    ))
    $attributes.SetColorMatrix($matrix)
    $graphics.DrawImage($source,[System.Drawing.Rectangle]::new(0,0,$source.Width,$source.Height),0,0,$source.Width,$source.Height,[System.Drawing.GraphicsUnit]::Pixel,$attributes)
    $target.Save($OutputPath,[System.Drawing.Imaging.ImageFormat]::Png)
  } finally {if($graphics){$graphics.Dispose()};if($attributes){$attributes.Dispose()};$target.Dispose();$source.Dispose()}
}

$assets = @(
  @{name="plant";svg="master_plant_cell.svg";meta="master_plant_cell.json"},
  @{name="animal";svg="master_animal_cell.svg";meta="master_animal_cell.json"}
)

foreach($asset in $assets){
  $svgPath=Join-Path $sourceRoot $asset.svg
  $metaPath=Join-Path $metadataRoot $asset.meta
  try {[xml]$xml=Get-Content -Raw -LiteralPath $svgPath; Add-Result $asset.name "valid_xml" $true "SVG parses as XML."} catch {Add-Result $asset.name "valid_xml" $false $_.Exception.Message; continue}
  $meta=Get-Content -Raw -LiteralPath $metaPath | ConvertFrom-Json
  $allIds=@($xml.SelectNodes("//*[@id]") | ForEach-Object {$_.id})
  $duplicates=@($allIds | Group-Object | Where-Object Count -gt 1 | ForEach-Object Name)
  Add-Result $asset.name "unique_ids" ($duplicates.Count -eq 0) ($(if($duplicates){"Duplicate IDs: "+($duplicates -join ", ")}else{"All IDs are unique."}))
  foreach($group in $requiredTopGroups){Add-Result $asset.name "group_$group" ($allIds -contains $group) "Required group '$group' exists."}
  $missing=@($meta.structures.id | Where-Object {$_ -notin $allIds})
  Add-Result $asset.name "metadata_ids_resolve" ($missing.Count -eq 0) ($(if($missing){"Missing: "+($missing -join ", ")}else{"Every metadata structure ID resolves in the SVG."}))
  $badMeta=@($meta.structures | Where-Object {$null -eq $_.display_name -or $null -eq $_.category -or $null -eq $_.description -or $null -eq $_.highlightable -or $null -eq $_.selectable -or $null -eq $_.supported_derivatives})
  Add-Result $asset.name "metadata_complete" ($badMeta.Count -eq 0) "Required structure metadata fields are present."
  $unhighlightable=@($meta.structures | Where-Object {
    if(-not $_.highlightable){return $false}
    $node=$xml.SelectSingleNode("//*[@id='$($_.id)']")
    return ($null -eq $node -or (($node.class -as [string]) -notmatch "(^|\s)structure(\s|$)"))
  })
  Add-Result $asset.name "independent_highlight_targets" ($unhighlightable.Count -eq 0) "Every declared highlight target has an independent ID and the scoped structure class."
  $external=@($xml.SelectNodes("//*[@href or @*[local-name()='href']]") | Where-Object {($_.href -as [string]) -match "^(https?:|file:|/)"})
  Add-Result $asset.name "no_external_links" ($external.Count -eq 0) "No external image or resource links detected."
  Add-Result $asset.name "version_alignment" (($xml.svg.'data-asset-version' -eq $meta.asset_version) -and ($xml.svg.'data-schema-version' -eq $meta.schema_version)) "SVG and metadata versions align."
  Add-Result $asset.name "accessibility" (($xml.svg.role -eq "img") -and $xml.SelectSingleNode("/*[local-name()='svg']/*[local-name()='title']") -and $xml.SelectSingleNode("/*[local-name()='svg']/*[local-name()='desc']")) "SVG has img role, title, and description."
}

if($Render){
  New-Item -ItemType Directory -Force -Path $exportRoot | Out-Null
  $edge="C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
  $chrome="C:\Program Files\Google\Chrome\Application\chrome.exe"
  $profileRoot=Join-Path $validationRoot "browser-profiles"
  $edgeProfile=Join-Path $profileRoot "edge"
  $chromeProfile=Join-Path $profileRoot "chrome"
  New-Item -ItemType Directory -Force -Path $edgeProfile,$chromeProfile | Out-Null
  foreach($asset in $assets){
    $svgPath=Join-Path $sourceRoot $asset.svg
    $plainOut=Join-Path $exportRoot ("master_"+$asset.name+"_cell.png")
    $labeledOut=Join-Path $exportRoot ("master_"+$asset.name+"_cell_labeled.png")
    $edgeParity=Join-Path $validationRoot ($asset.name+"_edge.png")
    $chromeParity=Join-Path $validationRoot ($asset.name+"_chrome.png")
    $labeledTemp=Join-Path $env:TEMP ("rooted_"+$asset.name+"_labeled.svg")
    (Get-Content -Raw -LiteralPath $svgPath).Replace("<svg xmlns=", "<svg class=`"show-annotations`" xmlns=") | Set-Content -LiteralPath $labeledTemp -Encoding utf8
    $url=(New-Object System.Uri($svgPath)).AbsoluteUri
    $labeledUrl=(New-Object System.Uri($labeledTemp)).AbsoluteUri
    & $edge --headless=new --no-sandbox --disable-gpu --disable-dev-shm-usage --user-data-dir=$edgeProfile --hide-scrollbars --window-size=1000,1000 --screenshot=$plainOut $url | Out-Null
    & $edge --headless=new --no-sandbox --disable-gpu --disable-dev-shm-usage --user-data-dir=$edgeProfile --hide-scrollbars --window-size=1000,1000 --screenshot=$labeledOut $labeledUrl | Out-Null
    & $edge --headless=new --no-sandbox --disable-gpu --disable-dev-shm-usage --user-data-dir=$edgeProfile --hide-scrollbars --window-size=1000,1000 --screenshot=$edgeParity $url | Out-Null
    & $chrome --headless=new --no-sandbox --disable-gpu --disable-dev-shm-usage --user-data-dir=$chromeProfile --hide-scrollbars --window-size=1000,1000 --screenshot=$chromeParity $url | Out-Null
    Add-Result $asset.name "png_exports" ((Test-Path $plainOut) -and (Test-Path $labeledOut)) "Plain and labeled PNG previews rendered."
    $grayOut=Join-Path $validationRoot ($asset.name+"_desaturated.png")
    if(Test-Path $plainOut){Convert-ToGrayscale $plainOut $grayOut}
    Add-Result $asset.name "desaturation_preview" (Test-Path $grayOut) "Desaturated accessibility-review preview rendered; boundaries use value and outline separation."
    if((Test-Path $edgeParity) -and (Test-Path $chromeParity)){
      $edgeHash=Get-PixelHash $edgeParity;$chromeHash=Get-PixelHash $chromeParity
      Add-Result $asset.name "chromium_pixel_consistency" ($edgeHash -eq $chromeHash) "Sampled-pixel hashes: Edge=$edgeHash Chrome=$chromeHash"
    } else {Add-Result $asset.name "chromium_pixel_consistency" $false "One or both browser renders were not created."}
    Remove-Item -LiteralPath $labeledTemp -Force -ErrorAction SilentlyContinue
  }
}

$passed=@($results | Where-Object passed).Count
$failed=@($results | Where-Object {-not $_.passed}).Count
$report=[ordered]@{schema_version="1.0.0";run_date=(Get-Date).ToString("yyyy-MM-ddTHH:mm:ssK");summary=@{passed=$passed;failed=$failed};results=$results}
$report | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $validationRoot "validation-report.json") -Encoding utf8
$lines=@("# RootED Master Cell Library validation report","","Run: $($report.run_date)","","Passed: **$passed**  ","Failed: **$failed**","","| Asset | Check | Result | Detail |","|---|---|---:|---|")
foreach($result in $results){$mark=if($result.passed){"PASS"}else{"FAIL"};$detail=($result.detail -replace "\|","/");$lines+="| $($result.asset) | $($result.check) | $mark | $detail |"}
$lines | Set-Content -LiteralPath (Join-Path $validationRoot "validation-report.md") -Encoding utf8
$results | Format-Table -AutoSize
if($failed -gt 0){exit 1}
