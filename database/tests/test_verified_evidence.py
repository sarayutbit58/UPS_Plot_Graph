"""Safety contracts for applying exact, manufacturer-sourced catalog facts."""

import copy
import unittest
from unittest.mock import patch

import database.verified_evidence as evidence_module
from database.published_catalog import project_model
from database.verified_evidence import apply_verified_evidence


def product(sku='UPS-1', model='UPS-1', manufacturer='APC'):
    return {
        'id': 1, 'manufacturer': manufacturer, 'model': model, 'sku': sku,
        'product_type': 'UPS', 'non_ups_reason': None,
        'rating_w': 800, 'rating_va': 1000, 'outlet_count': 9,
        'runtime_curve': {'points': [{'loadW': 400, 'runtimeMinutes': 5}]},
        'warnings': ['Earlier source disagreement'],
        'evidence': [{'path': 'private/original-source', 'locator': 'row:7'}],
        'prices': [{'amount': '12.34', 'currency': 'THB', 'vatTreatment': 'exclusive',
                    'source': {'path': 'private/commercial-source'}}],
        'estimated_cost': {'amount': '12.34', 'currency': 'THB'},
    }


def record(sku='UPS-1', manufacturer='APC'):
    return {
        'manufacturer': manufacturer, 'catalogSku': sku, 'manufacturerSku': 'UPS-1',
        'category': 'UPS', 'identitySource': {'url': 'https://www.se.com/product/ups-1'},
        'ratingWatts': 900, 'ratingVa': 1000,
        'ratingSource': {'url': 'https://www.se.com/specification/ups-1'},
        'outletGroups': [
            {'type': 'IEC_C13', 'power': 'battery', 'count': 4,
             'source': {'url': 'https://www.se.com/specification/ups-1'}},
            {'type': 'IEC_C19', 'power': 'surge', 'count': 1,
             'source': {'url': 'https://www.se.com/specification/ups-1'}},
        ],
        'batteryConfigurations': [{
            'id': 'internal', 'label': 'Built-in battery',
            'points': [{'loadW': 450, 'runtimeMinutes': 10},
                       {'loadW': 900, 'runtimeMinutes': 4}],
            'source': {'url': 'https://www.se.com/runtime/ups-1'},
            'includedPacks': [], 'extraPacks': [], 'conditions': 'new battery',
        }],
        'conflicts': [],
    }


def crosswalk_evidence():
    return {'path': 'private/original-source', 'locator': 'row:7',
            'ratingVaEvidence': True, 'ratingWEvidence': True,
            'runtimeEvidence': False, 'outletEvidence': False, 'expansionEvidence': False}


def valid_crosswalk():
    # SHA256(json.dumps(crosswalk_evidence(), sort_keys=True, ensure_ascii=False))[:24].
    return {'sourceId': '1f227cd0c8bdae24091b70df',
            'manufacturerUrl': 'https://www.se.com/product/ups-1'}


