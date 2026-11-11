from __future__ import annotations

import unittest
from datetime import datetime

import pandas as pd

from core.prospects import (
    apply_grading_fee,
    average_last_sales,
    card_matches_query,
    collect_sold_items,
    confidence_label,
    inventory_owns_card,
    prospect_margins,
    qualifies,
    queries_without_matches,
    score_shortlist,
    shortlist_cards,
    title_matches,
)


LUKA = {
    "card_name": "Luka Doncic",
    "card_number": "280",
    "parallel": "Silver",
    "category": "Basketball",
    "year": "2018",
    "set_name": "Prizm",
}

CHARIZARD = {
    "card_name": "Charizard ex",
    "card_number": "006",
    "parallel": "",
    "category": "Pokemon",
    "year": "2023",
    "set_name": "Obsidian Flames",
}


class MarginTests(unittest.TestCase):
    def test_dollar_and_percent_margin(self):
        dollars, percent = prospect_margins(100, 40, 10)
        self.assertEqual(dollars, 50.0)
        self.assertEqual(percent, 100.0)

    def test_zero_cost_basis(self):
        dollars, percent = prospect_margins(25, 0, 0)
        self.assertEqual(dollars, 25.0)
        self.assertEqual(percent, 0.0)

    def test_fee_is_applied_without_changing_the_source_row(self):
        source = [{"psa10_price": 80, "raw_price": 30, "card_name": "Luka Doncic"}]
        priced = apply_grading_fee(source, 20)
        self.assertNotIn("margin_dollars", source[0])
        self.assertEqual(priced[0]["margin_dollars"], 30.0)
        self.assertEqual(priced[0]["margin_pct"], 60.0)
        self.assertEqual(priced[0]["grading_fee"], 20.0)


class CutoffTests(unittest.TestCase):
    def test_requires_twenty_psa10_sales_and_five_raw_sales(self):
        self.assertFalse(qualifies(19, 5))
        self.assertFalse(qualifies(20, 4))
        self.assertTrue(qualifies(20, 5))

    def test_confidence_bands(self):
        self.assertEqual(confidence_label(19), "")
        self.assertEqual(confidence_label(20), "Fair")
        self.assertEqual(confidence_label(40), "Good")
        self.assertEqual(confidence_label(100), "Strong")

    def test_score_drops_cards_under_the_sale_counts(self):
        card = {
            **LUKA,
            "queries": {
                "psa10": "2018 Prizm Luka Doncic 280 Silver PSA 10",
                "raw": "2018 Prizm Luka Doncic 280 Silver",
            },
            "psa_gem_rate": 0.4,
            "psa_population": 80,
        }
        items = []
        for index in range(20):
            items.append(self._sale("psa10", index, 100 + index, card["queries"]["psa10"]))
        for index in range(4):
            items.append(self._sale("raw", index, 20, card["queries"]["raw"], graded=False))
        self.assertEqual(score_shortlist([card], items), [])

    def _sale(self, kind, index, price, keyword, graded=True):
        if kind == "psa10":
            title = f"2018 Prizm Silver Luka Doncic 280 PSA 10 #{index}"
        else:
            title = f"2018 Prizm Silver Luka Doncic 280 raw #{index}"
        return {
            "itemId": f"{kind}-{index}",
            "title": title,
            "priceValue": price,
            "currency": "USD",
            "sold": True,
            "soldDate": f"2026-06-{(index % 27) + 1:02d}",
            "searchQuery": keyword,
        }


class TitleTests(unittest.TestCase):
    def test_psa10_title_matches_name_number_and_parallel(self):
        title = "2018 Panini Prizm Silver Luka Doncic #280 PSA 10 GEM MINT"
        self.assertTrue(title_matches(title, LUKA, "psa10"))

    def test_lot_and_other_grades_are_rejected(self):
        self.assertFalse(title_matches("2018 Prizm Silver Luka Doncic 280 PSA 10 lot of 2", LUKA, "psa10"))
        self.assertFalse(title_matches("2018 Prizm Silver Luka Doncic 280 PSA 9", LUKA, "psa10"))
        self.assertFalse(title_matches("2018 Prizm Silver Luka Doncic 280 BGS 10", LUKA, "psa10"))
        self.assertFalse(title_matches("2018 Prizm Silver Luka Doncic 280 not PSA 10", LUKA, "psa10"))

    def test_card_number_does_not_match_a_longer_number(self):
        card = {**LUKA, "card_number": "22"}
        self.assertFalse(title_matches("2018 Prizm Silver Luka Doncic 223 PSA 10", card, "psa10"))
        self.assertTrue(title_matches("2018 Prizm Silver Luka Doncic #022 PSA 10", card, "psa10"))

    def test_pokemon_raw_requires_near_mint_and_rejects_graded(self):
        raw = "2023 Obsidian Flames Charizard ex 006 Near Mint"
        self.assertTrue(title_matches(raw, CHARIZARD, "raw"))
        self.assertTrue(title_matches("Obsidian Flames Charizard ex #6 NM", CHARIZARD, "raw"))
        self.assertFalse(title_matches("Obsidian Flames Charizard ex 006 Lightly Played", CHARIZARD, "raw"))
        self.assertFalse(title_matches("Obsidian Flames Charizard ex 006 PSA 10", CHARIZARD, "raw"))
        self.assertFalse(title_matches("Obsidian Flames Charizard ex 006", CHARIZARD, "raw"))

    def test_sports_raw_stays_ungraded_without_requiring_near_mint(self):
        title = "2018 Prizm Silver Luka Doncic 280 raw"
        self.assertTrue(title_matches(title, LUKA, "raw"))
        self.assertFalse(title_matches("2018 Prizm Silver Luka Doncic 280 PSA 10", LUKA, "raw"))
        self.assertFalse(title_matches("2018 Prizm Luka Doncic 280 raw", LUKA, "raw"))


