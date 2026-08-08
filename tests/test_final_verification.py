from dataclasses import FrozenInstanceError, replace
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from elman_os.agent_contracts import canonical_json
from elman_os.agent_output_validation import (
    AgentOutputValidationResult,
    AgentOutputValidationStatus,
    ArtifactClassification,
    ArtifactOperation,
    ArtifactValidationDecision,
    ArtifactValidationRecord,
)
from elman_os.artifact_application_plan import (
    ArtifactApplicationPolicy,
    ArtifactApplicationRequest,
    build_artifact_application_plan,
)
from elman_os.artifact_payload_verification import (
    ArtifactPayload,
    ArtifactPayloadVerification,
    ArtifactPayloadVerificationPolicy,
    ArtifactPayloadVerificationRequest,
)
from elman_os.execution_journal import (
    ExecutionEventType,
    ExecutionJournal,
)
from elman_os.execution_plan import (
    ExecutionPlan,
    ExecutionStep,
    PlanStatus,
    StepStatus,
)
from elman_os.final_verification import (
    FinalDecisionOutcomeLink,
    FinalEvidenceKind,
    FinalEvidenceRecord,
    FinalEvidenceStatus,
    FinalExecutionErrorRecord,
    FinalPolicyFinding,
    FinalReportSignatureError,
    FinalReportSigner,
    FinalVerificationError,
    FinalVerificationGate,
    FinalVerificationIntegrityError,
    FinalVerificationPolicy,
    FinalVerificationPolicyError,
    FinalVerificationReport,
    FinalVerificationRequest,
    FinalVerificationStatus,
    FinalVerifier,
)
from elman_os.metacognitive_supervision_decision_contracts import (
    MetacognitiveDecisionAction,
    MetacognitiveFindingKind,
    MetacognitiveRiskLevel,
    MetacognitiveSupervisionContext,
    MetacognitiveSupervisionDecision,
    MetacognitiveSupervisionDecisionPolicy,
    MetacognitiveSupervisionFinding,
)
from elman_os.project_memory import (
    ProjectMemoryKind,
    ProjectMemoryOrigin,
    ProjectMemoryRetentionClass,
    ProjectMemorySourceType,
    ProjectMemoryStore,
)


T0 = "2026-08-08T00:00:00Z"
T1 = "2026-08-08T00:00:01Z"
T2 = "2026-08-08T00:00:02Z"
T3 = "2026-08-08T00:00:03Z"
T4 = "2026-08-08T00:00:04Z"
T5 = "2026-08-08T00:00:05Z"
T6 = "2026-08-08T00:00:06Z"
T7 = "2026-08-08T00:00:07Z"
T8 = "2026-08-08T00:00:08Z"
T9 = "2026-08-08T00:00:09Z"

PLAN_ID = "plan:final-verification-001"
PROJECT_ID = "project:elman-os"
STEP_ID = "build.release"
AGENT_ID = "ELMAN_BUILDER"
OUTPUT_ID = "output-validation:final-001"
OUTPUT_EVIDENCE_ID = f"evidence:output:{OUTPUT_ID}"
CONTENT = b"verified artifact\n"
CONTENT_HASH = hashlib.sha256(CONTENT).hexdigest()


def plan_hash(plan: ExecutionPlan) -> str:
    return hashlib.sha256(
        canonical_json(plan.to_dict()).encode("utf-8")
    ).hexdigest()


def make_plan(
    *,
    status: PlanStatus = PlanStatus.COMPLETED,
    step_status: StepStatus = StepStatus.COMPLETED,
) -> ExecutionPlan:
    return ExecutionPlan(
        plan_id=PLAN_ID,
        project_id=PROJECT_ID,
        objective="Produce and verify one deterministic release artifact.",
        created_by="ELMAN_NEXUS",
        steps=(
            ExecutionStep(
                step_id=STEP_ID,
                title="Build release artifact",
                capability_id="build.release",
                objective="Build one release artifact and provide evidence.",
                assigned_agent_id=AGENT_ID,
                status=step_status,
            ),
        ),
        status=status,
        approval_reference="approval:final-release-001",
    )


