param(
  [switch]$ExportPreview
)

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Drawing

$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
$OutPath = Join-Path $Root "docs\LLM_POLICY_DECISION_STRUCTURE_CHANGE.pptx"
$PreviewDir = Join-Path $Root "docs\llm_policy_decision_structure_change_preview"

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
  Add-Text $Slide "Slide Number" 872 515 50 18 "$SlideNo/5" 8 $Muted $false 3 | Out-Null
}

function Add-Slide($Presentation) {
  return $Presentation.Slides.Add($Presentation.Slides.Count + 1, 12)
}

function Add-Slide1($Presentation) {
  $slide = Add-Slide $Presentation
  Add-Header $slide "LLM 중심 정책 결정 구조 변경" "휴리스틱 후보 생성에서 LLM 의사결정 구조로 전환"

  Add-Text $slide "Main" 70 128 820 74 "정책값을 휴리스틱이 먼저 정하지 않고,`rLLM이 관찰 자료를 바탕으로 policy_action을 결정한다." 21 $Text $true 1 | Out-Null

  Add-Box $slide "Before" 96 252 330 110 "변경 전`rmiss_rate 기반 휴리스틱이 추가 알림 후보를 먼저 생성" $RedSoft "FECACA" 13 $Text $true 2 | Out-Null
  Add-Arrow $slide 430 307 530 307 "CBD5E1" | Out-Null
  Add-Box $slide "After" 540 252 330 110 "변경 후`rLLM이 increase / keep / pause / ask / escalate를 직접 선택" $GreenSoft "BBF7D0" 13 $Text $true 2 | Out-Null

  Add-Box $slide "Takeaway" 112 430 736 48 "핵심 변화: LLM은 후보 변환자가 아니라 정책 의사결정자가 되고, system_app은 안전 검증에 집중한다." $Panel $Line 12 $Text $true 2 | Out-Null
  Add-Footer $slide "LLM 중심 정책 결정 구조 변경 요약" 1
}

function Add-Slide2($Presentation) {
  $slide = Add-Slide $Presentation
  Add-Header $slide "변경 전/후 구조" "정책 후보를 먼저 만들던 흐름을 정책 액션 결정 흐름으로 바꿈"

  Add-Pill $slide 92 130 72 "Before" $Red | Out-Null
  $before = @("Daily Pattern 입력", "Pattern Analyzer", "휴리스틱 후보 생성", "Policy Planner", "Tool call 변환")
  for ($i = 0; $i -lt $before.Count; $i++) {
    $x = 90 + ($i * 160)
    Add-Box $slide "Before Step" $x 170 118 58 $before[$i] "FFFFFF" $Red 9 $Text $true 2 | Out-Null
    if ($i -lt $before.Count - 1) {
      Add-Arrow $slide ($x + 118) 199 ($x + 150) 199 "CBD5E1" | Out-Null
    }
  }
  Add-Text $slide "Before Note" 108 248 744 24 "한계: 정책값 결정이 휴리스틱 단계에서 이미 끝나고, LLM은 후보 변환자에 가까웠다." 11 $Muted $false 2 | Out-Null

  Add-Pill $slide 92 312 72 "After" $Green | Out-Null
  $after = @("Daily Pattern 입력", "Pattern Features", "Policy Planner LLM", "policy_action", "안전 검증/알림")
  for ($i = 0; $i -lt $after.Count; $i++) {
    $x = 90 + ($i * 160)
    $stroke = if ($i -eq 3) { $Accent } else { $Green }
    Add-Box $slide "After Step" $x 352 118 58 $after[$i] "FFFFFF" $stroke 9 $Text $true 2 | Out-Null
    if ($i -lt $after.Count - 1) {
      Add-Arrow $slide ($x + 118) 381 ($x + 150) 381 "CBD5E1" | Out-Null
    }
  }
  Add-Text $slide "After Note" 108 430 744 24 "변경: 휴리스틱은 관찰 요약만 만들고, LLM이 정책 액션을 선택한다." 11 $Muted $false 2 | Out-Null
  Add-Footer $slide "구조 변화: 후보 생성 중심에서 액션 결정 중심으로 이동" 2
}

