param(
  [switch]$ExportPreview
)

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Drawing

$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
$OutPath = Join-Path $Root "docs\SIDE_EFFECT_MISSED_DOSE_POLICY_GUIDE.pptx"
$PreviewDir = Join-Path $Root "docs\side_effect_missed_dose_policy_guide_preview"

$SlideW = 960
$SlideH = 540

$Text = "111827"
$Muted = "64748B"
$Line = "E5E7EB"
$Panel = "F8FAFC"
$Accent = "2563EB"
$Green = "16A34A"
$Amber = "D97706"
$Red = "DC2626"
$Purple = "7C3AED"
$BlueSoft = "EFF6FF"
$GreenSoft = "F0FDF4"
$AmberSoft = "FFFBEB"
$RedSoft = "FEF2F2"
$PurpleSoft = "F5F3FF"

function Convert-HexColor($Hex) {
  $value = $Hex.TrimStart("#")
  $r = [Convert]::ToInt32($value.Substring(0, 2), 16)
  $g = [Convert]::ToInt32($value.Substring(2, 2), 16)
  $b = [Convert]::ToInt32($value.Substring(4, 2), 16)
  $color = [System.Drawing.Color]::FromArgb($r, $g, $b)
  return [System.Drawing.ColorTranslator]::ToOle($color)
}

function Set-Fill($Shape, $Hex) {
  $Shape.Fill.Visible = -1
  $Shape.Fill.ForeColor.RGB = Convert-HexColor $Hex
}

function Set-Line($Shape, $Hex, $Weight = 0.75) {
  $Shape.Line.Visible = -1
  $Shape.Line.ForeColor.RGB = Convert-HexColor $Hex
  $Shape.Line.Weight = [single]$Weight
}

function Set-NoLine($Shape) {
  $Shape.Line.Visible = 0
}

function Set-TextStyle($Shape, $Size, $Color, $Bold = $false, $Align = 1) {
  $range = $Shape.TextFrame.TextRange
  $range.Font.Name = "Malgun Gothic"
  $range.Font.NameFarEast = "Malgun Gothic"
  $range.Font.Size = $Size
  $range.Font.Bold = if ($Bold) { -1 } else { 0 }
  $range.Font.Color.RGB = Convert-HexColor $Color
  $range.ParagraphFormat.Alignment = $Align
  $Shape.TextFrame.MarginLeft = 7
  $Shape.TextFrame.MarginRight = 7
  $Shape.TextFrame.MarginTop = 5
  $Shape.TextFrame.MarginBottom = 5
}

function Add-Text($Slide, $Name, $Left, $Top, $Width, $Height, $TextValue, $Size = 12, $Color = $Text, $Bold = $false, $Align = 1) {
  $shape = $Slide.Shapes.AddTextbox(1, $Left, $Top, $Width, $Height)
  $shape.Name = $Name
  $shape.TextFrame.TextRange.Text = $TextValue
  Set-TextStyle $shape $Size $Color $Bold $Align
  Set-Fill $shape "FFFFFF"
  Set-NoLine $shape
  return $shape
}

function Add-Box($Slide, $Name, $Left, $Top, $Width, $Height, $TextValue, $Fill = "FFFFFF", $Stroke = $Line, $Size = 11, $Color = $Text, $Bold = $false, $Align = 1) {
  $shape = $Slide.Shapes.AddShape(1, $Left, $Top, $Width, $Height)
  $shape.Name = $Name
  Set-Fill $shape $Fill
  Set-Line $shape $Stroke 0.75
  $shape.TextFrame.TextRange.Text = $TextValue
  Set-TextStyle $shape $Size $Color $Bold $Align
  $shape.TextFrame.VerticalAnchor = 3
  return $shape
}

function Add-Pill($Slide, $Left, $Top, $Width, $TextValue, $Color) {
  $shape = $Slide.Shapes.AddShape(5, $Left, $Top, $Width, 24)
  Set-Fill $shape "FFFFFF"
  Set-Line $shape $Color 1
  $shape.TextFrame.TextRange.Text = $TextValue
  Set-TextStyle $shape 9 $Color $true 2
  $shape.TextFrame.VerticalAnchor = 3
  return $shape
}

function Add-Rule($Slide, $Left, $Top, $Width, $Color = $Line, $Weight = 0.75) {
  $shape = $Slide.Shapes.AddLine($Left, $Top, $Left + $Width, $Top)
  $shape.Line.ForeColor.RGB = Convert-HexColor $Color
  $shape.Line.Weight = [single]$Weight
  return $shape
}

