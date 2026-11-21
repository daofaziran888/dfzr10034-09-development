"""变更交付场景测试：交割窗口 2026-11-03 ~ 2026-11-04 的迟到删列补差。

时间线：
- 11-04 00:00 shipment 新增 carrier_code（来源序号 2），报文先到达并进入窗口原件；
- 原件已发出、外部对账按旧文件启动比对后，迟到收到 11-03 末删除 carrier_name
  的报文（来源序号 1）；
- 同一窗口重复导出只认第一次签发的文件编号，迟到变更次日走补差文件；
- 接收方按交付序号逐个核销两个字段动作，原件与补差合并不把 carrier_code 算两次；
- 差异报告以交付序号取前后值，补交的变更编号可在差异报告中核对。
"""
import app.store as store_mod
from app.services.change_capture import ChangeCaptureService
from app.services.change_delivery import ChangeDeliveryService

ASSET = "shipment"
WINDOW = "2026-11-03~2026-11-04"


def _fresh():
    """重置存储并返回（变更捕获, 变更交付）服务。"""
    store_mod.store.reset()
    return ChangeCaptureService(), ChangeDeliveryService()


def _capture_add_carrier_code(capture: ChangeCaptureService) -> dict:
    """四日零点新增 carrier_code 的报文先到达（来源序号 2）。"""
    return capture.capture_field_change(
        ASSET, "carrier_code", "新增列",
        before="", after="carrier_code",
        source="ddl_message", effective_time="2026-11-04 00:00:00", source_seq=2,
    )


def _capture_late_drop_carrier_name(capture: ChangeCaptureService) -> dict:
    """迟到报文：三日末删除旧 carrier_name（来源序号 1，先于新增但后到）。"""
    return capture.capture_field_change(
        ASSET, "carrier_name", "删除列",
        before="carrier_name", after="",
        source="ddl_message", effective_time="2026-11-03 23:59:59", source_seq=1,
    )


def _issue_original(capture, delivery):
    """首日：捕获新增列并签发窗口原件。"""
    add = _capture_add_carrier_code(capture)
    first = delivery.export_delivery(ASSET, WINDOW)
    return add, first


def _issue_supplement(capture, delivery):
    """次日：原件发出后收到迟到删列报文，重复导出同一窗口补送删列记录。"""
    add, first = _issue_original(capture, delivery)
    drop = _capture_late_drop_carrier_name(capture)
    again = delivery.export_delivery(ASSET, WINDOW)
    return add, first, drop, again


# ══════════════════════════════════════════════════════════════════
# 首次签发：字段结构变更导出为带来源序号的交付文件
# ══════════════════════════════════════════════════════════════════

class TestFirstDelivery:

    def test_first_export_issues_original_file(self):
        capture, delivery = _fresh()
        _, first = _issue_original(capture, delivery)
        assert first["ok"] is True
        assert first["reissued"] is False
        assert first["supplement"] is None
        original = first["delivery"]
        assert original["交付类型"] == "原件"
        assert original["目标资产"] == ASSET
        assert original["交割窗口"] == WINDOW
        assert original["文件编号"] == "DELV-20261103-0001"
        assert original["status"] == "已签发"

    def test_delivery_entries_carry_source_seq_and_delivery_seq(self):
        capture, delivery = _fresh()
        add, first = _issue_original(capture, delivery)
        entries = first["delivery"]["entries"]
        assert len(entries) == 1
        entry = entries[0]
        assert entry["字段名"] == "carrier_code"
        assert entry["字段动作"] == "新增列"
        assert entry["来源序号"] == 2
        assert entry["交付序号"] == 1
        assert entry["变更编号"] == add["change"]["记录编号"]
        assert entry["订正标志"] == "原始"
        assert entry["核销状态"] == "待核销"
        # 文件头标注有效范围
        assert first["delivery"]["起始序号"] == "1"
        assert first["delivery"]["截止序号"] == "1"
        assert first["delivery"]["变更条数"] == "1"

    def test_change_record_marked_delivered(self):
        capture, delivery = _fresh()
        add, _ = _issue_original(capture, delivery)
        change = add["change"]
        assert change["交付状态"] == "已交付"
        assert change["交付文件"] == "DELV-20261103-0001"
        assert change["交付序号"] == 1


# ══════════════════════════════════════════════════════════════════
# 幂等导出：同一窗口重复导出只认第一次签发的文件编号
# ══════════════════════════════════════════════════════════════════

class TestIdempotentExport:

    def test_repeat_export_keeps_first_file_number(self):
        capture, delivery = _fresh()
        _, first = _issue_original(capture, delivery)
        again = delivery.export_delivery(ASSET, WINDOW)
        assert again["reissued"] is True
        assert again["文件编号"] == first["文件编号"]
        assert again["supplement"] is None
        files = [d for d in store_mod.store.rows("change_delivery")
                 if d.get("目标资产") == ASSET and d.get("交割窗口") == WINDOW]
        assert len(files) == 1

    def test_no_new_changes_no_supplement(self):
        capture, delivery = _fresh()
        _, first, _, again = _issue_supplement(capture, delivery)
        third = delivery.export_delivery(ASSET, WINDOW)
        assert third["文件编号"] == first["文件编号"]
        assert third["supplement"] is None
        files = [d for d in store_mod.store.rows("change_delivery")
                 if d.get("目标资产") == ASSET and d.get("交割窗口") == WINDOW]
        assert len(files) == 2  # 原件 + 补差