def make_journal(*, terminal: bool = True) -> tuple[ExecutionJournal, object]:
    journal = ExecutionJournal(PLAN_ID)
    journal.append(
        ExecutionEventType.PLAN_CREATED,
        T0,
        agent_id="ELMAN_NEXUS",
    )
    journal.append(
        ExecutionEventType.PLAN_APPROVED,
        T1,
        agent_id="ELMAN_NEXUS",
    )
    journal.append(
        ExecutionEventType.PLAN_STARTED,
        T2,
        agent_id="ELMAN_NEXUS",
    )
    journal.append(
        ExecutionEventType.STEP_ASSIGNED,
        T3,
        step_id=STEP_ID,
        agent_id=AGENT_ID,
    )
    journal.append(
        ExecutionEventType.STEP_STARTED,
        T4,
        step_id=STEP_ID,
        agent_id=AGENT_ID,
    )
    journal.append(
        ExecutionEventType.STEP_COMPLETED,
        T5,
        step_id=STEP_ID,
        agent_id=AGENT_ID,
    )
    output_prefix = journal.seal()
    if terminal:
        journal.append(
            ExecutionEventType.PLAN_COMPLETED,
            T6,
            agent_id="ELMAN_NEXUS",
        )
    return journal, output_prefix


def make_output(
    prefix,
    *,
    status: AgentOutputValidationStatus = AgentOutputValidationStatus.ACCEPTED,
) -> AgentOutputValidationResult:
    record = ArtifactValidationRecord(
        index=0,
        path="dist/release.txt",
        decision=ArtifactValidationDecision.ACCEPTED,
        classification=ArtifactClassification.DOCUMENTATION,
        operation=ArtifactOperation.CREATE,
        sha256=CONTENT_HASH,
        size_bytes=len(CONTENT),
        media_type="text/plain",
        reasons=("ACCEPTED: declaration satisfies final policy",),
    )
    reasons = (
        ()
        if status is AgentOutputValidationStatus.ACCEPTED
        else (
            "REJECTED: forced rejection for final-verification test",
        )
        if status is AgentOutputValidationStatus.REJECTED
        else (
            "REVIEW: forced review for final-verification test",
        )
    )
    return AgentOutputValidationResult(
        validation_id=OUTPUT_ID,
        status=status,
        request_hash="1" * 64,
        policy_id="policy:output-final-001",
        policy_hash="2" * 64,
        ingestion_id="ingestion:final-001",
        ingestion_result_hash="3" * 64,
        plan_id=PLAN_ID,
        step_id=STEP_ID,
        agent_request_id="agent-request:final-001",
        agent_id=AGENT_ID,
        response_hash="4" * 64,
        records=(record,),
        top_level_reasons=reasons,
        accepted_count=1,
        review_count=0,
        rejected_count=0,
        total_declared_bytes=len(CONTENT),
        validated_at=T5,
        plan_state_hash="5" * 64,
        journal_event_count=prefix.event_count,
        journal_head_hash=prefix.head_hash,
        journal_hash=prefix.journal_hash,
    )


def make_payload_result(output: AgentOutputValidationResult):
    application_policy = ArtifactApplicationPolicy(
        policy_id="policy:application-final-001"
    )
    application_request = ArtifactApplicationRequest.from_validation_result(
        output,
        application_policy,
        requested_by="ELMAN_NEXUS",
        requested_at=T6,
    )
    application_plan = build_artifact_application_plan(
        application_request,
        output,
        application_policy,
    )
    payloads = tuple(
        ArtifactPayload(
            operation_id=operation.operation_id,
            destination_path=operation.destination_path,
            media_type=operation.media_type,
            content=CONTENT,
        )
        for operation in application_plan.operations
    )
    verification_policy = ArtifactPayloadVerificationPolicy(
        policy_id="policy:payload-final-001"
    )
    verification_request = ArtifactPayloadVerificationRequest.from_plan_and_payloads(
        application_plan,
        verification_policy,
        payloads,
        requested_by="ELMAN_NEXUS",
        requested_at=T7,
    )
    return ArtifactPayloadVerification(
        verification_request,
        application_plan,
        payloads,
        verification_policy,
    ).verify()


