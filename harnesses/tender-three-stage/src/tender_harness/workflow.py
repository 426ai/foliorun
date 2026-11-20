"""Temporal business workflow: extract -> semantic -> fill, one attempt per stage, parent chained."""
from datetime import timedelta
from temporalio import workflow
from temporalio.common import RetryPolicy
ACTIVITY = "tender-harness-stage"
@workflow.defn(name="TenderThreeStage")
class TenderThreeStage:
    @workflow.run
    async def run(self, req: dict) -> dict:
        parent = None; stages = []
        for stage in ("extract", "semantic", "fill"):
            r = await workflow.execute_activity(ACTIVITY, {"stage": stage, "business_id": req["business_id"], "parent": parent},
                start_to_close_timeout=timedelta(minutes=req.get("stage_minutes", 15)), heartbeat_timeout=timedelta(seconds=60), retry_policy=RetryPolicy(maximum_attempts=1))
            if not r["success"] or r["source_sha256"] != req["source_sha256"] or r["resources_sha256"] != req["resources_sha256"]:
                raise ValueError("stage_contract_failed")
            stages.append(r); parent = r
        return {"success": True, "stages": stages, "formal_bid_ready": False}