function Add-Arrow($Slide, $X1, $Y1, $X2, $Y2, $Color = "CBD5E1") {
  $shape = $Slide.Shapes.AddLine($X1, $Y1, $X2, $Y2)
  $shape.Line.ForeColor.RGB = Convert-HexColor $Color
  $shape.Line.Weight = [single]1.25
  $shape.Line.EndArrowheadStyle = 3
  return $shape
}

function Add-Header($Slide, $Title, $Subtitle) {
  $bar = $Slide.Shapes.AddShape(1, 34, 32, 5, 46)
  Set-Fill $bar $Accent
  Set-NoLine $bar
  Add-Text $Slide "Title" 54 26 760 34 $Title 21 $Text $true 1 | Out-Null
  Add-Text $Slide "Subtitle" 55 62 790 22 $Subtitle 10 $Muted $false 1 | Out-Null
  Add-Rule $Slide 34 96 888 $Line 0.75 | Out-Null
}

function Add-Footer($Slide, $TextValue, $SlideNo) {
  Add-Text $Slide "Footer" 39 515 730 18 $TextValue 8 $Muted $false 1 | Out-Null
  Add-Text $Slide "Slide Number" 872 515 50 18 "$SlideNo/6" 8 $Muted $false 3 | Out-Null
}

function Add-Slide($Presentation) {
  return $Presentation.Slides.Add($Presentation.Slides.Count + 1, 12)
}

function Add-Slide1($Presentation) {
  $slide = Add-Slide $Presentation
  Add-Header $slide "미복용 정책 추천 기준 재정의" "반복 미복용이라도 원인이 부작용이면 알림 강화가 답이 아닐 수 있다"

  Add-Text $slide "Main" 70 128 820 80 "반복 미복용을 보면 알림 빈도 변경 후보를 제안할 수 있다.`r하지만 미복용의 원인이 부작용이라면 먼저 의료적 확인이 필요하다." 19 $Text $true 1 | Out-Null

  Add-Box $slide "Current" 88 254 330 112 "현재 흐름`r반복 미복용 패턴을 기반으로 알림 강화 후보 생성" $BlueSoft "BFDBFE" 13 $Text $true 2 | Out-Null
  Add-Arrow $slide 420 310 540 310 "CBD5E1" | Out-Null
  Add-Box $slide "Issue" 542 254 330 112 "핵심 이슈`r부작용으로 복용이 어려운 상황에는 알림 강화가 부적절할 수 있음" $RedSoft "FECACA" 13 $Text $true 2 | Out-Null

  Add-Box $slide "Takeaway" 112 430 736 48 "의사결정 포인트: 알림 정책 추천은 '반복 미복용 여부'가 아니라 '미복용 원인'까지 보고 결정해야 한다." $Panel $Line 12 $Text $true 2 | Out-Null
  Add-Footer $slide "비개발자/기획자 공유용 요약" 1
}

function Add-Slide2($Presentation) {
  $slide = Add-Slide $Presentation
  Add-Header $slide "미복용 원인은 하나가 아니다" "같은 미복용 기록도 행동 보조 문제와 의료 확인 문제로 나뉜다"

  Add-Text $slide "Left Heading" 84 132 330 26 "행동 보조로 해결 가능한 경우" 15 $Text $true 1 | Out-Null
  Add-Rule $slide 84 166 330 $Green 1.25 | Out-Null
  Add-Box $slide "Behavior" 84 196 330 180 "깜빡함`r알림을 못 봄`r생활 패턴 변화`r복약 루틴이 불안정함" "FFFFFF" $Line 13 $Text $true 1 | Out-Null

  Add-Text $slide "Right Heading" 546 132 330 26 "의료적 확인이 먼저 필요한 경우" 15 $Text $true 1 | Out-Null
  Add-Rule $slide 546 166 330 $Red 1.25 | Out-Null
  Add-Box $slide "Medical" 546 196 330 180 "약 복용을 망설임`r약 부작용 때문에 복용이 어려움`r의료적 확인이 필요한 증상 발생`r심한 증상 또는 위험 신호" "FFFFFF" $Line 13 $Text $true 1 | Out-Null

  Add-Box $slide "Takeaway" 112 430 736 48 "따라서 정책 추천은 미복용 패턴을 감지한 뒤, 원인 신호를 함께 분류하는 단계가 필요하다." $Panel $Line 12 $Text $true 2 | Out-Null
  Add-Footer $slide "분류 기준: 행동 보조 문제인지, 의료 확인 문제인지 먼저 본다." 2
}