# ══════════════════════════════════════════════════════════════════
# 迟到删列：补差文件保留与原件的关系
# ══════════════════════════════════════════════════════════════════

class TestLateDropSupplement:

    def test_late_drop_goes_to_supplement_linked_to_original(self):
        capture, delivery = _fresh()
        _, first, drop, again = _issue_supplement(capture, delivery)
        # 重复导出仍只认第一次签发的文件编号
        assert again["文件编号"] == first["文件编号"]
        supplement = again["supplement"]
        assert supplement is not None
        assert supplement["交付类型"] == "补差"
        assert supplement["原件编号"] == first["文件编号"]
        assert supplement["文件编号"] != first["文件编号"]
        # 补差只含迟到的删列记录
        entries = supplement["entries"]
        assert len(entries) == 1
        assert entries[0]["字段名"] == "carrier_name"
        assert entries[0]["字段动作"] == "删除列"
        assert entries[0]["来源序号"] == 1
        assert entries[0]["交付序号"] == 2
        assert entries[0]["变更编号"] == drop["change"]["记录编号"]
        assert entries[0]["订正标志"] == "补交"

    def test_original_file_untouched_after_supplement(self):
        capture, delivery = _fresh()
        _, first, _, again = _issue_supplement(capture, delivery)
        original = again["delivery"]
        assert original["文件编号"] == first["文件编号"]
        assert original["变更条数"] == "1"
        assert original["起始序号"] == "1"
        assert original["截止序号"] == "1"
        assert len(original["entries"]) == 1
        assert original["entries"][0]["字段名"] == "carrier_code"

    def test_original_valid_range_remains_reconcilable(self):
        """外部对账按旧文件启动比对后，仍能拿到可继续核销的有效范围。"""
        capture, delivery = _fresh()
        _, first, _, again = _issue_supplement(capture, delivery)
        valid = delivery.valid_range(first["文件编号"])
        assert valid["起始序号"] == 1
        assert valid["截止序号"] == 1
        assert valid["可核销序号"] == [1]
        assert valid["已核销序号"] == []
        supp_valid = delivery.valid_range(again["supplement"]["文件编号"])
        assert supp_valid["可核销序号"] == [2]


# ══════════════════════════════════════════════════════════════════
# 接收方核销：逐个核销两个字段动作，合并不重复计算
# ══════════════════════════════════════════════════════════════════

class TestReconciliation:

    def test_receiver_reconciles_each_field_action(self):
        capture, delivery = _fresh()
        _, first, _, again = _issue_supplement(capture, delivery)
        supp_number = again["supplement"]["文件编号"]
        # 逐个核销：先核销原件里的新增列，再核销补差里的删除列
        r1 = delivery.reconcile_entry(first["文件编号"], 1)
        assert r1["ok"] is True
        assert r1["entry"]["核销状态"] == "已核销"
        assert r1["delivery"]["status"] == "已核销"
        r2 = delivery.reconcile_entry(supp_number, 2)
        assert r2["ok"] is True
        assert r2["entry"]["核销状态"] == "已核销"
        assert r2["delivery"]["status"] == "已核销"
        assert delivery.valid_range(first["文件编号"])["全部核销"] is True
        assert delivery.valid_range(supp_number)["全部核销"] is True

    def test_duplicate_reconcile_rejected(self):
        capture, delivery = _fresh()
        _, first, _, _ = _issue_supplement(capture, delivery)
        assert delivery.reconcile_entry(first["文件编号"], 1)["ok"] is True
        again = delivery.reconcile_entry(first["文件编号"], 1)
        assert again["ok"] is False
        assert "重复核销" in again["message"]

    def test_reconcile_out_of_valid_range_rejected(self):
        capture, delivery = _fresh()
        _, first, _, _ = _issue_supplement(capture, delivery)
        result = delivery.reconcile_entry(first["文件编号"], 99)
        assert result["ok"] is False
        assert "有效范围" in result["message"]

    def test_merged_view_counts_carrier_column_once(self):
        """原件与补差合在一起，新增承运商列不会被算两次。"""
        capture, delivery = _fresh()
        add, _, drop, _ = _issue_supplement(capture, delivery)
        merged = delivery.merged_entries(ASSET, WINDOW)
        assert len(merged) == 2
        carrier_code = [e for e in merged if e["字段名"] == "carrier_code"]
        assert len(carrier_code) == 1
        assert carrier_code[0]["字段动作"] == "新增列"
        carrier_name = [e for e in merged if e["字段名"] == "carrier_name"]
        assert len(carrier_name) == 1
        assert carrier_name[0]["字段动作"] == "删除列"
        # 合并台账按交付序号排列，变更编号与两条捕获记录一一对应
        assert [e["交付序号"] for e in merged] == [1, 2]
        assert {e["变更编号"] for e in merged} == {
            add["change"]["记录编号"], drop["change"]["记录编号"],
        }


