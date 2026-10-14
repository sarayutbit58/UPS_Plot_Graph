import unittest
from extract import candidates, model_in


class ExtractionEvidenceTests(unittest.TestCase):
    def test_separate_price_tiers_and_model_conflict(self):
        # Fictional identifiers and mock amounts; no partner pricelist data.
        rows = list(candidates('sample.md', 'line:1',
                    ['APC','MOCK-SKU100','UPS DEMO650X 650VA/360WATT','DEMO600X','101','102','103'],
                    ['Brand','Article Code','Article Description','Vendor Product Code','SRP inc.vat','SRP exc.vat','NDP ex.vat']))
        self.assertEqual(len(rows), 3)
        self.assertEqual([r['price_amount'] for r in rows], ['101','102','103'])
        self.assertEqual([r['vat_treatment'] for r in rows], ['inclusive','exclusive','exclusive'])
        self.assertEqual(len({r['source_locator'] for r in rows}), 3)
        self.assertEqual(rows[0]['rating_va'],650)
        self.assertEqual(rows[0]['rating_w'],360)
        self.assertIsNone(rows[0]['currency'])
        self.assertTrue(any('model_conflict' in n for n in rows[0]['extraction_notes']))

    def test_stock_quantity_never_becomes_price(self):
        row = next(candidates('Stock.xlsx','sheet:Stock;row:1', ['APC','UPS SMT1000IC 1000VA 700W','689']))
        self.assertIsNone(row['price_amount'])
        self.assertEqual(row['model'],'SMT1000IC')

    def test_kilo_rating_conversion(self):
        row = next(candidates('sample','line:1',['Eaton UPS 9SX3000 3kVA 2.7kW']))
        self.assertEqual(row['model'],'9SX3000')
        self.assertEqual(row['rating_va'],3000)
        self.assertEqual(row['rating_w'],2700)

    def test_price_currency_not_derived_from_unrelated_budget(self):
        row = next(candidates('sample','line:1',['APC','UPS DEMO1000 1000VA','101','Budget 200 บาท'],
                              ['Brand','Description','Dealer Price','ICT']))
        self.assertEqual(row['price_amount'],'101')
        self.assertIsNone(row['currency'])

    def test_explicit_non_ups_sheet_flagged(self):
        row = next(candidates('UPS.xlsx','sheet:AVR;row:1',['CyberPower','AVR1000 1000VA'],context='UPS.xlsx AVR'))
        self.assertTrue(any('non_ups_category' in note for note in row['extraction_notes']))

    def test_distributor_and_manufacturer_codes_distinct(self):
        row = next(candidates('sample','line:1',['APC','MOCK-SKU100','UPS DEMO1000 1000VA','DEMO1000'],
                              ['Brand','Article Code','Description','Vendor Product Code']))
        self.assertEqual(row['sku_type'],'distributor')
        self.assertEqual(row['source_sku'],'MOCK-SKU100')
        self.assertEqual(row['manufacturer_sku'],'DEMO1000')

    def test_capacity_voltage_and_form_tokens_are_not_models(self):
        self.assertIsNone(model_in('1000VA 10kVA 230V 500W 2U 3Phase PF1'))

    def test_explicit_model_over_distributor_article_without_description(self):
        row = next(candidates('UPS.xlsx','line:1',['APC','MOCK-SKU100','DEMO1000','1000VA'],
                              ['Brand','Article Code','Model','Rating'],context='UPS.xlsx'))
        self.assertEqual(row['model'],'DEMO1000')
        self.assertFalse(any('model_conflict' in note for note in row['extraction_notes']))

    def test_explicit_brand_preserved_supplier_not_promoted(self):
        row = next(candidates('UPS.xlsx','line:1',['MOCKBRAND','MOCKSUPPLIER','UPS DEMO1000 1000VA'],
                              ['Brand','Vendor','Description'],context='UPS.xlsx'))
        self.assertEqual(row['manufacturer'],'MOCKBRAND')

    def test_notebook_row_with_bundled_ups_is_not_promoted(self):
        row = next(candidates('sample','line:1',['MOCKPC','Notebook','DEMONB1000 bundle UPS 600VA'],
                              ['Brand','Category Level 2','Description']))
        self.assertTrue(any('non_ups_category' in note for note in row['extraction_notes']))


if __name__ == '__main__':
    unittest.main()
