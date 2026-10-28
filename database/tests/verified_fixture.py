"""Synthetic source-verified UPS facts for the allocation/optimization tests."""


def verified_curve(model, watts, va, outlets, points):
    """Describe one test model without asserting facts about real products."""
    source = {'url': f'https://vendor.example/{model.casefold()}.pdf'}
    return {
        'manufacturer': 'Test Vendor', 'model': model, 'sku': model,
        'identityVerified': True, 'ratingWattsVerified': True, 'ratedWatts': watts,
        'ratingVaVerified': True, 'ratedVa': va, 'outletCount': outlets,
        'points': points, 'source': source,
        'outletGroups': [{'type': 'IEC_C13', 'power': 'battery', 'count': outlets,
                          'manufacturer_verified': True, 'source': source}],
        'batteryConfigurations': [{
            'id': 'internal', 'label': 'Synthetic internal battery',
            'manufacturer_verified': True, 'source': source, 'points': points,
            'includedPacks': [], 'extraPacks': [],
        }],
    }