def make_memory_decision(expected_result_hash: str):
    with TemporaryDirectory() as directory:
        store = ProjectMemoryStore(Path(directory) / "memory.sqlite3")
        return store.record(
            tenant_id="tenant:local",
            project_id=PROJECT_ID,
            execution_id="execution:final-verification-001",
            kind=ProjectMemoryKind.DECISION,
            title="Release output must match the approved digest",
            content={
                "decision": "Accept only the approved release output digest.",
                "expected_result_hash": expected_result_hash,
            },
            labels=("final-verification", "release"),
            origin=ProjectMemoryOrigin(
                source_type=ProjectMemorySourceType.USER_APPROVAL,
                source_id="approval:final-decision-001",
                actor_id="human:owner-001",
                captured_at=T5,
                evidence_references=(OUTPUT_EVIDENCE_ID,),
            ),
            retention_class=ProjectMemoryRetentionClass.PERMANENT,
        )


def make_supervision(
    plan: ExecutionPlan,
    journal: ExecutionJournal,
    *,
    action: MetacognitiveDecisionAction = MetacognitiveDecisionAction.CONTINUE,
):
    context = MetacognitiveSupervisionContext.capture(
        plan_id=plan.plan_id,
        project_id=plan.project_id,
        plan_state_hash=plan_hash(plan),
        journal_hash=journal.seal().journal_hash,
        checkpoint_hash="c" * 64,
        evidence_references=(OUTPUT_EVIDENCE_ID,),
        observed_by="ELMAN_SUPERVISOR",
        observed_at=T7,
        objective="Authorize or deny final completion.",
    )
    policy = MetacognitiveSupervisionDecisionPolicy(
        policy_id="policy:supervision-final-001"
    )
    findings = ()
    approval_required = False
    approval_reference = None
    if action is not MetacognitiveDecisionAction.CONTINUE:
        finding = MetacognitiveSupervisionFinding.from_context(
            context=context,
            kind=MetacognitiveFindingKind.UNCERTAINTY,
            risk_level=MetacognitiveRiskLevel.MEDIUM,
            summary="Final clearance is intentionally withheld.",
            evidence_references=(OUTPUT_EVIDENCE_ID,),
            affected_step_ids=(STEP_ID,),
            confidence_bp=8000,
        )
        findings = (finding,)
        approval_required = True
        approval_reference = "approval:supervision-pause-001"
    return MetacognitiveSupervisionDecision.declare(
        policy=policy,
        context=context,
        findings=findings,
        action=action,
        confidence_bp=8500,
        approval_required=approval_required,
        approval_reference=approval_reference,
        decided_by="ELMAN_SUPERVISOR",
        decided_at=T8,
        rationale="Deterministic final supervision decision.",
    )


def make_external_evidence(
    *,
    evidence_id="evidence:external:resolution-001",
    status=FinalEvidenceStatus.VERIFIED,
    step_id=STEP_ID,
):
    return FinalEvidenceRecord(
        evidence_id=evidence_id,
        kind=FinalEvidenceKind.TEST_RESULT,
        status=status,
        plan_id=PLAN_ID,
        step_id=step_id,
        source_reference="external:test-run-001",
        source_hash="e" * 64,
        captured_at=T7,
    )


def make_request(
    *,
    plan_status=PlanStatus.COMPLETED,
    terminal_journal=True,
    include_output=True,
    output_status=AgentOutputValidationStatus.ACCEPTED,
    include_payload=True,
    include_supervision=True,
    supervision_action=MetacognitiveDecisionAction.CONTINUE,
    include_memory=True,
    include_link=True,
    evidence=(),
    policy_findings=(),
    execution_errors=(),
):
    plan = make_plan(status=plan_status)
    journal, prefix = make_journal(terminal=terminal_journal)
    output = make_output(prefix, status=output_status)
    outputs = (output,) if include_output else ()
    payload = make_payload_result(output)
    payloads = (payload,) if include_payload and include_output else ()
    memory = make_memory_decision(output.result_hash)
    memories = (memory,) if include_memory else ()
    link = FinalDecisionOutcomeLink(
        link_id="decision-link:final-001",
        memory_id=memory.memory_id,
        memory_revision_hash=memory.revision_hash,
        expected_result_hash=output.result_hash,
        observed_result_hash=output.result_hash,
        evidence_ids=(OUTPUT_EVIDENCE_ID,),
        linked_at=T8,
    )
    links = (link,) if include_link and include_memory and include_output else ()
    supervision = make_supervision(
        plan,
        journal,
        action=supervision_action,
    )
    supervisions = (supervision,) if include_supervision else ()
    return FinalVerificationRequest.capture(
        verification_id="final-verification:001",
        policy=FinalVerificationPolicy(
            policy_id="policy:final-verification-001"
        ),
        plan=plan,
        journal=journal,
        output_validations=outputs,
        payload_verifications=payloads,
        evidence=evidence,
        policy_findings=policy_findings,
        execution_errors=execution_errors,
        decision_links=links,
        supervision_decisions=supervisions,
        memory_records=memories,
        verifier_id="ELMAN_VERIFIER",
        requested_at=T9,
    )