# ══════════════════════════════════════════════════════════════════
# 差异报告联动：以交付序号取前后值，核对补交的变更编号
# ══════════════════════════════════════════════════════════════════

class TestDiffReportLinkage:

    def test_diff_report_keyed_by_delivery_seq(self):
        capture, delivery = _fresh()
        add, _, drop, _ = _issue_supplement(capture, delivery)
        # 交付序号 1：新增 carrier_code（原始）
        diff_add = delivery.diff_by_delivery_seq(ASSET, 1)
        assert diff_add is not None
        assert diff_add["变更编号"] == add["change"]["记录编号"]
        assert diff_add["源值"] == ""
        assert diff_add["目标值"] == "carrier_code"
        assert diff_add["订正标志"] == "原始"
        # 交付序号 2：删除 carrier_name（补交）
        diff_drop = delivery.diff_by_delivery_seq(ASSET, 2)
        assert diff_drop is not None
        assert diff_drop["变更编号"] == drop["change"]["记录编号"]
        assert diff_drop["源值"] == "carrier_name"
        assert diff_drop["目标值"] == ""
        assert diff_drop["订正标志"] == "补交"

    def test_supplement_diff_traceable_to_original_file(self):
        """接收方打开关联的差异报告，能核对补交的变更编号并查到原件。"""
        capture, delivery = _fresh()
        _, first, drop, again = _issue_supplement(capture, delivery)
        diff_drop = delivery.diff_by_delivery_seq(ASSET, 2)
        assert diff_drop["原件编号"] == first["文件编号"]
        assert diff_drop["交付文件"] == again["supplement"]["文件编号"]
        # 核对补交的变更编号：差异报告里的编号就是迟到删列的捕获记录
        assert diff_drop["变更编号"] == drop["change"]["记录编号"]

    def test_export_document_matches_reconciliation_detail(self):
        """导出文件与核对明细必须对得上。"""
        capture, delivery = _fresh()
        _, first, _, again = _issue_supplement(capture, delivery)
        for number in (first["文件编号"], again["supplement"]["文件编号"]):
            document = delivery.export_document(number)
            detail = delivery.reconciliation_detail(number)
            assert document is not None
            assert document["entries"] == detail
            assert document["变更条数"] == str(len(detail))
            assert [e["交付序号"] for e in document["entries"]] == \
                   [e["交付序号"] for e in detail]

    def test_correction_lineage_finds_original_change(self):
        """接收方能查到订正对应的原始变更。"""
        capture, delivery = _fresh()
        add, first, drop, again = _issue_supplement(capture, delivery)
        lineage = delivery.correction_lineage(again["supplement"]["文件编号"])
        assert lineage["交付类型"] == "补差"
        assert lineage["原件编号"] == first["文件编号"]
        assert lineage["原始变更"] == [add["change"]["记录编号"]]
        assert lineage["补交变更"] == [drop["change"]["记录编号"]]
        # 反向：按变更编号定位补交记录及其原件
        located = delivery.find_change_delivery(drop["change"]["记录编号"])
        assert located["文件编号"] == again["supplement"]["文件编号"]
        assert located["交付类型"] == "补差"
        assert located["原件编号"] == first["文件编号"]


# ══════════════════════════════════════════════════════════════════
# 按天交付：窗口按天各发一份交付文件
# ══════════════════════════════════════════════════════════════════

class TestDailyDeliveryWindows:

    def test_daily_windows_issue_two_files(self):
        """按天发出两份交付文件：三日、四日窗口各自签发独立原件。"""
        capture, delivery = _fresh()
        capture.capture_field_change(
            "parcel", "tracking_no", "新增列",
            before="", after="tracking_no",
            effective_time="2026-11-03 10:00:00", source_seq=1,
        )
        capture.capture_field_change(
            "parcel", "weight", "新增列",
            before="", after="weight",
            effective_time="2026-11-04 10:00:00", source_seq=2,
        )
        day3 = delivery.export_delivery("parcel", "2026-11-03")
        day4 = delivery.export_delivery("parcel", "2026-11-04")
        assert day3["delivery"]["交付类型"] == "原件"
        assert day4["delivery"]["交付类型"] == "原件"
        assert day3["文件编号"] == "DELV-20261103-0001"
        assert day4["文件编号"] == "DELV-20261104-0001"
        assert day3["delivery"]["entries"][0]["字段名"] == "tracking_no"
        assert day4["delivery"]["entries"][0]["字段名"] == "weight"
        # 交付序号按资产连续递增
        assert day3["delivery"]["entries"][0]["交付序号"] == 1
        assert day4["delivery"]["entries"][0]["交付序号"] == 2

    def test_empty_window_issues_heartbeat_file(self):
        """窗口内无变更也按天签发空文件，条数为零。"""
        _, delivery = _fresh()
        result = delivery.export_delivery("parcel", "2026-11-03")
        assert result["ok"] is True
        assert result["delivery"]["变更条数"] == "0"
        assert result["delivery"]["entries"] == []
