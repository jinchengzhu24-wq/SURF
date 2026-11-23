"""Shape generated message replies before binding and persistence."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import TYPE_CHECKING

from cocreation.domain.level_validation import format_map_claim_correction

if TYPE_CHECKING:
    from cocreation.llm.config_and_runtime import LLMExecutionResult


def _without_revision_execution(execution: LLMExecutionResult, **reply_updates) -> LLMExecutionResult:
    """Remove the executable change and its authorization together."""
    return replace(
        execution,
        proposed_rows=None,
        revision_plan={},
        revision_contract={},
        revision_operations=[],
        proposal_binding={},
        **reply_updates,
    )


def apply_map_claim_conflict_policy(
    execution: LLMExecutionResult,
    conflicts: Sequence[Mapping[str, object]] | None,
    language: str,
) -> LLMExecutionResult:
    """Correct a contradicted current-map claim and discard actionable output."""
    if not conflicts:
        return execution

    correction = format_map_claim_correction(conflicts, language)
    visible_body = str(execution.assistant_message or '').strip()
    if correction and correction not in visible_body:
        visible_body = '\n\n'.join(part for part in (correction, visible_body) if part)

    guidance = dict(execution.guidance or {})
    guidance.update({
        'move': 'offer_perspective',
        'intentHypothesis': None,
        'intentConfidence': None,
        'followUpQuestion': None,
        'proposalOffer': None,
        'disagreement': None,
        'uiCues': [],
        'coordinateLinks': [],
    })
    guidance.pop('designContextPatch', None)
    guidance.pop('designContextPatchError', None)
    return _without_revision_execution(
        execution,
        assistant_message=visible_body,
        proposal_diagnostics={**(execution.proposal_diagnostics or {}), 'category': 'map_claim_conflict'},
        guidance=guidance,
    )


def apply_conversation_only_policy(
    execution: LLMExecutionResult,
    *,
    active_disagreement: Mapping[str, object] | None,
) -> LLMExecutionResult:
    """Keep prose and permitted metadata without exposing an action or another card."""
    guidance = execution.guidance or {}
    next_guidance = {
        'move': 'offer_perspective',
        'intentHypothesis': None,
        'intentConfidence': None,
        'proposalOffer': None,
        'uiCues': [],
        'coordinateLinks': guidance.get('coordinateLinks') or [],
        'proposalDiscovery': guidance.get('proposalDiscovery'),
        'disagreement': {**active_disagreement, 'displayCard': False} if active_disagreement else None,
    }
    if guidance.get('_stateTransitionDeferred'):
        next_guidance['_stateTransitionDeferred'] = True
    return _without_revision_execution(
        execution,
        guidance=next_guidance,
    )
