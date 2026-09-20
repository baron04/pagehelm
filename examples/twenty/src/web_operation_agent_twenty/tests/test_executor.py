"""Twenty executor behaviour, tested against the adapter that owns it.

These cases used to sit in the core package's `test_business.py`, which forced the core
suite to install this distribution to run at all. What they actually assert is Twenty's
own projection, duplicate gate, readback and identity resolution -- none of which the core
package should know about. The generic `BusinessToolRuntime` contract they were filed
under stays in core, tested against a synthetic executor.
"""
import unittest

from web_operation_agent.business.runtime import BusinessToolError

from web_operation_agent_twenty.adapter import TwentyBusinessToolExecutor


class TwentyExecutor(unittest.TestCase):
        def test_twenty_adapter_projects_only_valid_returned_record_routes(self):
            class FixtureTwenty(TwentyBusinessToolExecutor):
                def __init__(self):
                    pass

                @staticmethod
                def _get_companies():
                    return ({"totalCount": 2, "data": {"companies": [
                        {"id": "f45ee421-8a3e-4aa5-a1cf-7207cc6754e1",
                         "name": "Anthropic", "domainName": {
                             "primaryLinkUrl": "anthropic.com"}},
                        {"id": "not-a-record-id", "name": "Other",
                         "domainName": {"primaryLinkUrl": "other.test"}},
                    ]}}, 200, False)

            result = FixtureTwenty().execute(
                "query_companies", {"name_contains": "Anthropic", "limit": 2})

            self.assertEqual(result.content["matched_count"], 1)
            self.assertEqual(result.content["collection_ui_path"], "/objects/companies")
            self.assertEqual(result.content["companies"], [{
                "name": "Anthropic", "domain": "anthropic.com",
                "ui_path": "/object/company/f45ee421-8a3e-4aa5-a1cf-7207cc6754e1",
            }])
            self.assertEqual(result.allowed_routes, (
                "/objects/companies",
                "/object/company/f45ee421-8a3e-4aa5-a1cf-7207cc6754e1"))

        def test_twenty_create_normalizes_checks_and_reads_back_same_user(self):
            class Tokens:
                @staticmethod
                def access_token():
                    return "test-token"

            class FixtureTwenty(TwentyBusinessToolExecutor):
                def __init__(self):
                    self.allow_mutations = True
                    self.tokens = Tokens()
                    self.posts = 0

                @staticmethod
                def _get_companies():
                    return ({"totalCount": 1, "data": {"companies": [
                        {"name": "Existing", "domainName": {
                            "primaryLinkLabel": "existing.example"}},
                    ]}}, 200, False)

                def _authorized_mutation_json(self, path, token, body):
                    self.posts += 1
                    self.asserted = (path, token, body)
                    return ({"data": {"createCompany": {
                        "id": "f45ee421-8a3e-4aa5-a1cf-7207cc6754e1"}}}, 201)

                @staticmethod
                def _authorized_json(path, token):
                    if path != "/rest/companies/f45ee421-8a3e-4aa5-a1cf-7207cc6754e1":
                        raise AssertionError(path)
                    if token != "test-token":
                        raise AssertionError("wrong actor token")
                    return {"data": {"company": {
                        "id": "f45ee421-8a3e-4aa5-a1cf-7207cc6754e1",
                        "name": "NovaForge Labs", "domainName": {
                            "primaryLinkLabel": "novaforge.example"}}}}

            executor = FixtureTwenty()
            proposal = executor.prepare_mutation(
                "create_company", {"name": " NovaForge Labs ",
                                   "domain": "NOVAFORGE.EXAMPLE."})
            self.assertEqual(proposal.args, {
                "name": "NovaForge Labs", "domain": "novaforge.example"})
            result = executor.execute_mutation("create_company", proposal.args)
            self.assertEqual(executor.posts, 1)
            self.assertEqual(executor.asserted[2]["domainName"]["primaryLinkUrl"],
                             "https://novaforge.example")
            self.assertTrue(result.content["readback_verified"])
            self.assertEqual(result.audit["identity_binding"], "unverified")
            self.assertNotIn("oauth_actor", result.audit)

        def test_twenty_resolves_delegated_actor_from_metadata_api(self):
            class Tokens:
                @staticmethod
                def access_token():
                    return "test-token"

            class FixtureTwenty(TwentyBusinessToolExecutor):
                def _authorized_metadata_json(self, token):
                    self.seen_token = token
                    return {"data": {
                        "currentUser": {"id": "actor-1"},
                        "currentWorkspace": {"id": "workspace-1"},
                    }}

            executor = FixtureTwenty(
                base_url="http://127.0.0.1:3000", tokens=Tokens())
            assertion = executor.resolve_delegated_identity(
                lifetime_s=60, now_ms=1_000_000)

            self.assertEqual(executor.seen_token, "test-token")
            self.assertEqual(assertion.source, "delegated_api")
            self.assertEqual(assertion.actor_id, "actor-1")
            self.assertEqual(assertion.workspace_id, "workspace-1")
            self.assertEqual(assertion.tenant_id, "workspace-1")
            self.assertEqual(assertion.expires_at, 1_060_000)

        def test_twenty_create_refuses_duplicate_or_non_domain_before_post(self):
            class FixtureTwenty(TwentyBusinessToolExecutor):
                def __init__(self):
                    self.allow_mutations = True

                @staticmethod
                def _get_companies():
                    return ({"totalCount": 1, "data": {"companies": [
                        {"name": "NovaForge Labs", "domainName": {
                            "primaryLinkLabel": "novaforge.example"}},
                    ]}}, 200, False)

            executor = FixtureTwenty()
            with self.assertRaisesRegex(BusinessToolError, "already exists"):
                executor.prepare_mutation(
                    "create_company", {"name": "novaforge labs",
                                       "domain": "other.example"})
            with self.assertRaisesRegex(BusinessToolError, "bare DNS"):
                executor.prepare_mutation(
                    "create_company", {"name": "Other",
                                       "domain": "https://other.example/path"})

if __name__ == "__main__":
    unittest.main()
