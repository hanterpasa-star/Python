import unittest

from shoe_shop_bot.stock import changed_stock, format_stock, parse_size_list, parse_stock


class StockTests(unittest.TestCase):
    def test_sizes_and_quantities(self):
        self.assertEqual(parse_stock("38, 39:2; 40.5 41: 3"), {"38": 1, "39": 2, "40.5": 1, "41": 3})

    def test_canonical_numbers_and_numeric_sort(self):
        self.assertEqual(list(parse_stock("40 9 38.0 38.5")), ["9", "38", "38.5", "40"])

    def test_rejects_duplicates_invalid_sizes_and_quantities(self):
        for value in ["", "38, 38.0", "0:1", "100", "38:0", "38:-1", "38:1.5", "38:10000", "38:2:3", "обувь", "38.55"]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_stock(value)

    def test_empty_stock_markers(self):
        for value in ["-", "нет", "0"]:
            self.assertEqual(parse_stock(value), {})
        self.assertEqual(format_stock({}), "нет в наличии")

    def test_add_accumulates_and_remove_removes_entire_size(self):
        original = {"38": 2, "39": 1}
        self.assertEqual(changed_stock(original, "add_sizes", {"38": 3, "40": 1}), {"38": 5, "39": 1, "40": 1})
        self.assertEqual(changed_stock(original, "remove_sizes", ["38"]), {"39": 1})
        self.assertEqual(original, {"38": 2, "39": 1})

    def test_replace_reduces_pair_count_and_can_clear(self):
        self.assertEqual(changed_stock({"38": 3}, "set_sizes", {"38": 2}), {"38": 2})
        self.assertEqual(changed_stock({"38": 3}, "set_sizes", {}), {})

    def test_quantity_limit_also_applies_to_sum(self):
        with self.assertRaises(ValueError):
            changed_stock({"38": 9999}, "add_sizes", {"38": 1})
        with self.assertRaises(ValueError):
            changed_stock({}, "add_sizes", {})

    def test_remove_requires_sizes_without_quantities(self):
        self.assertEqual(parse_size_list("38, 39.5"), ["38", "39.5"])
        for value in ["38:2", "-", ""]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_size_list(value)

    def test_caps_number_of_sizes(self):
        with self.assertRaises(ValueError):
            parse_stock(" ".join(str(size) for size in range(1, 27)))
