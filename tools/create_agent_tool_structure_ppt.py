from __future__ import annotations

from html import escape
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

OUT = Path("docs/AGENT_TOOL_STRUCTURE.pptx")
EMU_PER_INCH = 914400
SLIDE_W = 12192000
SLIDE_H = 6858000

COLORS = {
    "api": ("F8FAFC", "CBD5E1"),
    "service": ("FAFAFA", "D4D4D8"),
    "llm": ("FFFDF4", "E7C874"),
    "heuristic": ("F8FCF9", "B7D5BF"),
    "tool": ("FFF8F8", "E0A5A5"),
    "system": ("FAF8FF", "C4B5FD"),
    "white": ("FFFFFF", "E5E7EB"),
}


def emu(inches: float) -> int:
    return int(inches * EMU_PER_INCH)


def text_run(line: str, size: int, bold: bool, color: str) -> str:
    bold_attr = ' b="1"' if bold else ""
    return f"""
          <a:r>
            <a:rPr lang="ko-KR" sz="{size}"{bold_attr} dirty="0">
              <a:solidFill><a:srgbClr val="{color}"/></a:solidFill>
              <a:latin typeface="Malgun Gothic"/><a:ea typeface="Malgun Gothic"/>
            </a:rPr>
            <a:t>{escape(line)}</a:t>
          </a:r>"""


def text_body(text: str, size: int = 1300, bold: bool = False, color: str = "222222") -> str:
    parts: list[str] = []
    for index, line in enumerate(text.splitlines() or [""]):
        if index:
            parts.append("<a:br/>")
        parts.append(text_run(line, size, bold, color))
    return f"""
      <p:txBody>
        <a:bodyPr wrap="square" anchor="mid"><a:spAutoFit/></a:bodyPr>
        <a:lstStyle/>
        <a:p>
          <a:pPr algn="ctr"/>
          {''.join(parts)}
          <a:endParaRPr lang="ko-KR" sz="{size}" dirty="0">
            <a:latin typeface="Malgun Gothic"/><a:ea typeface="Malgun Gothic"/>
          </a:endParaRPr>
        </a:p>
      </p:txBody>"""


def shape(
    shape_id: int,
    name: str,
    x: float,
    y: float,
    w: float,
    h: float,
    text: str,
    kind: str,
    *,
    size: int = 1250,
    bold: bool = False,
    geom: str = "roundRect",
) -> str:
    fill, line = COLORS[kind]
    return f"""
    <p:sp>
      <p:nvSpPr><p:cNvPr id="{shape_id}" name="{escape(name)}"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr>
      <p:spPr>
        <a:xfrm><a:off x="{emu(x)}" y="{emu(y)}"/><a:ext cx="{emu(w)}" cy="{emu(h)}"/></a:xfrm>
        <a:prstGeom prst="{geom}"><a:avLst/></a:prstGeom>
        <a:solidFill><a:srgbClr val="{fill}"/></a:solidFill>
        <a:ln w="11430"><a:solidFill><a:srgbClr val="{line}"/></a:solidFill></a:ln>
      </p:spPr>
      {text_body(text, size=size, bold=bold)}
    </p:sp>"""


