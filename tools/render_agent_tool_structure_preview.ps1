param(
    [string]$OutputDir = "docs/agent_tool_structure_preview"
)

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Drawing

$SlideWidthIn = 13.333333
$SlideHeightIn = 7.5
$ImageWidth = 1600
$ImageHeight = 900
$ScaleX = $ImageWidth / $SlideWidthIn
$ScaleY = $ImageHeight / $SlideHeightIn

$Colors = @{
    api       = @{ Fill = "#F8FAFC"; Border = "#CBD5E1" }
    service   = @{ Fill = "#FAFAFA"; Border = "#D4D4D8" }
    llm       = @{ Fill = "#FFFDF4"; Border = "#E7C874" }
    heuristic = @{ Fill = "#F8FCF9"; Border = "#B7D5BF" }
    tool      = @{ Fill = "#FFF8F8"; Border = "#E0A5A5" }
    system    = @{ Fill = "#FAF8FF"; Border = "#C4B5FD" }
    white     = @{ Fill = "#FFFFFF"; Border = "#E5E7EB" }
}

function Convert-Color([string]$Hex) {
    return [System.Drawing.ColorTranslator]::FromHtml($Hex)
}

function New-Rect([double]$X, [double]$Y, [double]$W, [double]$H) {
    return [System.Drawing.RectangleF]::new(
        [single]($X * $ScaleX),
        [single]($Y * $ScaleY),
        [single]($W * $ScaleX),
        [single]($H * $ScaleY)
    )
}

function New-RoundedPath([System.Drawing.RectangleF]$Rect, [float]$Radius) {
    $Path = [System.Drawing.Drawing2D.GraphicsPath]::new()
    $D = $Radius * 2
    $Path.AddArc($Rect.X, $Rect.Y, $D, $D, 180, 90)
    $Path.AddArc($Rect.Right - $D, $Rect.Y, $D, $D, 270, 90)
    $Path.AddArc($Rect.Right - $D, $Rect.Bottom - $D, $D, $D, 0, 90)
    $Path.AddArc($Rect.X, $Rect.Bottom - $D, $D, $D, 90, 90)
    $Path.CloseFigure()
    return $Path
}

function Draw-Box(
    [System.Drawing.Graphics]$G,
    [string]$Text,
    [double]$X,
    [double]$Y,
    [double]$W,
    [double]$H,
    [string]$Kind,
    [int]$FontSize = 10,
    [bool]$Bold = $false,
    [bool]$Square = $false
) {
    $Rect = New-Rect $X $Y $W $H
    $Fill = [System.Drawing.SolidBrush]::new((Convert-Color ($Colors[$Kind].Fill)))
    $Pen = [System.Drawing.Pen]::new((Convert-Color ($Colors[$Kind].Border)), 1.4)
    if ($Square) {
        $G.FillRectangle($Fill, $Rect)
        $G.DrawRectangle($Pen, $Rect.X, $Rect.Y, $Rect.Width, $Rect.Height)
    } else {
        $Path = New-RoundedPath $Rect 10
        $G.FillPath($Fill, $Path)
        $G.DrawPath($Pen, $Path)
        $Path.Dispose()
    }
    $Style = if ($Bold) { [System.Drawing.FontStyle]::Bold } else { [System.Drawing.FontStyle]::Regular }
    $Font = [System.Drawing.Font]::new("Malgun Gothic", $FontSize, $Style)
    $Brush = [System.Drawing.SolidBrush]::new((Convert-Color "#1F2937"))
    $Format = [System.Drawing.StringFormat]::new()
    $Format.Alignment = [System.Drawing.StringAlignment]::Center
    $Format.LineAlignment = [System.Drawing.StringAlignment]::Center
    $G.DrawString($Text, $Font, $Brush, $Rect, $Format)
    $Format.Dispose()
    $Brush.Dispose()
    $Font.Dispose()
    $Pen.Dispose()
    $Fill.Dispose()
}

function Draw-Arrow(
    [System.Drawing.Graphics]$G,
    [double]$X1,
    [double]$Y1,
    [double]$X2,
    [double]$Y2
) {
    $Pen = [System.Drawing.Pen]::new((Convert-Color "#94A3B8"), 1.8)
    $Cap = [System.Drawing.Drawing2D.AdjustableArrowCap]::new(4, 5)
    $Pen.CustomEndCap = $Cap
    $G.DrawLine($Pen, [single]($X1 * $ScaleX), [single]($Y1 * $ScaleY), [single]($X2 * $ScaleX), [single]($Y2 * $ScaleY))
    $Cap.Dispose()
    $Pen.Dispose()
}

function Draw-Title([System.Drawing.Graphics]$G, [string]$Text, [string]$Subtitle) {
    Draw-Box $G "$Text`n$Subtitle" 0.35 0.18 12.6 0.62 "white" 14 $true $true
}

