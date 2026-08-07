"""Pinned MetaGPT coordination adapter for FOMO's four-role SOP.

MetaGPT supplies the collaboration primitives in this module: a real
``Role`` runs one real custom ``Action`` and exchanges real ``Message``
objects.  FOMO intentionally keeps ownership of the durable state machine,
artifact persistence, sandbox permissions, command execution, QA evidence,
and publishing in :mod:`fomo.agent_runtime.sop`.

The custom actions never call MetaGPT's configured LLM.  They call FOMO's
``ModelClient`` (LiteLLM in production) and immediately validate the result
against the Pydantic artifact schema selected by the SOP.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import tempfile
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, PrivateAttr

from .llm import ModelClient, RetryObserver

_ARTIFACT_KIND_BY_ROLE = {
    "product_manager": "product_spec",
    "architect": "technical_spec",
    "engineer": "implementation_report",
    "reviewer": "diagnostic_report",
}
_UPSTREAM_ROLES = {
    "product_manager": ("reviewer",),
    "architect": ("product_manager", "reviewer"),
    "engineer": ("product_manager", "architect", "reviewer"),
    "reviewer": ("product_manager", "architect", "engineer"),
}
_ROLE_DETAILS = {
    "product_manager": ("FOMO Product Manager", "Make a testable product specification."),
    "architect": ("FOMO Architect", "Turn product intent into an implementable technical specification."),
    "engineer": ("FOMO Engineer", "Produce a complete implementation hand-off."),
    "reviewer": ("FOMO Reviewer", "Assess supplied deterministic evidence independently."),
}
_RUNTIME_CONFIG_TEMPLATE = Path(__file__).resolve().parent / "metagpt_runtime" / "config" / "config2.yaml"
_prepared_runtime_root: Path | None = None


class MetaGPTUnavailable(RuntimeError):
    """Raised when the explicit MetaGPT runtime cannot be used."""


def prepare_metagpt_runtime() -> Path:
    """Install FOMO's non-secret MetaGPT config *before* importing MetaGPT.

    MetaGPT constructs its own provider object while validating ``Role`` and
    ``Action`` instances, even though FOMO never calls that object.  Its wheel
    does not ship a usable default ``config2.yaml``.  This deterministic local
    configuration therefore exists solely to satisfy that constructor; all
    real generation still goes through FOMO's injected ``ModelClient``.
    """
    global _prepared_runtime_root
    if not _RUNTIME_CONFIG_TEMPLATE.is_file():
        raise MetaGPTUnavailable("FOMO's packaged MetaGPT coordination config is missing")
    if _prepared_runtime_root is None:
        runtime_root = Path(tempfile.mkdtemp(prefix="fomo-metagpt-"))
        config_path = runtime_root / "config" / "config2.yaml"
        config_path.parent.mkdir(parents=True)
        shutil.copyfile(_RUNTIME_CONFIG_TEMPLATE, config_path)
        # The template is intentionally non-secret, but keep all runtime
        # coordination state private to the worker process regardless.
        config_path.chmod(0o600)
        _prepared_runtime_root = runtime_root
    # FOMO owns this integration boundary, so it always supplies a safe base
    # configuration rather than requiring a user-managed provider setup.
    os.environ["METAGPT_PROJECT_ROOT"] = str(_prepared_runtime_root)
    return _prepared_runtime_root


def _silence_metagpt_diagnostics() -> None:
    """Disable MetaGPT's opaque loguru sinks, which can diagnose frame locals."""
    from metagpt.logs import logger as metagpt_logger

    metagpt_logger.remove()
    # FOMO emits its own bounded events and safe error types. MetaGPT is only a
    # collaboration primitive here, so it must never serialize model headers,
    # request bodies, or exception locals to stderr or a log file.
    metagpt_logger.add(lambda _message: None, level="TRACE", backtrace=False, diagnose=False)


@dataclass(frozen=True, slots=True)
class MetaGPTAvailability:
    available: bool
    reason: str


@dataclass(frozen=True, slots=True)
class MetaGPTRuntimeTypes:
    """Actual classes imported from the SHA-pinned MetaGPT extra."""

    action_base: type[Any]
    role_base: type[Any]
    message_type: type[Any]
    user_requirement_type: type[Any]
    action_types: Mapping[str, type[Any]]
    role_types: Mapping[str, type[Any]]


@dataclass(frozen=True, slots=True)
class ArtifactReference:
    """The only artifact data permitted in an inter-role MetaGPT message."""

    artifact_id: str
    artifact_kind: str
    role: str
    summary: str

    def as_dict(self) -> dict[str, str]:
        return {
            "artifactId": self.artifact_id,
            "artifactKind": self.artifact_kind,
            "role": self.role,
            "summary": self.summary,
        }


