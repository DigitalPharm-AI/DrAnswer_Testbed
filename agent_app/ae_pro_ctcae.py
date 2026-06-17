from __future__ import annotations

import difflib
import math
import re
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import openpyxl

from shared.schemas import AEProCtcaeAssessmentResult, AEProCtcaeQuestion
from shared.settings import get_settings

PARSED_ITEMS_SHEET = "Parsed_Items"
OTHER_SYMPTOMS_SHEET = "Other_Symptoms"
KNOWN_SYMPTOM_ALIASES = {
    "Nausea": ("메스꺼", "속이메스꺼", "속울렁", "울렁거", "구역", "속불편"),
    "Dizziness": ("어지럽", "현기증", "핑돌"),
    "Vomiting": ("구토", "토했", "토할"),
    "Diarrhea": ("설사",),
    "Abdominal Pain": ("복통", "배아", "배가아"),
    "Headache": ("두통", "머리아"),
    "Muscle Ache": ("근육통", "근육이아", "쑤심"),
}


@dataclass(frozen=True)
class ProCtcaeQuestionRow:
    symptom_term: str
    korean_symptom_name: str
    item_code: str
    question: str
    response_type: str
    response_options: tuple[str, ...]
    pdf_page: int | None
    sheet_name: str


@dataclass(frozen=True)
class ProCtcaeSymptomEntry:
    symptom_term: str
    korean_symptom_name: str
    sheet_name: str
    questions: tuple[ProCtcaeQuestionRow, ...]

    @property
    def aliases(self) -> tuple[str, ...]:
        values = [self.symptom_term, self.korean_symptom_name]
        values.extend(_parenthetical_aliases(self.korean_symptom_name))
        values.extend(_parenthetical_aliases(self.symptom_term))
        values.extend(KNOWN_SYMPTOM_ALIASES.get(self.symptom_term, ()))
        values.extend(KNOWN_SYMPTOM_ALIASES.get(self.korean_symptom_name, ()))
        return tuple(value for value in values if value)


@dataclass(frozen=True)
class ProCtcaeWorkbook:
    parsed_entries: tuple[ProCtcaeSymptomEntry, ...]
    other_questions: tuple[ProCtcaeQuestionRow, ...]


def _text(value: object) -> str:
    return str(value).strip() if value is not None else ""


def _int_or_none(value: object) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _split_options(value: object) -> tuple[str, ...]:
    return tuple(part.strip() for part in _text(value).split("|") if part and part.strip())


def _normalize(value: str) -> str:
    return re.sub(r"[^0-9a-zA-Z가-힣]+", "", value).lower()


def _parenthetical_aliases(value: str) -> list[str]:
    return [match.strip() for match in re.findall(r"\(([^)]+)\)", value or "") if match.strip()]


def _ngrams(value: str, sizes: Iterable[int] = (1, 2, 3)) -> Counter[str]:
    normalized = _normalize(value)
    grams: Counter[str] = Counter()
    for size in sizes:
        if len(normalized) < size:
            continue
        for index in range(0, len(normalized) - size + 1):
            grams[normalized[index : index + size]] += 1
    if not grams and normalized:
        grams[normalized] += 1
    return grams


def _cosine(left: Counter[str], right: Counter[str]) -> float:
    if not left or not right:
        return 0.0
    dot = sum(count * right.get(key, 0) for key, count in left.items())
    left_norm = math.sqrt(sum(count * count for count in left.values()))
    right_norm = math.sqrt(sum(count * count for count in right.values()))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return dot / (left_norm * right_norm)


def _similarity(left: str, right: str) -> float:
    normalized_left = _normalize(left)
    normalized_right = _normalize(right)
    if not normalized_left or not normalized_right:
        return 0.0
    cosine = _cosine(_ngrams(normalized_left), _ngrams(normalized_right))
    sequence = difflib.SequenceMatcher(None, normalized_left, normalized_right).ratio()
    contains_bonus = 0.08 if normalized_left in normalized_right or normalized_right in normalized_left else 0.0
    return min(1.0, max(cosine, sequence) + contains_bonus)


def _is_exact_match(symptom_text: str, aliases: Iterable[str]) -> bool:
    normalized_input = _normalize(symptom_text)
    if not normalized_input:
        return False
    for alias in aliases:
        normalized_alias = _normalize(alias)
        if not normalized_alias:
            continue
        if normalized_input == normalized_alias:
            return True
        if len(normalized_alias) >= 3 and normalized_alias in normalized_input:
            return True
    return False


def _question_model(row: ProCtcaeQuestionRow) -> AEProCtcaeQuestion:
    return AEProCtcaeQuestion(
        symptom_term=row.symptom_term,
        korean_symptom_name=row.korean_symptom_name,
        item_code=row.item_code,
        question=row.question,
        response_type=row.response_type,
        response_options=list(row.response_options),
        pdf_page=row.pdf_page,
        sheet_name=row.sheet_name,
    )


def _entry_score(symptom_text: str, entry: ProCtcaeSymptomEntry) -> float:
    return max((_similarity(symptom_text, alias) for alias in entry.aliases), default=0.0)


