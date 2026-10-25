import json
import math
import os
import re
import statistics
from collections.abc import Callable

from groq import APIStatusError, Groq, GroqError

MODEL = "openai/gpt-oss-120b"
WORD_PATTERN = re.compile(r"\b[\w'-]+\b", re.UNICODE)
SENTENCE_PATTERN = re.compile(r"(?<=[.!?])\s+")


class DetectionError(RuntimeError):
    """Raised when a required detection signal cannot produce a valid result."""


def create_transparency_label(result: str, confidence: float) -> dict[str, str]:
    confidence_percent = round(confidence * 100)
    if result == "likely_ai":
        label_type = "likely_ai"
        label_text = (
            f"Likely AI-generated ({confidence_percent}% confidence). "
            "Automated detection can be wrong; the creator may appeal."
        )
    elif result == "likely_human":
        label_type = "likely_human"
        label_text = (
            f"Likely human-written ({confidence_percent}% confidence). "
            "Automated detection can be wrong."
        )
    else:
        label_type = "uncertain"
        label_text = (
            f"Uncertain ({confidence_percent}% confidence). We couldn't "
            "confidently determine whether this text is AI-generated or "
            "human-written."
        )
    return {"type": label_type, "text": label_text}


def classify_with_groq(text: str) -> dict[str, str | float]:
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key or api_key == "your_key_here":
        raise DetectionError("GROQ_API_KEY is not configured.")

    try:
        response = Groq(api_key=api_key).chat.completions.create(
            model=MODEL,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Assess whether the supplied text reads as AI-generated "
                        "or human-written. This is an uncertain estimate, not "
                        "proof of authorship. Return JSON with ai_likelihood, "
                        "a number from 0 to 1, and a short rationale."
                    ),
                },
                {"role": "user", "content": text},
            ],
            response_format={"type": "json_object"},
            temperature=0,
        )
    except GroqError as exc:
        if isinstance(exc, APIStatusError):
            error_message = (
                "Groq classification request failed "
                f"(HTTP {exc.status_code})."
            )
        else:
            error_message = (
                "Groq classification request failed "
                f"({type(exc).__name__})."
            )
        raise DetectionError(error_message) from exc

    content = response.choices[0].message.content
    try:
        result = json.loads(content or "")
        raw_likelihood = result["ai_likelihood"]
        rationale = result["rationale"]
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise DetectionError("Groq returned an invalid classification.") from exc
    if (
        not isinstance(result, dict)
        or isinstance(raw_likelihood, bool)
        or not isinstance(raw_likelihood, (int, float))
        or not isinstance(rationale, str)
        or not rationale.strip()
    ):
        raise DetectionError("Groq returned an invalid classification.")
    likelihood = float(raw_likelihood)
    if not math.isfinite(likelihood) or not 0 <= likelihood <= 1:
        raise DetectionError("Groq returned an invalid classification.")
    return {"ai_likelihood": likelihood, "rationale": rationale}


def stylometric_signal(text: str) -> dict[str, object]:
    words = [word.lower() for word in WORD_PATTERN.findall(text)]
    sentences = [
        part.strip()
        for part in SENTENCE_PATTERN.split(text.strip())
        if part.strip()
    ]

    if len(words) < 10:
        return {
            "ai_likelihood": 0.5,
            "evidence": "Insufficient text for a useful stylometric estimate.",
            "features": {
                "word_count": len(words),
                "sentence_count": len(sentences),
            },
        }

    sentence_lengths = [
        len(WORD_PATTERN.findall(sentence)) for sentence in sentences
    ]
    length_cv = 0.0
    if len(sentence_lengths) > 1 and statistics.mean(sentence_lengths) > 0:
        length_cv = (
            statistics.pstdev(sentence_lengths) / statistics.mean(sentence_lengths)
        )

    type_token_ratio = len(set(words)) / len(words)
    punctuation_density = sum(
        1 for character in text if character in ",;:!?—-"
    ) / len(words)

    # These hand-built cues are weak indicators, not a trained authorship test.
    uniformity_score = max(0.0, min(1.0, (0.8 - length_cv) / 0.8))
    vocabulary_score = max(0.0, min(1.0, (0.65 - type_token_ratio) / 0.4))
    punctuation_score = max(0.0, min(1.0, (0.12 - punctuation_density) / 0.12))
    likelihood = (
        0.50 * uniformity_score
        + 0.35 * vocabulary_score
        + 0.15 * punctuation_score
    )

    return {
        "ai_likelihood": round(likelihood, 4),
        "evidence": "Structural estimate from sentence-length variability, "
        "type-token ratio, and punctuation density.",
        "features": {
            "word_count": len(words),
            "sentence_count": len(sentences),
            "sentence_length_coefficient_of_variation": round(length_cv, 4),
            "type_token_ratio": round(type_token_ratio, 4),
            "punctuation_per_word": round(punctuation_density, 4),
        },
    }


def analyze_text(
    text: str,
    classifier: Callable[[str], dict[str, str | float]] = classify_with_groq,
) -> dict[str, object]:
    llm = classifier(text)
    llm_likelihood = float(llm["ai_likelihood"])
    if not math.isfinite(llm_likelihood) or not 0 <= llm_likelihood <= 1:
        raise DetectionError("LLM classifier returned an invalid likelihood.")

    stylometric = stylometric_signal(text)
    style_likelihood = float(stylometric["ai_likelihood"])
    disagreement = abs(llm_likelihood - style_likelihood)
    combined = 0.78 * llm_likelihood + 0.22 * style_likelihood
    confidence = 0.5 + abs(combined - 0.5) * (1 - disagreement)

    if combined > 0.75:
        result = "likely_ai"
    elif combined < 0.35:
        result = "likely_human"
    else:
        result = "uncertain"

    confidence = round(confidence, 4)

    return {
        "result": result,
        "combined_ai_likelihood": round(combined, 4),
        "confidence": confidence,
        "label": create_transparency_label(result, confidence),
        "signals": {
            "llm": {
                "ai_likelihood": round(llm_likelihood, 4),
                "rationale": str(llm["rationale"]),
            },
            "stylometric": stylometric,
        },
    }