def arrow(line_id: int, x1: float, y1: float, x2: float, y2: float, color: str = "94A3B8") -> str:
    flip_h = ' flipH="1"' if x2 < x1 else ""
    flip_v = ' flipV="1"' if y2 < y1 else ""
    width = max(abs(x2 - x1), 0.01)
    height = max(abs(y2 - y1), 0.01)
    return f"""
    <p:cxnSp>
      <p:nvCxnSpPr><p:cNvPr id="{line_id}" name="Arrow {line_id}"/><p:cNvCxnSpPr/><p:nvPr/></p:nvCxnSpPr>
      <p:spPr>
        <a:xfrm{flip_h}{flip_v}>
          <a:off x="{emu(min(x1, x2))}" y="{emu(min(y1, y2))}"/>
          <a:ext cx="{emu(width)}" cy="{emu(height)}"/>
        </a:xfrm>
        <a:prstGeom prst="line"><a:avLst/></a:prstGeom>
        <a:ln w="12700">
          <a:solidFill><a:srgbClr val="{color}"/></a:solidFill>
          <a:tailEnd type="none"/><a:headEnd type="triangle"/>
        </a:ln>
      </p:spPr>
      <p:style>
        <a:lnRef idx="2"><a:schemeClr val="accent1"/></a:lnRef>
        <a:fillRef idx="0"><a:schemeClr val="accent1"/></a:fillRef>
        <a:effectRef idx="1"><a:schemeClr val="accent1"/></a:effectRef>
        <a:fontRef idx="minor"><a:schemeClr val="tx1"/></a:fontRef>
      </p:style>
    </p:cxnSp>"""


def title(shape_id: int, text: str, subtitle: str) -> str:
    return shape(shape_id, "Title", 0.35, 0.18, 12.6, 0.62, f"{text}\n{subtitle}", "white", size=1650, bold=True, geom="rect")


def legend(start_id: int, x: float, y: float) -> str:
    items = [("LLM Agent", "llm"), ("Heuristic", "heuristic"), ("Tool", "tool"), ("System/API", "system")]
    return "".join(
        shape(start_id + index, f"Legend {label}", x + index * 1.55, y, 1.35, 0.28, label, kind, size=850)
        for index, (label, kind) in enumerate(items)
    )


def slide_xml(shapes: list[str]) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:sld xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">
  <p:cSld>
    <p:bg><p:bgPr><a:solidFill><a:srgbClr val="FFFFFF"/></a:solidFill><a:effectLst/></p:bgPr></p:bg>
    <p:spTree>
      <p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>
      <p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/><a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr>
      {''.join(shapes)}
    </p:spTree>
  </p:cSld>
  <p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr>