class VerifiedEvidenceTests(unittest.TestCase):
    def apply(self, products, records):
        return apply_verified_evidence(products, records)

    def test_schneider_electric_brand_uses_its_own_oem_sources_without_rebranding(self):
        candidate = record(manufacturer='Schneider Electric')
        result = self.apply([product(manufacturer='Schneider Electric')], [candidate])[0]
        self.assertEqual('Schneider Electric', result['manufacturer'])
        self.assertTrue(result['identity_verified'])
        self.assertTrue(result['rating_w_verified'])

    def test_exact_match_updates_only_verified_facts_without_mutating_sources_or_prices(self):
        original = product()
        before = copy.deepcopy(original)
        overlay = record()
        overlay_before = copy.deepcopy(overlay)
        result = self.apply([original], [overlay])[0]
        self.assertEqual(original, before)
        self.assertEqual(overlay, overlay_before)
        self.assertIsNot(result, original)
        self.assertTrue(result['identity_verified'])
        self.assertEqual(result['manufacturer_sku'], 'UPS-1')
        self.assertEqual(result['product_category'], 'UPS')
        self.assertEqual(result['identity_source']['url'], 'https://www.se.com/product/ups-1')
        self.assertEqual((result['rating_w'], result['rating_va']), (900, 1000))
        self.assertTrue(result['rating_w_verified'])
        self.assertTrue(result['rating_va_verified'])
        self.assertEqual(result['rating_w_source']['url'], 'https://www.se.com/specification/ups-1')
        self.assertEqual(result['rating_va_source']['url'], 'https://www.se.com/specification/ups-1')
        self.assertEqual(result['prices'], before['prices'])
        self.assertEqual(result['estimated_cost'], before['estimated_cost'])
        self.assertIsNot(result['prices'], original['prices'])
        self.assertIn('Earlier source disagreement', result['warnings'])
        self.assertEqual(result['evidence'], before['evidence'])
        self.assertEqual(result['runtime_curve'], before['runtime_curve'])
        self.assertEqual(result['outlet_count'], 9)  # Legacy total is not inferred from groups.
        self.assertEqual([(g['type'], g['power'], g['count']) for g in result['outlet_groups']],
                         [('IEC_C13', 'battery', 4), ('IEC_C19', 'surge', 1)])
        self.assertTrue(all(group['manufacturer_verified'] is True for group in result['outlet_groups']))
        self.assertEqual(result['battery_configurations'][0]['id'], 'internal')
        self.assertTrue(result['battery_configurations'][0]['manufacturer_verified'])
        self.assertEqual(result['battery_configurations'][0]['points'],
                         [{'loadW': 450, 'runtimeMinutes': 10},
                          {'loadW': 900, 'runtimeMinutes': 4}])

    def test_exact_sku_and_explicit_model_only_matching_do_not_guess_kit_or_prefix(self):
        exact = product()
        kit = product(sku='KIT-UPS-1')
        prefix = product(sku='APC-UPS-1')
        model_only = product(sku=None)
        lower_case = product(sku='ups-1')
        other_brand = product(manufacturer='Eaton')
        explicit_model_only = {**record(sku=None), 'catalogSku': None}
        results = self.apply([exact, kit, prefix, model_only, lower_case, other_brand],
                             [record(), explicit_model_only])
        self.assertEqual([item.get('identity_verified', False) for item in results],
                         [True, False, False, True, False, False])

    def test_distributor_sku_requires_explicit_crosswalk_record(self):
        row = product(sku='DIST-1')
        row['evidence'] = [crosswalk_evidence()]
        candidate = {**record(sku='DIST-1'), 'manufacturerSku': 'UPS-1',
                     'crosswalkSource': valid_crosswalk()}
        result = self.apply([row], [candidate])[0]
        self.assertTrue(result['identity_verified'])
        self.assertEqual(result['manufacturer_sku'], 'UPS-1')
        self.assertEqual(result['identity_crosswalk'], valid_crosswalk())

    def test_mismatched_sku_without_matching_crosswalk_remains_unverified(self):
        row = product(sku='APC-UPS-1')
        row['evidence'] = [crosswalk_evidence()]
        before = copy.deepcopy(row)
        candidate = record(sku='APC-UPS-1')
        variants = [None, {},
                    {'sourceId': '0' * 24, 'manufacturerUrl': 'https://www.se.com/product/ups-1'},
                    {'sourceId': 'not-a-source-id', 'manufacturerUrl': 'https://www.se.com/product/ups-1'},
                    {'sourceId': valid_crosswalk()['sourceId'], 'manufacturerUrl': 'https://www.eaton.com/spec'},
                    {'sourceId': valid_crosswalk()['sourceId'], 'manufacturerUrl': 'https://www.se.com/spec#page=1'}]
        for crosswalk in variants:
            with self.subTest(crosswalk=crosswalk):
                test_record = copy.deepcopy(candidate)
                if crosswalk is not None:
                    test_record['crosswalkSource'] = crosswalk
                result = self.apply([row], [test_record])[0]
                self.assertFalse(result['identity_verified'])
                self.assertFalse(result['rating_w_verified'])
                self.assertEqual(result['rating_w'], 800)
                self.assertEqual(result['prices'], before['prices'])
                self.assertEqual(result['evidence'], before['evidence'])
        self.assertEqual(row, before)

    def test_crosswalk_must_belong_to_the_same_product(self):
        first = product(sku='DIST-1')
        first['evidence'] = [crosswalk_evidence()]
        second = product(sku='DIST-2')
        second['id'] = 2
        second['evidence'] = [{'sourceId': 'a' * 24}]
        no_evidence = product(sku='DIST-3')
        no_evidence['id'] = 3
        no_evidence['evidence'] = []
        candidate_second = {**record(sku='DIST-2'), 'crosswalkSource': valid_crosswalk()}
        candidate_empty = {**record(sku='DIST-3'), 'crosswalkSource': valid_crosswalk()}
        results = self.apply([first, second, no_evidence], [candidate_second, candidate_empty])
        self.assertFalse(results[1]['identity_verified'])
        self.assertFalse(results[2]['identity_verified'])

    def test_crosswalk_output_omits_unapproved_extra_metadata(self):
        row = product(sku='DIST-1')
        row['evidence'] = [crosswalk_evidence()]
        crosswalk = {**valid_crosswalk(), 'path': 'private/unapproved-metadata'}
        result = self.apply([row], [{**record(sku='DIST-1'), 'crosswalkSource': crosswalk}])[0]
        self.assertEqual(result['identity_crosswalk'], valid_crosswalk())

    def test_raw_and_projected_evidence_use_same_crosswalk_id(self):
        raw = product(sku='DIST-1')
        raw['evidence'] = [crosswalk_evidence()]
        projected = project_model(raw, include_prices=True)
        self.assertEqual(projected['evidence'][0]['sourceId'], valid_crosswalk()['sourceId'])
        candidate = {**record(sku='DIST-1'), 'crosswalkSource': valid_crosswalk()}
        for row in (raw, projected):
            with self.subTest(projected=row is projected):
                original_prices = copy.deepcopy(row['prices'])
                result = self.apply([row], [candidate])[0]
                self.assertTrue(result['identity_verified'])
                self.assertEqual(result['identity_crosswalk'], valid_crosswalk())
                self.assertEqual(result['prices'], original_prices)
        matched = self.apply([projected], [candidate])[0]
        reprojected = project_model(matched, include_prices=True)
        self.assertEqual(reprojected['evidence'][0]['sourceId'], valid_crosswalk()['sourceId'])
        self.assertEqual(reprojected['identity_crosswalk'], valid_crosswalk())
        self.assertTrue(self.apply([reprojected], [])[0]['identity_verified'])

    def test_untrusted_new_crosswalk_does_not_erase_prior_verified_mapping(self):
        row = product(sku='DIST-1')
        row['evidence'] = [crosswalk_evidence()]
        good = {**record(sku='DIST-1'), 'crosswalkSource': valid_crosswalk()}
        first = self.apply([row], [good])[0]
        bad = copy.deepcopy(good)
        bad['crosswalkSource']['sourceId'] = '0' * 24
        result = self.apply([first], [bad])[0]
        self.assertTrue(result['identity_verified'])
        self.assertEqual(result['identity_crosswalk'], valid_crosswalk())
        self.assertEqual(result['rating_w'], 900)

    def test_preexisting_crosswalk_verification_requires_matching_source_evidence(self):
        row = product(sku='DIST-1')
        row.update(identity_verified=True, manufacturer_sku='UPS-1',
                   identity_source={'url': 'https://www.se.com/product/ups-1'},
                   identity_crosswalk=valid_crosswalk())
        row['evidence'] = [{'sourceId': '0' * 24}]
        result = self.apply([row], [])[0]
        self.assertFalse(result['identity_verified'])
        self.assertEqual(result['identity_crosswalk'], valid_crosswalk())

    def test_positive_source_pages_are_preserved_without_accepting_invalid_pages(self):
        candidate = record()
        candidate['identitySource']['page'] = 1
        candidate['ratingSource']['page'] = 2
        candidate['outletGroups'][0]['source']['page'] = 3
        candidate['batteryConfigurations'][0]['source']['page'] = 4
        result = self.apply([product()], [candidate])[0]
        self.assertEqual(result['identity_source']['page'], 1)
        self.assertEqual(result['rating_w_source']['page'], 2)
        self.assertEqual(result['outlet_groups'][0]['source']['page'], 3)
        self.assertEqual(result['battery_configurations'][0]['source']['page'], 4)
        for page in (0, -1, True, 1.5, '2'):
            with self.subTest(page=page):
                invalid = record()
                invalid['identitySource']['page'] = page
                result = self.apply([product()], [invalid])[0]
                self.assertTrue(result['identity_verified'])
                self.assertNotIn('page', result['identity_source'])

    def test_untrusted_identity_url_cannot_verify_identity_or_override_ratings(self):
        bad_urls = [
            'http://www.se.com/product',
            'https://se.com.attacker.test/product',
            'https://notse.com/product',
            'https://www.se.com@attacker.test/product',
            'https://user@www.se.com/product',
            'https://www.se.com/product?token=secret',
            'https://www.se.com/product#page=1',
            'https://www.se.com/product?',
            'https://www.se.com/product#',
            'https://www.se.com/\nproduct',
            'https://www.se.com/product\x7f',
            'https://bad..se.com/product',
            'https://-bad.se.com/product',
            'https://under_score.se.com/product',
            'https://www.eaton.com/product',
        ]
        for url in bad_urls:
            with self.subTest(url=url):
                candidate = record()
                candidate['identitySource'] = {'url': url}
                result = self.apply([product()], [candidate])[0]
                self.assertFalse(result.get('identity_verified', False))
                self.assertEqual(result['rating_w'], 800)
                self.assertFalse(result.get('rating_w_verified', False))

    def test_approved_manufacturer_domains_are_isolated_by_brand(self):
        brands = [
            ('APC', 'https://apc.com/spec'),
            ('APC', 'https://support.schneider-electric.com/spec'),
            ('Eaton', 'https://www.eaton.com/spec'),
            ('Eaton', 'https://assets.tripplite.com/cad-drawing/example.pdf'),
            ('CyberPower', 'https://support.cyberpower.com/spec'),
            ('CyberPower', 'https://www.cyberpowersystems.com/spec'),
            ('Delta', 'https://www.deltaww.com/spec'),
            ('Delta', 'https://deltapowersolutions.com/spec'),
            ('Syndome', 'https://www.syndome.com/spec'),
            ('Syndome', 'https://www.syndome.co.th/spec'),
            ('Vertiv', 'https://www.vertiv.com/spec'),
        ]
        for brand, url in brands:
            with self.subTest(brand=brand, url=url):
                candidate = record(manufacturer=brand)
                candidate['identitySource'] = {'url': url}
                candidate['ratingSource'] = {'url': url}
                candidate['outletGroups'] = []
                candidate['batteryConfigurations'] = []
                result = self.apply([product(manufacturer=brand)], [candidate])[0]
                self.assertTrue(result['identity_verified'])
                self.assertTrue(result['rating_w_verified'])

    def test_eaton_tripp_lite_asset_domain_does_not_allow_lookalikes(self):
        validator = evidence_module.is_manufacturer_url
        self.assertTrue(validator('Eaton', 'https://assets.tripplite.com/cad-drawing/example.pdf'))
        self.assertFalse(validator('Eaton', 'https://assets.tripplite.com.attacker.test/example.pdf'))
        self.assertFalse(validator('Eaton', 'https://sub.assets.tripplite.com/example.pdf'))
        self.assertFalse(validator('Eaton', 'https://other.tripplite.com/example.pdf'))
        self.assertFalse(validator('APC', 'https://assets.tripplite.com/cad-drawing/example.pdf'))

    def test_public_url_validator_uses_patchable_manufacturer_allowlist(self):
        validator = getattr(evidence_module, 'is_manufacturer_url', None)
        domains = getattr(evidence_module, 'MANUFACTURER_DOMAINS', None)
        self.assertTrue(callable(validator))
        self.assertIsInstance(domains, dict)
        self.assertFalse(validator('Synthetic', 'https://docs.example.test/spec'))
        with patch.dict(domains, {'synthetic': ('example.test',)}):
            self.assertTrue(validator('Synthetic', 'https://docs.example.test/spec'))
            self.assertFalse(validator('APC', 'https://docs.example.test/spec'))
            candidate = record(manufacturer='Synthetic')
            candidate['identitySource'] = {'url': 'https://docs.example.test/product'}
            candidate['ratingSource'] = {'url': 'https://docs.example.test/spec'}
            candidate['outletGroups'] = []
            candidate['batteryConfigurations'] = []
            result = self.apply([product(manufacturer='Synthetic')], [candidate])[0]
            self.assertTrue(result['identity_verified'])
            self.assertTrue(result['rating_w_verified'])

    def test_invalid_or_unsourced_rating_cannot_replace_existing_value(self):
        for value in (None, 0, -1, True, float('nan'), float('inf'), '900'):
            with self.subTest(value=value):
                candidate = record()
                candidate['ratingWatts'] = value
                result = self.apply([product()], [candidate])[0]
                self.assertEqual(result['rating_w'], 800)
                self.assertFalse(result.get('rating_w_verified', False))
                self.assertTrue(result['rating_va_verified'])
        candidate = record()
        candidate['ratingSource'] = {'url': 'https://example.com/spec'}
        result = self.apply([product()], [candidate])[0]
        self.assertEqual(result['rating_w'], 800)
        self.assertFalse(result.get('rating_w_verified', False))
        self.assertFalse(result.get('rating_va_verified', False))

    def test_prior_rating_source_path_is_retained_when_official_rating_replaces_it(self):
        existing = product()
        existing['rating_w_source'] = {'path': 'private/original-source', 'locator': 'row:7'}
        result = self.apply([existing], [record()])[0]
        self.assertEqual(result['rating_w'], 900)
        self.assertIn(existing['rating_w_source'], result['previous_rating_w_source'])
        self.assertEqual(result['rating_w_source']['url'], 'https://www.se.com/specification/ups-1')

    def test_groups_require_valid_source_power_count_and_type_without_inventing_total(self):
        candidate = record()
        candidate['outletGroups'] = [
            {'type': 'IEC_C13', 'power': 'battery', 'count': 2,
             'source': {'url': 'https://www.se.com/spec'}},
            {'type': 'IEC_C19', 'power': 'battery', 'count': 1,
             'source': {'url': 'https://www.se.com.attacker.test/spec'}},
            {'type': 'IEC_C13', 'power': 'battery', 'count': -1,
             'source': {'url': 'https://www.se.com/spec'}},
            {'type': 'IEC_C13', 'power': 'battery', 'count': True,
             'source': {'url': 'https://www.se.com/spec'}},
            {'type': 'HARDWIRE', 'power': 'battery', 'count': 1,
             'source': {'url': 'https://www.se.com/spec'}},
        ]
        result = self.apply([product()], [candidate])[0]
        self.assertEqual(result['outlet_count'], 9)
        self.assertEqual([(g['type'], g['count']) for g in result['outlet_groups']],
                         [('IEC_C13', 2), ('HARDWIRE', 1)])

    def test_battery_curve_must_have_explicit_pack_configuration_and_monotonic_points(self):
        candidate = record()
        base = candidate['batteryConfigurations'][0]
        candidate['batteryConfigurations'] = [
            {**base, 'id': 'valid'},
            {**base, 'id': 'increasing_time', 'points': [
                {'loadW': 450, 'runtimeMinutes': 4}, {'loadW': 900, 'runtimeMinutes': 10}]},
            {**base, 'id': 'duplicate_load', 'points': [
                {'loadW': 450, 'runtimeMinutes': 10}, {'loadW': 450, 'runtimeMinutes': 4}]},
            {**base, 'id': 'bad_source', 'source': {'url': 'https://vendor.invalid/curve'}},
            {**base, 'id': 'too_high', 'points': [
                {'loadW': 450, 'runtimeMinutes': 10}, {'loadW': 1000, 'runtimeMinutes': 4}]},
            {**base, 'id': 'no_pack_fields', 'includedPacks': None},
            {**base, 'id': 'bad_packs', 'extraPacks': [{'sku': 'BAT-1', 'quantity': -1}]},
        ]
        result = self.apply([product()], [candidate])[0]
        self.assertEqual([config['id'] for config in result['battery_configurations']], ['valid'])

    def test_configuration_id_matches_the_public_allocation_api_contract(self):
        candidate = record()
        baseline = candidate['batteryConfigurations'][0]
        candidate['batteryConfigurations'] = [
            {**baseline, 'id': 'valid:pack-1._'},
            {**baseline, 'id': 'a' * 80},
            {**baseline, 'id': 'invalid id'},
            {**baseline, 'id': 'invalid/slash'},
            {**baseline, 'id': 'ข้อมูล'},
            {**baseline, 'id': 'a' * 81},
        ]
        result = self.apply([product()], [candidate])[0]
        self.assertEqual([item['id'] for item in result['battery_configurations']],
                         ['valid:pack-1._', 'a' * 80])

    def test_later_partial_batch_preserves_prior_verified_metadata(self):
        first = self.apply([product()], [record()])[0]
        later = {'manufacturer': 'APC', 'catalogSku': 'UPS-1', 'manufacturerSku': 'UPS-1',
                 'category': 'UPS', 'identitySource': {'url': 'https://www.se.com/updated'},
                 'conflicts': []}
        result = self.apply([first], [later])[0]
        self.assertTrue(result['rating_w_verified'])
        self.assertEqual(result['rating_w'], 900)
        self.assertEqual(result['outlet_groups'], first['outlet_groups'])
        self.assertEqual(result['battery_configurations'], first['battery_configurations'])

    def test_official_group_and_curve_replace_unsourced_legacy_facts_with_history(self):
        existing = product()
        existing['outlet_groups'] = [
            {'type': 'IEC_C13', 'power': 'battery', 'count': 4,
             'source': {'path': 'private/legacy-outlets'}}]
        legacy_curve = copy.deepcopy(record()['batteryConfigurations'][0])
        legacy_curve['source'] = {'path': 'private/legacy-runtime'}
        existing['battery_configurations'] = [legacy_curve]
        result = self.apply([existing], [record()])[0]
        outlet = next(group for group in result['outlet_groups'] if group['power'] == 'battery')
        self.assertEqual(outlet['source']['url'], 'https://www.se.com/specification/ups-1')
        self.assertEqual(result['battery_configurations'][0]['source']['url'],
                         'https://www.se.com/runtime/ups-1')
        self.assertIn(existing['outlet_groups'][0], result['previous_outlet_groups'])
        self.assertIn(legacy_curve, result['previous_battery_configurations'])

    def test_new_exact_record_upgrades_older_url_only_facts_with_a_verified_marker(self):
        existing = product()
        existing['outlet_groups'] = copy.deepcopy(record()['outletGroups'])
        existing['battery_configurations'] = copy.deepcopy(record()['batteryConfigurations'])
        result = self.apply([existing], [record()])[0]
        self.assertTrue(all(group['manufacturer_verified'] is True for group in result['outlet_groups']))
        self.assertTrue(result['battery_configurations'][0]['manufacturer_verified'])
        self.assertTrue(result.get('previous_outlet_groups'))
        self.assertTrue(result.get('previous_battery_configurations'))

    def test_malformed_old_group_is_replaced_by_valid_new_manufacturer_fact(self):
        existing = product()
        source = {'url': 'https://www.se.com/specification/ups-1'}
        existing['outlet_groups'] = [{'type': 'IEC_C13', 'power': 'battery', 'count': True,
                                      'source': source, 'manufacturer_verified': True}]
        candidate = record()
        candidate['outletGroups'][0]['count'] = 1
        result = self.apply([existing], [candidate])[0]
        battery = next(group for group in result['outlet_groups'] if group['power'] == 'battery')
        self.assertIs(type(battery['count']), int)
        self.assertEqual(battery['count'], 1)
        self.assertIn(existing['outlet_groups'][0], result['previous_outlet_groups'])

    def test_existing_flags_with_invalid_manufacturer_sources_are_downgraded(self):
        existing = product()
        existing.update(identity_verified=True, identity_source={'url': 'https://evil.test/product'},
                        rating_w_verified=True, rating_w_source={'url': 'https://se.com.evil.test/spec'},
                        rating_va_verified=True, rating_va_source={'url': 'https://www.se.com/spec'})
        result = self.apply([existing], [])[0]
        self.assertFalse(result['identity_verified'])
        self.assertFalse(result['rating_w_verified'])
        self.assertTrue(result['rating_va_verified'])
        self.assertEqual(result['rating_w'], 800)
        self.assertEqual(result['identity_source'], existing['identity_source'])
        self.assertEqual(result['prices'], existing['prices'])

    def test_existing_invalid_ratings_or_missing_manufacturer_sku_are_not_verified(self):
        existing = product()
        existing.update(identity_verified=True, identity_source={'url': 'https://www.se.com/product'},
                        manufacturer_sku=None, rating_w=-2, rating_w_verified=True,
                        rating_w_source={'url': 'https://www.se.com/spec'}, rating_va=0,
                        rating_va_verified=True, rating_va_source={'url': 'https://www.se.com/spec'})
        result = self.apply([existing], [])[0]
        self.assertFalse(result['identity_verified'])
        self.assertFalse(result['rating_w_verified'])
        self.assertFalse(result['rating_va_verified'])
        self.assertEqual((result['rating_w'], result['rating_va']), (-2, 0))

    def test_unresolved_conflicts_invalidate_only_affected_fields_or_configuration(self):
        first = self.apply([product()], [record()])[0]
        second = {**record(), 'batteryConfigurations': [
            {**record()['batteryConfigurations'][0], 'id': 'internal'},
            {**record()['batteryConfigurations'][0], 'id': 'external', 'label': 'External',
             'extraPacks': [{'sku': 'BAT-1', 'quantity': 1}]},
        ]}
        second['conflicts'] = [{'field': 'ratingWatts'}, {'field': 'outlets'},
                               {'field': 'runtime', 'configurationId': 'external'}]
        result = self.apply([first], [second])[0]
        self.assertFalse(result['rating_w_verified'])
        self.assertTrue(result['rating_va_verified'])
        self.assertEqual(result['outlet_groups'], [])
        self.assertEqual([c['id'] for c in result['battery_configurations']], ['internal'])
        self.assertIn('Earlier source disagreement', result['warnings'])
        self.assertEqual(result['evidence'], first['evidence'])

    def test_prior_conflicts_apply_without_a_new_matching_record(self):
        candidate = record()
        original = candidate['batteryConfigurations'][0]
        candidate['batteryConfigurations'].append({
            **copy.deepcopy(original), 'id': 'external', 'label': 'External',
            'extraPacks': [{'sku': 'BAT-1', 'quantity': 1}],
        })
        existing = self.apply([product()], [candidate])[0]
        previous_prices = copy.deepcopy(existing['prices'])
        existing['manufacturer_conflicts'] = [
            {'field': 'ratingWatts'},
            {'field': 'outlets', 'outletType': 'IEC_C13'},
            {'field': 'runtime', 'configurationId': 'external'},
        ]
        result = self.apply([existing], [record(sku='UNRELATED')])[0]
        self.assertTrue(result['identity_verified'])
        self.assertFalse(result['rating_w_verified'])
        self.assertTrue(result['rating_va_verified'])
        self.assertEqual([(group['type'], group['power']) for group in result['outlet_groups']],
                         [('IEC_C19', 'surge')])
        self.assertEqual([config['id'] for config in result['battery_configurations']], ['internal'])
        self.assertEqual(result['prices'], previous_prices)
        self.assertTrue(result['previous_outlet_groups'])
        self.assertEqual(result['previous_battery_configurations'][0]['id'], 'external')

    def test_prior_identity_conflict_invalidates_verified_fields_without_a_batch(self):
        existing = self.apply([product()], [record()])[0]
        existing['manufacturer_conflicts'] = [{'field': 'identity'}]
        result = self.apply([existing], [])[0]
        self.assertFalse(result['identity_verified'])
        self.assertFalse(result['rating_w_verified'])
        self.assertFalse(result['rating_va_verified'])
        self.assertEqual(result['outlet_groups'], [])
        self.assertEqual(result['battery_configurations'], [])
        self.assertEqual(result['prices'], existing['prices'])
        self.assertTrue(result['previous_outlet_groups'])
        self.assertTrue(result['previous_battery_configurations'])

    def test_non_ups_category_is_explicit_and_prior_reason_is_not_erased(self):
        candidate = record()
        candidate['category'] = 'voltage_regulator'
        classified = self.apply([product()], [candidate])[0]
        self.assertEqual(classified['product_category'], 'voltage_regulator')
        self.assertEqual(classified['non_ups_reason'], 'manufacturer_verified_non_ups')
        fixed = self.apply([classified], [record()])[0]
        self.assertIsNone(fixed['non_ups_reason'])
        earlier = product()
        earlier['non_ups_reason'] = 'legacy classification for review'
        result = self.apply([earlier], [record()])[0]
        self.assertEqual(result['non_ups_reason'], 'legacy classification for review')

    def test_conflicting_duplicate_record_rejects_batch_but_identical_duplicates_dedupe(self):
        baseline = product()
        before = copy.deepcopy(baseline)
        one = record()
        two = copy.deepcopy(one)
        two['ratingWatts'] = 901
        with self.assertRaises(ValueError):
            self.apply([baseline], [one, two])
        self.assertEqual(baseline, before)
        result = self.apply([baseline], [one, copy.deepcopy(one)])[0]
        self.assertEqual(result['rating_w'], 900)
        self.assertEqual(len(result['outlet_groups']), 2)
        self.assertEqual(len(result['battery_configurations']), 1)

    def test_conflicting_outlet_evidence_cannot_be_resurrected_by_a_later_group(self):
        candidate = record()
        source = {'url': 'https://www.se.com/spec'}
        candidate['outletGroups'] = [
            {'type': 'IEC_C13', 'power': 'battery', 'count': 4, 'source': source},
            {'type': 'IEC_C13', 'power': 'battery', 'count': 5, 'source': source},
            {'type': 'IEC_C13', 'power': 'battery', 'count': 4, 'source': source},
        ]
        result = self.apply([product()], [candidate])[0]
        self.assertEqual(result['outlet_groups'], [])
        self.assertTrue(result['manufacturer_conflicts'])
        reapplied = self.apply([result], [record()])[0]
        self.assertFalse(any(group['type'] == 'IEC_C13' for group in reapplied['outlet_groups']))

    def test_conflicting_battery_configuration_cannot_be_resurrected_in_same_batch(self):
        candidate = record()
        valid = candidate['batteryConfigurations'][0]
        changed = copy.deepcopy(valid)
        changed['points'][1]['runtimeMinutes'] = 3
        candidate['batteryConfigurations'] = [valid, changed, copy.deepcopy(valid)]
        result = self.apply([product()], [candidate])[0]
        self.assertEqual(result['battery_configurations'], [])
        self.assertTrue(result['manufacturer_conflicts'])
        reapplied = self.apply([result], [record()])[0]
        self.assertEqual(reapplied['battery_configurations'], [])

    def test_conflicting_verified_oem_ratings_are_not_silently_overwritten(self):
        first = self.apply([product()], [record()])[0]
        changed = {'manufacturer': 'APC', 'catalogSku': 'UPS-1', 'manufacturerSku': 'UPS-1',
                   'category': 'UPS', 'identitySource': {'url': 'https://www.se.com/sku'},
                   'ratingWatts': 800, 'ratingSource': {'url': 'https://www.se.com/new-spec'}}
        result = self.apply([first], [changed])[0]
        self.assertFalse(result['rating_w_verified'])
        self.assertTrue(result['rating_va_verified'])
        self.assertTrue(result['manufacturer_conflicts'])
        self.assertFalse(self.apply([result], [record()])[0]['rating_w_verified'])

    def test_lower_verified_capacity_quarantines_existing_curve_above_rating(self):
        first = self.apply([product()], [record()])[0]
        update = {'manufacturer': 'APC', 'catalogSku': 'UPS-1', 'manufacturerSku': 'UPS-1',
                  'category': 'UPS', 'identitySource': {'url': 'https://www.se.com/sku'},
                  'ratingWatts': 800, 'ratingSource': {'url': 'https://www.se.com/spec'}}
        result = self.apply([first], [update])[0]
        self.assertFalse(result['rating_w_verified'])
        self.assertEqual(result['battery_configurations'], [])
        self.assertTrue(result.get('previous_battery_configurations'))

    def test_conflicting_crosswalk_invalidates_identity_and_related_facts(self):
        row = product(sku='DIST-1')
        row['evidence'] = [crosswalk_evidence()]
        first_record = {**record(sku='DIST-1'), 'crosswalkSource': valid_crosswalk()}
        first = self.apply([row], [first_record])[0]
        conflicting = {**record(sku='DIST-1'), 'manufacturerSku': 'UPS-2',
                       'crosswalkSource': {**valid_crosswalk(),
                                           'manufacturerUrl': 'https://www.se.com/product/ups-2'}}
        result = self.apply([first], [conflicting])[0]
        self.assertFalse(result['identity_verified'])
        self.assertFalse(result['rating_w_verified'])
        self.assertEqual(result['outlet_groups'], [])
        self.assertEqual(result['battery_configurations'], [])
        self.assertTrue(result.get('previous_outlet_groups'))

    def test_malformed_evidence_fields_fail_closed_without_crashing(self):
        candidate = record()
        candidate['category'] = []
        self.assertFalse(self.apply([product()], [candidate])[0].get('identity_verified', False))
        candidate = record()
        candidate['outletGroups'] = [{'type': 'IEC_C13', 'power': [], 'count': 1,
                                      'source': {'url': 'https://www.se.com/spec'}}]
        self.assertEqual(self.apply([product()], [candidate])[0]['outlet_groups'], [])
        candidate['conflicts'] = [{'field': 'outlets', 'valueA': 10 ** 500}]
        self.assertEqual(self.apply([product()], [candidate])[0]['outlet_groups'], [])


if __name__ == '__main__':
    unittest.main()