function Add-Slide3($Presentation) {
  $slide = Add-Slide $Presentation
  Add-Header $slide "부작용 상황에서 알림 강화가 위험한 이유" "환자는 '도움'이 아니라 '복용 압박'으로 느낄 수 있다"

  $items = @(
    @("환자 경험", "아픈데 더 먹으라고 재촉한다고 느낄 수 있음", $Red, $RedSoft),
    @("해석 오류", "시스템이 미복용의 실제 원인을 잘못 이해한 것처럼 보임", $Amber, $AmberSoft),
    @("우선순위 충돌", "의료진 확인이 필요한 상황에서 행동 보조 정책이 먼저 노출됨", $Purple, $PurpleSoft),
    @("신뢰 저하", "부작용 평가와 정책 변경 흐름이 충돌하면 사용자 신뢰가 떨어짐", $Accent, $BlueSoft)
  )
  for ($i = 0; $i -lt $items.Count; $i++) {
    $x = 80 + (($i % 2) * 410)
    $y = 142 + ([math]::Floor($i / 2) * 146)
    Add-Pill $slide $x $y 92 $items[$i][0] $items[$i][2] | Out-Null
    Add-Box $slide "Risk" $x ($y + 34) 350 82 $items[$i][1] $items[$i][3] $Line 12 $Text $true 2 | Out-Null
  }

  Add-Box $slide "PHR CTCAE" 112 430 736 48 "PHR 주의사항과 증상이 연결되고 CTCAE 설문에서도 의미 있게 확인되면, 알림 변경보다 부작용 확인과 상담 안내가 우선이다." $Panel $Line 11 $Text $true 2 | Out-Null
  Add-Footer $slide "사용자 신뢰 관점: 의료적 신호를 행동 보조 추천으로 덮지 않는다." 3
}

function Add-Slide4($Presentation) {
  $slide = Add-Slide $Presentation
  Add-Header $slide "권장 정책 기준" "단순 미복용 패턴 중심에서 미복용 원인 기반 조건부 추천으로 전환"

  $rows = @(
    @("1", "단순 깜빡함 / 알림 확인 실패", "알림 빈도나 주기 변경 후보 제안", $Green, $GreenSoft),
    @("2", "부작용 가능성은 있으나 원인 불명확", "임시 복약 보조 후보 + 의료진 확인 필요성 동시 안내", $Amber, $AmberSoft),
    @("3", "부작용 때문에 복용이 어려운 정황 강함", "알림 강화 추천 보류, 현행 유지와 상담 안내", $Red, $RedSoft),
    @("4", "심한 증상 또는 위험 신호", "정책 변경 흐름 중단, 안전 안내와 의료진 연결 우선", $Purple, $PurpleSoft)
  )
  for ($i = 0; $i -lt $rows.Count; $i++) {
    $y = 130 + ($i * 78)
    Add-Pill $slide 76 $y 54 $rows[$i][0] $rows[$i][3] | Out-Null
    Add-Box $slide "Signal" 150 $y 310 54 $rows[$i][1] "FFFFFF" $Line 11 $Text $true 2 | Out-Null
    Add-Arrow $slide 466 ($y + 27) 506 ($y + 27) "CBD5E1" | Out-Null
    Add-Box $slide "Action" 522 $y 350 54 $rows[$i][2] $rows[$i][4] $Line 10 $Text $true 2 | Out-Null
  }

  Add-Footer $slide "추천 기준: 반복 미복용의 원인이 행동 보조 문제일 때만 알림 정책을 추천한다." 4
}