@dataclass(slots=True)
class MetaGPTInvocation:
    """Inspectable evidence that a real MetaGPT role/action/message path ran."""

    run_id: str
    role: str
    action: Any
    role_instance: Any
    input_messages: tuple[Any, ...]
    output_message: Any
    upstream_artifact_ids: tuple[str, ...]


@dataclass(slots=True)
class _PendingHandoff:
    action: Any
    role_instance: Any
    output_message: Any


class MetaGPTAdapter:
    """Execute FOMO artifacts through four real MetaGPT ``Role``/``Action`` pairs.

    This adapter deliberately has no Team, software-company, or repository
    generation abstraction.  It is a coordination layer only; its caller is
    still responsible for every durable FOMO transition and side effect.
    """

    def __init__(self, model_client: ModelClient) -> None:
        self.model_client = model_client
        self._runtime = self._load_runtime()
        self._handoffs: dict[str, dict[str, Any]] = defaultdict(dict)
        self._pending_handoffs: dict[tuple[str, str], _PendingHandoff] = {}
        self._invocations: list[MetaGPTInvocation] = []

    @staticmethod
    def availability() -> MetaGPTAvailability:
        if importlib.util.find_spec("metagpt") is None:
            return MetaGPTAvailability(
                available=False,
                reason=(
                    "AGENT_FRAMEWORK=metagpt requires the optional MetaGPT extra. "
                    "Install it with: uv sync --extra metagpt --extra dev."
                ),
            )
        return MetaGPTAvailability(available=True, reason="the pinned MetaGPT package is available")

    @property
    def runtime_types(self) -> MetaGPTRuntimeTypes:
        return self._runtime

    @property
    def invocations(self) -> tuple[MetaGPTInvocation, ...]:
        return tuple(self._invocations)

    def handoff(self, run_id: str, role: str) -> Any | None:
        """Return the latest real MetaGPT artifact-reference message for a role."""
        return self._handoffs.get(run_id, {}).get(role)

    async def run_action(
        self,
        *,
        run_id: str,
        role: str,
        model_alias: str,
        schema: type[BaseModel],
        messages: Sequence[dict[str, str]],
        persist_handoff: bool = True,
        on_retry: RetryObserver | None = None,
    ) -> BaseModel:
        """Run exactly one custom action via the matching real MetaGPT role."""
        if role not in _ARTIFACT_KIND_BY_ROLE:
            raise ValueError(f"unsupported MetaGPT FOMO role: {role}")

        action = self._runtime.action_types[role]()
        input_messages = self._input_messages(run_id, role)
        watched_actions = (
            [self._runtime.user_requirement_type]
            if not input_messages
            else [self._runtime.action_types[source_role] for source_role in _UPSTREAM_ROLES[role]]
        )
        profile, goal = _ROLE_DETAILS[role]
        role_instance = self._runtime.role_types[role](
            name=profile,
            profile=profile,
            goal=goal,
            constraints="Exchange only FOMO artifact references and bounded summaries with other roles.",
            actions=[action],
            watch=watched_actions,
        )
        # Role construction is allowed to normalize/copy actions. Configure the
        # action that the actual MetaGPT Role will invoke, not merely the object
        # passed to its constructor.
        action = role_instance.actions[0]
        action.configure(
            model_client=self.model_client,
            model_alias=model_alias,
            schema=schema,
            messages=messages,
            role=role,
            message_type=self._runtime.message_type,
            persist_handoff=persist_handoff,
            retry_observer=on_retry,
        )
        if input_messages:
            for handoff in input_messages:
                role_instance.put_message(handoff)
        else:
            role_instance.put_message(self._kickoff_message(run_id))

        try:
            output_message = await role_instance.run()
        except Exception as exc:
            # This is only a defensive fallback. Structured model/schema
            # failures return a controlled Message below and therefore never
            # enter MetaGPT's diagnostic exception decorator.
            if action.error is not None:
                raise action.error from exc
            raise
        if action.error is not None:
            # Re-raise outside MetaGPT so SOPRunner can apply the appropriate
            # error policy without MetaGPT logging request-frame locals.
            raise action.error
        if output_message is None or action.artifact is None:
            raise MetaGPTUnavailable(f"MetaGPT {role} role did not produce a structured artifact")

        upstream_ids = tuple(self._artifact_id_from_message(message) for message in input_messages)
        if persist_handoff:
            # Intermediate Engineer plan/batch actions deliberately opt out.
            # Only a final role action may occupy this `(run_id, role)` slot,
            # which prevents a later batch from overwriting the final report's
            # candidate-commit handoff.
            self._pending_handoffs[(run_id, role)] = _PendingHandoff(
                action=action,
                role_instance=role_instance,
                output_message=output_message,
            )
        self._invocations.append(
            MetaGPTInvocation(
                run_id=run_id,
                role=role,
                action=action,
                role_instance=role_instance,
                input_messages=tuple(input_messages),
                output_message=output_message,
                upstream_artifact_ids=upstream_ids,
            )
        )
        return action.artifact

    def register_artifact(
        self,
        *,
        run_id: str,
        role: str,
        artifact_id: str,
        artifact: BaseModel,
    ) -> None:
        """Attach a persisted FOMO artifact reference to the role's output message.

        Persistence happens in SOPRunner first because FOMO owns storage.  Only
        after that do we turn the real MetaGPT action output into a hand-off
        message; raw artifact JSON never crosses the role boundary.
        """
        pending = self._pending_handoffs.pop((run_id, role), None)
        if pending is None:
            raise MetaGPTUnavailable(f"MetaGPT {role} artifact was persisted without a role action")
        reference = ArtifactReference(
            artifact_id=artifact_id,
            artifact_kind=_ARTIFACT_KIND_BY_ROLE[role],
            role=role,
            summary=self._artifact_summary(role, artifact),
        )
        output_message = pending.output_message
        output_message.content = f"FOMO artifact handoff: {json.dumps(reference.as_dict(), ensure_ascii=False)}"
        output_message.role = "assistant"
        output_message.cause_by = type(pending.action)
        output_message.sent_from = f"fomo.{role}"
        output_message.metadata = {"fomo": {"kind": "artifact_handoff", "artifact": reference.as_dict()}}
        self._handoffs[run_id][role] = output_message

    def _input_messages(self, run_id: str, role: str) -> list[Any]:
        available = self._handoffs.get(run_id, {})
        return [available[source_role] for source_role in _UPSTREAM_ROLES[role] if source_role in available]

    def _kickoff_message(self, run_id: str) -> Any:
        return self._runtime.message_type(
            content=(
                f"FOMO run {run_id} started. The controlled SOP prompt is attached to this role's action, "
                "not to the coordination message."
            ),
            role="user",
            cause_by=self._runtime.user_requirement_type,
            sent_from="fomo.sop_runner",
            metadata={"fomo": {"kind": "run_kickoff", "runId": run_id}},
        )

    @staticmethod
    def _artifact_id_from_message(message: Any) -> str:
        try:
            artifact = message.metadata["fomo"]["artifact"]
            artifact_id = artifact["artifactId"]
        except (AttributeError, KeyError, TypeError) as exc:
            raise MetaGPTUnavailable("MetaGPT handoff is missing its persisted artifact reference") from exc
        if not isinstance(artifact_id, str) or not artifact_id:
            raise MetaGPTUnavailable("MetaGPT handoff has an invalid persisted artifact reference")
        return artifact_id

    @classmethod
    def _artifact_summary(cls, role: str, artifact: BaseModel) -> str:
        data = artifact.model_dump(mode="json", by_alias=True)
        if role == "product_manager":
            return cls._bound_summary(
                f"ProductSpec {data.get('title', 'untitled')!s}; "
                f"{len(data.get('acceptanceCriteria', []))} acceptance criteria."
            )
        if role == "architect":
            return cls._bound_summary(
                f"TechnicalSpec {data.get('framework', 'unspecified')!s}; "
                f"{len(data.get('routes', []))} routes and {len(data.get('filePlan', []))} planned files."
            )
        if role == "engineer":
            return cls._bound_summary(
                f"ImplementationReport; {len(data.get('fileChanges', []))} changed files and "
                f"{len(data.get('implementedAcceptanceIds', []))} implemented acceptance criteria."
            )
        return cls._bound_summary(
            f"DiagnosticReport; {len(data.get('blockingIssues', []))} blocking issues and "
            f"{len(data.get('gates', []))} quality gates."
        )

    @staticmethod
    def _bound_summary(value: str) -> str:
        return " ".join(value.split())[:240]

    @classmethod
    def _load_runtime(cls) -> MetaGPTRuntimeTypes:
        availability = cls.availability()
        if not availability.available:
            raise MetaGPTUnavailable(availability.reason)
        # This must happen before *any* MetaGPT import: importing Action alone
        # loads Config.default() in the pinned package.
        prepare_metagpt_runtime()
        try:
            from metagpt.actions import Action
            from metagpt.actions.add_requirement import UserRequirement
            from metagpt.roles import Role
            from metagpt.schema import Message
        except Exception as exc:  # pragma: no cover - exact dependency fault varies by platform
            raise MetaGPTUnavailable(
                "AGENT_FRAMEWORK=metagpt could not import the pinned MetaGPT runtime. "
                "Run: uv sync --extra metagpt --extra dev."
            ) from exc
        _silence_metagpt_diagnostics()

        class _StructuredArtifactAction(Action):
            _model_client: ModelClient | None = PrivateAttr(default=None)
            _model_alias: str = PrivateAttr(default="")
            _schema: type[BaseModel] | None = PrivateAttr(default=None)
            _messages: list[dict[str, str]] = PrivateAttr(default_factory=list)
            _role: str = PrivateAttr(default="")
            _message_type: type[Any] | None = PrivateAttr(default=None)
            _persist_handoff: bool = PrivateAttr(default=True)
            _retry_observer: RetryObserver | None = PrivateAttr(default=None)
            _artifact: BaseModel | None = PrivateAttr(default=None)
            _error: Exception | None = PrivateAttr(default=None)

            def configure(
                self,
                *,
                model_client: ModelClient,
                model_alias: str,
                schema: type[BaseModel],
                messages: Sequence[dict[str, str]],
                role: str,
                message_type: type[Any],
                persist_handoff: bool,
                retry_observer: RetryObserver | None,
            ) -> None:
                self._model_client = model_client
                self._model_alias = model_alias
                self._schema = schema
                self._messages = [dict(message) for message in messages]
                self._role = role
                self._message_type = message_type
                self._persist_handoff = persist_handoff
                self._retry_observer = retry_observer
                self._artifact = None
                self._error = None

            @property
            def artifact(self) -> BaseModel | None:
                return self._artifact

            @property
            def error(self) -> Exception | None:
                return self._error

            async def run(self, history: list[Any]) -> Any:
                if self._model_client is None or self._schema is None or self._message_type is None:
                    raise MetaGPTUnavailable("MetaGPT action was not configured by FOMO")
                references = []
                for message in history:
                    try:
                        reference = message.metadata["fomo"]["artifact"]
                    except (AttributeError, KeyError, TypeError):
                        continue
                    if isinstance(reference, dict):
                        references.append(
                            {
                                key: reference[key]
                                for key in ("artifactId", "artifactKind", "role", "summary")
                                if key in reference
                            }
                        )
                coordination_message = {
                    "role": "system",
                    "content": (
                        "MetaGPT coordination envelope (artifact references only): "
                        + json.dumps({"upstreamArtifacts": references}, ensure_ascii=False)
                    ),
                }
                try:
                    payload = await self._model_client.complete_json(
                        self._model_alias,
                        [*self._messages, coordination_message],
                        self._schema.__name__,
                        on_retry=self._retry_observer,
                    )
                    self._artifact = self._schema.model_validate(payload)
                except Exception as exc:
                    self._error = exc
                    # Do not cross MetaGPT Role.run() with a model exception:
                    # its decorator records a diagnostic traceback with frame
                    # locals, which could include HTTP request headers. The
                    # adapter re-raises the original safe FOMO error after the
                    # role returns, where FOMO owns retry and logging policy.
                    return self._message_type(
                        content="FOMO model artifact request failed; FOMO owns recovery.",
                        role="assistant",
                        cause_by=type(self),
                        sent_from=f"fomo.{self._role}",
                        metadata={"fomo": {"kind": "artifact_error", "role": self._role}},
                    )
                return self._message_type(
                    content=(
                        "FOMO structured artifact completed; awaiting durable artifact reference."
                        if self._persist_handoff
                        else "FOMO intermediate artifact completed; FOMO will persist it without an inter-role handoff."
                    ),
                    role="assistant",
                    cause_by=type(self),
                    sent_from=f"fomo.{self._role}",
                    metadata={
                        "fomo": {
                            "kind": "artifact_pending" if self._persist_handoff else "intermediate_artifact",
                            "role": self._role,
                        }
                    },
                )

        def action_type(name: str) -> type[Any]:
            return type(name, (_StructuredArtifactAction,), {"__module__": __name__})

        def role_type(name: str) -> type[Any]:
            return type(name, (Role,), {"__module__": __name__})

        actions = {
            "product_manager": action_type("FomoProductManagerAction"),
            "architect": action_type("FomoArchitectAction"),
            "engineer": action_type("FomoEngineerAction"),
            "reviewer": action_type("FomoReviewerAction"),
        }
        roles = {
            "product_manager": role_type("FomoProductManagerRole"),
            "architect": role_type("FomoArchitectRole"),
            "engineer": role_type("FomoEngineerRole"),
            "reviewer": role_type("FomoReviewerRole"),
        }
        return MetaGPTRuntimeTypes(
            action_base=Action,
            role_base=Role,
            message_type=Message,
            user_requirement_type=UserRequirement,
            action_types=actions,
            role_types=roles,
        )
