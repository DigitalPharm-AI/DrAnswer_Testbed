from __future__ import annotations

import json
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from shared.settings import get_settings
from shared.time_utils import utc_now
from system_app.models import (
    NutritionOntologyNode,
    NutritionOntologyTriple,
    NutritionPatientPreferenceTriple,
)

PREFERENCE_PREDICATES = {
    "likes",
    "dislikes",
    "prefers",
    "avoids_by_preference",
    "cannot_consume",
    "allergic_to",
    "medically_avoids",
    "religious_avoids",
}
HARD_CONSTRAINT_PREDICATES = {"cannot_consume", "allergic_to", "medically_avoids", "religious_avoids"}
SOFT_PREFERENCE_PREDICATES = PREFERENCE_PREDICATES - HARD_CONSTRAINT_PREDICATES
NODE_TYPES = {"food", "ingredient", "food_category", "cuisine", "preparation", "nutrient", "nutrient_risk", "restriction", "diet_style"}
ALLERGEN_RELATION_PREDICATE = "contains_allergen"
FOOD_ALLERGEN_SEEDS = {
    "두부된장국": ["대두"],
    "짜장면": ["밀", "대두"],
    "탕수육": ["밀"],
    "콤비네이션피자": ["밀", "우유"],
}

PREDICATE_LABELS = {
    "likes": "선호",
    "dislikes": "비선호",
    "prefers": "선호 경향",
    "avoids_by_preference": "기호상 회피",
    "cannot_consume": "\uc12d\ucde8 \ubd88\uac00",
    "allergic_to": "알레르기",
    "medically_avoids": "의학적 제한",
    "religious_avoids": "종교/신념 제한",
}


def normalize_ontology_label(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def parse_preference_csv(value: str | None) -> list[str]:
    items: list[str] = []
    seen: set[str] = set()
    for raw_item in str(value or "").replace("，", ",").split(","):
        item = re.sub(r"\s+", " ", raw_item.strip().strip("[](){}\"'")).strip()
        if not item:
            continue
        key = normalize_ontology_label(item)
        if key in seen:
            continue
        seen.add(key)
        items.append(item)
    return items


def ontology_node_key(node_type: str, label: str) -> str:
    return f"{node_type}:{normalize_ontology_label(label)}"


def resolve_patient_id(patient_id: str | None = None) -> str:
    return str(patient_id or get_settings().patient_id)


def ensure_ontology_node(
    session: Session,
    *,
    label: str,
    node_type: str = "food",
    source: str = "agent_tool",
    metadata: dict[str, Any] | None = None,
) -> NutritionOntologyNode:
    normalized_label = normalize_ontology_label(label)
    if not normalized_label:
        raise ValueError("ontology_node_label_required")
    resolved_type = node_type if node_type in NODE_TYPES else "food"
    node_key = ontology_node_key(resolved_type, normalized_label)
    node = session.scalar(select(NutritionOntologyNode).where(NutritionOntologyNode.node_key == node_key))
    metadata_json = json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True)
    if node is None:
        node = NutritionOntologyNode(
            node_key=node_key,
            node_type=resolved_type,
            label=str(label).strip(),
            normalized_label=normalized_label,
            source=source,
            metadata_json=metadata_json,
        )
        session.add(node)
        session.flush()
        return node
    node.label = str(label).strip() or node.label
    node.node_type = resolved_type
    node.normalized_label = normalized_label
    node.source = source or node.source
    if metadata:
        node.metadata_json = metadata_json
    node.updated_at = utc_now()
    session.flush()
    return node


def upsert_ontology_triple(
    session: Session,
    *,
    subject_node: NutritionOntologyNode,
    predicate: str,
    object_node: NutritionOntologyNode,
    confidence: float = 1.0,
    source: str = "seed",
    metadata: dict[str, Any] | None = None,
) -> NutritionOntologyTriple:
    triple = session.scalar(
        select(NutritionOntologyTriple).where(
            NutritionOntologyTriple.subject_node_id == subject_node.id,
            NutritionOntologyTriple.predicate == predicate,
            NutritionOntologyTriple.object_node_id == object_node.id,
        )
    )
    metadata_json = json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True)
    if triple is None:
        triple = NutritionOntologyTriple(
            subject_node_id=subject_node.id,
            predicate=predicate,
            object_node_id=object_node.id,
            confidence=_clamp_float(confidence),
            source=source,
            metadata_json=metadata_json,
        )
        session.add(triple)
        session.flush()
        return triple
    triple.confidence = _clamp_float(confidence)
    triple.source = source or triple.source
    if metadata:
        triple.metadata_json = metadata_json
    triple.updated_at = utc_now()
    session.flush()
    return triple


