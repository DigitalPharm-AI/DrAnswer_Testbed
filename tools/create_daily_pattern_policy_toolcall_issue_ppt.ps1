param(
  [switch]$ExportPreview
)

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Drawing

$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
$OutPath = Join-Path $Root "docs\DAILY_PATTERN_POLICY_TOOLCALL_ISSUE.pptx"
$PreviewDir = Join-Path $Root "docs\daily_pattern_policy_toolcall_issue_preview"

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
  $Shape.TextFrame.MarginLeft = 6
  $Shape.TextFrame.MarginRight = 6
  $Shape.TextFrame.MarginTop = 4
  $Shape.TextFrame.MarginBottom = 4
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
  Add-Text $Slide "Title" 54 26 650 34 $Title 21 $Text $true 1 | Out-Null
  Add-Text $Slide "Subtitle" 55 62 760 22 $Subtitle 10 $Muted $false 1 | Out-Null
  Add-Rule $Slide 34 96 888 $Line 0.75 | Out-Null
}

function Add-Footer($Slide, $TextValue, $SlideNo) {
  Add-Text $Slide "Footer" 39 515 690 18 $TextValue 8 $Muted $false 1 | Out-Null
  Add-Text $Slide "Slide Number" 872 515 50 18 "$SlideNo/6" 8 $Muted $false 3 | Out-Null
}

function Add-Slide($Presentation) {
  return $Presentation.Slides.Add($Presentation.Slides.Count + 1, 12)
}

function Add-Slide1($Presentation) {
  $slide = Add-Slide $Presentation
  Add-Header $slide "Daily Pattern Tool Call 누락 이슈" "분석은 성공했지만 정책 후보 확인 알림 생성이 멈춘 사례"
  Add-Text $slide "Main" 54 122 840 70 "패턴 분석과 후보 생성은 완료됐지만,`r최종 후보 알림을 만드는 Tool call이 비어 제품 흐름이 끊겼다." 20 $Text $true 1 | Out-Null

  $metrics = @(
    @("복약 기록", "3회 미복용", "아침 08:00 / 점심 13:00 / 야간 21:00", $Accent),
    @("분석 결과", "후보 3개 생성", "schema recovery 후 suggested_policies 정상 생성", $Green),
    @("누락 지점", "Tool call 0개", "propose_notification_policy가 반환되지 않음", $Red),
    @("사용자 영향", "알림 미생성", "자동 정책 조정 확인 흐름 중단", $Amber)
  )
  for ($i = 0; $i -lt $metrics.Count; $i++) {
    $x = 54 + ($i * 222)
    Add-Pill $slide $x 228 88 $metrics[$i][0] $metrics[$i][3] | Out-Null
    Add-Box $slide "Metric" $x 264 184 86 "$($metrics[$i][1])`r`r$($metrics[$i][2])" "FFFFFF" $Line 11 $Text $true 2 | Out-Null
  }

  Add-Box $slide "Takeaway" 82 420 796 50 "핵심은 '분석 실패'가 아니라 '후보를 실행 가능한 제품 액션으로 전환하지 못한 판단 기준'이다." $Panel $Line 12 $Text $true 2 | Out-Null
  Add-Footer $slide "Daily pattern 정책 후보 Tool call 누락 이슈" 1
}

function Add-Slide2($Presentation) {
  $slide = Add-Slide $Presentation
  Add-Header $slide "관찰된 현상" "후보는 있었지만 실행 가능한 Tool call이 없었다"
  $columns = @(
    @("정상", $Green, "분석/후보 생성", "summary: 2026-04-20 총 3회 미복용`rrepeated_missed_slots: 3개 시간대`rsuggested_policy_count: 3"),
    @("문제", $Red, "policy_planner 응답", "tool_calls: []`rneeds_clarification: 3개`radvice: 의료진 상담 우선"),
    @("로그", $Amber, "서버 결과", "daily_pattern_policy_tool_call_missing`rtools_executed: false`rdecision: pattern_policy_recommendation")
  )
  for ($i = 0; $i -lt $columns.Count; $i++) {
    $x = 58 + ($i * 276)
    Add-Pill $slide $x 128 62 $columns[$i][0] $columns[$i][1] | Out-Null
    Add-Text $slide "Column Heading" $x 170 230 24 $columns[$i][2] 13 $Text $true 1 | Out-Null
    Add-Rule $slide $x 205 234 $Line 0.75 | Out-Null
    Add-Box $slide "Column Body" $x 226 234 132 $columns[$i][3] "FFFFFF" $Line 10 $Text $false 1 | Out-Null
  }
  Add-Text $slide "Finding" 80 420 800 34 "실패 지점은 패턴 분석이 아니라 정책 후보를 알림 후보로 넘기는 마지막 판단 단계다." 14 $Text $true 2 | Out-Null
  Add-Footer $slide "비개발 관점: 데이터는 준비됐지만 제품 액션으로 전환되지 않은 상태" 2
}