</p:sld>"""


def slide_overview() -> str:
    s = [title(2, "Agent & Tool Structure", "요청 종류별 Agent 라우팅과 Tool 호출 흐름"), legend(3, 6.65, 6.72)]
    sid = 10
    apis = [
        ("/agent/async/medication-events\ndaily_pattern", 0.35, 1.25),
        ("/agent/async/medication-events\nmissed_dose", 0.35, 2.15),
        ("/agent/sync/chat", 0.35, 3.05),
    ]
    for label, x, y in apis:
        s.append(shape(sid, label, x, y, 2.05, 0.48, label, "api", size=850))
        sid += 1
    s.append(shape(sid, "IngressRouter", 2.75, 2.50, 1.75, 0.70, "IngressRouter\nrequest_kind guard", "service", size=900, bold=True))
    sid += 1
    for _, x, y in apis:
        s.append(arrow(sid, x + 2.05, y + 0.24, 2.75, 2.85))
        sid += 1

    services = [
        ("DailyPattern\nOptimizationService", 5.05, 1.05),
        ("MissedDose\nCoachingService", 5.05, 2.05),
        ("SystemPolicyRequest\nService", 5.05, 3.55),
    ]
    for label, x, y in services:
        s.append(shape(sid, label, x, y, 2.05, 0.62, label, "service", size=900, bold=True))
        sid += 1
        s.append(arrow(sid, 4.50, 2.85, x, y + 0.31))
        sid += 1

    agents = [
        ("pattern_analyzer\nLLM", 7.75, 0.95),
        ("policy_planner\nLLM", 10.15, 1.20),
        ("missed_dose_coach\nLLM", 7.75, 2.05),
        ("multiturn_chat\nagent LLM", 7.75, 3.55),
        ("policy_planner\nLLM", 10.15, 3.95),
    ]
    for label, x, y in agents:
        s.append(shape(sid, label, x, y, 1.75, 0.58, label, "llm", size=850, bold=True))
        sid += 1
    for x1, y1, x2, y2 in [
        (7.10, 1.36, 7.75, 1.24),
        (9.50, 1.24, 10.15, 1.49),
        (7.10, 2.36, 7.75, 2.34),
        (7.10, 3.86, 7.75, 3.84),
        (9.50, 3.84, 10.15, 4.24),
    ]:
        s.append(arrow(sid, x1, y1, x2, y2))
        sid += 1
    s.append(shape(sid, "PolicyIntent", 8.25, 4.85, 1.95, 0.45, "policy_change_intent\n구조화", "heuristic", size=760))
    sid += 1
    s.append(shape(sid, "Tools", 10.60, 2.25, 2.10, 1.35, "Tools\npropose_notification_policy\nget_pro_ctcae_questionnaire\nget_medication_side_effect_assessment\nupdate_medication_dose_event_status", "tool", size=720, bold=True))
    sid += 1
    for x1, y1, x2, y2 in [(11.00, 1.78, 11.45, 2.25), (8.65, 2.63, 10.60, 2.70), (8.65, 4.13, 10.60, 2.95), (11.00, 4.53, 11.45, 3.60)]:
        s.append(arrow(sid, x1, y1, x2, y2))
        sid += 1
    return slide_xml(s)


def slide_tools() -> str:
    s = [title(2, "Agent별 Tool 접근 권한", "정책 변경은 승인 후 쓰기, 부작용은 Backend Snapshot 조회, 복약 체크는 Backend action"), legend(3, 7.0, 0.93)]
    sid = 20
    agents = [
        ("policy_planner\nDaily / Multiturn 정책 전문", 0.55, 1.35),
        ("missed_dose_coach\n미복용 최초 알림", 0.55, 2.25),
        ("multiturn_chat_agent\n자유 대화, 미복용 후속 답변", 0.55, 3.60),
        ("side_effect_response_writer\n평가 결과 환자 답변화", 0.55, 4.95),
    ]
    for label, x, y in agents:
        s.append(shape(sid, label, x, y, 2.75, 0.58, label, "llm", size=820, bold=True))
        sid += 1
    tools = [
        ("propose_notification_policy\n정책 변경 후보 생성\n확인 알림으로 이어짐", 5.05, 1.35),
        ("get_medication_side_effect_assessment\n의약품 기준정보/부작용 조회", 5.05, 2.95),
        ("update_medication_dose_event_status\n복약 완료 체크 요청", 5.05, 4.35),
    ]
    for label, x, y in tools:
        s.append(shape(sid, label, x, y, 2.65, 0.78, label, "tool", size=800, bold=True))
        sid += 1
    systems = [
        ("system_app\n정책 확인 알림\nDB override 저장", 9.30, 1.35),
        ("Backend Snapshot\n+ AI 기준정보", 9.30, 2.95),
        ("system_app\nDoseEvent taken 처리", 9.30, 4.35),
    ]
    for label, x, y in systems:
        s.append(shape(sid, label, x, y, 2.65, 0.78, label, "system", size=800, bold=True))
        sid += 1
    for x1, y1, x2, y2 in [
        (3.30, 1.64, 5.05, 1.74),
        (3.30, 2.54, 5.05, 3.34),
        (3.30, 3.44, 5.05, 1.74),
        (3.30, 3.44, 5.05, 3.34),
        (3.30, 3.44, 5.05, 4.74),
        (3.30, 4.34, 5.05, 3.34),
        (3.30, 4.34, 5.05, 4.74),
        (7.70, 1.74, 9.30, 1.74),
        (7.70, 3.34, 9.30, 3.34),
        (7.70, 4.74, 9.30, 4.74),
        (7.70, 3.34, 3.30, 5.24),
    ]:
        s.append(arrow(sid, x1, y1, x2, y2))
        sid += 1
    return slide_xml(s)


def slide_policy_handoff() -> str:
    s = [title(2, "Multiturn 정책 요청: policy_planner Handoff", "복합 정책 요청은 대화 Agent가 직접 Tool을 만들지 않고 정책 전문 Agent로 넘김")]
    sid = 30
    boxes = [
        ("환자 자유 대화\n“아침은 더 자주, 점심은 그대로, 저녁은 줄여줘”", 0.55, 1.25, "api"),
        ("SystemPolicyRequestService\n정책 요청 여부 감지", 0.55, 2.25, "service"),
        ("policy_change_intent\nrequested_changes 리스트 생성\nneeds_clarification 포함", 3.65, 2.25, "heuristic"),
        ("policy_planner\n정책 전문 LLM\ntool_calls[] 표준 출력", 6.75, 2.25, "llm"),
        ("propose_notification_policy\nslot별 정책 후보 리스트", 9.85, 2.25, "tool"),
        ("정책 변경 확인 알림\n1. 변경 / 2. 현행 유지", 9.85, 3.65, "system"),
    ]
    for label, x, y, kind in boxes:
        s.append(shape(sid, label, x, y, 2.55, 0.78, label, kind, size=800, bold=True))
        sid += 1
    for x1, y1, x2, y2 in [(1.82, 2.03, 1.82, 2.25), (3.10, 2.64, 3.65, 2.64), (6.20, 2.64, 6.75, 2.64), (9.30, 2.64, 9.85, 2.64), (11.12, 3.03, 11.12, 3.65)]:
        s.append(arrow(sid, x1, y1, x2, y2))
        sid += 1
    s.append(shape(sid, "IntentExample", 0.65, 4.35, 5.25, 1.30, "policy_change_intent 예시\nrequested_changes: [\n  {slot_hint: 아침, direction: increase},\n  {slot_hint: 점심, direction: keep},\n  {slot_hint: 저녁, direction: decrease}\n]\nneeds_clarification: [저녁=야간 21:00인지, 구체 횟수/간격]", "white", size=690, geom="rect"))
    sid += 1
    s.append(shape(sid, "ToolCallsExample", 6.25, 4.35, 5.70, 1.30, "policy_planner output 표준\n{\n  tool_calls: [\n    {name: propose_notification_policy, arguments: {...}}\n  ],\n  unchanged_slots: [...],\n  needs_clarification: [...]\n}", "white", size=720, geom="rect"))
    return slide_xml(s)


def package_files(slides: list[str]) -> dict[str, str]:
    content_overrides = "".join(
        f'<Override PartName="/ppt/slides/slide{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/>'
        for i in range(1, len(slides) + 1)
    )
    slide_rels = "".join(
        f'<Relationship Id="rId{i + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide" Target="slides/slide{i}.xml"/>'
        for i in range(1, len(slides) + 1)
    )
    slide_ids = "".join(f'<p:sldId id="{255 + i}" r:id="rId{i + 1}"/>' for i in range(1, len(slides) + 1))
    files = {
        "[Content_Types].xml": f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/ppt/presentation.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/>
  <Override PartName="/ppt/slideMasters/slideMaster1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideMaster+xml"/>
  <Override PartName="/ppt/slideLayouts/slideLayout1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideLayout+xml"/>
  <Override PartName="/ppt/theme/theme1.xml" ContentType="application/vnd.openxmlformats-officedocument.theme+xml"/>
  {content_overrides}
</Types>""",
        "_rels/.rels": """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="ppt/presentation.xml"/>
</Relationships>""",
        "ppt/presentation.xml": f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:presentation xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">
  <p:sldMasterIdLst><p:sldMasterId id="2147483648" r:id="rId1"/></p:sldMasterIdLst>
  <p:sldIdLst>{slide_ids}</p:sldIdLst>
  <p:sldSz cx="{SLIDE_W}" cy="{SLIDE_H}" type="wide"/>
  <p:notesSz cx="6858000" cy="9144000"/>