def seed_nutrition_ontology(session: Session) -> dict[str, Any]:
    from system_app.services.nutrition_service import NUTRIENTS, NUTRITION_SCENARIOS

    before_nodes = session.query(NutritionOntologyNode).count()
    before_triples = session.query(NutritionOntologyTriple).count()
    nutrient_nodes = {
        nutrient: ensure_ontology_node(session, label=nutrient, node_type="nutrient", source="nutrition_seed")
        for nutrient in NUTRIENTS
    }
    sample_food_node = ensure_ontology_node(session, label="샘플 음식", node_type="food_category", source="nutrition_seed")
    high_sodium_node = ensure_ontology_node(session, label="고나트륨", node_type="nutrient_risk", source="nutrition_seed")
    high_fat_node = ensure_ontology_node(session, label="고지방", node_type="nutrient_risk", source="nutrition_seed")
    high_calorie_node = ensure_ontology_node(session, label="고열량", node_type="nutrient_risk", source="nutrition_seed")

    for scenario_key, scenario in NUTRITION_SCENARIOS.items():
        for food in scenario.get("foods", []):
            food_name = str(food.get("food_name") or "")
            if not food_name:
                continue
            food_node = ensure_ontology_node(
                session,
                label=food_name,
                node_type="food",
                source="nutrition_seed",
                metadata={"scenario_key": scenario_key, "food_ref_id": food.get("food_ref_id", "")},
            )
            upsert_ontology_triple(session, subject_node=food_node, predicate="is_a", object_node=sample_food_node, source="nutrition_seed")
            nutrients = food.get("nutrients") if isinstance(food.get("nutrients"), dict) else {}
            for nutrient_name, nutrient_node in nutrient_nodes.items():
                raw = nutrients.get(nutrient_name)
                amount = raw.get("value") if isinstance(raw, dict) else 0
                if float(amount or 0) > 0:
                    upsert_ontology_triple(
                        session,
                        subject_node=food_node,
                        predicate="contains_nutrient",
                        object_node=nutrient_node,
                        source="nutrition_seed",
                        metadata={"amount": amount},
                    )
            sodium = _nutrient_amount(nutrients, "나트륨")
            fat = _nutrient_amount(nutrients, "지방")
            calories = _nutrient_amount(nutrients, "칼로리")
            if sodium >= 700:
                upsert_ontology_triple(session, subject_node=food_node, predicate="has_nutrient_risk", object_node=high_sodium_node, source="nutrition_seed")
            if fat >= 20:
                upsert_ontology_triple(session, subject_node=food_node, predicate="has_nutrient_risk", object_node=high_fat_node, source="nutrition_seed")
            if calories >= 500:
                upsert_ontology_triple(session, subject_node=food_node, predicate="has_nutrient_risk", object_node=high_calorie_node, source="nutrition_seed")
            for allergen_label in FOOD_ALLERGEN_SEEDS.get(food_name, []):
                allergen_node = ensure_ontology_node(
                    session,
                    label=allergen_label,
                    node_type="ingredient",
                    source="nutrition_seed",
                    metadata={"allergen": True},
                )
                upsert_ontology_triple(
                    session,
                    subject_node=food_node,
                    predicate=ALLERGEN_RELATION_PREDICATE,
                    object_node=allergen_node,
                    source="nutrition_seed",
                    metadata={"relation_type": "allergy"},
                )

    return {
        "success": True,
        "nodes_created_or_updated": session.query(NutritionOntologyNode).count() - before_nodes,
        "triples_created_or_updated": session.query(NutritionOntologyTriple).count() - before_triples,
    }


def record_preference_fact(
    session: Session,
    *,
    predicate: str,
    object_label: str,
    patient_id: str | None = None,
    object_type: str = "food",
    strength: float = 1.0,
    confidence: float = 1.0,
    safety_level: str | None = None,
    source: str = "agent_tool",
    evidence_text: str = "",
) -> dict[str, Any]:
    if predicate not in PREFERENCE_PREDICATES:
        raise ValueError(f"unsupported_preference_predicate:{predicate}")
    target_patient_id = resolve_patient_id(patient_id)
    node = ensure_ontology_node(session, label=object_label, node_type=object_type, source=source)
    resolved_safety_level = "hard" if predicate in HARD_CONSTRAINT_PREDICATES else "soft"
    if safety_level in {"hard", "soft"} and predicate not in HARD_CONSTRAINT_PREDICATES:
        resolved_safety_level = safety_level
    fact = session.scalar(
        select(NutritionPatientPreferenceTriple).where(
            NutritionPatientPreferenceTriple.patient_id == target_patient_id,
            NutritionPatientPreferenceTriple.predicate == predicate,
            NutritionPatientPreferenceTriple.object_node_id == node.id,
        )
    )
    now = utc_now()
    if fact is None:
        fact = NutritionPatientPreferenceTriple(
            patient_id=target_patient_id,
            predicate=predicate,
            object_node_id=node.id,
            strength=_clamp_float(strength),
            safety_level=resolved_safety_level,
            confidence=_clamp_float(confidence),
            source=source,
            evidence_text=evidence_text,
            status="active",
            created_at=now,
            updated_at=now,
        )
        session.add(fact)
        session.flush()
    else:
        fact.strength = _clamp_float(strength)
        fact.safety_level = resolved_safety_level
        fact.confidence = _clamp_float(confidence)
        fact.source = source or fact.source
        fact.evidence_text = evidence_text or fact.evidence_text
        fact.status = "active"
        fact.updated_at = now
        session.flush()
    return {"success": True, "fact": preference_fact_view(fact, node), "preferences": nutrition_preference_summary(session, patient_id=target_patient_id)}