def signer(byte=b"k") -> FinalReportSigner:
    return FinalReportSigner(
        key_id="key:final-verification-001",
        secret=byte * 32,
    )


def gate(report, name):
    return next(item for item in report.gates if item.gate is name)


class PolicyAndEvidenceTests(unittest.TestCase):
    def test_policy_round_trip_and_hash_are_deterministic(self):
        policy = FinalVerificationPolicy(policy_id="policy:final-001")
        restored = FinalVerificationPolicy.from_json(policy.to_json())
        self.assertEqual(restored, policy)
        self.assertEqual(restored.policy_hash, policy.policy_hash)

    def test_policy_rejects_every_disabled_mandatory_gate(self):
        fields = (
            "require_current_supervision",
            "require_memory_decision_links",
            "require_terminal_journal",
            "require_signed_report",
            "fail_closed",
        )
        for name in fields:
            with self.subTest(name=name):
                with self.assertRaises(FinalVerificationPolicyError):
                    FinalVerificationPolicy(
                        policy_id="policy:unsafe-final-001",
                        **{name: False},
                    )

    def test_policy_rejects_invalid_evidence_threshold(self):
        for value in (0, 17, True):
            with self.subTest(value=value):
                with self.assertRaises(FinalVerificationError):
                    FinalVerificationPolicy(
                        policy_id="policy:invalid-final-001",
                        minimum_verified_evidence_per_step=value,
                    )

    def test_evidence_round_trip_is_hash_bound_and_immutable(self):
        evidence = make_external_evidence()
        restored = FinalEvidenceRecord.from_json(evidence.to_json())
        self.assertEqual(restored, evidence)
        restored.verify_hash()
        with self.assertRaises(FrozenInstanceError):
            evidence.status = FinalEvidenceStatus.FAILED  # type: ignore[misc]

    def test_evidence_detects_tampering(self):
        data = make_external_evidence().to_dict()
        data["status"] = "failed"
        with self.assertRaises(FinalVerificationIntegrityError):
            FinalEvidenceRecord.from_dict(data)

    def test_resolved_policy_finding_requires_evidence_reference(self):
        with self.assertRaises(FinalVerificationError):
            FinalPolicyFinding(
                finding_id="finding:001",
                rule_id="release.policy",
                summary="Resolved without evidence.",
                resolved=True,
                detected_at=T6,
            )

    def test_unresolved_error_forbids_resolution_reference(self):
        with self.assertRaises(FinalVerificationError):
            FinalExecutionErrorRecord(
                error_id="error:001",
                code="build.failure",
                summary="Still unresolved.",
                resolved=False,
                resolution_evidence_id="evidence:external:resolution-001",
                detected_at=T6,
            )

    def test_decision_link_exposes_coherence_without_mutability(self):
        link = FinalDecisionOutcomeLink(
            link_id="decision-link:001",
            memory_id="memory:001",
            memory_revision_hash="a" * 64,
            expected_result_hash="b" * 64,
            observed_result_hash="b" * 64,
            evidence_ids=("evidence:001",),
            linked_at=T8,
        )
        self.assertTrue(link.coherent)
        self.assertFalse(
            replace(link, observed_result_hash="c" * 64, link_hash=None).coherent
        )


