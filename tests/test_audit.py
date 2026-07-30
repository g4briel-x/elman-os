import asyncio
import json
import unittest
from dataclasses import replace

from elman_os.audit import (
    AuditEventType,
    AuditIntegrityError,
    AuditSigner,
    AuditTrail,
    AuditedAIExecutor,
    AuthenticationMethod,
    ExecutionAuditContext,
    ExecutionAuthorizationError,
    ExecutionPrincipal,
    ExecutionPurpose,
    InMemoryAuditSink,
)
from elman_os.execution import ResilientAIExecutor, RetryPolicy
from elman_os.provider import (
    DeterministicModelProvider,
    MessageRole,
    ModelMessage,
    ModelRequest,
)

KEY = b"audit-test-key-with-at-least-32-bytes"


def principal(
    *,
    method: AuthenticationMethod = AuthenticationMethod.JWT,
    roles: frozenset[str] = frozenset({"ai.execute"}),
) -> ExecutionPrincipal:
    return ExecutionPrincipal(
        subject_id="user-secret-123",
        tenant_id="tenant-secret-456",
        authentication_method=method,
        roles=roles,
    )


def context(**kwargs: object) -> ExecutionAuditContext:
    return ExecutionAuditContext(
        principal=kwargs.get("principal", principal()),
        purpose=kwargs.get("purpose", ExecutionPurpose.AGENT_TASK),
        correlation_id="corr-001",
    )


def request(content: str = "Prompt strictement confidentiel") -> ModelRequest:
    return ModelRequest(
        request_id="request-secret-789",
        model="deterministic-v1",
        messages=(ModelMessage(MessageRole.USER, content),),
        max_output_tokens=20,
    )


def audited(
    sink: InMemoryAuditSink | None = None,
) -> tuple[AuditedAIExecutor, InMemoryAuditSink]:
    actual_sink = sink or InMemoryAuditSink()
    trail = AuditTrail(AuditSigner(KEY), actual_sink)
    executor = AuditedAIExecutor(
        ResilientAIExecutor(
            DeterministicModelProvider(responses=("Réponse confidentielle",)),
            RetryPolicy(max_attempts=1),
        ),
        trail,
    )
    return executor, actual_sink


class AuditAuthenticationTests(unittest.IsolatedAsyncioTestCase):
    async def test_authenticated_authorized_execution_succeeds(self) -> None:
        executor, sink = audited()
        result = await executor.generate(request(), context())
        self.assertEqual(result.response.content, "Réponse confidentielle")
        self.assertEqual(
            [item.event.event_type for item in sink.events],
            [AuditEventType.STARTED, AuditEventType.SUCCEEDED],
        )

    async def test_anonymous_execution_is_denied_before_provider_call(self) -> None:
        executor, sink = audited()
        anonymous = principal(method=AuthenticationMethod.ANONYMOUS)
        with self.assertRaises(ExecutionAuthorizationError):
            await executor.generate(request(), context(principal=anonymous))
        self.assertEqual(executor.executor.ledger.provider_calls, 0)
        self.assertEqual(sink.events[0].event.event_type, AuditEventType.DENIED)

    async def test_missing_execute_role_is_denied(self) -> None:
        executor, _ = audited()
        with self.assertRaises(ExecutionAuthorizationError):
            await executor.generate(
                request(),
                context(principal=principal(roles=frozenset({"ai.read"}))),
            )

    async def test_invalid_correlation_identifier_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ExecutionAuditContext(
                principal=principal(),
                purpose=ExecutionPurpose.AGENT_TASK,
                correlation_id="unsafe\nvalue",
            )

    async def test_empty_identity_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ExecutionPrincipal(
                subject_id="",
                tenant_id="tenant",
                authentication_method=AuthenticationMethod.JWT,
                roles=frozenset({"ai.execute"}),
            )


