"""Host-side cost reservation before every model send. Foliorun's journal remains the anti-resend authority.
Costs are rate-based estimates from configured prices, never a provider invoice reconciliation."""
import contextlib, fcntl, json, os, time
from pathlib import Path
class BudgetStop(RuntimeError):
    def __init__(self, code):
        super().__init__(code); self.code = code
class Budget:
    def __init__(self, path, pricing, stop_usd):
        self.path = Path(path); self.pricing = pricing; self.stop_usd = float(stop_usd)
    @contextlib.contextmanager
    def locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(str(self.path) + ".lock", "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            d = json.loads(self.path.read_text()) if self.path.exists() else {"spent_usd": 0.0, "pending": {}, "usage": []}
            yield d
            t = self.path.with_suffix(".tmp"); t.write_text(json.dumps(d, indent=2)); os.replace(t, self.path)
    def reserve(self, estimate, envelope):
        rid = str(time.time_ns())
        with self.locked() as d:
            if d["pending"]:
                raise BudgetStop("unreconciled_model_cost_window")
            if d["spent_usd"] + estimate >= self.stop_usd:
                raise BudgetStop("monetary_stop")
            d["pending"][rid] = {"reserved_usd": estimate, **envelope}
        return rid
    def settle(self, rid, input_tokens, output_tokens, response_id=None):
        p = self.pricing
        cost = (input_tokens * p.input_per_million_usd + output_tokens * p.output_per_million_usd) / 1e6
        with self.locked() as d:
            if rid not in d["pending"]:
                raise BudgetStop("reservation_not_pending")
            d["pending"].pop(rid); d["spent_usd"] += cost
            d["usage"].append({"reservation": rid, "provider_response_id": response_id, "input_tokens": input_tokens, "output_tokens": output_tokens, "rate_based_usd": cost, "billing_classification": p.classification})
        return cost
    def snapshot(self):
        return json.loads(self.path.read_text()) if self.path.exists() else {"spent_usd": 0.0, "pending": {}, "usage": []}
def budgeted_model(model, budget):
    from pydantic_ai.messages import ModelMessagesTypeAdapter
    from pydantic_ai.models.wrapper import WrapperModel
    from foliorun.contracts import RuntimeFault
    class BudgetedModel(WrapperModel):
        async def request(self, messages, settings, parameters):
            max_tokens = (settings or {}).get("max_tokens")
            if not isinstance(max_tokens, int):
                raise RuntimeFault("uncapped_model_output_not_allowed")
            schemas = [{"name": t.name, "description": t.description, "schema": t.parameters_json_schema} for t in parameters.function_tools]
            bound = len(ModelMessagesTypeAdapter.dump_json(messages)) + len(json.dumps(schemas).encode()) + 2048
            p = budget.pricing
            est = (bound * p.input_per_million_usd + max_tokens * p.output_per_million_usd) / 1e6
            try:
                rid = budget.reserve(est, {"input_token_envelope": bound, "max_output_tokens": max_tokens})
            except BudgetStop as ex:
                raise RuntimeFault(ex.code) from None
            response = await self.wrapped.request(messages, settings, parameters)
            budget.settle(rid, response.usage.input_tokens, response.usage.output_tokens, response.provider_response_id)
            return response
    return BudgetedModel(model)