class RequestIntegrityTests(unittest.TestCase):
    def test_capture_round_trip_preserves_all_bindings(self):
        request = make_request()
        restored = FinalVerificationRequest.from_json(request.to_json())
        self.assertEqual(restored.to_dict(), request.to_dict())
        restored.verify_hash()

    def test_capture_adds_output_and_payload_evidence(self):
        request = make_request()
        kinds = {item.kind for item in request.evidence}
        self.assertIn(FinalEvidenceKind.OUTPUT_VALIDATION, kinds)
        self.assertIn(FinalEvidenceKind.ARTIFACT_INTEGRITY, kinds)
        self.assertEqual(len(request.evidence), 2)

    def test_tampered_plan_hash_is_rejected(self):
        data = make_request().to_dict()
        data["plan_state_hash"] = "0" * 64
        with self.assertRaises(FinalVerificationIntegrityError):
            FinalVerificationRequest.from_dict(data)

    def test_tampered_journal_is_rejected(self):
        data = make_request().to_dict()
        data["journal_jsonl"] = data["journal_jsonl"].replace(
            '"agent_id":"ELMAN_NEXUS"',
            '"agent_id":"ELMAN_INTRUDER"',
            1,
        )
        with self.assertRaises(FinalVerificationIntegrityError):
            FinalVerificationRequest.from_dict(data)

    def test_output_must_bind_to_a_real_journal_prefix(self):
        plan = make_plan()
        journal, prefix = make_journal()
        wrong = replace(
            make_output(prefix),
            journal_hash="f" * 64,
            result_hash=None,
        )
        with self.assertRaises(FinalVerificationIntegrityError):
            FinalVerificationRequest.capture(
                verification_id="final-verification:bad-prefix",
                policy=FinalVerificationPolicy(policy_id="policy:final-001"),
                plan=plan,
                journal=journal,
                output_validations=(wrong,),
                verifier_id="ELMAN_VERIFIER",
                requested_at=T9,
            )

    def test_payload_must_reference_an_embedded_output(self):
        plan = make_plan()
        journal, prefix = make_journal()
        output = make_output(prefix)
        payload = make_payload_result(output)
        with self.assertRaises(FinalVerificationIntegrityError):
            FinalVerificationRequest.capture(
                verification_id="final-verification:unknown-output",
                policy=FinalVerificationPolicy(policy_id="policy:final-001"),
                plan=plan,
                journal=journal,
                output_validations=(),
                payload_verifications=(payload,),
                verifier_id="ELMAN_VERIFIER",
                requested_at=T9,
            )

    def test_unknown_evidence_source_requires_external_namespace(self):
        invalid = replace(
            make_external_evidence(),
            source_reference="unknown:source",
            evidence_hash=None,
        )
        with self.assertRaises(FinalVerificationIntegrityError):
            make_request(evidence=(invalid,))

    def test_duplicate_automatic_evidence_is_rejected(self):
        journal, prefix = make_journal()
        output = make_output(prefix)
        duplicate = FinalEvidenceRecord(
            evidence_id=OUTPUT_EVIDENCE_ID,
            kind=FinalEvidenceKind.EXTERNAL,
            status=FinalEvidenceStatus.VERIFIED,
            plan_id=PLAN_ID,
            step_id=STEP_ID,
            source_reference="external:duplicate",
            source_hash="d" * 64,
            captured_at=T7,
        )
        with self.assertRaises(FinalVerificationError):
            FinalVerificationRequest.capture(
                verification_id="final-verification:duplicate-evidence",
                policy=FinalVerificationPolicy(policy_id="policy:final-001"),
                plan=make_plan(),
                journal=journal,
                output_validations=(output,),
                evidence=(duplicate,),
                verifier_id="ELMAN_VERIFIER",
                requested_at=T9,
            )

    def test_resolution_evidence_must_be_verified(self):
        evidence = make_external_evidence(status=FinalEvidenceStatus.FAILED)
        finding = FinalPolicyFinding(
            finding_id="finding:resolved-001",
            rule_id="release.policy",
            summary="Declared resolved by failed evidence.",
            resolved=True,
            resolution_evidence_id=evidence.evidence_id,
            detected_at=T6,
        )
        with self.assertRaises(FinalVerificationIntegrityError):
            make_request(evidence=(evidence,), policy_findings=(finding,))

    def test_memory_record_must_match_plan_project(self):
        with TemporaryDirectory() as directory:
            store = ProjectMemoryStore(Path(directory) / "memory.sqlite3")
            record = store.record(
                tenant_id="tenant:local",
                project_id="project:other",
                kind=ProjectMemoryKind.TEST_RESULT,
                title="Wrong project",
                content={"result": "pass"},
                origin=ProjectMemoryOrigin(
                    source_type=ProjectMemorySourceType.TEST_RUN,
                    source_id="test-run:other-001",
                    actor_id="system:test-runner",
                    captured_at=T5,
                ),
                retention_class=ProjectMemoryRetentionClass.PROJECT,
            )
        request = make_request(include_memory=False, include_link=False)
        with self.assertRaises(FinalVerificationIntegrityError):
            FinalVerificationRequest.capture(
                verification_id="final-verification:wrong-memory",
                policy=request.policy,
                plan=request.plan,
                journal=request.journal,
                output_validations=request.output_validations,
                payload_verifications=request.payload_verifications,
                supervision_decisions=request.supervision_decisions,
                memory_records=(record,),
                verifier_id="ELMAN_VERIFIER",
                requested_at=T9,
            )

    def test_decision_link_must_match_memory_revision(self):
        request = make_request(include_link=False)
        memory = request.memory_records[0]
        link = FinalDecisionOutcomeLink(
            link_id="decision-link:wrong-revision",
            memory_id=memory.memory_id,
            memory_revision_hash="0" * 64,
            expected_result_hash="1" * 64,
            observed_result_hash="1" * 64,
            evidence_ids=(OUTPUT_EVIDENCE_ID,),
            linked_at=T8,
        )
        with self.assertRaises(FinalVerificationIntegrityError):
            FinalVerificationRequest.capture(
                verification_id="final-verification:wrong-link",
                policy=request.policy,
                plan=request.plan,
                journal=request.journal,
                output_validations=request.output_validations,
                payload_verifications=request.payload_verifications,
                decision_links=(link,),
                supervision_decisions=request.supervision_decisions,
                memory_records=request.memory_records,
                verifier_id="ELMAN_VERIFIER",
                requested_at=T9,
            )

    def test_supervision_must_match_plan_identity(self):
        request = make_request(include_supervision=False)
        other_plan = ExecutionPlan(
            plan_id="plan:other-final-001",
            project_id=PROJECT_ID,
            objective=request.plan.objective,
            created_by="ELMAN_NEXUS",
            steps=request.plan.steps,
            status=PlanStatus.COMPLETED,
            approval_reference="approval:other-001",
        )
        decision = make_supervision(other_plan, request.journal)
        with self.assertRaises(FinalVerificationIntegrityError):
            FinalVerificationRequest.capture(
                verification_id="final-verification:wrong-supervision",
                policy=request.policy,
                plan=request.plan,
                journal=request.journal,
                output_validations=request.output_validations,
                payload_verifications=request.payload_verifications,
                decision_links=request.decision_links,
                supervision_decisions=(decision,),
                memory_records=request.memory_records,
                verifier_id="ELMAN_VERIFIER",
                requested_at=T9,
            )

    def test_request_content_tampering_is_rejected(self):
        data = make_request().to_dict()
        data["requested_at"] = "2026-08-08T01:00:00.000000Z"
        with self.assertRaises(FinalVerificationIntegrityError):
            FinalVerificationRequest.from_dict(data)


