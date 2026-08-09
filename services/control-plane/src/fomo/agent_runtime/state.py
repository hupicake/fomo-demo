"""Explicit SOP state and failure routing rules."""

from __future__ import annotations

import hashlib
import json

from fomo.schemas import DiagnosticReport, RunPhase


class InvalidTransition(ValueError):
    pass


class SOPStateMachine:
    _allowed: dict[RunPhase, set[RunPhase]] = {
        RunPhase.queued: {RunPhase.product_analysis},
        RunPhase.product_analysis: {RunPhase.architecture},
        RunPhase.architecture: {RunPhase.implementation},
        RunPhase.implementation: {RunPhase.verification},
        RunPhase.verification: {RunPhase.repair, RunPhase.publishing},
        RunPhase.repair: {
            RunPhase.product_analysis,
            RunPhase.architecture,
            RunPhase.implementation,
        },
        RunPhase.publishing: set(),
    }

    def transition(self, current: RunPhase, target: RunPhase) -> RunPhase:
        if current == target:
            return target
        if target not in self._allowed[current]:
            raise InvalidTransition(f"cannot transition from {current} to {target}")
        return target


class FailureRouter:
    """Route deterministic evidence before accepting a model's suggested owner."""

    def route(self, report: DiagnosticReport) -> str:
        text = " ".join(report.blocking_issues + [item.message for item in report.findings]).lower()
        if any(token in text for token in ("acceptance", "requirement", "missing user story", "scope")):
            return "product_manager"
        if any(token in text for token in ("architecture", "route boundary", "component boundary", "state model")):
            return "architect"
        return "engineer"

    def fingerprint(self, report: DiagnosticReport) -> str:
        material = {
            "gates": [
                {"gate": item.gate, "status": item.status.value, "summary": item.summary[:500]}
                for item in report.gates
            ],
            "issues": sorted(report.blocking_issues),
            "findings": sorted(item.message for item in report.findings if item.severity in {"major", "error"}),
        }
        return hashlib.sha256(json.dumps(material, sort_keys=True).encode("utf-8")).hexdigest()[:24]
