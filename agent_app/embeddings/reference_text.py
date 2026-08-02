from __future__ import annotations

ADVERSE_REACTION_TEXT_VERSION = "adverse-reaction-text-v2"
PRO_CTCAE_TEXT_VERSION = "pro-ctcae-alias-text-v2"

_ADVERSE_REACTION_PREFIX = "의약품 부작용 증상 용어: "
_PRO_CTCAE_PREFIX = "Pro-CTCAE 환자 보고 증상 별칭: "


def adverse_reaction_embedding_text(value: str) -> str:
    return _ADVERSE_REACTION_PREFIX + value.strip()


def pro_ctcae_embedding_text(value: str) -> str:
    return _PRO_CTCAE_PREFIX + value.strip()


def adverse_reaction_source_version(
    source_sha256: str,
    extractor_version: str,
) -> str:
    return (
        f"mfds:{source_sha256.strip().lower()}:"
        f"{extractor_version.strip()}:{ADVERSE_REACTION_TEXT_VERSION}"
    )


def versioned_pro_ctcae_source(workbook_sha256: str) -> str:
    return (
        f"pro_ctcae:{workbook_sha256.strip().lower()}:"
        f"{PRO_CTCAE_TEXT_VERSION}"
    )
