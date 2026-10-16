"""Release-bound money attestations use only synthetic source and values."""
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from web_operation_agent_yudao.money_review import VerifiedMoneyReview


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class VerifiedMoneyReviewTests(unittest.TestCase):
    def test_exact_release_source_endpoint_and_nonzero_attestation(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "ui"
            for relative in ("src/utils/index.ts", "src/utils/formatter.ts",
                             "src/views/demo/items.vue"):
                file = source / relative
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_text('<el-table-column label="价格" prop="price">'
                                '<template #default="scope">{{ scope.row.price / 100 }}</template>'
                                '</el-table-column>' if relative.endswith(".vue") else relative)
            document = {"schema_version": 1, "release": "synthetic-v1", "review": "same actor",
                "source_functions": {relative: sha(source / relative) for relative in
                    ("src/utils/index.ts", "src/utils/formatter.ts")},
                "fields": [{"route": "/demo/items", "endpoint": "/demo/items/page",
                    "field": "price", "factor": "0.01", "display_unit": "元",
                    "component": "demo/items", "component_sha256": sha(source / "src/views/demo/items.vue"),
                    "nonzero_samples": 2}]}
            path = root / "verified-money.json"
            path.write_text(json.dumps(document))
            ui = SimpleNamespace(views={"demo/items": SimpleNamespace(endpoint="/demo/items/page")})
            review = VerifiedMoneyReview.load(path, ui_source=source, release="synthetic-v1", ui=ui)
            self.assertEqual(review.fields_for("/demo/items/page")["price"].display_value(12345), 123.45)
            self.assertEqual(review.fields_for("/demo/items/page")["price"].display_value(0), 0)
            self.assertFalse(review.fields_for("/other/page"))
            with self.assertRaisesRegex(ValueError, "deployment release"):
                VerifiedMoneyReview.load(path, ui_source=source, release="wrong", ui=ui)
            (source / "src/utils/index.ts").write_text("changed")
            with self.assertRaisesRegex(ValueError, "formatter source changed"):
                VerifiedMoneyReview.load(path, ui_source=source, release="synthetic-v1", ui=ui)
            (source / "src/utils/index.ts").write_text("src/utils/index.ts")
            wrong_ui = SimpleNamespace(views={"demo/items": SimpleNamespace(endpoint="/other/page")})
            with self.assertRaisesRegex(ValueError, "endpoint no longer matches"):
                VerifiedMoneyReview.load(path, ui_source=source, release="synthetic-v1", ui=wrong_ui)
            document["fields"][0]["factor"] = "1"
            path.write_text(json.dumps(document))
            with self.assertRaisesRegex(ValueError, "factor no longer matches"):
                VerifiedMoneyReview.load(path, ui_source=source, release="synthetic-v1", ui=ui)
            document["fields"][0]["factor"] = "0.01"
            document["fields"][0]["nonzero_samples"] = 0
            path.write_text(json.dumps(document))
            with self.assertRaisesRegex(ValueError, "invalid field"):
                VerifiedMoneyReview.load(path, ui_source=source, release="synthetic-v1", ui=ui)


if __name__ == "__main__":
    unittest.main()
