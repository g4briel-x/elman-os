import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from elman_os.audit import (
    AuditEvent,
    AuditEventType,
    AuditIntegrityError,
    AuditSigner,
    AuthenticationMethod,
    ExecutionPurpose,
)
from elman_os.governance import (
    IdentityQuota,
    IdentityQuotaErrorCode,
    IdentityQuotaExceededError,
)
from elman_os.persistent_governance import (
    PersistentAuditTrail,
    PersistentIdentityQuotaManager,
)
from elman_os.transactional_persistence import SQLitePersistence


KEY = b"persistent-governance-test-key-32-bytes-minimum"
IDENTITY = "a" * 64


def event(signer: AuditSigner, tenant: str, event_id: str = "event-1") -> AuditEvent:
    return AuditEvent(
        schema_version=1,
        event_id=event_id,
        occurred_at=datetime.now(timezone.utc).isoformat(),
        event_type=AuditEventType.STARTED,
        correlation_id="correlation-1",
        principal_fingerprint=signer.fingerprint("principal", "subject-1"),
        tenant_fingerprint=signer.fingerprint("tenant", tenant),
        authentication_method=AuthenticationMethod.JWT,
        purpose=ExecutionPurpose.AGENT_TASK,
        request_fingerprint=signer.fingerprint("request", "request-1"),
        provider_id="deterministic",
        model="test-model",
        attempts=0,
        input_tokens=0,
        output_tokens=0,
        elapsed_ms=0,
    )


class PersistentQuotaTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "governance.sqlite3"
        self.backend = SQLitePersistence(self.path)

    async def asyncTearDown(self) -> None:
        await self.backend.close()
        self.temp.cleanup()

    def manager(self, **kwargs):
        return PersistentIdentityQuotaManager(
            "tenant-a",
            self.backend,
            quota=kwargs.pop(
                "quota",
                IdentityQuota(max_requests=10, max_tokens=1000, max_concurrent=2),
            ),
            **kwargs,
        )

    async def test_reserve_and_settle_persist_usage(self) -> None:
        manager = self.manager()
        reservation = await manager.reserve(IDENTITY, 100)
        await manager.settle(reservation, actual_tokens=42)
        self.assertEqual((await manager.snapshot(IDENTITY)).tokens, 42)

    async def test_request_limit_is_shared_between_instances(self) -> None:
        quota = IdentityQuota(max_requests=1, max_tokens=1000, max_concurrent=2)
        first = self.manager(quota=quota)
        second = self.manager(quota=quota)
        reservation = await first.reserve(IDENTITY, 10)
        await first.settle(reservation, actual_tokens=1)
        with self.assertRaises(IdentityQuotaExceededError) as raised:
            await second.reserve(IDENTITY, 10)
        self.assertEqual(raised.exception.code, IdentityQuotaErrorCode.REQUEST_LIMIT)

    async def test_token_reservations_are_shared_between_instances(self) -> None:
        quota = IdentityQuota(max_requests=10, max_tokens=100, max_concurrent=2)
        first = self.manager(quota=quota)
        second = self.manager(quota=quota)
        await first.reserve(IDENTITY, 80)
        with self.assertRaises(IdentityQuotaExceededError) as raised:
            await second.reserve(IDENTITY, 21)
        self.assertEqual(raised.exception.code, IdentityQuotaErrorCode.TOKEN_LIMIT)

    async def test_concurrency_limit_is_shared_between_instances(self) -> None:
        quota = IdentityQuota(max_requests=10, max_tokens=1000, max_concurrent=1)
        await self.manager(quota=quota).reserve(IDENTITY, 10)
        with self.assertRaises(IdentityQuotaExceededError) as raised:
            await self.manager(quota=quota).reserve(IDENTITY, 10)
        self.assertEqual(
            raised.exception.code, IdentityQuotaErrorCode.CONCURRENCY_LIMIT
        )

    async def test_settlement_releases_active_and_reserved_tokens(self) -> None:
        manager = self.manager()
        reservation = await manager.reserve(IDENTITY, 100)
        before = await manager.snapshot(IDENTITY)
        await manager.settle(reservation, actual_tokens=25)
        after = await manager.snapshot(IDENTITY)
        self.assertEqual((before.active, before.reserved_tokens), (1, 100))
        self.assertEqual((after.active, after.reserved_tokens), (0, 0))

    async def test_duplicate_settlement_is_rejected(self) -> None:
        manager = self.manager()
        reservation = await manager.reserve(IDENTITY, 10)
        await manager.settle(reservation, actual_tokens=1)
        with self.assertRaises(ValueError):
            await manager.settle(reservation, actual_tokens=1)

    async def test_expired_reservation_is_reclaimed(self) -> None:
        now = [1000.0]
        quota = IdentityQuota(max_requests=10, max_tokens=1000, max_concurrent=1)
        manager = self.manager(
            quota=quota,
            reservation_ttl_seconds=10,
            wall_clock=lambda: now[0],
        )
        await manager.reserve(IDENTITY, 100)
        now[0] = 1011.0
        second = await manager.reserve(IDENTITY, 50)
        self.assertEqual(second.reserved_tokens, 50)
        snapshot = await manager.snapshot(IDENTITY)
        self.assertEqual((snapshot.active, snapshot.reserved_tokens), (1, 50))

    async def test_tenants_are_isolated(self) -> None:
        first = self.manager()
        second = PersistentIdentityQuotaManager("tenant-b", self.backend)
        reservation = await first.reserve(IDENTITY, 10)
        await first.settle(reservation, actual_tokens=7)
        self.assertEqual((await second.snapshot(IDENTITY)).tokens, 0)

    async def test_invalid_fingerprint_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            await self.manager().reserve("not-a-fingerprint", 10)

    async def test_invalid_estimate_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            await self.manager().reserve(IDENTITY, 0)

    async def test_usage_survives_backend_reopen(self) -> None:
        manager = self.manager()
        reservation = await manager.reserve(IDENTITY, 20)
        await manager.settle(reservation, actual_tokens=12)
        await self.backend.close()
        reopened = SQLitePersistence(self.path)
        try:
            snapshot = await PersistentIdentityQuotaManager(
                "tenant-a", reopened
            ).snapshot(IDENTITY)
            self.assertEqual(snapshot.tokens, 12)
        finally:
            await reopened.close()


class PersistentAuditTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "audit.sqlite3"
        self.backend = SQLitePersistence(self.path)
        self.signer = AuditSigner(KEY)

    async def asyncTearDown(self) -> None:
        await self.backend.close()
        self.temp.cleanup()

    def trail(self, tenant: str = "tenant-a", signer=None):
        return PersistentAuditTrail(
            signer or self.signer,
            self.backend,
            tenant,
        )

    async def test_append_and_load_signed_event(self) -> None:
        trail = self.trail()
        signed = await trail.append(event(self.signer, "tenant-a"))
        self.assertTrue(self.signer.verify(signed))
        self.assertEqual((await trail.load())[0], signed)

    async def test_two_instances_extend_one_chain(self) -> None:
        first = self.trail()
        second = self.trail()
        one = await first.append(event(self.signer, "tenant-a", "event-1"))
        two = await second.append(event(self.signer, "tenant-a", "event-2"))
        self.assertEqual(two.previous_signature, one.signature)
        self.assertTrue(await first.verify_persisted())

    async def test_chain_survives_backend_reopen(self) -> None:
        await self.trail().append(event(self.signer, "tenant-a"))
        await self.backend.close()
        reopened = SQLitePersistence(self.path)
        try:
            trail = PersistentAuditTrail(self.signer, reopened, "tenant-a")
            self.assertTrue(await trail.verify_persisted())
            self.assertEqual(len(await trail.load()), 1)
        finally:
            await reopened.close()

    async def test_wrong_tenant_event_is_rejected(self) -> None:
        with self.assertRaises(AuditIntegrityError):
            await self.trail("tenant-a").append(event(self.signer, "tenant-b"))

    async def test_wrong_signer_cannot_extend_chain(self) -> None:
        await self.trail().append(event(self.signer, "tenant-a"))
        other = AuditSigner(b"different-persistent-governance-key-minimum")
        with self.assertRaises(AuditIntegrityError):
            await self.trail(signer=other).append(event(other, "tenant-a", "event-2"))

    async def test_tenants_have_independent_chains(self) -> None:
        first = self.trail("tenant-a")
        second = self.trail("tenant-b")
        await first.append(event(self.signer, "tenant-a", "event-a"))
        await second.append(event(self.signer, "tenant-b", "event-b"))
        self.assertEqual(len(await first.load()), 1)
        self.assertEqual(len(await second.load()), 1)
        self.assertTrue(await first.verify_persisted())
        self.assertTrue(await second.verify_persisted())

    async def test_tampered_event_fails_persisted_verification(self) -> None:
        trail = self.trail()
        await trail.append(event(self.signer, "tenant-a"))
        async with self.backend.transaction("tenant-a", "audit-v1") as transaction:
            record = await transaction.get("event-00000000000000000001")
            value = dict(record.value)
            payload = dict(value["event"])
            payload["output_tokens"] = 999
            value["event"] = payload
            await transaction.put(
                record.key, value, expected_version=record.version
            )
        self.assertFalse(await trail.verify_persisted())

    async def test_empty_chain_is_valid(self) -> None:
        self.assertTrue(await self.trail().verify_persisted())
        self.assertEqual(await self.trail().load(), ())

    async def test_event_order_is_stable(self) -> None:
        trail = self.trail()
        for index in range(3):
            await trail.append(event(self.signer, "tenant-a", f"event-{index}"))
        self.assertEqual(
            [item.event.event_id for item in await trail.load()],
            ["event-0", "event-1", "event-2"],
        )

    async def test_removed_event_breaks_state_verification(self) -> None:
        trail = self.trail()
        await trail.append(event(self.signer, "tenant-a", "event-1"))
        await trail.append(event(self.signer, "tenant-a", "event-2"))
        async with self.backend.transaction("tenant-a", "audit-v1") as transaction:
            record = await transaction.get("event-00000000000000000001")
            await transaction.delete(record.key, expected_version=record.version)
        self.assertFalse(await trail.verify_persisted())

    async def test_constructor_requires_tenant(self) -> None:
        with self.assertRaises(ValueError):
            PersistentAuditTrail(self.signer, self.backend, "")


if __name__ == "__main__":
    unittest.main()