class SignerAndReportTests(unittest.TestCase):
    def test_short_or_non_bytes_signing_key_is_rejected(self):
        for secret in (b"short", "k" * 32):
            with self.subTest(secret_type=type(secret).__name__):
                with self.assertRaises(FinalReportSignatureError):
                    FinalReportSigner(key_id="key:bad-001", secret=secret)  # type: ignore[arg-type]

    def test_signer_representation_redacts_secret(self):
        value = signer()
        self.assertNotIn("kkkk", repr(value))
        self.assertIn("key:final-verification-001", repr(value))

    def test_verified_report_round_trip_and_signature(self):
        value = signer()
        report = FinalVerifier(make_request(), value).verify()
        restored = FinalVerificationReport.from_json(report.to_json())
        self.assertEqual(restored, report)
        restored.verify_signature(value)
        self.assertEqual(report.status, FinalVerificationStatus.VERIFIED)

    def test_report_rejects_wrong_signer(self):
        report = FinalVerifier(make_request(), signer()).verify()
        with self.assertRaises(FinalReportSignatureError):
            report.verify_signature(signer(b"z"))

    def test_report_detects_tampered_content(self):
        report = FinalVerifier(make_request(), signer()).verify()
        data = report.to_dict()
        data["verified_at"] = "2026-08-08T01:00:00.000000Z"
        with self.assertRaises(FinalVerificationIntegrityError):
            FinalVerificationReport.from_dict(data)

    def test_report_requires_signature_material(self):
        data = FinalVerifier(make_request(), signer()).verify().to_dict()
        del data["signature"]
        with self.assertRaises(FinalReportSignatureError):
            FinalVerificationReport.from_dict(data)

    def test_same_snapshot_produces_same_signed_report(self):
        request = make_request()
        value = signer()
        first = FinalVerifier(request, value).verify()
        second = FinalVerifier(request, value).verify()
        self.assertEqual(first.to_json(), second.to_json())