function Draw-Legend([System.Drawing.Graphics]$G, [double]$X, [double]$Y) {
    $Items = @(
        @{ Label = "LLM Agent"; Kind = "llm" },
        @{ Label = "Heuristic"; Kind = "heuristic" },
        @{ Label = "Tool"; Kind = "tool" },
        @{ Label = "System/API"; Kind = "system" }
    )
    for ($I = 0; $I -lt $Items.Count; $I++) {
        Draw-Box $G ($Items[$I].Label) ($X + $I * 1.55) $Y 1.35 0.28 ($Items[$I].Kind) 7 $false
    }
}

function New-Canvas {
    $Bitmap = [System.Drawing.Bitmap]::new($ImageWidth, $ImageHeight)
    $G = [System.Drawing.Graphics]::FromImage($Bitmap)
    $G.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
    $G.TextRenderingHint = [System.Drawing.Text.TextRenderingHint]::ClearTypeGridFit
    $G.Clear([System.Drawing.Color]::White)
    return @{ Bitmap = $Bitmap; Graphics = $G }
}

function Save-Canvas($Canvas, [string]$Path) {
    $Canvas.Graphics.Dispose()
    $Canvas.Bitmap.Save($Path, [System.Drawing.Imaging.ImageFormat]::Png)
    $Canvas.Bitmap.Dispose()
}

function Render-Overview([string]$Path) {
    $C = New-Canvas
    $G = $C.Graphics
    Draw-Title $G "Agent & Tool Structure" "요청 종류별 Agent 라우팅과 Tool 호출 흐름"
    Draw-Legend $G 6.65 6.72
    $Apis = @(
        @("/agent/async/daily-patterns", 0.35, 1.25),
        @("/agent/async/missed-dose-events", 0.35, 2.15),
        @("/agent/multiturn-chat", 0.35, 3.05)
    )
    foreach ($Api in $Apis) {
        Draw-Box $G ($Api[0]) ($Api[1]) ($Api[2]) 2.05 0.48 "api" 7 $false
        Draw-Arrow $G (($Api[1]) + 2.05) (($Api[2]) + 0.24) 2.75 2.85
    }
    Draw-Box $G "IngressRouter`nrequest_kind guard" 2.75 2.50 1.75 0.70 "service" 8 $true
    $Services = @(
        @("DailyPattern`nOptimizationService", 5.05, 1.05),
        @("MissedDose`nCoachingService", 5.05, 2.05),
        @("SystemPolicyRequest`nService", 5.05, 3.55)
    )
    foreach ($Svc in $Services) {
        Draw-Box $G ($Svc[0]) ($Svc[1]) ($Svc[2]) 2.05 0.62 "service" 8 $true
        Draw-Arrow $G 4.50 2.85 ($Svc[1]) (($Svc[2]) + 0.31)
    }
    $Agents = @(
        @("pattern_analyzer`nLLM", 7.75, 0.95),
        @("policy_planner`nLLM", 10.15, 1.20),
        @("missed_dose_coach`nLLM", 7.75, 2.05),
        @("multiturn_chat`nagent LLM", 7.75, 3.55),
        @("policy_planner`nLLM", 10.15, 3.95)
    )
    foreach ($Agent in $Agents) {
        Draw-Box $G ($Agent[0]) ($Agent[1]) ($Agent[2]) 1.75 0.58 "llm" 7 $true
    }
    $Lines = @(
        @(7.10, 1.36, 7.75, 1.24), @(9.50, 1.24, 10.15, 1.49),
        @(7.10, 2.36, 7.75, 2.34), @(7.10, 3.86, 7.75, 3.84),
        @(9.50, 3.84, 10.15, 4.24),
        @(11.00, 1.78, 11.45, 2.25), @(8.65, 2.63, 10.60, 2.70),
        @(8.65, 4.13, 10.60, 2.95), @(11.00, 4.53, 11.45, 3.60)
    )
    foreach ($L in $Lines) { Draw-Arrow $G ($L[0]) ($L[1]) ($L[2]) ($L[3]) }
    Draw-Box $G "policy_change_intent`n구조화" 8.25 4.85 1.95 0.45 "heuristic" 6 $false
    Draw-Box $G "Tools`napply_notification_policy`nAE_pro_ctcae`nlookup_side_effect_info`nmark_dose_taken" 10.60 2.25 2.10 1.35 "tool" 6 $true
    Save-Canvas $C $Path
}

