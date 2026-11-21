"""变更交付领域服务：字段结构变更按资产打包成交付文件，供外部对账核销。

核心规则：
- 同一（资产, 交割窗口）重复导出只认第一次签发的文件编号；
- 文件签发后迟到的变更进入补差文件，补差通过原件编号保留与原件的关系；
- 差异报告以交付序号取前后值，导出文件与核对明细必须一致；
- 接收方按交付序号逐个核销，原件与补差合并时同一变更不重复计算。
"""
from __future__ import annotations

import time as _time
from datetime import datetime, timedelta
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class ChangeDeliveryService(BaseModuleService):
    MODULE = "change_delivery"
    ENTITY = "交付文件"
    OPERATION_TERM = "核对"
    LIST_FIELDS = ["文件编号", "目标资产", "交割窗口", "交付类型", "原件编号", "起始序号", "截止序号", "变更条数"]
    REQUIRED_FIELDS = ["文件编号", "目标资产", "交割窗口"]
    STATUS_ORDER = ["已签发", "已送达", "已核销", "已作废"]
    ACTION_RULES = {"确认送达": "已送达", "确认核销": "已核销", "作废文件": "已作废"}
    NEGATIVE_ACTIONS = ["作废文件"]

    # ── 导出交付 ──────────────────────────────────────────────

    def export_delivery(self, asset_code: str, window: str) -> dict[str, Any]:
        """按资产和交割窗口导出变更交付文件。

        第一次导出签发原件；同一窗口重复导出仍只认第一次签发的文件编号，
        窗口签发后迟到的变更打包成补差文件，补差保留与原件的关系。
        """
        start, end = self._parse_window(window)
        original = self._find_original(asset_code, window)
        pending_changes = [
            change for change in self._field_changes(asset_code)
            if self._in_window(change.get("生效时间", ""), start, end)
            and not change.get("交付文件")
        ]
        if original is None:
            delivery = self._issue_file(asset_code, window, "原件", "", pending_changes)
            return {
                "ok": True,
                "reissued": False,
                "文件编号": delivery["文件编号"],
                "delivery": delivery,
                "supplement": None,
                "message": (
                    f"{asset_code} 窗口 {window} 首次签发 {delivery['文件编号']}，"
                    f"交付{len(pending_changes)}条变更"
                ),
            }
        supplement = None
        if pending_changes:
            supplement = self._issue_file(
                asset_code, window, "补差", original["文件编号"], pending_changes)
        message = f"窗口 {window} 只认第一次签发的 {original['文件编号']}"
        if supplement is not None:
            message += f"，迟到变更{len(pending_changes)}条补送 {supplement['文件编号']}"
        return {
            "ok": True,
            "reissued": True,
            "文件编号": original["文件编号"],
            "delivery": original,
            "supplement": supplement,
            "message": message,
        }

    def _issue_file(self, asset_code: str, window: str, delivery_type: str,
                    original_number: str, changes: list[dict[str, Any]]) -> dict[str, Any]:
        """签发一份交付文件：分配交付序号、回写变更、标差异报告。"""
        now = _time.strftime("%Y-%m-%d %H:%M:%S")
        number = self._next_file_number(window)
        entries: list[dict[str, Any]] = []
        seq = self._next_delivery_seq(asset_code)
        for change in changes:
            entries.append({
                "交付序号": seq,
                "交付文件": number,
                "变更编号": change.get("记录编号", ""),
                "change_id": change.get("id"),
                "字段名": change.get("字段名", ""),
                "字段动作": change.get("字段动作", ""),
                "变更前值": change.get("变更前值", ""),
                "变更后值": change.get("变更后值", ""),
                "来源序号": change.get("来源序号", ""),
                "生效时间": change.get("生效时间", ""),
                "订正标志": "原始" if delivery_type == "原件" else "补交",
                "核销状态": "待核销",
            })
            change["交付状态"] = "已交付"
            change["交付文件"] = number
            change["交付序号"] = seq
            self._stamp_diff(change, number, seq, delivery_type, original_number)
            seq += 1
        delivery = {
            "id": store.next_id(self.MODULE),
            "文件编号": number,
            "目标资产": asset_code,
            "交割窗口": window,
            "交付类型": delivery_type,
            "原件编号": original_number or "-",
            "起始序号": str(entries[0]["交付序号"]) if entries else "-",
            "截止序号": str(entries[-1]["交付序号"]) if entries else "-",
            "变更条数": str(len(entries)),
            "签发时间": now,
            "status": "已签发",
            "pending": True,
            "abnormal": False,
            "created_at": now,
            "entries": entries,
        }
        store.rows(self.MODULE).append(delivery)
        store.log_change(
            self.MODULE, delivery["id"],
            "签发交付文件" if delivery_type == "原件" else "签发补差文件",
            before=window, after=number,
            source="change_delivery", scope=f"{asset_code}:{window}",
        )
        self.emit_event("delivery_issued", {
            "文件编号": number,
            "目标资产": asset_code,
            "交割窗口": window,
            "交付类型": delivery_type,
            "原件编号": original_number or "-",
            "变更条数": len(entries),
        })
        return delivery

    def _stamp_diff(self, change: dict[str, Any], file_number: str, delivery_seq: int,
                    delivery_type: str, original_number: str) -> None:
        """差异报告以交付序号取前后值，补交记录标注对应的原件编号。"""
        for row in store.rows("diff_report"):
            if row.get("change_id") != change.get("id"):
                continue
            row["交付序号"] = delivery_seq
            row["交付文件"] = file_number
            row["变更编号"] = change.get("记录编号", "")
            row["订正标志"] = "原始" if delivery_type == "原件" else "补交"
            row["原件编号"] = original_number or "-"

    # ── 接收方核销 ────────────────────────────────────────────

    def reconcile_entry(self, file_number: str, delivery_seq: int) -> dict[str, Any]:
        """接收方按交付序号逐个核销交付条目。"""
        delivery = self._find_file(file_number)
        if delivery is None:
            return {"ok": False, "message": f"交付文件 {file_number} 不存在或已归档"}
        if delivery.get("status") == "已作废":
            return {"ok": False, "message": f"交付文件 {file_number} 已作废，不能核销"}
        entry = next(
            (e for e in delivery.get("entries", []) if e.get("交付序号") == delivery_seq),
            None,
        )
        if entry is None:
            return {
                "ok": False,
                "message": f"交付序号 {delivery_seq} 不在文件 {file_number} 的有效范围内",
            }
        if entry.get("核销状态") == "已核销":
            return {
                "ok": False,
                "message": f"交付序号 {delivery_seq} 已核销，不能重复核销",
                "entry": entry,
            }
        now = _time.strftime("%Y-%m-%d %H:%M:%S")
        entry["核销状态"] = "已核销"
        entry["核销时间"] = now
        entries = delivery.get("entries", [])
        if entries and all(e.get("核销状态") == "已核销" for e in entries):
            delivery["status"] = "已核销"
            delivery["pending"] = False
            delivery["updated_at"] = now
        store.log_change(
            self.MODULE, delivery["id"], "核销交付条目",
            before="待核销", after=f"交付序号{delivery_seq}已核销",
            source="reconciliation", scope=file_number,
        )
        return {
            "ok": True,
            "message": (
                f"{file_number} 交付序号 {delivery_seq}"
                f"（{entry.get('字段动作')}{entry.get('字段名')}）已核销"
            ),
            "entry": entry,
            "delivery": delivery,
        }

    def valid_range(self, file_number: str) -> dict[str, Any] | None:
        """文件的有效范围：外部对账按旧文件启动比对后，仍可继续核销的序号范围。"""
        delivery = self._find_file(file_number)
        if delivery is None:
            return None
        entries = delivery.get("entries", [])
        done = [e["交付序号"] for e in entries if e.get("核销状态") == "已核销"]
        todo = [e["交付序号"] for e in entries if e.get("核销状态") != "已核销"]
        return {
            "文件编号": delivery["文件编号"],
            "交付类型": delivery["交付类型"],
            "起始序号": entries[0]["交付序号"] if entries else None,
            "截止序号": entries[-1]["交付序号"] if entries else None,
            "可核销序号": todo,
            "已核销序号": done,
            "全部核销": bool(entries) and not todo,
        }

    def merged_entries(self, asset_code: str, window: str) -> list[dict[str, Any]]:
        """原件与补差合并后的核销台账：按变更编号去重，同一变更不重复计算。"""
        files = [
            f for f in store.rows(self.MODULE)
            if f.get("目标资产") == asset_code
            and f.get("交割窗口") == window
            and f.get("status") != "已作废"
        ]
        files.sort(key=lambda f: (0 if f.get("交付类型") == "原件" else 1, f.get("id", 0)))
        seen: set[str] = set()
        merged: list[dict[str, Any]] = []
        for delivery in files:
            for entry in delivery.get("entries", []):
                key = str(entry.get("变更编号", ""))
                if key in seen:
                    continue
                seen.add(key)
                merged.append(entry)
        merged.sort(key=lambda e: e.get("交付序号", 0))
        return merged

    # ── 对账查询 ──────────────────────────────────────────────

    def reconciliation_detail(self, file_number: str) -> list[dict[str, Any]]:
        """文件的核对明细：与导出文件正文同源，保证两边对得上。"""
        delivery = self._find_file(file_number)
        if delivery is None:
            return []
        return list(delivery.get("entries", []))

    def export_document(self, file_number: str) -> dict[str, Any] | None:
        """交付文件正文：文件头加条目明细，与核对明细一致。"""
        delivery = self._find_file(file_number)
        if delivery is None:
            return None
        return {
            "文件编号": delivery["文件编号"],
            "目标资产": delivery["目标资产"],
            "交割窗口": delivery["交割窗口"],
            "交付类型": delivery["交付类型"],
            "原件编号": delivery["原件编号"],
            "起始序号": delivery["起始序号"],
            "截止序号": delivery["截止序号"],
            "变更条数": delivery["变更条数"],
            "签发时间": delivery.get("签发时间", ""),
            "entries": self.reconciliation_detail(file_number),
        }

    def diff_by_delivery_seq(self, asset_code: str, delivery_seq: int) -> dict[str, Any] | None:
        """差异报告以交付序号取前后值（源值/目标值）。"""
        for row in store.rows("diff_report"):
            if row.get("交付序号") == delivery_seq and row.get("比较对象") == asset_code:
                return row
        return None

    def correction_lineage(self, file_number: str) -> dict[str, Any] | None:
        """订正关系：补差文件对应的原件与原始变更，或原件名下的补差文件。"""
        delivery = self._find_file(file_number)
        if delivery is None:
            return None
        if delivery.get("交付类型") == "补差":
            original = self._find_file(delivery.get("原件编号", ""))
            return {
                "文件编号": delivery["文件编号"],
                "交付类型": "补差",
                "原件编号": delivery.get("原件编号", ""),
                "原件": original,
                "原始变更": [e.get("变更编号") for e in (original or {}).get("entries", [])],
                "补交变更": [e.get("变更编号") for e in delivery.get("entries", [])],
            }
        supplements = [
            f["文件编号"] for f in store.rows(self.MODULE)
            if f.get("原件编号") == delivery.get("文件编号")
        ]
        return {
            "文件编号": delivery["文件编号"],
            "交付类型": "原件",
            "补差文件": supplements,
            "原始变更": [e.get("变更编号") for e in delivery.get("entries", [])],
        }

    def find_change_delivery(self, change_number: str) -> dict[str, Any] | None:
        """按变更编号定位交付它的文件与条目（接收方查补交记录的落点）。"""
        for delivery in store.rows(self.MODULE):
            for entry in delivery.get("entries", []):
                if entry.get("变更编号") == change_number:
                    return {
                        "文件编号": delivery["文件编号"],
                        "交付类型": delivery["交付类型"],
                        "原件编号": delivery.get("原件编号", "-"),
                        "entry": entry,
                    }
        return None

    # ── 内部工具 ──────────────────────────────────────────────

    def _find_file(self, file_number: str) -> dict[str, Any] | None:
        for delivery in store.rows(self.MODULE):
            if delivery.get("文件编号") == file_number:
                return delivery
        return None

    def _find_original(self, asset_code: str, window: str) -> dict[str, Any] | None:
        for delivery in store.rows(self.MODULE):
            if (
                delivery.get("目标资产") == asset_code
                and delivery.get("交割窗口") == window
                and delivery.get("交付类型") == "原件"
                and delivery.get("status") != "已作废"
            ):
                return delivery
        return None

    def _field_changes(self, asset_code: str) -> list[dict[str, Any]]:
        """资产的字段级结构变更（只有字段动作才进入交付）。"""
        return [
            row for row in store.rows("change_capture")
            if row.get("目标资产") == asset_code and row.get("字段动作")
        ]

    def _next_file_number(self, window: str) -> str:
        date_compact = window.split("~")[0].strip().replace("-", "")
        prefix = f"DELV-{date_compact}-"
        seqs = [
            int(str(d["文件编号"])[len(prefix):])
            for d in store.rows(self.MODULE)
            if str(d.get("文件编号", "")).startswith(prefix)
            and str(d["文件编号"])[len(prefix):].isdigit()
        ]
        return f"{prefix}{max(seqs, default=0) + 1:04d}"

    def _next_delivery_seq(self, asset_code: str) -> int:
        seqs = [
            entry["交付序号"]
            for delivery in store.rows(self.MODULE)
            if delivery.get("目标资产") == asset_code
            for entry in delivery.get("entries", [])
            if isinstance(entry.get("交付序号"), int)
        ]
        return max(seqs, default=0) + 1

    @staticmethod
    def _parse_window(window: str) -> tuple[str, str]:
        """交割窗口 → 闭区间 [起, 止]。

        '2026-11-03' 表示当日零点到次日零点；'2026-11-03~2026-11-04'
        表示三日零点到四日零点，窗口右端含边界时刻（如四日零点的变更）。
        """
        parts = [part.strip() for part in window.split("~") if part.strip()]
        start_day = parts[0]
        start = f"{start_day} 00:00:00"
        if "~" in window:
            end = f"{parts[-1]} 00:00:00"
        else:
            end = (datetime.strptime(start_day, "%Y-%m-%d") + timedelta(days=1)).strftime(
                "%Y-%m-%d 00:00:00")
        return start, end

    @staticmethod
    def _in_window(effective_time: str, start: str, end: str) -> bool:
        moment = effective_time.strip()
        if not moment:
            return False
        if len(moment) == 10:
            moment = f"{moment} 00:00:00"
        return start <= moment <= end
