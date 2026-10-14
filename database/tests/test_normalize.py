"""Generic synthetic assertions; no partner price-list fixtures."""

import unittest

from database.normalize import normalize_record


class NormalizationTests(unittest.TestCase):
    def record(self, **changes):
        record = dict(source_path='demo/catalog.md', source_locator='line:7:price',
                      raw_text='synthetic source', manufacturer='Example Vendor',
                      model='U-650/AS', sku=None, product_type='UPS')
        record.update(changes)
        return normalize_record(record)

    def test_unknown_currency_and_vat_stay_unknown(self):
        normalized = self.record(price_amount='123.4500')
        self.assertTrue(normalized['accepted'])
        self.assertEqual(normalized['price_amount'], '123.4500')
        self.assertIsNone(normalized['currency'])
        self.assertEqual(normalized['vat_treatment'], 'unknown')

    def test_punctuation_and_case_variants_do_not_merge(self):
        keys = {self.record(model=model)['identity_key']
                for model in ['U-650/AS', 'U650/AS', 'U-650-AS', 'u-650/as']}
        self.assertEqual(len(keys), 4)

    def test_brand_case_and_whitespace_deduplicate(self):
        self.assertEqual(self.record()['identity_key'],
                         self.record(manufacturer=' EXAMPLE  Vendor ')['identity_key'])

    def test_distributor_skus_do_not_split_manufacturer_model_identity(self):
        first = self.record(sku='DISTRIBUTOR-001')
        second = self.record(sku='OTHER-DISTRIBUTOR-002', sku_type='distributor')
        self.assertTrue(first['accepted'])
        self.assertEqual(first['identity_key'], second['identity_key'])
        self.assertIsNone(first['sku'])
        self.assertIn('DISTRIBUTOR-001', first['raw_json'])

    def test_explicit_manufacturer_skus_distinguish_identity(self):
        first = self.record(sku='MAKER-001', sku_type='manufacturer')
        second = self.record(manufacturer_sku='MAKER-002', sku='DIST-002')
        self.assertNotEqual(first['identity_key'], second['identity_key'])
        self.assertEqual(first['sku'], 'MAKER-001')
        self.assertEqual(second['sku'], 'MAKER-002')

    def test_non_ups_category_flags_rejected(self):
        self.assertFalse(self.record(non_ups_category=True)['accepted'])
        self.assertFalse(self.record(extraction_notes=['non_ups_category: AVR'])['accepted'])
        self.assertFalse(self.record(model_conflict=True)['accepted'])

    def test_unknown_brand_and_ambiguous_models_rejected(self):
        for changes in [dict(manufacturer=None), dict(manufacturer='unknown'),
                        dict(model=None), dict(identity_ambiguous=True),
                        dict(extraction_notes=['model_conflict: two model columns'])]:
            self.assertFalse(self.record(**changes)['accepted'])

    def test_bad_prices_are_not_invented_or_parsed_as_float(self):
        for amount in ['1,234.50', '-12', 'NaN', '1.2.3', 12.34, True]:
            self.assertFalse(self.record(price_amount=amount)['accepted'])

    def test_invalid_ratings_rejected(self):
        for rating in [0, -1, 'NaN', float('inf'), True]:
            self.assertFalse(self.record(rating_va=rating)['accepted'])

    def test_traversal_and_absolute_paths_rejected(self):
        for path in ['../catalog.md', '/catalog.md', 'C:/catalog.md']:
            self.assertFalse(self.record(source_path=path)['accepted'])

    def test_source_path_and_sheet_locator_preserve_exact_whitespace(self):
        path = 'ราคา  ไทย/ เครื่องสำรองไฟ  รุ่น.xlsx '
        locator = 'sheet: รายการ  ไทย ;row:7;column:ราคา  รวม'
        normalized = self.record(source_path=path, source_locator=locator)
        self.assertTrue(normalized['accepted'])
        self.assertEqual(normalized['source_path'], path)
        self.assertEqual(normalized['source_locator'], locator)

    def test_price_tier_and_unknown_spec_preserved_in_raw(self):
        normalized = self.record(price_type='dealer net', topology='unspecified custom',
                                 price_amount='1.00')
        self.assertTrue(normalized['accepted'])
        self.assertEqual(normalized['price_type'], 'source-marked')
        self.assertIsNone(normalized['topology'])
        self.assertIn('dealer net', normalized['raw_json'])
        self.assertIn('unspecified custom', normalized['raw_json'])

    def test_dates_must_be_calendar_dates(self):
        self.assertFalse(self.record(source_date='2026-02-30')['accepted'])
        self.assertFalse(self.record(source_date='20261014')['accepted'])
        self.assertEqual(self.record(source_date='2026-10-14')['source_date'], '2026-10-14')


if __name__ == '__main__':
    unittest.main()
