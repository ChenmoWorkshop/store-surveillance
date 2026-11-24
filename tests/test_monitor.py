import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from monitor import platforms, storage, worker, xhs_product
from monitor.models import Reading


class MonitorTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        db_patch = patch.object(storage, "DB_FILE", Path(self.directory.name) / "test.db")
        db_patch.start()
        self.addCleanup(db_patch.stop)
        storage.init()
        browser = patch.object(xhs_product, "read_product", side_effect=RuntimeError("测试默认关闭实时浏览器"))
        browser.start()
        self.addCleanup(browser.stop)
        self.slot = datetime(2026, 11, 22, 10, tzinfo=storage.CST)

    def target(self, platform, external_id):
        with storage.database() as db:
            identifier = db.execute(
                """INSERT INTO targets(platform,kind,external_id,url,title,active_since)
                   VALUES (?,'product',?,'https://example.test/','Sample',?)""",
                (platform, external_id, storage.stamp(self.slot - timedelta(days=2))),
            ).lastrowid
            return dict(db.execute("SELECT * FROM targets WHERE id=?", (identifier,)).fetchone())

    def add(self, target, offset, **reading):
        slot = self.slot + timedelta(hours=offset)
        with storage.database() as db:
            storage.record(db, target, Reading(**reading), slot + timedelta(minutes=5), "scheduled", slot)

    def test_links_are_classified_and_notes_are_rejected(self):
        a = platforms.resolve("https://www.goofish.com/item?categoryId=0&id=1066323490724")
        b = platforms.resolve("https://www.xiaohongshu.com/goods-detail/664c48fb12abc123")
        self.assertEqual((a.platform, a.external_id), ("xianyu", "1066323490724"))
        self.assertEqual((b.platform, b.kind), ("xiaohongshu", "product"))
        with self.assertRaises(ValueError):
            platforms.resolve("https://www.xiaohongshu.com/discovery/item/664c48fb12abc123")
        with self.assertRaises(ValueError):
            platforms.resolve("https://127.0.0.1/item?id=12345")


    def test_xianyu_share_text_and_html_short_link(self):
        raw = r"https\://m.tb.cn/h.sample?tk=test" + chr(92)
        link, = platforms.links(raw)
        self.assertEqual(link, "https://m.tb.cn/h.sample?tk=test")
        page = b"""<script>
        var url = 'https://h5.m.goofish.com/item?itemId=12345&id=12345&tracking=test';
        </script>"""
        with patch.object(platforms, "open_once", return_value=io.BytesIO(page)):
            target = platforms.resolve(link)
        self.assertEqual(target.url, "https://www.goofish.com/item?id=12345")
        other = page.replace(b"h5.m.goofish.com", b"example.test")
        with patch.object(platforms, "open_once", return_value=io.BytesIO(other)):
            with self.assertRaises(ValueError):
                platforms.resolve(link)

    def test_import_keeps_resolved_identity_and_labels_share_title(self):
        from monitor.app import import_links
        from monitor.models import ImportLinks

        fish = "https://www.goofish.com/item?id=12345"
        book = "https://www.xiaohongshu.com/goods-detail/abcdef1234567890"
        text = f"【闲鱼】{fish} abc 「快来捡漏【示例商品甲】」\n【小红书】示例商品乙 😆 abc123 😆 {book}"
        rows = import_links(ImportLinks(text=text))["items"]
        self.assertEqual([(r["platform"], r["external_id"], r["title"], r["title_source"]) for r in rows], [
            ("xianyu", "12345", "示例商品甲", "share"),
            ("xiaohongshu", "abcdef1234567890", "示例商品乙", "share"),
        ])
        self.assertEqual(rows[0]["resolved_url"], fish)
        repeated, = import_links(ImportLinks(text=fish))["items"]
        self.assertEqual((repeated["status"], repeated["title"], repeated["title_source"]),
                         ("existing", "示例商品甲", "saved"))
        wrong, = import_links(ImportLinks(text=f"【小红书】错误标题 😆 abc123 😆 {fish.replace('12345','12346')}"))["items"]
        self.assertEqual((wrong["platform"], wrong["title_source"]), ("xianyu", "unavailable"))

    def test_visible_xianyu_text_records_active_price_and_want_count(self):
        from monitor.app import add_visible_reading, target_series
        from monitor.models import VisibleReading, xianyu_visible_reading

        target = self.target("xianyu", "12345")
        body = VisibleReading(text="活动价 ￥2.00 原价 ¥6 包邮 78 人想要 为你推荐 ¥1 999 人想要")
        result = add_visible_reading(target["id"], body)
        self.assertEqual((result["price_fen"], result["want_count"]), (200, 78))
        reading, = target_series(target["id"], 10)["items"]
        self.assertEqual((reading["price_fen"], reading["want_count"], reading["source"]), (200, 78, "manual"))
        self.assertEqual(xianyu_visible_reading("¥ 0 .50 0 人想要").want_count, 0)
        with self.assertRaises(ValueError):
            xianyu_visible_reading("¥2 78人想要 ¥8 90人想要")

    def test_xianyu_detail_parses_want_count_price_and_state(self):
        from monitor import xianyu_product

        payload = {"ret": ["SUCCESS::调用成功"], "data": {"itemDO": {
            "itemId": "123456",
            "wantCnt": 82,
            "soldPrice": "2",
            "promotionPriceDO": {"promotionName": "活动价", "promotionPriceMin": "2.00"},
            "title": "示例闲鱼商品",
            "itemStatusStr": "在线",
            "imageInfos": [{"url": "http://img.alicdn.com/bao/uploaded/a.jpg?x=1"}],
        }}}
        result = xianyu_product.parse_detail(payload, "123456")
        self.assertEqual((result["want_count"], result["price_fen"]), (82, 200))
        self.assertEqual((result["listing_state"], result["price_label"]), ("active", "活动价"))
        self.assertEqual(result["cover_url"], "https://img.alicdn.com/bao/uploaded/a.jpg?x=1")
        Reading.model_validate(result)
        with self.assertRaises(ValueError):
            xianyu_product.parse_detail(payload, "999999")

    def test_shop_deltas_follow_active_listing_count(self):
        with storage.database() as db:
            target_id = db.execute(
                """INSERT INTO targets(platform,kind,external_id,url,title,active_since)
                   VALUES('xiaohongshu','shop','a' * 24,'https://www.xiaohongshu.com/shop/x',
                          '店铺',?)""",
                (storage.stamp(self.slot - timedelta(days=2)),),
            ).lastrowid
            target = dict(db.execute("SELECT * FROM targets WHERE id=?", (target_id,)).fetchone())
        self.add(target, 0, shop_catalog={"active_count": 24, "items": [], "has_more": False})
        self.add(target, 1, shop_catalog={"active_count": 27, "items": [], "has_more": False})
        with storage.database() as db:
            latest = storage.series(db, target["id"], 10)[0]
            self.assertEqual((latest["hourly_delta"], latest["daily_delta"]), (3, 3))

    def test_precise_delta_and_alert_deduplication(self):
        target = self.target("xiaohongshu", "sample")
        self.add(target, -11, exact_sales=990)
        self.add(target, 0, exact_sales=1000)
        self.add(target, 1, exact_sales=1006, price_fen=2900)
        self.add(target, 2, raw_sales="1000+")
        self.add(target, 3, exact_sales=1030)
        self.add(target, 5, exact_sales=1100)
        with storage.database() as db:
            rows = storage.series(db, target["id"], 10)
            # 缺采的整点不放弃，回溯到最近一条有效读数计算；跨 11 小时超出上限仍留空。
            self.assertEqual([r["hourly_delta"] for r in rows], [70, 24, None, 6, None, None])
            self.assertEqual(rows[3]["daily_delta"], 6)
            self.assertEqual(db.execute("SELECT count(*) FROM events WHERE notify=1").fetchone()[0], 1)

    def test_counter_reset_restarts_daily_baseline(self):
        target = self.target("xiaohongshu", "sample")
        self.add(target, -11, exact_sales=1000)
        self.add(target, 0, exact_sales=1010)
        self.add(target, 1, exact_sales=990)
        self.add(target, 2, exact_sales=1020)
        with storage.database() as db:
            rows = storage.series(db, target["id"], 10)
            self.assertEqual(rows[1]["quality"], "counter_reset")
            # 回退样本本身即当日新基准，今日增量从 0 起算而不是留空。
            self.assertEqual(rows[1]["daily_delta"], 0)
            # 回退时读到的新值成为当日新基准，后续整点不再整天空白
            self.assertEqual((rows[0]["hourly_delta"], rows[0]["daily_delta"]), (30, 30))
            self.assertEqual(rows[0]["daily_baseline_at"], rows[1]["observed_at"])

    def test_recompute_replays_deltas_for_existing_snapshots(self):
        target = self.target("xiaohongshu", "sample")
        self.add(target, 0, exact_sales=1000)
        self.add(target, 1, exact_sales=1010)
        self.add(target, 2, exact_sales=1016)
        with storage.database() as db:
            expected = [(row["hourly_delta"], row["daily_delta"]) for row in storage.series(db, target["id"], 10)]
            db.execute("UPDATE snapshots SET hourly_delta=NULL,daily_delta=NULL,daily_baseline_at=NULL")
            self.assertEqual(storage.recompute_deltas(db), 3)
            replayed = [(row["hourly_delta"], row["daily_delta"]) for row in storage.series(db, target["id"], 10)]
            self.assertEqual(replayed, expected)

    def test_idle_state_transition_is_not_sales(self):
        target = self.target("xianyu", "12345")
        with storage.database() as db:
            db.execute("UPDATE rules SET settings=? WHERE id=1", ('{"state_alerts":true,"price_drop_fen":100}',))
        self.add(target, 0, price_fen=5900, listing_state="active")
        self.add(target, 1, price_fen=4900, listing_state="sold")
        self.add(target, 2, price_fen=4900, listing_state="sold")
        with storage.database() as db:
            rows = storage.series(db, target["id"], 10)
            self.assertTrue(all(row["hourly_delta"] is None for row in rows))
            self.assertEqual(db.execute("SELECT count(*) FROM events WHERE notify=1").fetchone()[0], 1)

    def test_import_shows_authorized_product_metrics_without_writing_history(self):
        from monitor.app import import_links, target_series, targets
        from monitor.models import ImportLinks

        payload = {"external_id": "12345", "price_fen": 200, "want_count": 78}
        with (
            patch.object(platforms, "provider_configured", return_value=True),
            patch.object(platforms, "observe", return_value=payload) as observe,
        ):
            imported, = import_links(ImportLinks(text="https://www.goofish.com/item?id=12345"))["items"]
        observe.assert_called_once()
        self.assertEqual((imported["source_preview"]["price_fen"], imported["source_preview"]["want_count"]),
                         (200, 78))
        self.assertIsNone(imported["reading"])
        saved, = targets("xianyu", "product", "", "", 0, 10)["items"]
        self.assertEqual((saved["price_fen"], saved["want_count"]), (200, 78))
        self.assertIsNone(saved["observed_at"])
        self.assertEqual(target_series(imported["id"], 10)["items"], [])

    def test_xhs_import_reads_displayed_price_and_sales_without_history(self):
        from monitor.app import import_links, target_series, targets
        from monitor.models import ImportLinks

        result = xhs_product.parse_product_metric("¥\n5.9", "已售42", "券后价")
        link = "https://www.xiaohongshu.com/goods-detail/" + "b" * 24
        with (
            patch.object(platforms, "provider_configured", return_value=False),
            patch.object(xhs_product, "read_product", return_value=result),
        ):
            item, = import_links(ImportLinks(text=link))["items"]
        self.assertEqual(item["source_preview"]["price_label"], "券后价")
        row, = targets("xiaohongshu", "product", "", "", 0, 10)["items"]
        self.assertEqual((row["price_fen"], row["raw_sales"], row["price_label"]), (590, "已售42", "券后价"))
        self.assertEqual(target_series(item["id"], 10)["items"], [])

    def test_import_does_not_start_unscheduled_sampling(self):
        from monitor.app import import_links, target_series
        from monitor.models import ImportLinks

        instant = self.slot.replace(hour=4, minute=55)
        with (
            patch.object(worker, "now", return_value=instant),
            patch.object(worker, "target_configured", return_value=True),
            patch.object(worker, "observe") as observe,
            patch.object(worker, "channels", return_value={}),
        ):
            imported, = import_links(ImportLinks(text="https://www.goofish.com/item?id=12345"))["items"]
            worker.cycle()
            observe.assert_not_called()
        self.assertIsNone(imported["reading"])
        self.assertEqual(target_series(imported["id"], 10)["items"], [])

    def test_public_xhs_shop_retries_and_saves_partial_catalog(self):
        from monitor.app import shop_products

        seller_id = "a" * 24
        url = "https://www.xiaohongshu.com/shop/" + seller_id
        card = {
            "defaultGoodsComponentVO": {
                "baseInfo": {"sellerId": seller_id, "itemId": "b" * 24,
                             "title": "真题", "itemSaleNum": 0},
                "marketingInfo": {"priceInfo": {"expectedPrice": {"priceStr": "¥0.98"}}},
                "tagStrategyMap": {"afterPrice": [{"content": "已售4686"}]},
            }
        }
        document = {"xhs_shop_home_data": [{
            "componentName": "DefaultMixCardComponent",
            "componentData": {"total": 24, "hasMore": True, "componentVOList": [card]},
        }]}
        page = ("window.__SETUP_SERVER_STATE__=" + json.dumps(document)).encode()
        with storage.database() as db:
            target_id = db.execute(
                """INSERT INTO targets(platform,kind,external_id,url,title,active_since)
                   VALUES('xiaohongshu','shop',?,?,'店铺',?)""",
                (seller_id, url, storage.stamp(self.slot)),
            ).lastrowid
        with (
            patch.object(worker, "now", return_value=self.slot + timedelta(minutes=2)),
            patch.object(platforms, "provider_configured", return_value=False),
            patch.dict("os.environ", {"MONITOR_XIAOHONGSHU_ENDPOINT": ""}),
            patch.object(platforms, "open_once", side_effect=[TimeoutError(), io.BytesIO(page)]) as fetch,
            patch.object(worker, "channels", return_value={}),
        ):
            worker.cycle()
            fetch.assert_called()
            self.assertEqual(fetch.call_count, 2)
        result = shop_products(target_id)
        self.assertEqual(result["pagination"]["total"], 1)
        self.assertEqual(result["latest"]["active_count"], 24)
        self.assertTrue(result["latest"]["has_more"])
        item, = result["products"]
        self.assertEqual((item["price_fen"], item["raw_sales"]), (98, "已售4686"))

        from monitor.app import import_links, target_series
        from monitor.models import ImportLinks

        body = ImportLinks(text=item["url"])
        imported, = import_links(body)["items"]
        repeated, = import_links(body)["items"]
        self.assertIsNone(imported["reading"])
        self.assertEqual(imported["catalog_preview"]["observed_at"], result["latest"]["observed_at"])
        self.assertEqual(imported["catalog_preview"]["raw_sales"], "已售4686")
        from monitor.app import targets
        preview, = targets("xiaohongshu", "product", "", "", 0, 10)["items"]
        self.assertEqual((preview["raw_sales"], preview["price_fen"]), ("已售4686", 98))
        self.assertIsNone(preview["observed_at"])
        self.assertEqual(preview["catalog_observed_at"], result["latest"]["observed_at"])
        self.assertEqual(repeated["status"], "existing")
        self.assertEqual(target_series(imported["id"], 10)["items"], [])


    def test_shop_products_paginate_a_large_catalog_in_collection_order(self):
        from monitor.app import shop_products

        items = [{"id": str(10000 + index), "title": f"商品 {index}"} for index in range(503)]
        with storage.database() as db:
            target_id = db.execute(
                """INSERT INTO targets(platform,kind,external_id,url,title,active_since)
                   VALUES('xianyu','shop','56789','https://www.goofish.com/personal?userId=56789','店铺',?)""",
                (storage.stamp(self.slot),),
            ).lastrowid
            target = dict(db.execute("SELECT * FROM targets WHERE id=?", (target_id,)).fetchone())
            storage.record(db, target, Reading(shop_catalog={"active_count": 520, "items": items}),
                           self.slot, "manual")

        first = shop_products(target_id)
        second = shop_products(target_id, page=2)
        last = shop_products(target_id, page=99, page_size=100)
        self.assertEqual(len(first["products"]), 20)
        self.assertEqual(second["products"][0]["id"], "10020")
        self.assertEqual(last["pagination"], {
            "page": 6, "page_size": 100, "page_count": 6, "total": 503, "offset": 500,
        })
        self.assertEqual([item["id"] for item in last["products"]], ["10500", "10501", "10502"])
        self.assertEqual(last["latest"]["active_count"], 520)
        self.assertNotIn("items", last["latest"])

    def test_delete_stops_sampling_and_reimport_restores_history(self):
        from fastapi import HTTPException
        from monitor.app import delete_target, import_links, target_series, targets
        from monitor.models import ImportLinks

        target = self.target("xianyu", "12345")
        target_id = target["id"]
        self.add(target, 0, price_fen=4900, want_count=12)
        with storage.database() as db:
            slot = storage.stamp(self.slot + timedelta(hours=1))
            db.execute("INSERT INTO jobs(target_id,slot,next_at) VALUES(?,?,?)", (target_id, slot, slot))

        self.assertTrue(delete_target(target_id)["deleted"])
        self.assertTrue(delete_target(target_id)["deleted"])
        self.assertEqual(targets("xianyu", "", "", "", 0, 20)["total"], 0)
        with self.assertRaises(HTTPException) as error:
            target_series(target_id, 10)
        self.assertEqual(error.exception.status_code, 404)
        with storage.database() as db:
            self.assertEqual(db.execute("SELECT state FROM jobs").fetchone()[0], "canceled")

        result, = import_links(ImportLinks(text="https://www.goofish.com/item?id=12345"))["items"]
        self.assertEqual((result["status"], result["id"]), ("restored", target_id))
        history = target_series(target_id, 10)
        self.assertTrue(history["target"]["enabled"])
        self.assertEqual(history["items"][0]["want_count"], 12)

    def test_daily_details_use_last_observation_adjacent_days_and_exact_values(self):
        from monitor.app import target_series

        target = self.target("xiaohongshu", "sample")
        self.add(target, 0, exact_sales=100)
        self.add(target, 1, exact_sales=102)
        self.add(target, 24, exact_sales=109)
        self.add(target, 72, exact_sales=117)
        self.add(target, 96, raw_sales="100+")
        latest, gap, adjacent, first = target_series(target["id"], 100)["days"]
        self.assertEqual(first["value"], 102)
        self.assertEqual(adjacent["delta"], 7)
        self.assertIsNone(gap["delta"])
        self.assertEqual(latest["display"], "100+")
        self.assertIsNone(latest["value"])
        self.assertIsNone(latest["delta"])

    def test_detail_saves_full_title_store_and_category_separately(self):
        from monitor.app import target_series, update_target
        from monitor.models import TargetPatch

        target = self.target("xiaohongshu", "sample")
        long_title = "完整商品标题" * 35
        with storage.database() as db:
            storage.record(db, target, Reading(raw_sales="已售12", official_title=long_title,
                                               shop_name="示例资料的店"),
                           self.slot, "refresh")
        update_target(target["id"], TargetPatch(category="自定义笔试"))
        detail = target_series(target["id"], 100)
        self.assertEqual(detail["target"]["official_title"], long_title)
        self.assertEqual(detail["target"]["title"], "Sample")
        self.assertEqual(detail["target"]["shop_name"], "示例资料的店")
        self.assertEqual(detail["target"]["category"], "自定义笔试")
        self.assertIn("自定义笔试", detail["categories"])
        self.assertEqual(detail["observation"]["last_fetched_at"], storage.stamp(self.slot))
        self.assertIsNone(detail["three_hour_delta"])

    def test_three_hour_change_requires_three_adjacent_valid_deltas(self):
        hours = [{"slot": storage.stamp(self.slot - timedelta(hours=index)), "state": "success",
                  "hourly_delta": value} for index, value in enumerate([2, 0, 3])]
        self.assertEqual(storage.three_hour_delta(hours), 5)
        self.assertIsNone(storage.three_hour_delta(hours[:2]))
        self.assertIsNone(storage.three_hour_delta([*hours[:2], hours[2] | {"state": "missed"}]))
        gap = hours[2] | {"slot": storage.stamp(self.slot - timedelta(hours=3))}
        self.assertIsNone(storage.three_hour_delta([*hours[:2], gap]))

    def test_midnight_resets_daily_delta_but_keeps_adjacent_hour_delta(self):
        target = self.target("xiaohongshu", "sample")
        self.add(target, -11, exact_sales=200)
        self.add(target, -10, exact_sales=203)
        self.add(target, -9, exact_sales=209)
        with storage.database() as db:
            newest, midnight, _ = storage.series(db, target["id"], 10)
        self.assertEqual((midnight["hourly_delta"], midnight["daily_delta"]), (3, 0))
        self.assertEqual((newest["hourly_delta"], newest["daily_delta"]), (6, 6))
        self.assertEqual(newest["daily_baseline_at"], midnight["observed_at"])

    def test_want_count_allows_negative_changes_without_sales_alerts(self):
        target = self.target("xianyu", "12345")
        self.add(target, 0, want_count=78)
        self.add(target, 1, want_count=90)
        self.add(target, 2, want_count=77)
        with storage.database() as db:
            latest, previous, _ = storage.series(db, target["id"], 10)
            self.assertEqual((previous["hourly_delta"], previous["daily_delta"]), (12, 12))
            self.assertEqual((latest["hourly_delta"], latest["daily_delta"]), (-13, -1))
            self.assertEqual(db.execute("SELECT count(*) FROM events WHERE notify=1").fetchone()[0], 0)

    def test_unconfigured_source_records_slot_without_fake_observation(self):
        from monitor.app import target_series

        # 平台组合还没有数据源时，只占位不伪造读数（按需模拟，不依赖真实缺口）。
        with storage.database() as db:
            target_id = db.execute(
                """INSERT INTO targets(platform,kind,external_id,url,title,active_since)
                   VALUES('xianyu','shop','12345','https://www.goofish.com/personal?userId=12345',
                          '店铺',?)""",
                (storage.stamp(self.slot - timedelta(days=2)),),
            ).lastrowid
        with (
            patch.object(worker, "now", return_value=self.slot.replace(hour=0, minute=3)),
            patch.object(worker, "target_configured", return_value=False),
            patch.object(worker, "observe") as observe,
            patch.object(worker, "channels", return_value={}),
        ):
            worker.cycle()
            observe.assert_not_called()
        history = target_series(target_id, 10)
        hour, = history["hours"]
        self.assertEqual(hour["state"], "unavailable")
        self.assertIsNone(hour["observed_at"])
        self.assertIsNone(history["observation"]["first_observed_at"])
        self.assertEqual(history["items"], [])

    def test_scheduler_does_not_duplicate_the_same_slot(self):
        target = self.target("xianyu", "12345")
        instant = self.slot + timedelta(minutes=2)
        with (
            patch.object(worker, "now", return_value=instant),
            patch.object(worker, "target_configured", return_value=True),
            patch.object(worker, "observe", return_value={"price_fen": 4900}),
            patch.object(worker, "channels", return_value={}),
        ):
            worker.cycle()
            worker.cycle()
        with storage.database() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM jobs WHERE state='success'").fetchone()[0], 1)
            self.assertEqual(storage.series(db, target["id"], 10)[0]["price_fen"], 4900)


    def test_database_backup_round_trip(self):
        target = self.target("xiaohongshu", "abc123abc123abc123abc123")
        self.add(target, 0, exact_sales=100, price_fen=990)
        dump = storage.dump_database()
        self.assertEqual(dump["counts"]["targets"], 1)
        self.assertEqual(dump["counts"]["snapshots"], 1)
        self.assertEqual(dump["counts"]["rules"], 1)
        with self.assertRaises(ValueError):
            storage.restore_database(dump)
        storage.restore_database(dump, replace=True)
        self.assertEqual(storage.dump_database()["tables"], dump["tables"])
        with self.assertRaises(ValueError):
            storage.restore_database({"version": 99, "tables": {}})
        with self.assertRaises(ValueError):
            storage.restore_database({"version": 1, "tables": {"targets": []}})
        with tempfile.TemporaryDirectory() as path:
            with patch.object(storage, "DB_FILE", Path(path) / "fresh.db"):
                storage.init()
                storage.restore_database(dump)
                self.assertEqual(storage.dump_database()["tables"], dump["tables"])


if __name__ == "__main__":
    unittest.main()
