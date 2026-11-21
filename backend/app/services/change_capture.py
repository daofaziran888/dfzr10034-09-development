"""变更捕获领域服务：Schema和元数据的变更捕获、版本对比和差异通知。

Pipeline 6: Change capture → diff reporting → version history
"""
from __future__ import annotations

import time as _time
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class ChangeCaptureService(BaseModuleService):
    MODULE = "change_capture"
    ENTITY = "变更记录"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["记录编号", "目标资产", "变更类型", "变更前值", "变更后值", "变更来源", "变更时间", "影响范围"]
    REQUIRED_FIELDS = ["记录编号", "目标资产", "变更类型"]
    STATUS_ORDER = ["已捕获", "已对比", "已通知", "已确认"]
    ACTION_RULES = {"启动对比": "已对比", "发送通知": "已通知", "确认变更": "已确认"}
    NEGATIVE_ACTIONS = []

    def capture_change(self, asset_code: str, change_type: str, before: Any, after: Any,
                       source: str = "schema_evolution") -> dict[str, Any]:
        """捕获一次变更并自动触发差异对比和版本快照（Pipeline 6 入口）。"""
        change_id = store.next_id(self.MODULE)
        change = {
            "id": change_id,
            "记录编号": f"CHAN-{change_id:04d}",
            "目标资产": asset_code,
            "变更类型": change_type,
            "变更前值": str(before),
            "变更后值": str(after),
            "变更来源": source,
            "变更时间": _time.strftime("%Y-%m-%d %H:%M:%S"),
            "影响范围": self._compute_impact_scope(asset_code, change_type),
            "status": "已捕获",
            "pending": True,
            "abnormal": False,
            "created_at": _time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        store.rows(self.MODULE).append(change)

        # Pipeline 6: 自动触发差异报告和版本快照
        diff_result = self._generate_diff_report(change)
        version_snap = self._create_version_snapshot(change)

        # 记录血缘中受影响的下游资产
        affected_downstream = self._find_downstream_assets(asset_code)

        store.log_change(
            self.MODULE, change_id, "变更捕获完成",
            before=change_type, after=f"差异{diff_result.get('diff_count',0)}条，影响{len(affected_downstream)}个下游",
            source="pipeline_6", scope="change_capture->diff_report->version_history"
        )

        self.emit_event("change_captured", {
            "change_id": change_id,
            "asset_code": asset_code,
            "change_type": change_type,
            "downstream_count": len(affected_downstream),
        })

        return {
            "change": change,
            "diff_report": diff_result,
            "version_snapshot": version_snap,
            "affected_downstream": affected_downstream,
            "pipeline": "change_capture->diff_report->version_history",
        }

    def capture_field_change(self, asset_code: str, field_name: str, action: str,
                             before: Any = "", after: Any = "", source: str = "schema_evolution",
                             effective_time: str | None = None,
                             source_seq: int | None = None) -> dict[str, Any]:
        """捕获字段级结构变更（新增列/删除列等），登记生效时间和来源序号。

        来源序号是源端报文的序号：迟到报文的来源序号可能小于已交付变更，
        变更交付按生效时间归入交割窗口，已签发窗口的迟到变更走补差。
        """
        result = self.capture_change(asset_code, action, before, after, source)
        change = result["change"]
        change["字段名"] = field_name
        change["字段动作"] = action
        change["生效时间"] = effective_time or change["变更时间"]
        change["来源序号"] = (
            source_seq if source_seq is not None else self._next_source_seq(asset_code)
        )
        change["交付状态"] = "未交付"
        change["交付文件"] = ""
        change["交付序号"] = ""
        # 让自动生成的差异报告带上变更编号，接收方按编号核对补交记录
        for row in store.rows("diff_report"):
            if row.get("change_id") == change["id"]:
                row["变更编号"] = change["记录编号"]
                row["字段名"] = field_name
        return result

    def _next_source_seq(self, asset_code: str) -> int:
        """资产维度递增的来源序号（源端报文未带序号时兜底）。"""
        seqs: list[int] = []
        for row in store.rows(self.MODULE):
            if row.get("目标资产") != asset_code:
                continue
            value = row.get("来源序号")
            if isinstance(value, int):
                seqs.append(value)
            elif isinstance(value, str) and value.isdigit():
                seqs.append(int(value))
        return max(seqs, default=0) + 1

    def _compute_impact_scope(self, asset_code: str, change_type: str) -> str:
        """计算影响范围。"""
        downstream = self._find_downstream_assets(asset_code)
        if downstream:
            return f"影响{len(downstream)}个下游资产: {', '.join(downstream)}"
        return f"无下游依赖"

    def _generate_diff_report(self, change: dict[str, Any]) -> dict[str, Any]:
        """生成差异报告。"""
        diff_id = store.next_id("diff_report")
        diff = {
            "id": diff_id,
            "记录编号": f"DIFF-CHG-{diff_id:04d}",
            "比较对象": change.get("目标资产", ""),
            "比较维度": change.get("变更类型", ""),
            "差异字段": change.get("变更类型", ""),
            "源值": change.get("变更前值", ""),
            "目标值": change.get("变更后值", ""),
            "差异等级": "修改",
            "报告时间": _time.strftime("%Y-%m-%d %H:%M:%S"),
            "status": "已生成",
            "pending": False,
            "abnormal": False,
            "change_id": change["id"],
            "created_at": _time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        store.rows("diff_report").append(diff)
        return {"diff_id": diff_id, "diff_count": 1}

    def _create_version_snapshot(self, change: dict[str, Any]) -> dict[str, Any]:
        """创建版本快照记录变更历史。"""
        store.create_snapshot(f"change-{change['id']}", [change.get("目标资产", "unknown")])
        return {"snapshot_id": f"change-{change['id']}", "created": True}

    def _find_downstream_assets(self, asset_code: str) -> list[str]:
        """查找下游资产（通过血缘关系）。"""
        downstream = []
        for row in store.rows("lineage_track"):
            if row.get("上游资产") == asset_code:
                ds = row.get("下游资产", "")
                if ds and ds not in downstream:
                    downstream.append(ds)
        return downstream

    def compare_versions(self, asset_code: str, version_a: str, version_b: str) -> list[dict[str, Any]]:
        """比较资产的两个版本差异。"""
        diffs = store.diff_snapshots(version_a, version_b, asset_code)
        return diffs

    def get_changes_by_asset(self, asset_code: str) -> list[dict[str, Any]]:
        """获取资产的所有变更记录。"""
        return store.find_by_field(self.MODULE, "目标资产", asset_code)

    def get_pending_changes(self) -> list[dict[str, Any]]:
        """获取未确认的变更。"""
        return [
            r for r in store.rows(self.MODULE)
            if r.get("status") in ("已捕获", "已对比")
        ]