</p:presentation>""",
        "ppt/_rels/presentation.xml.rels": f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideMaster" Target="slideMasters/slideMaster1.xml"/>
  {slide_rels}
</Relationships>""",
        "ppt/slideMasters/slideMaster1.xml": """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:sldMaster xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">
  <p:cSld><p:spTree><p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/><a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr></p:spTree></p:cSld>
  <p:clrMap bg1="lt1" tx1="dk1" bg2="lt2" tx2="dk2" accent1="accent1" accent2="accent2" accent3="accent3" accent4="accent4" accent5="accent5" accent6="accent6" hlink="hlink" folHlink="folHlink"/>
  <p:sldLayoutIdLst><p:sldLayoutId id="2147483649" r:id="rId1"/></p:sldLayoutIdLst>
</p:sldMaster>""",
        "ppt/slideMasters/_rels/slideMaster1.xml.rels": """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout" Target="../slideLayouts/slideLayout1.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/theme" Target="../theme/theme1.xml"/>
</Relationships>""",
        "ppt/slideLayouts/slideLayout1.xml": """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:sldLayout xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" type="blank" preserve="1">
  <p:cSld name="Blank"><p:spTree><p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/><a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr></p:spTree></p:cSld>
  <p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr>
</p:sldLayout>""",
        "ppt/slideLayouts/_rels/slideLayout1.xml.rels": """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideMaster" Target="../slideMasters/slideMaster1.xml"/>
</Relationships>""",
        "ppt/theme/theme1.xml": """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<a:theme xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" name="Agent Theme">
  <a:themeElements>
    <a:clrScheme name="Agent"><a:dk1><a:srgbClr val="222222"/></a:dk1><a:lt1><a:srgbClr val="FFFFFF"/></a:lt1><a:dk2><a:srgbClr val="444444"/></a:dk2><a:lt2><a:srgbClr val="F7F7F7"/></a:lt2><a:accent1><a:srgbClr val="5599CC"/></a:accent1><a:accent2><a:srgbClr val="C44A4A"/></a:accent2><a:accent3><a:srgbClr val="3A8F5A"/></a:accent3><a:accent4><a:srgbClr val="B88A00"/></a:accent4><a:accent5><a:srgbClr val="6B5FC7"/></a:accent5><a:accent6><a:srgbClr val="999999"/></a:accent6><a:hlink><a:srgbClr val="0563C1"/></a:hlink><a:folHlink><a:srgbClr val="954F72"/></a:folHlink></a:clrScheme>
    <a:fontScheme name="Agent"><a:majorFont><a:latin typeface="Malgun Gothic"/><a:ea typeface="Malgun Gothic"/></a:majorFont><a:minorFont><a:latin typeface="Malgun Gothic"/><a:ea typeface="Malgun Gothic"/></a:minorFont></a:fontScheme>
    <a:fmtScheme name="Agent"><a:fillStyleLst><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:fillStyleLst><a:lnStyleLst><a:ln w="9525"><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:ln></a:lnStyleLst><a:effectStyleLst><a:effectStyle><a:effectLst/></a:effectStyle></a:effectStyleLst><a:bgFillStyleLst><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:bgFillStyleLst></a:fmtScheme>
  </a:themeElements>
</a:theme>""",
    }
    for i, slide in enumerate(slides, start=1):
        files[f"ppt/slides/slide{i}.xml"] = slide
        files[f"ppt/slides/_rels/slide{i}.xml.rels"] = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>"""
    return files


def write_pptx(path: Path) -> None:
    slides = [slide_overview(), slide_tools(), slide_policy_handoff()]
    files = package_files(slides)
    path.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(path, "w", ZIP_DEFLATED) as zf:
        for name, data in files.items():
            zf.writestr(name, data.encode("utf-8"))


if __name__ == "__main__":
    write_pptx(OUT)
    print(OUT.resolve())