function Render-Tools([string]$Path) {
    $C = New-Canvas
    $G = $C.Graphics
    Draw-Title $G "Agent별 Tool 접근 권한" "정책 변경은 후보 생성, 부작용은 PHR 조회, 복약 체크는 system_app action"
    Draw-Legend $G 7.0 0.93
    $Agents = @(
        @("policy_planner`nDaily / Multiturn 정책 전문", 0.55, 1.35),
        @("missed_dose_coach`n미복용 최초 알림", 0.55, 2.25),
        @("multiturn_chat_agent`n자유 대화, 미복용 후속 답변", 0.55, 3.60),
        @("side_effect_response_writer`nPHR 결과 환자 답변화", 0.55, 4.95)
    )
    foreach ($Agent in $Agents) { Draw-Box $G ($Agent[0]) ($Agent[1]) ($Agent[2]) 2.75 0.58 "llm" 7 $true }
    Draw-Box $G "apply_notification_policy`n정책 변경 후보 생성`n확인 알림으로 이어짐" 5.05 1.35 2.65 0.78 "tool" 7 $true
    Draw-Box $G "lookup_side_effect_info`nPHR 주의사항/부작용 조회" 5.05 2.95 2.65 0.78 "tool" 7 $true
    Draw-Box $G "mark_dose_taken`n복약 완료 체크 요청" 5.05 4.35 2.65 0.78 "tool" 7 $true
    Draw-Box $G "system_app`n정책 확인 알림`nDB override 저장" 9.30 1.35 2.65 0.78 "system" 7 $true
    Draw-Box $G "PHR service`n/phr/side-effects/assess" 9.30 2.95 2.65 0.78 "system" 7 $true
    Draw-Box $G "system_app`nDoseEvent taken 처리" 9.30 4.35 2.65 0.78 "system" 7 $true
    $Lines = @(
        @(3.30, 1.64, 5.05, 1.74), @(3.30, 2.54, 5.05, 3.34),
        @(3.30, 3.44, 5.05, 1.74), @(3.30, 3.44, 5.05, 3.34),
        @(3.30, 3.44, 5.05, 4.74), @(3.30, 4.34, 5.05, 3.34),
        @(3.30, 4.34, 5.05, 4.74), @(7.70, 1.74, 9.30, 1.74),
        @(7.70, 3.34, 9.30, 3.34), @(7.70, 4.74, 9.30, 4.74),
        @(7.70, 3.34, 3.30, 5.24)
    )
    foreach ($L in $Lines) { Draw-Arrow $G ($L[0]) ($L[1]) ($L[2]) ($L[3]) }
    Save-Canvas $C $Path
}

function Render-Handoff([string]$Path) {
    $C = New-Canvas
    $G = $C.Graphics
    Draw-Title $G "Multiturn 정책 요청: policy_planner Handoff" "복합 정책 요청은 대화 Agent가 직접 Tool을 만들지 않고 정책 전문 Agent로 넘김"
    Draw-Box $G "환자 자유 대화`n""아침은 더 자주, 점심은 그대로, 저녁은 줄여줘""" 0.55 1.25 2.55 0.78 "api" 7 $true
    Draw-Box $G "SystemPolicyRequestService`n정책 요청 여부 감지" 0.55 2.25 2.55 0.78 "service" 7 $true
    Draw-Box $G "policy_change_intent`nrequested_changes 리스트 생성`nneeds_clarification 포함" 3.65 2.25 2.55 0.78 "heuristic" 7 $true
    Draw-Box $G "policy_planner`n정책 전문 LLM`ntool_calls[] 표준 출력" 6.75 2.25 2.55 0.78 "llm" 7 $true
    Draw-Box $G "apply_notification_policy`nslot별 정책 후보 리스트" 9.85 2.25 2.55 0.78 "tool" 7 $true
    Draw-Box $G "정책 변경 확인 알림`n1. 변경 / 2. 현행 유지" 9.85 3.65 2.55 0.78 "system" 7 $true
    $Lines = @(
        @(1.82, 2.03, 1.82, 2.25), @(3.10, 2.64, 3.65, 2.64),
        @(6.20, 2.64, 6.75, 2.64), @(9.30, 2.64, 9.85, 2.64),
        @(11.12, 3.03, 11.12, 3.65)
    )
    foreach ($L in $Lines) { Draw-Arrow $G ($L[0]) ($L[1]) ($L[2]) ($L[3]) }
    Draw-Box $G "policy_change_intent 예시`nrequested_changes: [`n  {slot_hint: 아침, direction: increase},`n  {slot_hint: 점심, direction: keep},`n  {slot_hint: 저녁, direction: decrease}`n]`nneeds_clarification: [저녁=야간 21:00인지, 구체 횟수/간격]" 0.65 4.35 5.25 1.30 "white" 6 $false $true
    Draw-Box $G "policy_planner output 표준`n{`n  tool_calls: [`n    {name: apply_notification_policy, arguments: {...}}`n  ],`n  unchanged_slots: [...],`n  needs_clarification: [...]`n}" 6.25 4.35 5.70 1.30 "white" 6 $false $true
    Save-Canvas $C $Path
}

New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null
Render-Overview (Join-Path $OutputDir "slide_1_overview.png")
Render-Tools (Join-Path $OutputDir "slide_2_tools.png")
Render-Handoff (Join-Path $OutputDir "slide_3_policy_handoff.png")

Get-ChildItem $OutputDir -Filter "*.png" | ForEach-Object {
    $Image = [System.Drawing.Image]::FromFile($_.FullName)
    try {
        if ($Image.Width -ne $ImageWidth -or $Image.Height -ne $ImageHeight) {
            throw "Unexpected PNG size for $($_.FullName): $($Image.Width)x$($Image.Height)"
        }
        [PSCustomObject]@{
            File = $_.FullName
            Width = $Image.Width
            Height = $Image.Height
            Bytes = $_.Length
        }
    } finally {
        $Image.Dispose()
    }
}