function Add-Slide3($Presentation) {
  $slide = Add-Slide $Presentation
  Add-Header $slide "현재 처리 흐름" "suggested_policies만으로는 알림이 만들어지지 않는다"
  $steps = @(
    @("01", "패턴 분석", "pattern_analyzer"),
    @("02", "후보 생성", "suggested_policies"),
    @("03", "정책 판단", "policy_planner"),
    @("04", "Tool 추출", "propose_notification_policy"),
    @("05", "확인 알림", "환자 확인용 후보")
  )
  for ($i = 0; $i -lt $steps.Count; $i++) {
    $x = 52 + ($i * 176)
    $color = if ($i -eq 3) { $Red } else { $Accent }
    Add-Text $slide "Step No" $x 132 42 18 $steps[$i][0] 10 $color $true 1 | Out-Null
    Add-Box $slide "Step" $x 158 137 86 "$($steps[$i][1])`r$($steps[$i][2])" "FFFFFF" $color 10 $Text $true 2 | Out-Null
    if ($i -lt 4) {
      Add-Arrow $slide ($x + 137) 201 ($x + 168) 201 "CBD5E1" | Out-Null
    }
  }
  Add-Box $slide "Break" 550 300 235 70 "단절점`rTool call이 비어 5번 단계로 넘어가지 못함" "FEF2F2" "FECACA" 11 $Text $true 2 | Out-Null
  Add-Arrow $slide 610 244 635 300 $Red | Out-Null
  Add-Box $slide "Condition" 82 410 796 54 "정책 확인 알림은 policy_planner가 propose_notification_policy Tool call을 반환해야만 생성된다." $Panel $Line 13 $Text $true 2 | Out-Null
  Add-Footer $slide "해결 초점: 후보 생성 로직보다 policy_planner의 의사결정 기준" 3
}

function Add-Slide4($Presentation) {
  $slide = Add-Slide $Presentation
  Add-Header $slide "원인" "프롬프트 기준이 두 판단을 동시에 허용했다"
  Add-Box $slide "Prompt A" 82 142 338 84 "지침 A`r정책 변경 후보가 필요하고 도구가 있으면 tool_calls 반환" "F0FDF4" "BBF7D0" 12 $Text $true 2 | Out-Null
  Add-Box $slide "Prompt B" 540 142 338 84 "지침 B`r정책 적용이 필요 없거나 모호하면 clarification/advice 반환" "FFFBEB" "FDE68A" 12 $Text $true 2 | Out-Null
  Add-Arrow $slide 250 226 410 278 "CBD5E1" | Out-Null
  Add-Arrow $slide 710 226 550 278 "CBD5E1" | Out-Null
  Add-Box $slide "Decision" 322 278 316 92 "실제 판단`r메스꺼움/부작용 가능성을 더 크게 보고 의료진 상담 우선으로 Tool call 생략" "FEF2F2" "FECACA" 11 $Text $true 2 | Out-Null
  Add-Text $slide "Root Cause" 86 420 790 38 "근본 원인: 반복 미복용이 명확할 때 후보 알림을 만들어야 한다는 제품 기준이 프롬프트에 고정되어 있지 않았다." 13 $Text $true 2 | Out-Null
  Add-Footer $slide "단순 실행 오류보다 LLM 의사결정 기준 불일치에 가까운 이슈" 4
}

