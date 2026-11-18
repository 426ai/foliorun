"""Deterministic orchestration only. All external I/O is in named Activities."""

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy


@workflow.defn(name="foliorun-v02")
class DocumentWorkflow:
    def __init__(self):
        self.state = "QUEUED"
        self.collected = False
        self.released = False
        self.run_id = ""

    @workflow.query
    def status(self) -> str:
        return self.state

    @workflow.query
    def awaiting_release(self) -> bool:
        return self.collected and not self.released

    @workflow.signal
    def release(self):
        self.released = True

    def control_queue(self):
        if workflow.patched("control-queue-v1"):
            return {"task_queue": workflow.info().task_queue + "-control"}
        return {}

    @workflow.update
    async def cancel(self) -> bool:
        if self.state in {"SUCCEEDED", "FAILED", "CANCELLED", "NEEDS_REVIEW"}:
            return False
        return await workflow.execute_activity(
            "foliorun-cancel-fence-v02",
            self.run_id,
            start_to_close_timeout=timedelta(seconds=20),
            retry_policy=RetryPolicy(maximum_attempts=3),
            **self.control_queue(),
        )

    @workflow.run
    async def run(self, request: dict) -> str:
        self.run_id = request["run_id"]
        self.state = "RUNNING"
        try:
            await workflow.execute_activity(
                "foliorun-agent-v02",
                request,
                start_to_close_timeout=timedelta(seconds=660),
                heartbeat_timeout=timedelta(seconds=30),
                # No automatic model/tool retries after an unknown activity window.
                retry_policy=RetryPolicy(maximum_attempts=1),
            )
        except Exception:
            # Finalize maps missing preparation to NEEDS_REVIEW; exception text stays opaque.
            pass
        self.state = "COLLECTING"
        self.collected = True
        if request.get("hold_after_collect", False):
            await workflow.wait_condition(lambda: self.released)
        self.state = await workflow.execute_activity(
            "foliorun-finalize-v02",
            self.run_id,
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(maximum_attempts=3),
            **self.control_queue(),
        )
        await workflow.wait_condition(lambda: workflow.all_handlers_finished())
        return self.state


@workflow.defn(name="foliorun-v03")
class DocumentWorkflowV03:
    """New history identity; never changes the v02 command sequence."""

    def __init__(self):
        self.state = "QUEUED"
        self.run_id = ""
        self.root_run_id = ""
        self.recovered = False
        self.cancelled = False
        self.collected = False
        self.released = False

    @workflow.query
    def status(self) -> str:
        return self.state

    @workflow.query
    def awaiting_release(self) -> bool:
        return self.collected and not self.released

    @workflow.signal
    def release(self):
        self.released = True

    @workflow.signal
    def collection_recovered(self):
        self.recovered = True

    def control(self):
        return {
            "task_queue": workflow.info().task_queue + "-control",
            "start_to_close_timeout": timedelta(seconds=30),
            "retry_policy": RetryPolicy(maximum_attempts=3),
        }

    @workflow.update
    async def cancel(self) -> bool:
        if self.state in {"SUCCEEDED", "FAILED", "CANCELLED", "NEEDS_REVIEW"}:
            return False
        self.cancelled = await workflow.execute_activity(
            "foliorun-cancel-fence-v03", self.root_run_id, **self.control()
        )
        return self.cancelled

    @workflow.run
    async def run(self, request: dict) -> str:
        self.root_run_id = request["run_id"]
        for _ in range(6):
            self.run_id = request["run_id"]
            self.state = "RUNNING"
            try:
                await workflow.execute_activity(
                    "foliorun-agent-v03",
                    request,
                    start_to_close_timeout=timedelta(seconds=660),
                    heartbeat_timeout=timedelta(seconds=30),
                    retry_policy=RetryPolicy(maximum_attempts=1),
                )
            except Exception:
                pass
            child = await workflow.execute_activity(
                "foliorun-fallback-v03", self.run_id, **self.control()
            )
            if child is None:
                break
            request = {**request, **child}
        self.state = "COLLECTING"
        self.collected = True
        if request.get("hold_after_collect", False):
            await workflow.wait_condition(lambda: self.released or self.cancelled)
        self.state = await workflow.execute_activity(
            "foliorun-finalize-v03", self.run_id, **self.control()
        )
        while self.state == "RECOVERY_PENDING":
            await workflow.wait_condition(lambda: self.recovered or self.cancelled)
            self.recovered = False
            self.state = await workflow.execute_activity(
                "foliorun-finalize-v03", self.run_id, **self.control()
            )
        await workflow.wait_condition(lambda: workflow.all_handlers_finished())
        return self.state