function Add-Slide3($Presentation) {
  $slide = Add-Slide $Presentation
  Add-Header $slide "구현 포인트" "정책 후보 생성 로직을 제거하고 판단 상태를 구조화"

  $items = @(
    @("1", "휴리스틱 후보 제거", "daily pattern 경로에서 suggest_policies_from_features() 호출 제거", $Red, $RedSoft),
    @("2", "LLM 입력 확장", "policy_generation_mode, pattern_analysis, pattern_features, 정책 범위/경계, 대화 맥락 전달", $Accent, $BlueSoft),
    @("3", "정책 결정 필드 추가", "policy_action, reason_category, proposed_policy_summary 추가", $Green, $GreenSoft),
    @("4", "프롬프트 v14", "miss_rate만 보고 자동 알림 강화 금지, LLM이 policy_action 선택", $Purple, $PurpleSoft)
  )
  for ($i = 0; $i -lt $items.Count; $i++) {
    $x = 78 + (($i % 2) * 412)
    $y = 132 + ([math]::Floor($i / 2) * 146)
    Add-Pill $slide $x $y 52 $items[$i][0] $items[$i][3] | Out-Null
    Add-Box $slide "Implementation" $x ($y + 34) 354 82 "$($items[$i][1])`r$($items[$i][2])" $items[$i][4] $Line 10 $Text $true 2 | Out-Null
  }
  Add-Footer $slide "구현 핵심: 정책 결정 근거를 구조화하고, 부작용 맥락을 입력 관찰값으로 전달" 3
}

function Add-Slide4($Presentation) {
  $slide = Add-Slide $Presentation
  Add-Header $slide "해결된 문제" "tool call 유무가 아니라 policy_action으로 판단을 해석"

  $rows = @(
    @("휴리스틱이 정책 후보를 먼저 결정", "LLM이 policy_action을 직접 선택", $GreenSoft),
    @("부작용 상황에서도 알림 강화 후보 생성", "side_effect_concern이면 보류/의료 확인 가능", $AmberSoft),
    @("tool call 없음이 실패처럼 보임", "pause/keep/ask/escalate는 정상 no-change로 기록", $BlueSoft),
    @("정책 판단 이유 설명 어려움", "reason_category와 proposed_policy_summary로 감사 가능", $PurpleSoft)
  )
  for ($i = 0; $i -lt $rows.Count; $i++) {
    $y = 130 + ($i * 78)
    Add-Box $slide "Problem" 86 $y 330 54 $rows[$i][0] "FFFFFF" $Line 10 $Text $true 2 | Out-Null
    Add-Arrow $slide 430 ($y + 27) 500 ($y + 27) "CBD5E1" | Out-Null
    Add-Box $slide "Solved" 518 $y 356 54 $rows[$i][1] $rows[$i][2] $Line 10 $Text $true 2 | Out-Null
  }
  Add-Footer $slide "운영 의미: 실패 로그와 정상 보류/유지를 명확히 구분" 4
}

function Add-Slide5($Presentation) {
  $slide = Add-Slide $Presentation
  Add-Header $slide "테스트 결과와 기대 효과" "정책 변경이 미복용률만으로 자동 결정되지 않는다"

  Add-Box $slide "Tests" 92 146 300 118 "테스트 결과`r168 passed in 12.81s" $GreenSoft "BBF7D0" 18 $Text $true 2 | Out-Null
  Add-Box $slide "Effect1" 438 126 390 70 "환자 답변과 부작용 맥락이 정책 결정 전에 반영됨" "FFFFFF" $Line 12 $Text $true 2 | Out-Null
  Add-Box $slide "Effect2" 438 216 390 70 "부작용으로 복약을 피하는 상황에서는 증상 평가와 의료진 확인을 우선" "FFFFFF" $Line 12 $Text $true 2 | Out-Null
  Add-Box $slide "Effect3" 438 306 390 70 "system_app은 안전 검증과 중복 차단 역할에 집중" "FFFFFF" $Line 12 $Text $true 2 | Out-Null

  Add-Box $slide "Bottom" 112 430 736 48 "기대 효과: '미복용률이 높으니 알림 강화'가 아니라 '상황에 맞는 정책 액션'으로 전환된다." $Panel $Line 12 $Text $true 2 | Out-Null
  Add-Footer $slide "결론: LLM 중심 정책 결정으로 제품 판단과 감사 가능성을 함께 개선" 5
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

    $tempPath = Join-Path ([System.IO.Path]::GetTempPath()) ("llm_policy_decision_structure_change_{0}.pptx" -f ([Guid]::NewGuid().ToString("N")))
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
