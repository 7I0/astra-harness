from __future__ import annotations

"""Small offline evaluator for closed-set Jev decisions."""

from collections import Counter
from typing import Any, Iterable


def classification_metrics(expected: Iterable[str], predicted: Iterable[str]) -> dict[str, Any]:
    truth = list(expected)
    guess = list(predicted)
    if len(truth) != len(guess):
        raise ValueError("expected and predicted lengths differ")
    labels = sorted(set(truth) | set(guess))
    per_class: dict[str, dict[str, float]] = {}
    for label in labels:
        tp = sum(actual == label and got == label for actual, got in zip(truth, guess))
        fp = sum(actual != label and got == label for actual, got in zip(truth, guess))
        fn = sum(actual == label and got != label for actual, got in zip(truth, guess))
        precision = tp / (tp + fp) if tp + fp else 0.0
        recall = tp / (tp + fn) if tp + fn else 0.0
        f1 = (2 * precision * recall / (precision + recall)) if precision + recall else 0.0
        per_class[label] = {"precision": precision, "recall": recall, "f1": f1}
    macro_f1 = sum(row["f1"] for row in per_class.values()) / len(per_class) if per_class else 0.0
    return {
        "count": len(truth),
        "accuracy": sum(actual == got for actual, got in zip(truth, guess)) / len(truth) if truth else 0.0,
        "macro_f1": macro_f1,
        "per_class": per_class,
        "confusion": {f"{actual}->{got}": count for (actual, got), count in Counter(zip(truth, guess)).items()},
    }