function Add-Slide5($Presentation) {
  $slide = Add-Slide $Presentation
  Add-Header $slide "권장 의사결정" "반복 미복용이면 항상 알림을 강화한다는 기준을 버린다"

  Add-Box $slide "Bad Rule" 90 152 330 130 "기존 단순 기준`r반복 미복용이면 알림 강화 후보 제안" $RedSoft "FECACA" 15 $Text $true 2 | Out-Null
  Add-Arrow $slide 430 216 530 216 "CBD5E1" | Out-Null
  Add-Box $slide "Good Rule" 540 152 330 130 "권장 기준`r반복 미복용의 원인이 행동 보조 문제일 때만 알림 정책 추천" $GreenSoft "BBF7D0" 15 $Text $true 2 | Out-Null

  Add-Text $slide "State Heading" 118 330 720 26 "새로 두면 좋은 상태" 15 $Text $true 2 | Out-Null
  Add-Box $slide "Hold State" 162 366 636 58 "정책 변경 보류: 부작용 확인 필요" $Panel $Line 17 $Text $true 2 | Out-Null
  Add-Text $slide "State Desc" 162 432 636 30 "이 상태에서는 알림 강화보다 현행 유지, 부작용 확인, 의료진/약사 상담 안내를 우선 노출한다." 11 $Muted $false 2 | Out-Null

  Add-Footer $slide "제품 결정: 알림 강화는 복약 행동 보조가 필요한 상황에 한정한다." 5
}

function Add-Slide6($Presentation) {
  $slide = Add-Slide $Presentation
  Add-Header $slide "기획 적용안" "정책 추천 전에 원인 신호를 확인하는 한 단계를 추가한다"

  $steps = @(
    @("01", "미복용 패턴 감지", $Accent),
    @("02", "원인 신호 확인", $Accent),
    @("03", "부작용 가능성 판단", $Amber),
    @("04", "정책 추천 또는 보류", $Green)
  )
  for ($i = 0; $i -lt $steps.Count; $i++) {
    $x = 90 + ($i * 210)
    Add-Text $slide "Step No" $x 138 42 18 $steps[$i][0] 10 $steps[$i][2] $true 1 | Out-Null
    Add-Box $slide "Step" $x 164 150 76 $steps[$i][1] "FFFFFF" $steps[$i][2] 12 $Text $true 2 | Out-Null
    if ($i -lt 3) {
      Add-Arrow $slide ($x + 150) 202 ($x + 198) 202 "CBD5E1" | Out-Null
    }
  }

  Add-Box $slide "Decision Rules" 92 300 356 118 "알림 추천 가능`r깜빡함, 알림 미확인, 생활 패턴 변화처럼 행동 보조가 필요한 경우" $GreenSoft "BBF7D0" 12 $Text $true 2 | Out-Null
  Add-Box $slide "Hold Rules" 512 300 356 118 "알림 추천 보류`r부작용 정황이 강하거나 심한 증상, 위험 신호가 확인되는 경우" $RedSoft "FECACA" 12 $Text $true 2 | Out-Null

  Add-Box $slide "Bottom" 112 450 736 38 "핵심: 정책 추천은 자동화하되, 의료적 신호가 강하면 안전 안내와 상담 흐름이 우선한다." $Panel $Line 11 $Text $true 2 | Out-Null
  Add-Footer $slide "다음 액션: 원인 분류 기준과 보류 상태를 제품/프롬프트/테스트에 반영" 6
}

$app = New-Object -ComObject PowerPoint.Application
$app.DisplayAlerts = 1

try {
  $presentation = $app.Presentations.Add($false)
  try {
    $presentation.PageSetup.SlideWidth = $SlideW
    $presentation.PageSetup.SlideHeight = $SlideH

    Add-Slide1 $presentation
    Add-Slide2 $presentation
    Add-Slide3 $presentation
    Add-Slide4 $presentation
    Add-Slide5 $presentation
    Add-Slide6 $presentation

    $tempPath = Join-Path ([System.IO.Path]::GetTempPath()) ("side_effect_missed_dose_policy_guide_{0}.pptx" -f ([Guid]::NewGuid().ToString("N")))
    $presentation.SaveAs($tempPath)
    $presentation.Close()

    Move-Item -LiteralPath $tempPath -Destination $OutPath -Force
  } catch {
    if ($presentation) {
      $presentation.Close()
    }
    throw
  }

  if ($ExportPreview) {
    New-Item -ItemType Directory -Force -Path $PreviewDir | Out-Null
    Get-ChildItem -Path $PreviewDir -Filter "*.PNG" -ErrorAction SilentlyContinue | Remove-Item -Force
    $check = $app.Presentations.Open($OutPath, $true, $false, $false)
    try {
      $check.Export($PreviewDir, "PNG", 1920, 1080)
    } finally {
      $check.Close()
    }
  }
} finally {
  $app.Quit()
}

Write-Output $OutPath
if ($ExportPreview) {
  Get-ChildItem -Path $PreviewDir -Filter "*.PNG" | Sort-Object Name | Select-Object FullName, Length
}