def _candidate(entry: ProCtcaeSymptomEntry, score: float, match_type: str = "similarity") -> dict:
    return {
        "symptom_term": entry.symptom_term,
        "korean_symptom_name": entry.korean_symptom_name,
        "sheet_name": entry.sheet_name,
        "similarity": round(score, 4),
        "match_type": match_type,
    }


def _load_sheet_rows(path: Path, sheet_name: str) -> list[ProCtcaeQuestionRow]:
    workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    if sheet_name not in workbook.sheetnames:
        return []
    worksheet = workbook[sheet_name]
    rows = list(worksheet.iter_rows(values_only=True))
    if not rows:
        return []
    header = [_text(value) for value in rows[0]]
    offset = 1 if "No" in header else 0
    parsed: list[ProCtcaeQuestionRow] = []
    for row in rows[1:]:
        if not row or not any(_text(value) for value in row):
            continue
        parsed.append(
            ProCtcaeQuestionRow(
                symptom_term=_text(row[offset]),
                korean_symptom_name=_text(row[offset + 1]),
                item_code=_text(row[offset + 2]),
                question=_text(row[offset + 3]),
                response_type=_text(row[offset + 4]),
                response_options=_split_options(row[offset + 5]),
                pdf_page=_int_or_none(row[offset + 6] if len(row) > offset + 6 else None),
                sheet_name=sheet_name,
            )
        )
    return parsed


def _group_entries(rows: Iterable[ProCtcaeQuestionRow]) -> tuple[ProCtcaeSymptomEntry, ...]:
    grouped: dict[tuple[str, str, str], list[ProCtcaeQuestionRow]] = defaultdict(list)
    for row in rows:
        grouped[(row.sheet_name, row.symptom_term, row.korean_symptom_name)].append(row)
    return tuple(
        ProCtcaeSymptomEntry(
            sheet_name=sheet_name,
            symptom_term=symptom_term,
            korean_symptom_name=korean_symptom_name,
            questions=tuple(questions),
        )
        for (sheet_name, symptom_term, korean_symptom_name), questions in grouped.items()
    )


@lru_cache(maxsize=8)
def _load_workbook_cached(path_text: str, modified_time: float) -> ProCtcaeWorkbook:
    del modified_time
    path = Path(path_text)
    parsed_rows = _load_sheet_rows(path, PARSED_ITEMS_SHEET)
    other_rows = _load_sheet_rows(path, OTHER_SYMPTOMS_SHEET)
    return ProCtcaeWorkbook(parsed_entries=_group_entries(parsed_rows), other_questions=tuple(other_rows))


def load_workbook(path: Path | None = None) -> ProCtcaeWorkbook:
    resolved_path = Path(path or get_settings().pro_ctcae_workbook_path)
    modified_time = resolved_path.stat().st_mtime
    return _load_workbook_cached(str(resolved_path), modified_time)


def match_pro_ctcae_symptom(symptom_text: str, *, threshold: float | None = None, workbook_path: Path | None = None) -> AEProCtcaeAssessmentResult:
    settings = get_settings()
    workbook = load_workbook(workbook_path)
    clean_symptom = symptom_text.strip()
    threshold_value = threshold if threshold is not None else settings.pro_ctcae_similarity_threshold
    scored_entries = sorted(((_entry_score(clean_symptom, entry), entry) for entry in workbook.parsed_entries), key=lambda item: item[0], reverse=True)
    exact_entry = next((entry for _, entry in scored_entries if _is_exact_match(clean_symptom, entry.aliases)), None)
    if exact_entry is not None:
        candidates = [_candidate(exact_entry, 1.0, "exact")]
        candidates.extend(_candidate(entry, score) for score, entry in scored_entries[:4] if entry != exact_entry)
        return AEProCtcaeAssessmentResult(
            input_symptom=clean_symptom,
            matched=True,
            match_type="exact",
            matched_symptom_term=exact_entry.symptom_term,
            matched_korean_symptom_name=exact_entry.korean_symptom_name,
            similarity=1.0,
            threshold=threshold_value,
            scoring_method="exact",
            embedding_provider="",
            sheet_name=exact_entry.sheet_name,
            questions=[_question_model(row) for row in exact_entry.questions],
            candidates=candidates[:5],
        )

    best_score, best_entry = scored_entries[0] if scored_entries else (0.0, None)
    if best_entry is not None and best_score >= threshold_value:
        return AEProCtcaeAssessmentResult(
            input_symptom=clean_symptom,
            matched=True,
            match_type="similarity",
            matched_symptom_term=best_entry.symptom_term,
            matched_korean_symptom_name=best_entry.korean_symptom_name,
            similarity=round(best_score, 4),
            threshold=threshold_value,
            scoring_method="local_similarity",
            embedding_provider="",
            sheet_name=best_entry.sheet_name,
            questions=[_question_model(row) for row in best_entry.questions],
            candidates=[_candidate(entry, score) for score, entry in scored_entries[:5]],
        )

    return AEProCtcaeAssessmentResult(
        input_symptom=clean_symptom,
        matched=False,
        match_type="other_symptoms",
        similarity=round(best_score, 4),
        threshold=threshold_value,
        scoring_method="local_similarity",
        embedding_provider="",
        sheet_name=OTHER_SYMPTOMS_SHEET,
        questions=[_question_model(row) for row in workbook.other_questions],
        candidates=[_candidate(entry, score) for score, entry in scored_entries[:5]],
    )