class SaleAverageTests(unittest.TestCase):
    def test_average_uses_the_five_most_recent_sales(self):
        sales = [
            {"price": 10, "sold_at": datetime(2026, 1, 1)},
            {"price": 20, "sold_at": datetime(2026, 2, 1)},
            {"price": 30, "sold_at": datetime(2026, 3, 1)},
            {"price": 40, "sold_at": datetime(2026, 4, 1)},
            {"price": 50, "sold_at": datetime(2026, 5, 1)},
            {"price": 60, "sold_at": datetime(2026, 6, 1)},
        ]
        average, count = average_last_sales(sales, 5)
        self.assertEqual(count, 6)
        self.assertEqual(average, 40.0)

    def test_queries_with_no_clean_comps_fall_back_off_the_category(self):
        card = {
            **LUKA,
            "queries": {"psa10": "luka psa 10", "raw": "luka raw"},
        }
        calls = []

        def run_actor(keywords, category_id):
            calls.append((list(keywords), category_id))
            if category_id:
                return []
            return [
                {
                    "itemId": f"{keyword}-{index}",
                    "title": "2018 Prizm Silver Luka Doncic 280 PSA 10" if "psa" in keyword else "2018 Prizm Silver Luka Doncic 280 raw",
                    "priceValue": 100 if "psa" in keyword else 25,
                    "currency": "USD",
                    "sold": True,
                    "soldDate": f"2026-07-{(index % 27) + 1:02d}",
                    "searchQuery": keyword,
                }
                for keyword in keywords
                for index in range(20 if "psa" in keyword else 5)
            ]

        self.assertEqual(queries_without_matches([card], []), ["luka psa 10", "luka raw"])
        items = collect_sold_items([card], run_actor, "212")
        self.assertEqual(calls[0][1], "212")
        self.assertEqual(calls[1][1], "")
        prospects = score_shortlist([card], items)
        self.assertEqual(len(prospects), 1)
        self.assertEqual(prospects[0]["psa10_sales"], 20)
        self.assertEqual(prospects[0]["confidence"], "Fair")
        self.assertEqual(prospects[0]["psa10_price"], 100.0)


class ShortlistTests(unittest.TestCase):
    def test_query_filter_keeps_the_selected_set_and_category(self):
        card = {
            "description": "2018 Panini Prizm Luka Doncic 280",
            "parsed_description": {
                "category": "Basketball",
                "year": "2018",
                "set_name": "Prizm",
                "name": "Luka Doncic",
                "card_number": "280",
            },
        }
        self.assertTrue(card_matches_query(card, "Basketball", "Prizm", "2018"))
        self.assertFalse(card_matches_query(card, "Pokemon", "Prizm", "2018"))
        self.assertFalse(card_matches_query(card, "Basketball", "Optic", "2018"))

    def test_shortlist_keeps_the_fifty_highest_gem_rates_above_the_population_floor(self):
        records = [
            {"card_name": f"Card {index}", "card_number": str(index), "psa_population": 10, "psa_gem_rate": 0.9}
            for index in range(10)
        ]
        records.extend(
            {
                "card_name": f"Deep {index}",
                "card_number": str(index),
                "year": "2024",
                "set_name": "Prizm",
                "psa_population": 100 + index,
                "psa_gem_rate": index / 100,
            }
            for index in range(60)
        )
        shortlist = shortlist_cards(records, limit=50)
        self.assertEqual(len(shortlist), 50)
        self.assertEqual(shortlist[0]["card_name"], "Deep 59")
        self.assertTrue(all(card["psa_population"] >= 50 for card in shortlist))
        self.assertIn("PSA 10", shortlist[0]["queries"]["psa10"])


class InventoryTests(unittest.TestCase):
    def test_owned_matches_a_raw_inventory_row(self):
        inventory = pd.DataFrame(
            [
                {
                    "card_type": "Sports",
                    "product_type": "Card",
                    "set_name": "Panini Prizm",
                    "card_name": "Luka Doncic",
                    "card_number": "280",
                    "grade": "",
                    "grading_company": "",
                    "condition": "Near Mint",
                },
                {
                    "card_type": "Sports",
                    "product_type": "Graded Card",
                    "set_name": "Prizm",
                    "card_name": "Luka Doncic",
                    "card_number": "280",
                    "grade": "10",
                    "grading_company": "PSA",
                    "condition": "Graded",
                },
            ]
        )
        self.assertTrue(inventory_owns_card(LUKA, inventory))
        graded_only = inventory.iloc[1:].copy()
        self.assertFalse(inventory_owns_card(LUKA, graded_only))


if __name__ == "__main__":
    unittest.main()