class AuditPrivacyTests(unittest.IsolatedAsyncioTestCase):
    async def test_events_exclude_prompt_response_and_raw_identities(self) -> None:
        executor, sink = audited()
        await executor.generate(request(), context())
        serialized = json.dumps(
            [item.to_safe_dict() for item in sink.events],
            ensure_ascii=False,
        )
        for forbidden in (
            "Prompt strictement confidentiel",
            "Réponse confidentielle",
            "user-secret-123",
            "tenant-secret-456",
            "request-secret-789",
        ):
            self.assertNotIn(forbidden, serialized)

    async def test_fingerprints_are_stable_but_separated_by_namespace(self) -> None:
        signer = AuditSigner(KEY)
        self.assertEqual(
            signer.fingerprint("principal", "same"),
            signer.fingerprint("principal", "same"),
        )
        self.assertNotEqual(
            signer.fingerprint("principal", "same"),
            signer.fingerprint("tenant", "same"),
        )

    async def test_signer_never_reveals_key(self) -> None:
        signer = AuditSigner(KEY)
        self.assertNotIn(KEY.decode(), repr(signer))
        self.assertIn("redacted", str(signer))

    async def test_short_signing_key_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            AuditSigner(b"short")

    async def test_unsafe_model_name_is_redacted(self) -> None:
        executor, sink = audited()
        model_request = replace(request(), model="secret model\nvalue")
        with self.assertRaises(Exception):
            await executor.generate(model_request, context())
        serialized = json.dumps([item.to_safe_dict() for item in sink.events])
        self.assertNotIn("secret model", serialized)


class AuditIntegrityTests(unittest.IsolatedAsyncioTestCase):
    async def test_event_signatures_and_chain_verify(self) -> None:
        executor, sink = audited()
        await executor.generate(request(), context())
        self.assertTrue(executor.trail.verify_chain(sink.events))
        self.assertEqual(
            sink.events[1].previous_signature,
            sink.events[0].signature,
        )

    async def test_tampered_event_fails_verification(self) -> None:
        executor, sink = audited()
        await executor.generate(request(), context())
        original = sink.events[1]
        tampered = replace(
            original,
            event=replace(original.event, output_tokens=999),
        )
        self.assertFalse(
            executor.trail.verify_chain([sink.events[0], tampered])
        )

    async def test_removed_event_breaks_chain(self) -> None:
        executor, sink = audited()
        await executor.generate(request(), context())
        self.assertFalse(executor.trail.verify_chain([sink.events[1]]))

    async def test_sink_failure_is_fail_closed_before_provider_call(self) -> None:
        class FailingSink:
            async def append(self, event: object) -> None:
                raise OSError("disk detail must not escape")

        trail = AuditTrail(AuditSigner(KEY), FailingSink())
        executor = AuditedAIExecutor(
            ResilientAIExecutor(DeterministicModelProvider()),
            trail,
        )
        with self.assertRaises(AuditIntegrityError) as raised:
            await executor.generate(request(), context())
        self.assertNotIn("disk detail", str(raised.exception))
        self.assertEqual(executor.executor.ledger.provider_calls, 0)


class AuditOutcomeTests(unittest.IsolatedAsyncioTestCase):
    async def test_failure_produces_normalized_failed_event(self) -> None:
        executor, sink = audited()
        bad_request = replace(request(), model="unknown-model")
        with self.assertRaises(Exception):
            await executor.generate(bad_request, context())
        self.assertEqual(sink.events[-1].event.event_type, AuditEventType.FAILED)
        self.assertEqual(sink.events[-1].event.error_code, "model_not_found")

    async def test_success_records_usage_without_response_content(self) -> None:
        executor, sink = audited()
        await executor.generate(request(), context())
        event = sink.events[-1].event
        self.assertGreater(event.input_tokens, 0)
        self.assertGreater(event.output_tokens, 0)
        self.assertFalse(hasattr(event, "content"))

    async def test_cancellation_is_audited_and_propagated(self) -> None:
        class CancelProvider(DeterministicModelProvider):
            async def generate(self, model_request: ModelRequest):
                raise asyncio.CancelledError()

        sink = InMemoryAuditSink()
        executor = AuditedAIExecutor(
            ResilientAIExecutor(CancelProvider()),
            AuditTrail(AuditSigner(KEY), sink),
        )
        with self.assertRaises(asyncio.CancelledError):
            await executor.generate(request(), context())
        self.assertEqual(sink.events[-1].event.event_type, AuditEventType.CANCELLED)

    async def test_audit_schema_contains_no_free_form_metadata_field(self) -> None:
        executor, sink = audited()
        await executor.generate(request(), context())
        keys = sink.events[-1].event.to_safe_dict()
        self.assertNotIn("metadata", keys)
        self.assertNotIn("provider_request_id", keys)


if __name__ == "__main__":
    unittest.main()
