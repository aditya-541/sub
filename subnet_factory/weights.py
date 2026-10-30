"""Convert independently validated miner work into a reviewable weight plan."""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable
from typing import Any

from .protocol import BuildContribution, ValidationReport, WeightPlan


class WeightPlanError(ValueError):
    pass


def build_weight_plan(
    netuid: int,
    contributions: Iterable[BuildContribution],
    reports: Iterable[ValidationReport],
    neurons: Iterable[Any],
) -> WeightPlan:
    """Average each miner's best score per task, then normalize across known UIDs.

    Rejected contributions score zero. A miner can submit multiple candidate
    artifacts for a task, but only its best score for that task counts. Across
    tasks, each task has equal influence so a single large task cannot dominate.
    """
    if netuid < 0:
        raise WeightPlanError("netuid must be non-negative")
    contribution_by_id = {item.contribution_id: item for item in contributions}
    report_by_id: dict[str, ValidationReport] = {}
    for report in reports:
        if report.contribution_id in report_by_id:
            raise WeightPlanError(f"Duplicate report for contribution {report.contribution_id}")
        report_by_id[report.contribution_id] = report

    task_scores: dict[str, dict[str, float]] = defaultdict(dict)
    excluded = 0
    for contribution_id, report in report_by_id.items():
        contribution = contribution_by_id.get(contribution_id)
        if contribution is None or contribution.task_id != report.task_id:
            excluded += 1
            continue
        hotkey = contribution.miner_hotkey
        if not hotkey:
            excluded += 1
            continue
        score = report.score if report.accepted else 0.0
        if not math.isfinite(score) or not 0 <= score <= 1:
            excluded += 1
            continue
        task_scores[hotkey][contribution.task_id] = max(
            score, task_scores[hotkey].get(contribution.task_id, 0.0)
        )

    raw_by_hotkey = {
        hotkey: sum(scores.values()) / len(scores)
        for hotkey, scores in task_scores.items()
        if scores
    }
    uid_by_hotkey: dict[str, int] = {}
    for neuron in neurons:
        hotkey = getattr(neuron, "hotkey", None)
        uid = getattr(neuron, "uid", None)
        if hotkey is None or uid is None:
            continue
        try:
            uid_by_hotkey[str(hotkey)] = int(uid)
        except (TypeError, ValueError):
            continue

    eligible = {
        hotkey: score
        for hotkey, score in raw_by_hotkey.items()
        if hotkey in uid_by_hotkey and score > 0
    }
    excluded += sum(1 for hotkey in raw_by_hotkey if hotkey not in uid_by_hotkey)
    total = sum(eligible.values())
    planned = (
        [
            {
                "hotkey": hotkey,
                "uid": uid_by_hotkey[hotkey],
                "raw_score": eligible[hotkey],
                "weight": eligible[hotkey] / total,
            }
            for hotkey in sorted(eligible, key=lambda item: uid_by_hotkey[item])
        ]
        if total > 0
        else []
    )
    return WeightPlan(
        netuid=netuid,
        weights=planned,
        excluded_submissions=excluded,
    )
