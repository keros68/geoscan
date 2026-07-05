from __future__ import annotations

from dataclasses import dataclass, field
from difflib import SequenceMatcher


@dataclass(frozen=True)
class ErrorPair:
    ocr_text: str
    standard_text: str
    categories: set[str] = field(default_factory=set)
    crop_paths: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class StandardTerm:
    standard_text: str
    categories: set[str] = field(default_factory=set)
    source_ocr_texts: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class CorrectionDictionary:
    error_pairs: dict[str, ErrorPair]
    standard_terms: dict[str, StandardTerm]


@dataclass(frozen=True)
class CorrectionSuggestion:
    suggested_text: str
    method: str
    score: float
    matched_text: str
    review_required: str
    reason: str


def _clean_text(value: object) -> str:
    return str(value or "").strip()


def _category(value: object) -> str:
    return _clean_text(value) or "unknown"


def build_correction_dictionary(rows: list[dict[str, object]]) -> CorrectionDictionary:
    error_pairs: dict[str, ErrorPair] = {}
    standard_terms: dict[str, StandardTerm] = {}

    error_categories: dict[str, set[str]] = {}
    error_crops: dict[str, set[str]] = {}
    error_standard: dict[str, str] = {}
    term_categories: dict[str, set[str]] = {}
    term_sources: dict[str, set[str]] = {}

    for row in rows:
        ocr_text = _clean_text(row.get("ocr_text"))
        standard_text = _clean_text(row.get("review_text"))
        if not ocr_text or not standard_text:
            continue

        category = _category(row.get("category"))
        crop_path = _clean_text(row.get("crop_path"))

        term_categories.setdefault(standard_text, set()).add(category)
        term_sources.setdefault(standard_text, set()).add(ocr_text)

        if ocr_text != standard_text:
            error_standard[ocr_text] = standard_text
            error_categories.setdefault(ocr_text, set()).add(category)
            if crop_path:
                error_crops.setdefault(ocr_text, set()).add(crop_path)

    for ocr_text, standard_text in error_standard.items():
        error_pairs[ocr_text] = ErrorPair(
            ocr_text=ocr_text,
            standard_text=standard_text,
            categories=error_categories.get(ocr_text, set()),
            crop_paths=error_crops.get(ocr_text, set()),
        )

    for standard_text in term_categories:
        standard_terms[standard_text] = StandardTerm(
            standard_text=standard_text,
            categories=term_categories.get(standard_text, set()),
            source_ocr_texts=term_sources.get(standard_text, set()),
        )

    return CorrectionDictionary(error_pairs=error_pairs, standard_terms=standard_terms)


def _similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return round(SequenceMatcher(None, a, b).ratio(), 6)


def suggest_ocr_correction(
    row: dict[str, object],
    dictionary: CorrectionDictionary,
    *,
    fuzzy_threshold: float = 0.72,
) -> CorrectionSuggestion:
    ocr_text = _clean_text(row.get("ocr_text"))
    category = _category(row.get("category"))
    if not ocr_text:
        return CorrectionSuggestion("", "empty", 0.0, "", "yes", "empty OCR text")

    exact_pair = dictionary.error_pairs.get(ocr_text)
    if exact_pair is not None and category in exact_pair.categories:
        return CorrectionSuggestion(
            suggested_text=exact_pair.standard_text,
            method="exact_error_pair",
            score=1.0,
            matched_text=ocr_text,
            review_required="yes",
            reason="matched a manually confirmed OCR error pair",
        )

    exact_term = dictionary.standard_terms.get(ocr_text)
    if exact_term is not None and category in exact_term.categories:
        return CorrectionSuggestion(
            suggested_text=ocr_text,
            method="exact_standard_term",
            score=1.0,
            matched_text=ocr_text,
            review_required="yes",
            reason="OCR text already matches a known standard term",
        )

    best_term = ""
    best_score = 0.0
    for term in dictionary.standard_terms.values():
        if category not in term.categories:
            continue
        score = _similarity(ocr_text, term.standard_text)
        score = min(1.0, score + 0.05)
        if score > best_score:
            best_score = score
            best_term = term.standard_text

    if best_score >= fuzzy_threshold:
        return CorrectionSuggestion(
            suggested_text=best_term,
            method="fuzzy_standard_term",
            score=round(best_score, 6),
            matched_text=best_term,
            review_required="yes",
            reason="similar to a known standard term; manual confirmation required",
        )

    return CorrectionSuggestion(
        suggested_text="",
        method="no_suggestion",
        score=round(best_score, 6),
        matched_text=best_term,
        review_required="yes",
        reason="no dictionary suggestion above threshold",
    )
