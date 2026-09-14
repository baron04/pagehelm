import random
import unittest
from decimal import Decimal

from evals.odoo import population


def seeds(count):
    rng = random.Random(7407)
    return [f"{rng.getrandbits(128):032x}" for _ in range(count)]


class PopulationTests(unittest.TestCase):
    def test_generation_is_deterministic_and_seed_bound(self):
        seed = seeds(1)[0]
        self.assertEqual(population.generate(seed), population.generate(seed))
        self.assertNotEqual(population.generate(seed)["seed_commitment"],
                            population.generate(seeds(2)[1])["seed_commitment"])
        for bad in ("", "ABC", "0" * 31, "g" * 32):
            with self.assertRaises(ValueError):
                population.seed_commitment(bad)

    def test_structural_invariants_hold_for_many_seeds(self):
        for seed in seeds(60):
            with self.subTest(seed=seed):
                pop = population.generate(seed)
                main = [item for item in pop["customers"]
                        if item["company"] == "main" and item["customer_rank"] > 0]
                names = [item["name"].casefold() for item in pop["customers"]]
                self.assertEqual(len(names), len(set(names)))
                prefix = pop["roles"]["customer.ambiguous_prefix"].casefold()
                self.assertEqual(sum(prefix in item["name"].casefold() for item in main), 2)
                noun = pop["roles"]["product.ambiguous_prefix"].casefold()
                self.assertEqual(sum(noun in item["name"].casefold()
                                     for item in pop["products"]), 2)
                usd = [Decimal(item["amount_total"]) for item in pop["orders"]
                       if item["state"] in {"draft", "sent"} and item["currency"] == "USD"
                       and item["company"] == "main"]
                self.assertEqual(usd.count(max(usd)), 1)
                prices = [Decimal(item["list_price"]) for item in pop["products"]]
                self.assertEqual(prices.count(max(prices)), 1)
                self.assertGreater(len(main), 200)
                for role, key in pop["roles"].items():
                    if isinstance(key, str) and ":" in key:
                        population.record(pop, role)
                self.assertNotIn(pop["absent"]["absent.customer"].casefold(), names)
                references = [item["name"] for item in pop["orders"]]
                self.assertNotIn(pop["absent"]["absent.quotation_reference"], references)
                self.assertEqual(len(references), len(set(references)))

    def test_record_names_cover_distinctive_values(self):
        pop = population.generate(seeds(1)[0])
        values = population.record_names(pop)
        self.assertIn(pop["roles"]["customer.ambiguous_prefix"] + " North", values)
        self.assertIn(pop["companies"]["second"]["name"], values)
        self.assertTrue(all(len(value) >= 3 for value in values))


if __name__ == "__main__":
    unittest.main()