def record_preference_csv_lists(
    session: Session,
    *,
    patient_id: str | None = None,
    preferred_foods_csv: str = "",
    avoided_foods_csv: str = "",
    diet_styles_csv: str = "",
    source: str = "ui_form",
) -> dict[str, Any]:
    target_patient_id = resolve_patient_id(patient_id)
    fields = [
        ("likes", "food", preferred_foods_csv, "선호 음식"),
        ("dislikes", "food", avoided_foods_csv, "비선호 음식"),
        ("prefers", "diet_style", diet_styles_csv, "식단 성향"),
    ]
    facts: list[dict[str, Any]] = []
    for predicate, object_type, raw_csv, evidence_label in fields:
        for item in parse_preference_csv(raw_csv):
            result = record_preference_fact(
                session,
                patient_id=target_patient_id,
                predicate=predicate,
                object_label=item,
                object_type=object_type,
                source=source,
                evidence_text=f"{evidence_label}: {raw_csv.strip()}",
            )
            facts.append(result["fact"])
    return {
        "success": True,
        "recorded_count": len(facts),
        "facts": facts,
        "preferences": nutrition_preference_summary(session, patient_id=target_patient_id),
    }


def nutrition_preference_summary(session: Session, patient_id: str | None = None) -> dict[str, Any]:
    target_patient_id = resolve_patient_id(patient_id)
    rows = session.execute(
        select(NutritionPatientPreferenceTriple, NutritionOntologyNode)
        .join(NutritionOntologyNode, NutritionPatientPreferenceTriple.object_node_id == NutritionOntologyNode.id)
        .where(
            NutritionPatientPreferenceTriple.patient_id == target_patient_id,
            NutritionPatientPreferenceTriple.status == "active",
        )
        .order_by(NutritionPatientPreferenceTriple.safety_level.asc(), NutritionPatientPreferenceTriple.updated_at.desc())
    ).all()
    hard_constraints: list[dict[str, Any]] = []
    soft_preferences: list[dict[str, Any]] = []
    for fact, node in rows:
        view = preference_fact_view(fact, node)
        if fact.safety_level == "hard" or fact.predicate in HARD_CONSTRAINT_PREDICATES:
            hard_constraints.append(view)
        else:
            soft_preferences.append(view)
    return {
        "patient_id": target_patient_id,
        "hard_constraints": hard_constraints,
        "soft_preferences": soft_preferences,
        "recommendation_guidance": recommendation_guidance(hard_constraints, soft_preferences),
        "counts": {
            "hard": len(hard_constraints),
            "soft": len(soft_preferences),
            "total": len(hard_constraints) + len(soft_preferences),
        },
    }


def preference_fact_view(fact: NutritionPatientPreferenceTriple, node: NutritionOntologyNode) -> dict[str, Any]:
    return {
        "id": fact.id,
        "patient_id": fact.patient_id,
        "predicate": fact.predicate,
        "predicate_label": PREDICATE_LABELS.get(fact.predicate, fact.predicate),
        "object_node_id": node.id,
        "object_key": node.node_key,
        "object_type": node.node_type,
        "object_label": node.label,
        "strength": fact.strength,
        "safety_level": fact.safety_level,
        "confidence": fact.confidence,
        "source": fact.source,
        "evidence_text": fact.evidence_text,
        "status": fact.status,
        "updated_at": fact.updated_at.isoformat() if fact.updated_at else "",
    }


def recommendation_guidance(hard_constraints: list[dict[str, Any]], soft_preferences: list[dict[str, Any]]) -> list[str]:
    guidance = []
    for item in hard_constraints:
        guidance.append(f"강제 제외: {item['object_label']} ({item['predicate_label']})")
    for item in soft_preferences:
        if item["predicate"] in {"likes", "prefers"}:
            guidance.append(f"가능하면 반영: {item['object_label']} ({item['predicate_label']})")
        else:
            guidance.append(f"가능하면 대체: {item['object_label']} ({item['predicate_label']})")
    return guidance


