param()
$ErrorActionPreference="Stop"
$packageRoot=Split-Path -Parent $PSScriptRoot
$sourceRoot=Join-Path $packageRoot "source"
$demoRoot=Join-Path $PSScriptRoot "demonstrations"
$chrome="C:\Program Files\Google\Chrome\Application\chrome.exe"
$profile=Join-Path $env:TEMP "RootEDCanonicalCellDemoChromeProfile"
New-Item -ItemType Directory -Force -Path $demoRoot,$profile | Out-Null

function Add-Class([string]$Svg,[string]$Id,[string]$ClassName){
  $escaped=[regex]::Escape($Id)
  $pattern="(<g id=`"$escaped`" class=`")([^`"]*)(`")"
  return [regex]::Replace($Svg,$pattern,{'{0}{1} {2}{3}' -f $args[0].Groups[1].Value,$args[0].Groups[2].Value,$ClassName,$args[0].Groups[3].Value},1)
}
function Set-Hidden([string]$Svg,[string]$Id){
  $escaped=[regex]::Escape($Id)
  return [regex]::Replace($Svg,"(<g id=`"$escaped`")",'$1 style="display:none"',1)
}
function Write-Demo([string]$Name,[string]$Svg){
  $svgPath=Join-Path $demoRoot ($Name+".svg")
  $pngPath=Join-Path $demoRoot ($Name+".png")
  Set-Content -LiteralPath $svgPath -Value $Svg -Encoding utf8
  $url=(New-Object System.Uri($svgPath)).AbsoluteUri
  & $chrome --headless=new --no-sandbox --disable-gpu --disable-dev-shm-usage --user-data-dir=$profile --hide-scrollbars --window-size=1000,1000 --screenshot=$pngPath $url | Out-Null
  if(-not (Test-Path -LiteralPath $pngPath)){throw "Render failed: $Name"}
}

$animal=Get-Content -Raw -LiteralPath (Join-Path $sourceRoot "master_animal_cell.svg")
$plant=Get-Content -Raw -LiteralPath (Join-Path $sourceRoot "master_plant_cell.svg")

$nucleusDemo=$animal
foreach($id in @("cell_membrane","cytoplasm","rough_er","smooth_er","golgi_apparatus","mitochondria","ribosomes","vesicles","lysosomes","centrioles")){
  $nucleusDemo=Add-Class $nucleusDemo $id "rooted-muted"
}
$nucleusDemo=Add-Class $nucleusDemo "nucleus" "rooted-highlight"
Write-Demo "01_nucleus_highlight_others_faded" $nucleusDemo

$wallHidden=Set-Hidden $plant "cell_wall"
$wallHidden=Add-Class $wallHidden "cell_membrane" "rooted-highlight"
Write-Demo "02_plant_wall_hidden_membrane_visible" $wallHidden

$allChloroplasts=$plant
foreach($id in @("cell_wall","cell_membrane","cytoplasm","central_vacuole","rough_er","smooth_er","nucleus","golgi_apparatus","mitochondria","ribosomes","vesicles")){
  $allChloroplasts=Add-Class $allChloroplasts $id "rooted-muted"
}
$allChloroplasts=Add-Class $allChloroplasts "chloroplasts" "rooted-highlight"
Write-Demo "03_all_chloroplasts_highlighted" $allChloroplasts

$oneChloroplast=$plant
$oneChloroplast=Add-Class $oneChloroplast "chloroplast_01" "rooted-highlight"
$oneChloroplast=Add-Class $oneChloroplast "chloroplast_02" "rooted-muted"
$oneChloroplast=Add-Class $oneChloroplast "chloroplast_03" "rooted-muted"
Write-Demo "04_chloroplast_01_highlighted" $oneChloroplast

$labeled=$animal.Replace("<svg xmlns=","<svg class=`"show-annotations`" xmlns=")
Write-Demo "05_labeled_instructional_export" $labeled

@{
  schema_version="1.0.0"
  generated=(Get-Date).ToString("yyyy-MM-ddTHH:mm:ssK")
  canonical_sources=@("../source/master_animal_cell.svg","../source/master_plant_cell.svg")
  demonstrations=@(
    @{id="nucleus_highlight";file="01_nucleus_highlight_others_faded.png";target="nucleus";behavior="Only nucleus highlighted; other top-level instructional structures faded."},
    @{id="wall_hidden";file="02_plant_wall_hidden_membrane_visible.png";target="cell_wall";behavior="Cell wall hidden; cell membrane remains visible and is highlighted."},
    @{id="chloroplast_group";file="03_all_chloroplasts_highlighted.png";target="chloroplasts";behavior="All chloroplast children highlighted through their stable parent group."},
    @{id="chloroplast_individual";file="04_chloroplast_01_highlighted.png";target="chloroplast_01";behavior="One chloroplast highlighted by permanent child ID; sibling chloroplasts faded."},
    @{id="labeled_export";file="05_labeled_instructional_export.png";target="annotations";behavior="Optional annotations enabled independently from instructional artwork."}
  )
} | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath (Join-Path $demoRoot "manifest.json") -Encoding utf8
