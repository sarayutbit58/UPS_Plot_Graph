"""Regional lifecycle evidence does not imply Thailand availability."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from database.verified_evidence import apply_verified_evidence
from database.published_catalog import project_model
from test_verified_evidence import product, record


class LifecycleEvidenceTests(unittest.TestCase):
    def test_thai_notice_requires_a_thai_regional_manufacturer_source(self):
        candidate = record()
        candidate['lifecycleObservation'] = {
            'country': 'Thailand', 'endOfSaleDate': '2026-06-18', 'observedOn': '2026-10-28',
            'source': {'url': 'https://www.se.com/sg/en/product/ups-1'},
        }
        self.assertFalse(apply_verified_evidence([product()], [candidate])[0].get('lifecycle_observations'))
        candidate['lifecycleObservation']['source']['url'] = 'https://www.se.com/th/en/product/OTHER-999'
        self.assertFalse(apply_verified_evidence([product()], [candidate])[0].get('lifecycle_observations'))
        candidate['lifecycleObservation']['country'] = 'Kingdom of Thailand'
        candidate['lifecycleObservation']['source']['url'] = 'https://www.se.com/sg/en/product/ups-1'
        self.assertFalse(apply_verified_evidence([product()], [candidate])[0].get('lifecycle_observations'))
        candidate['lifecycleObservation']['source']['url'] = 'https://www.se.com/th/en/product/ups-1'
        observation = apply_verified_evidence([product()], [candidate])[0]['lifecycle_observations'][0]
        self.assertEqual('Thailand', observation['country'])
        self.assertEqual('2026-06-18', observation['endOfSaleDate'])
        self.assertEqual('unknown', observation['thailandStatus'])

    def test_malformed_region_and_future_observation_are_rejected(self):
        candidate = record()
        candidate['lifecycleObservation'] = {
            'country': 'Thailand.', 'endOfSaleDate': '2026-06-18', 'observedOn': '2026-10-28',
            'source': {'url': 'https://www.se.com/sg/en/product/ups-1'},
        }
        self.assertFalse(apply_verified_evidence([product()], [candidate])[0].get('lifecycle_observations'))
        for path in ('/th/../sg/en/product/ups-1', '/th/%2e%2e/sg/en/product/ups-1'):
            candidate['lifecycleObservation'].update(country='Thailand',
                source={'url': 'https://www.se.com' + path})
            self.assertFalse(apply_verified_evidence([product()], [candidate])[0].get('lifecycle_observations'))
        candidate['lifecycleObservation'].update(country='Thailand', observedOn='2099-01-01',
            source={'url': 'https://www.se.com/th/en/product/ups-1'})
        self.assertFalse(apply_verified_evidence([product()], [candidate])[0].get('lifecycle_observations'))

    def test_regional_end_of_sale_is_preserved_with_source_and_unknown_thailand_status(self):
        candidate = record()
        candidate['lifecycleObservation'] = {
            'country': 'Singapore', 'endOfSaleDate': '2023-03-03',
            'observedOn': '2026-10-28', 'thailandStatus': 'unknown',
            'source': {'url': 'https://www.se.com/sg/en/product/ups-1'},
        }
        result = apply_verified_evidence([product()], [candidate])[0]
        observations = result.get('lifecycle_observations') or []
        self.assertEqual(1, len(observations))
        self.assertEqual('Singapore', observations[0]['country'])
        self.assertEqual('unknown', observations[0]['thailandStatus'])
        self.assertEqual('2023-03-03', observations[0]['endOfSaleDate'])
        projected = project_model(result, include_prices=True)
        self.assertEqual('Singapore', projected.get('lifecycle_observations', [{}])[0].get('country'))

    def test_untrusted_lifecycle_source_is_not_preserved(self):
        candidate = record()
        candidate['lifecycleObservation'] = {
            'country': 'Singapore', 'endOfSaleDate': '2023-03-03',
            'observedOn': '2026-10-28', 'thailandStatus': 'unknown',
            'source': {'url': 'https://example.com/not-manufacturer'},
        }
        result = apply_verified_evidence([product()], [candidate])[0]
        self.assertFalse(result.get('lifecycle_observations'))


if __name__ == '__main__':
    unittest.main()
