"""Release-bound money attestations use only synthetic source and values."""
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from web_operation_agent_yudao.reads.money_review import VerifiedMoneyReview


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class VerifiedMoneyReviewTests(unittest.TestCase):
    def test_compound_rate_requires_its_exact_native_field_unit_source(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            sources = {
                "src/utils/index.ts": "formatter",
                "src/utils/formatter.ts": "formatter",
                "src/views/demo/rates.vue": '<el-table-column label="日租费" prop="price" :formatter="erpPriceTableColumnFormatter" />',
                "src/views/demo/RateForm.vue": '<el-form-item label="日租费" prop="price"><el-input-number '
                    'v-model="formData.price" placeholder="请输入日租费，单位：元/天/KG" /></el-form-item>',
            }
            for name, text in sources.items():
                file = root / name
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_text(text)
            item = {"route":"/demo/rates","endpoint":"/demo/rates/page","field":"price","factor":"1",
                "display_unit":"元/天/KG","component":"demo/rates",
                "component_sha256":sha(root / "src/views/demo/rates.vue"),"nonzero_samples":3,
                "unit_source":{"component":"demo/RateForm","sha256":sha(root / "src/views/demo/RateForm.vue")}}
            document = {"schema_version":1,"release":"synthetic-v1","review":"same actor",
                "source_functions":{name:sha(root/name) for name in ("src/utils/index.ts","src/utils/formatter.ts")},
                "fields":[item]}
            path = root / "policy.json"
            ui = SimpleNamespace(views={"demo/rates":SimpleNamespace(endpoint="/demo/rates/page")})
            def load():
                path.write_text(json.dumps(document))
                return VerifiedMoneyReview.load(path,ui_source=root,release="synthetic-v1",ui=ui)
            field = load().fields_for("demo/rates/page")["price"]
            self.assertEqual((field.display_value(10),field.display_unit),(10,"元/天/KG"))
            # Omitting the denominator is not an acceptable currency default.
            item["display_unit"] = "元"
            with self.assertRaisesRegex(ValueError,"unit no longer matches"):
                load()
            item["display_unit"] = "元/天/KG"
            proof = item.pop("unit_source")
            with self.assertRaisesRegex(ValueError,"invalid field"):
                load()
            item["unit_source"] = proof
            unit_file = root / "src/views/demo/RateForm.vue"
            unit_file.write_text(sources["src/views/demo/RateForm.vue"].replace('prop="price"','prop="otherPrice"'))
            with self.assertRaisesRegex(ValueError,"unit source changed"):
                load()
            proof["sha256"] = sha(unit_file)
            with self.assertRaisesRegex(ValueError,"unit no longer matches"):
                load()
            unit_file.write_text(sources["src/views/demo/RateForm.vue"] * 2)
            proof["sha256"] = sha(unit_file)
            with self.assertRaisesRegex(ValueError,"unit no longer matches"):
                load()
            proof["component"] = "../escape"
            with self.assertRaises(ValueError):
                load()

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
            self.assertEqual(review.fields_for("demo/items/page")["price"].display_value(12345), 123.45)
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
