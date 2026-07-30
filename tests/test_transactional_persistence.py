import asyncio
import tempfile
import unittest
from pathlib import Path

from elman_os.transactional_persistence import (
    PersistenceConflictError,
    SQLitePersistence,
    TransactionClosedError,
)


class SQLitePersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "elman.sqlite3"
        self.store = SQLitePersistence(self.db)

    async def asyncTearDown(self) -> None:
        await self.store.close()
        self.temp.cleanup()

    async def test_create_and_read_record(self) -> None:
        async with self.store.transaction("tenant-a", "agents") as transaction:
            created = await transaction.put(
                "agent-1", {"status": "ready"}, expected_version=0
            )
            self.assertEqual(created.version, 1)
            self.assertEqual(created.value["status"], "ready")
            loaded = await transaction.get("agent-1")
            self.assertEqual(loaded, created)

    async def test_data_survives_backend_reopen(self) -> None:
        async with self.store.transaction("tenant-a") as transaction:
            await transaction.put("key-1", {"value": 7}, expected_version=0)
        await self.store.close()
        reopened = SQLitePersistence(self.db)
        try:
            async with reopened.transaction("tenant-a") as transaction:
                loaded = await transaction.get("key-1")
                self.assertEqual(loaded.value["value"], 7)
        finally:
            await reopened.close()

    async def test_tenant_isolation_for_reads(self) -> None:
        async with self.store.transaction("tenant-a") as transaction:
            await transaction.put("shared-key", {"owner": "a"}, expected_version=0)
        async with self.store.transaction("tenant-b") as transaction:
            self.assertIsNone(await transaction.get("shared-key"))

    async def test_tenant_isolation_for_lists(self) -> None:
        async with self.store.transaction("tenant-a") as transaction:
            await transaction.put("job-1", {"owner": "a"}, expected_version=0)
        async with self.store.transaction("tenant-b") as transaction:
            await transaction.put("job-2", {"owner": "b"}, expected_version=0)
            records = await transaction.list(prefix="job-")
            self.assertEqual([item.key for item in records], ["job-2"])

    async def test_namespace_isolation(self) -> None:
        async with self.store.transaction("tenant-a", "one") as transaction:
            await transaction.put("key", {"scope": 1}, expected_version=0)
        async with self.store.transaction("tenant-a", "two") as transaction:
            self.assertIsNone(await transaction.get("key"))

    async def test_create_only_rejects_existing_record(self) -> None:
        async with self.store.transaction("tenant-a") as transaction:
            await transaction.put("key", {"version": 1}, expected_version=0)
            with self.assertRaises(PersistenceConflictError):
                await transaction.put("key", {"version": 2}, expected_version=0)

    async def test_compare_and_swap_updates_version(self) -> None:
        async with self.store.transaction("tenant-a") as transaction:
            first = await transaction.put("key", {"value": 1}, expected_version=0)
            second = await transaction.put(
                "key", {"value": 2}, expected_version=first.version
            )
            self.assertEqual(second.version, 2)

    async def test_stale_update_is_rejected(self) -> None:
        async with self.store.transaction("tenant-a") as transaction:
            await transaction.put("key", {"value": 1}, expected_version=0)
            await transaction.put("key", {"value": 2}, expected_version=1)
            with self.assertRaises(PersistenceConflictError):
                await transaction.put("key", {"value": 3}, expected_version=1)

    async def test_unconditional_upsert_increments_version(self) -> None:
        async with self.store.transaction("tenant-a") as transaction:
            first = await transaction.put("key", {"value": 1}, expected_version=None)
            second = await transaction.put("key", {"value": 2}, expected_version=None)
            self.assertEqual((first.version, second.version), (1, 2))

    async def test_delete_honors_expected_version(self) -> None:
        async with self.store.transaction("tenant-a") as transaction:
            await transaction.put("key", {"value": 1}, expected_version=0)
            with self.assertRaises(PersistenceConflictError):
                await transaction.delete("key", expected_version=2)
            self.assertTrue(await transaction.delete("key", expected_version=1))
            self.assertIsNone(await transaction.get("key"))

    async def test_transaction_rolls_back_on_error(self) -> None:
        with self.assertRaises(RuntimeError):
            async with self.store.transaction("tenant-a") as transaction:
                await transaction.put("key", {"value": 1}, expected_version=0)
                raise RuntimeError("abort")
        async with self.store.transaction("tenant-a") as transaction:
            self.assertIsNone(await transaction.get("key"))

    async def test_transaction_commits_multiple_operations_atomically(self) -> None:
        async with self.store.transaction("tenant-a") as transaction:
            await transaction.put("one", {"value": 1}, expected_version=0)
            await transaction.put("two", {"value": 2}, expected_version=0)
        async with self.store.transaction("tenant-a") as transaction:
            self.assertEqual(len(await transaction.list()), 2)

    async def test_prefix_listing_is_ordered_and_paginated(self) -> None:
        async with self.store.transaction("tenant-a") as transaction:
            for key in ("job-c", "other", "job-a", "job-b"):
                await transaction.put(key, {"key": key}, expected_version=0)
            records = await transaction.list(prefix="job-", limit=2, offset=1)
            self.assertEqual([item.key for item in records], ["job-b", "job-c"])

    async def test_value_must_be_strict_json(self) -> None:
        async with self.store.transaction("tenant-a") as transaction:
            with self.assertRaises(ValueError):
                await transaction.put(
                    "key", {"invalid": float("nan")}, expected_version=0
                )

    async def test_value_is_immutable_after_read(self) -> None:
        async with self.store.transaction("tenant-a") as transaction:
            record = await transaction.put(
                "key", {"value": 1}, expected_version=0
            )
            with self.assertRaises(TypeError):
                record.value["value"] = 2

    async def test_unsafe_scope_and_key_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.store.transaction("tenant\nunsafe")
        async with self.store.transaction("tenant-a") as transaction:
            with self.assertRaises(ValueError):
                await transaction.get("../unsafe key")

    async def test_closed_backend_rejects_new_transaction(self) -> None:
        await self.store.close()
        with self.assertRaises(Exception):
            self.store.transaction("tenant-a")

    async def test_transaction_object_rejects_use_after_exit(self) -> None:
        context = self.store.transaction("tenant-a")
        transaction = await context.__aenter__()
        await context.__aexit__(None, None, None)
        with self.assertRaises(TransactionClosedError):
            await transaction.get("key")

    async def test_concurrent_transactions_are_serialized_locally(self) -> None:
        order: list[str] = []

        async def first() -> None:
            async with self.store.transaction("tenant-a"):
                order.append("first-start")
                await asyncio.sleep(0.02)
                order.append("first-end")

        async def second() -> None:
            await asyncio.sleep(0)
            async with self.store.transaction("tenant-a"):
                order.append("second")

        await asyncio.gather(first(), second())
        self.assertEqual(order, ["first-start", "first-end", "second"])

    async def test_two_backend_instances_detect_stale_version(self) -> None:
        other = SQLitePersistence(self.db)
        try:
            async with self.store.transaction("tenant-a") as transaction:
                await transaction.put("key", {"value": 1}, expected_version=0)
            async with self.store.transaction("tenant-a") as transaction:
                current = await transaction.get("key")
            async with other.transaction("tenant-a") as transaction:
                await transaction.put(
                    "key",
                    {"value": 2},
                    expected_version=current.version,
                )
            async with self.store.transaction("tenant-a") as transaction:
                with self.assertRaises(PersistenceConflictError):
                    await transaction.put(
                        "key",
                        {"value": 3},
                        expected_version=current.version,
                    )
        finally:
            await other.close()


if __name__ == "__main__":
    unittest.main()
