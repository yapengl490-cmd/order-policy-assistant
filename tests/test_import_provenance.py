"""导入→授权查询→来源的一条完整离线链路；与真实PG验收分开。"""
import copy
from datetime import datetime, timezone
from unittest.mock import patch
from sqlalchemy import Column, ForeignKey, Table, Text, func, select, event
import test_import_data as base_tests
from models import Order, Refund
from import_data import import_payload, ImportValidationError, ImportConflictError, ImportExecutionError
from merchant_service import check_shop_order
from refund_service import check_order


def _restore_known_utc(target, context):
    if context.session.bind.dialect.name == "sqlite":
        field = "paid_at" if isinstance(target, Order) else "refunded_at"
        value = getattr(target, field)
        if value.tzinfo is None:
            setattr(target, field, value.replace(tzinfo=timezone.utc))


class ImportProvenanceTests(base_tests.ImportDataTests):
    def setUp(self):
        super().setUp()
        # SQLite不保存时区；仅测试的规范化UTC样本补回时区，正式PG逻辑不改。
        for cls in (Order, Refund):
            event.listen(cls, "load", _restore_known_utc)
            self.addCleanup(event.remove, cls, "load", _restore_known_utc)
        metadata = self.batches.metadata
        self.shops = Table("shops", metadata, Column("shop_id", Text, primary_key=True),
                           Column("name", Text, nullable=False))
        self.mappings = Table("order_shops", metadata,
            Column("order_id", Text, ForeignKey("public.orders.order_id"), primary_key=True),
            Column("shop_id", Text, ForeignKey("public.shops.shop_id"), nullable=False))
        self.sources = Table("import_row_sources", metadata,
            Column("record_kind", Text, primary_key=True), Column("record_id", Text, primary_key=True),
            Column("batch_sha256", Text, ForeignKey("public.import_batches.batch_sha256"), primary_key=True))
        metadata.create_all(self.engine)
        self.order_id = self.payload["orders"][0]["order_id"]
        self.buyer_id = self.payload["buyers"][0]["buyer_id"]
        self.cutoff = datetime.fromisoformat("2026-12-01T00:00:00Z")

    def extended(self):
        result = copy.deepcopy(self.payload)
        result["shops"] = [{"shop_id": "S_IMPORT", "name": "导入演示店铺"}]
        result["order_shops"] = [{"order_id": self.order_id, "shop_id": "S_IMPORT"}]
        return result

    def all_snapshot(self):
        with self.engine.connect() as conn:
            return tuple(tuple(tuple(row) for row in conn.execute(select(table).order_by(*table.primary_key.columns)))
                         for table in (self.buyers,self.orders,self.refunds,self.batches,self.shops,self.mappings,self.sources))

    def test_full_import_scoped_query_and_provenance(self):
        result = import_payload(self.engine, self.extended())
        self.assertEqual(result["provenance"], "recorded")
        got = check_shop_order(self.engine,self.order_id,"S_IMPORT",self.cutoff)
        self.assertEqual((got["paid_amount_fen"],got["refund_amount_fen"],got["net_amount_fen"]),(10000,3000,7000))
        refs = got["provenance"]
        self.assertEqual(refs["status"],"recorded")
        self.assertEqual(len(refs["records"]),4)  # order + mapping + two refunds
        self.assertEqual(refs["batches"][0]["batch_sha256"],result["batch_sha256"])
        self.assertEqual(refs["batches"][0]["source"],self.payload["metadata"]["source"])
        denied = check_shop_order(self.engine,self.order_id,"S_OTHER",self.cutoff)
        self.assertEqual(denied["status"],"not_found");self.assertNotIn("provenance",denied)
        buyer = check_order(self.engine,self.order_id,self.buyer_id,self.cutoff)
        self.assertEqual(buyer["provenance"]["status"],"recorded")
        self.assertNotIn("order_shops", {r["kind"] for r in buyer["provenance"]["records"]})

    def test_legacy_input_does_not_grant_shop_access(self):
        import_payload(self.engine,self.payload)
        got = check_shop_order(self.engine,self.order_id,"S_IMPORT",self.cutoff)
        self.assertEqual(got["status"],"not_found")

    def test_repeat_no_duplicate_lineage_and_second_source_kept(self):
        payload=self.extended();import_payload(self.engine,payload)
        before=self.all_snapshot();import_payload(self.engine,payload);self.assertEqual(self.all_snapshot(),before)
        payload["metadata"]["version"]="second-declaration"
        result=import_payload(self.engine,payload)
        self.assertTrue(all(n==0 for n in result["inserted_counts"].values()))
        got=check_shop_order(self.engine,self.order_id,"S_IMPORT",self.cutoff)
        self.assertEqual(len(got["provenance"]["batches"]),2)
        self.assertTrue(all(len(r["batch_sha256"])==2 for r in got["provenance"]["records"]))

    def test_cannot_reassign_existing_order_to_another_shop(self):
        payload=self.extended();import_payload(self.engine,payload);before=self.all_snapshot()
        payload["metadata"]["version"]="conflict"
        payload["shops"]=[{"shop_id":"OTHER","name":"另一店铺"}]
        payload["order_shops"][0]["shop_id"]="OTHER"
        with self.assertRaises(ImportConflictError):import_payload(self.engine,payload)
        self.assertEqual(self.all_snapshot(),before)

    def test_missing_mapping_target_and_partial_optional_group_rejected(self):
        for missing in ["order","shop","group"]:
            payload=self.extended();before=self.all_snapshot()
            if missing=="order":payload["order_shops"][0]["order_id"]="MISSING"
            elif missing=="shop":payload["order_shops"][0]["shop_id"]="MISSING"
            else:del payload["shops"]
            with self.assertRaises(ImportValidationError):import_payload(self.engine,payload)
            self.assertEqual(self.all_snapshot(),before)

    def test_legacy_rows_unknown_without_guessing_source(self):
        import_payload(self.engine,self.extended())
        with self.engine.begin() as conn:conn.execute(self.sources.delete())
        got=check_shop_order(self.engine,self.order_id,"S_IMPORT",self.cutoff)
        self.assertEqual(got["provenance"]["status"],"unknown")
        self.assertEqual(got["provenance"]["batches"],[])
        self.assertTrue(all(r["status"]=="unknown" for r in got["provenance"]["records"]))

    def test_metadata_failure_is_not_empty_success(self):
        import_payload(self.engine,self.extended())
        with patch("provenance.provenance_table_available", side_effect=__import__('sqlalchemy').exc.OperationalError("hidden",{},Exception())):
            got=check_order(self.engine,self.order_id,self.buyer_id,self.cutoff)
        self.assertEqual(got["status"],"ok")
        self.assertEqual(got["provenance"]["status"],"unavailable")
        self.assertEqual(got["provenance"]["reason"],"provenance_read_failed")

    def test_bounded_sources_mark_partial(self):
        import_payload(self.engine,self.extended())
        with patch("provenance.MAX_RECORDS",2):
            got=check_shop_order(self.engine,self.order_id,"S_IMPORT",self.cutoff)
        self.assertEqual(got["provenance"]["status"],"partial")
        self.assertTrue(got["provenance"]["truncated"])
        self.assertEqual(got["provenance"]["returned_records"],2)

    def test_late_lineage_failure_rolls_back_business_and_batch(self):
        with self.engine.begin() as conn:
            conn.exec_driver_sql("CREATE TRIGGER public.reject_source BEFORE INSERT ON import_row_sources BEGIN SELECT RAISE(ABORT,'controlled'); END")
        before=self.all_snapshot()
        with self.assertRaises(ImportExecutionError):import_payload(self.engine,self.extended())
        self.assertEqual(self.all_snapshot(),before)

    def test_without_migration_old_input_still_works_explicit_unavailable(self):
        with self.engine.begin() as conn:self.sources.drop(conn)
        result=import_payload(self.engine,self.payload)
        self.assertEqual(result["provenance"],"unavailable")
        got=check_order(self.engine,self.order_id,self.buyer_id,self.cutoff)
        self.assertEqual(got["status"],"ok")
        self.assertEqual(got["provenance"]["reason"],"provenance_not_migrated")