def annotate_food_candidate(session: Session | None, candidate: dict[str, Any], patient_id: str | None = None) -> dict[str, Any]:
    if session is None:
        return candidate
    preferences = nutrition_preference_summary(session, patient_id=patient_id)
    food_name = str(candidate.get("food_name") or candidate.get("name") or "")
    relation_index = _candidate_relation_index(session, food_name)
    hard_matches = [item for item in preferences["hard_constraints"] if _candidate_matches_preference(candidate, item, food_name, relation_index)]
    soft_matches = [item for item in preferences["soft_preferences"] if _candidate_matches_preference(candidate, item, food_name, relation_index)]
    positive = [item for item in soft_matches if item["predicate"] in {"likes", "prefers"}]
    negative = [item for item in soft_matches if item["predicate"] in {"dislikes", "avoids_by_preference"}]
    if hard_matches:
        status = "blocked"
    elif positive and not negative:
        status = "positive"
    elif negative and not positive:
        status = "negative"
    elif soft_matches:
        status = "mixed"
    else:
        status = "neutral"
    return {
        **candidate,
        "preference_match": {
            "status": status,
            "matched_count": len(hard_matches) + len(soft_matches),
            "positive_count": len(positive),
            "negative_count": len(negative),
        },
        "hard_constraint_violations": [_preference_note(item) for item in hard_matches],
        "soft_preference_notes": [_preference_note(item) for item in soft_matches],
        "ontology_relations": relation_index,
    }


def _candidate_relation_index(session: Session, food_name: str) -> dict[str, list[dict[str, Any]]]:
    relation_index: dict[str, list[dict[str, Any]]] = {ALLERGEN_RELATION_PREDICATE: []}
    normalized_food_name = normalize_ontology_label(food_name)
    if not normalized_food_name:
        return relation_index
    food_node = session.scalar(
        select(NutritionOntologyNode).where(
            NutritionOntologyNode.node_type == "food",
            NutritionOntologyNode.normalized_label == normalized_food_name,
        )
    )
    if food_node is None:
        return relation_index
    rows = session.execute(
        select(NutritionOntologyTriple, NutritionOntologyNode)
        .join(NutritionOntologyNode, NutritionOntologyTriple.object_node_id == NutritionOntologyNode.id)
        .where(
            NutritionOntologyTriple.subject_node_id == food_node.id,
            NutritionOntologyTriple.predicate == ALLERGEN_RELATION_PREDICATE,
        )
        .order_by(NutritionOntologyNode.label.asc())
    ).all()
    relation_index[ALLERGEN_RELATION_PREDICATE] = [
        {
            "predicate": triple.predicate,
            "object_node_id": node.id,
            "object_label": node.label,
            "object_type": node.node_type,
            "confidence": triple.confidence,
            "source": triple.source,
        }
        for triple, node in rows
    ]
    return relation_index


def _candidate_matches_preference(
    candidate: dict[str, Any],
    item: dict[str, Any],
    food_name: str,
    relation_index: dict[str, list[dict[str, Any]]] | None = None,
) -> bool:
    object_label = normalize_ontology_label(str(item.get("object_label") or ""))
    target_name = normalize_ontology_label(food_name)
    if not object_label or not target_name:
        return False
    if item.get("predicate") == "allergic_to" and _relation_contains_label(relation_index, ALLERGEN_RELATION_PREDICATE, object_label):
        return True
    object_type = item.get("object_type")
    if object_type in {"food", "ingredient"}:
        return object_label in target_name or target_name in object_label
    if object_type == "food_category":
        return object_label == normalize_ontology_label(str(candidate.get("category") or ""))
    return object_label in target_name


def _relation_contains_label(relation_index: dict[str, list[dict[str, Any]]] | None, predicate: str, normalized_label: str) -> bool:
    if not relation_index:
        return False
    for relation in relation_index.get(predicate, []):
        if normalize_ontology_label(str(relation.get("object_label") or "")) == normalized_label:
            return True
    return False


def _preference_note(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "predicate": item.get("predicate", ""),
        "predicate_label": item.get("predicate_label", ""),
        "object_label": item.get("object_label", ""),
        "object_type": item.get("object_type", ""),
        "safety_level": item.get("safety_level", ""),
        "confidence": item.get("confidence", 0.0),
    }


def _nutrient_amount(nutrients: dict[str, Any], nutrient_name: str) -> float:
    raw = nutrients.get(nutrient_name)
    if isinstance(raw, dict):
        return float(raw.get("value") or 0)
    return 0.0


def _clamp_float(value: float | int | str | None) -> float:
    try:
        return max(0.0, min(1.0, float(value if value is not None else 1.0)))
    except (TypeError, ValueError):
        return 1.0