class FinalGateTests(unittest.TestCase):
    def test_valid_snapshot_passes_all_nine_gates(self):
        report = FinalVerifier(make_request(), signer()).verify()
        self.assertEqual(len(report.gates), 9)
        self.assertTrue(all(item.passed for item in report.gates))

    def test_incomplete_plan_is_rejected(self):
        report = FinalVerifier(
            make_request(plan_status=PlanStatus.RUNNING), signer()
        ).verify()
        result = gate(report, FinalVerificationGate.PLAN_COMPLETION)
        self.assertFalse(result.passed)
        self.assertIn("plan.not-completed", result.issue_codes)

    def test_missing_terminal_journal_event_is_rejected(self):
        report = FinalVerifier(
            make_request(terminal_journal=False), signer()
        ).verify()
        result = gate(report, FinalVerificationGate.JOURNAL_INTEGRITY)
        self.assertFalse(result.passed)
        self.assertIn("journal.terminal-completion-missing", result.issue_codes)

    def test_missing_output_is_rejected(self):
        report = FinalVerifier(
            make_request(include_output=False, include_payload=False), signer()
        ).verify()
        result = gate(report, FinalVerificationGate.OUTPUT_VALIDATION)
        self.assertFalse(result.passed)
        self.assertIn("output.missing", result.issue_codes)

    def test_rejected_output_is_rejected_again_by_final_gate(self):
        report = FinalVerifier(
            make_request(
                output_status=AgentOutputValidationStatus.REJECTED,
                include_payload=False,
                include_memory=False,
                include_link=False,
            ),
            signer(),
        ).verify()
        result = gate(report, FinalVerificationGate.OUTPUT_VALIDATION)
        self.assertFalse(result.passed)
        self.assertIn("output.rejected", result.issue_codes)

    def test_missing_payload_verification_is_rejected(self):
        report = FinalVerifier(
            make_request(include_payload=False), signer()
        ).verify()
        result = gate(report, FinalVerificationGate.ARTIFACT_INTEGRITY)
        self.assertFalse(result.passed)
        self.assertIn("artifact.verification-missing", result.issue_codes)

    def test_failed_evidence_is_rejected_even_with_automatic_evidence(self):
        report = FinalVerifier(
            make_request(
                evidence=(
                    make_external_evidence(status=FinalEvidenceStatus.FAILED),
                )
            ),
            signer(),
        ).verify()
        result = gate(report, FinalVerificationGate.EVIDENCE_COMPLETENESS)
        self.assertFalse(result.passed)
        self.assertIn("evidence.failed", result.issue_codes)

    def test_unresolved_policy_finding_is_rejected(self):
        finding = FinalPolicyFinding(
            finding_id="finding:unresolved-001",
            rule_id="release.policy",
            summary="A mandatory policy check failed.",
            resolved=False,
            detected_at=T6,
        )
        report = FinalVerifier(
            make_request(policy_findings=(finding,)), signer()
        ).verify()
        result = gate(report, FinalVerificationGate.POLICY_COMPLIANCE)
        self.assertFalse(result.passed)
        self.assertIn("policy.unresolved-violation", result.issue_codes)

    def test_resolved_policy_finding_with_verified_evidence_passes(self):
        evidence = make_external_evidence()
        finding = FinalPolicyFinding(
            finding_id="finding:resolved-001",
            rule_id="release.policy",
            summary="The policy failure was corrected and retested.",
            resolved=True,
            resolution_evidence_id=evidence.evidence_id,
            detected_at=T6,
        )
        report = FinalVerifier(
            make_request(evidence=(evidence,), policy_findings=(finding,)),
            signer(),
        ).verify()
        self.assertTrue(gate(report, FinalVerificationGate.POLICY_COMPLIANCE).passed)

    def test_unresolved_execution_error_is_rejected(self):
        error = FinalExecutionErrorRecord(
            error_id="error:unresolved-001",
            code="build.failure",
            summary="The build error has no verified resolution.",
            resolved=False,
            detected_at=T6,
            step_id=STEP_ID,
        )
        report = FinalVerifier(
            make_request(execution_errors=(error,)), signer()
        ).verify()
        result = gate(report, FinalVerificationGate.ERROR_RESOLUTION)
        self.assertFalse(result.passed)
        self.assertIn("error.unresolved", result.issue_codes)

    def test_active_memory_decision_without_link_is_rejected(self):
        report = FinalVerifier(
            make_request(include_link=False), signer()
        ).verify()
        result = gate(report, FinalVerificationGate.DECISION_COHERENCE)
        self.assertFalse(result.passed)
        self.assertIn("decision.link-missing", result.issue_codes)

    def test_mismatched_decision_outcome_is_rejected(self):
        request = make_request(include_link=False)
        memory = request.memory_records[0]
        observed = make_external_evidence(
            evidence_id="evidence:external:observed-mismatch",
        )
        link = FinalDecisionOutcomeLink(
            link_id="decision-link:mismatch-001",
            memory_id=memory.memory_id,
            memory_revision_hash=memory.revision_hash,
            expected_result_hash=request.output_validations[0].result_hash,
            observed_result_hash=observed.source_hash,
            evidence_ids=(observed.evidence_id,),
            linked_at=T8,
        )
        mismatched = FinalVerificationRequest.capture(
            verification_id="final-verification:mismatch",
            policy=request.policy,
            plan=request.plan,
            journal=request.journal,
            output_validations=request.output_validations,
            payload_verifications=request.payload_verifications,
            evidence=(observed,),
            decision_links=(link,),
            supervision_decisions=request.supervision_decisions,
            memory_records=request.memory_records,
            verifier_id="ELMAN_VERIFIER",
            requested_at=T9,
        )
        report = FinalVerifier(mismatched, signer()).verify()
        result = gate(report, FinalVerificationGate.DECISION_COHERENCE)
        self.assertFalse(result.passed)
        self.assertIn("decision.outcome-mismatch", result.issue_codes)

    def test_missing_current_supervision_is_rejected(self):
        report = FinalVerifier(
            make_request(include_supervision=False), signer()
        ).verify()
        result = gate(report, FinalVerificationGate.SUPERVISION_CLEARANCE)
        self.assertFalse(result.passed)
        self.assertIn("supervision.current-decision-missing", result.issue_codes)

    def test_pause_supervision_denies_final_clearance(self):
        report = FinalVerifier(
            make_request(supervision_action=MetacognitiveDecisionAction.PAUSE),
            signer(),
        ).verify()
        result = gate(report, FinalVerificationGate.SUPERVISION_CLEARANCE)
        self.assertFalse(result.passed)
        self.assertIn("supervision.clearance-denied", result.issue_codes)

    def test_rejected_report_remains_signed_and_auditable(self):
        value = signer()
        report = FinalVerifier(
            make_request(include_supervision=False), value
        ).verify()
        self.assertEqual(report.status, FinalVerificationStatus.REJECTED)
        report.verify_signature(value)


if __name__ == "__main__":
    unittest.main()