function Add-Slide5($Presentation) {
  $slide = Add-Slide $Presentation
  Add-Header $slide "해결 방향 비교" "권장안은 후보 생성과 의료진 확인 안내를 함께 제공하는 B안"
  $options = @(
    @("A안", "부작용 가능성 시 보류", "의료 리스크 상황에서 복약 압박 감소`r명확한 미복용 패턴에도 자동 흐름 중단", $Amber, "FFFFFF", $Line),
    @("B안", "후보 생성 + 의료진 확인 안내", "반복 미복용이 명확하면 Tool call 반환`r의료진 확인 필요성과 알림 변경 한계를 메시지에 병기", $Green, "F0FDF4", "86EFAC"),
    @("C안", "부작용 경로와 정책 경로 분리", "의료 안내와 정책 후보가 명확히 분리`rUI와 대화 상태 관리 복잡도 증가", $Purple, "FFFFFF", $Line)
  )
  for ($i = 0; $i -lt $options.Count; $i++) {
    $x = 58 + ($i * 288)
    $top = if ($i -eq 1) { 118 } else { 136 }
    $height = if ($i -eq 1) { 214 } else { 188 }
    Add-Pill $slide $x $top 58 $options[$i][0] $options[$i][3] | Out-Null
    Add-Box $slide "Option" $x ($top + 38) 236 $height "$($options[$i][1])`r`r$($options[$i][2])" $options[$i][4] $options[$i][5] 11 $Text $true 1 | Out-Null
  }
  Add-Box $slide "Recommendation" 82 422 796 42 "B안은 기존 설계인 '정책 변경은 즉시 적용이 아니라 환자 확인 후보'라는 흐름과 가장 잘 맞는다." $Panel $Line 11 $Text $true 2 | Out-Null
  Add-Footer $slide "제품 기준: 안전 안내는 남기되, 명확한 패턴 기반 후보 생성은 끊지 않는다." 5
}

function Add-Slide6($Presentation) {
  $slide = Add-Slide $Presentation
  Add-Header $slide "적용 내용과 검증" "policy_planner_v13에서 B안 기준을 명시"
  Add-Text $slide "Applied Heading" 62 128 360 24 "적용된 기준" 14 $Text $true 1 | Out-Null
  Add-Text $slide "Validation Heading" 508 128 360 24 "검증 시나리오" 14 $Text $true 1 | Out-Null
  Add-Rule $slide 62 164 370 $Accent 1.25 | Out-Null
  Add-Rule $slide 508 164 370 $Accent 1.25 | Out-Null
  $applied = "suggested_policies가 있으면 Tool call 반환`r부작용/의료진 상담 필요성이 있어도 생략하지 않음`rTool call은 즉시 적용이 아닌 환자 확인용 후보`rmessage와 reason에 의료진 확인 필요성 병기`r생략 조건을 boundary/필수 인자/slot 모호/현행 유지로 제한"
  $validation = "전 시간대 미복용 + 부작용 맥락에서도 Tool call 3개 생성`rmessage에 전체 패턴과 의료진 확인 필요성 포함`rboundary 위반 시 Tool call 미생성`r정상 복약일은 no change 유지`r현행 유지 명시 시 Tool call 미생성"
  Add-Box $slide "Applied" 62 194 370 168 $applied "FFFFFF" $Line 10 $Text $false 1 | Out-Null
  Add-Box $slide "Validation" 508 194 370 168 $validation "FFFFFF" $Line 10 $Text $false 1 | Out-Null
  Add-Box $slide "Outcome" 82 422 796 42 "기대 결과: 명확한 반복 미복용 패턴은 후보 알림으로 연결되고, 의료적 주의는 메시지에서 함께 안내된다." $Panel $Line 11 $Text $true 2 | Out-Null
  Add-Footer $slide "런타임 API 변경 없이 프롬프트 기준과 검증 케이스로 제품 판단을 고정" 6
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

    $tempPath = Join-Path ([System.IO.Path]::GetTempPath()) ("daily_pattern_policy_toolcall_issue_{0}.pptx" -f ([Guid]::NewGuid().ToString("N")))
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
