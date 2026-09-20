"""Reusable application adapter fixture; contains no test cases."""
import json

from web_operation_agent.business.runtime import (
    BusinessSkill, BusinessToolResult, BusinessToolRuntime,
)
from web_operation_agent.composition.descriptor import (
    APPLICATION_ADAPTER_VERSION, ApplicationDescriptor,
)
from web_operation_agent.composition.ports import (
    AnonymousIdentityPort,
    ApplicationPorts,
    ConservativeMutationReconciler,
    DeclaredDataClassifier,
    DeclaredRoutePort,
    ResultPostconditionVerifier,
)


_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "query_invoices",
        "description": "Read invoices from the delegated application account.",
        "parameters": {
            "type": "object", "properties": {}, "additionalProperties": False,
        },
    },
}


class _InvoiceExecutor:
    def tool_schemas(self):
        return (_TOOL_SCHEMA,)

    def tool_effects(self):
        return {"query_invoices": "read"}

    def execute(self, name, args):
        return BusinessToolResult(
            content={"invoices": [{"id": "invoice-1", "ui_path":
                                    "/invoices/invoice-1"}]},
            audit={"operation": name, "count": 0},
            allowed_routes=("/invoices/invoice-1",),
        )

    def prepare_mutation(self, name, args):
        raise AssertionError("read-only adapter")

    def execute_mutation(self, name, args):
        raise AssertionError("read-only adapter")


def _skill() -> BusinessSkill:
    raw = json.dumps({
        "schema_version": 1,
        "skill_id": "ledger.invoice-query",
        "title": "Invoice lookup",
        "intent": "Find current invoices and their payment state.",
        "triggers": ["invoice", "invoices", "发票"],
        "instructions": ["Load current invoice data through the typed read tool."],
        "tool_names": ["query_invoices"],
        "provenance": ["api:mock@v1:/ledger/public-api#get"],
        "read_only": True,
    }, sort_keys=True).encode()
    return BusinessSkill.from_bytes(raw, artifact_uri="package://mock/invoice.json")


class _InvoiceAdapter:
    def __init__(self, executor=None):
        data = DeclaredDataClassifier((("query_invoices", ("business.invoice",)),))
        postconditions = ResultPostconditionVerifier(())
        routes = DeclaredRoutePort("ledger-demo", (r"/invoices/[a-z0-9-]+",))
        self.business_tools = BusinessToolRuntime(
            (_skill(),), executor or _InvoiceExecutor(), application_id="ledger-demo",
            data_classifier=data, postcondition_verifier=postconditions,
            route_validator=routes,
        )
        self.ports = ApplicationPorts(
            identity=AnonymousIdentityPort(), routes=routes,
            postconditions=postconditions,
            reconciliation=ConservativeMutationReconciler(), data=data,
        )
        self.descriptor = ApplicationDescriptor(
            schema_version=APPLICATION_ADAPTER_VERSION,
            application_id="ledger-demo", display_name="Ledger Demo",
            origin="https://ledger.example", navigation_mode="document",
            page_controller_module="https://ledger.example/assets/page-controller.js",
            catalog_sha256=self.business_tools.catalog_sha256,
        )

    def health(self):
        return {"passed": True, "http_status": 200}
