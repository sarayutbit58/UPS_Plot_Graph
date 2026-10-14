"""Extraction-cache integrity tests using synthetic text files."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from database import extract as extractor


class ExtractionCacheTests(unittest.TestCase):
    def test_identical_file_content_in_different_brand_paths_keeps_each_manufacturer(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'source'
            output = Path(temporary) / 'output'
            first = root / 'APC' / 'catalog.txt'
            second = root / 'Eaton' / 'catalog.txt'
            first.parent.mkdir(parents=True)
            second.parent.mkdir(parents=True)
            content = 'UPS DEMO1000 1000VA 700W\n'
            first.write_text(content, encoding='utf-8')
            second.write_text(content, encoding='utf-8')
            cache = Path(temporary) / 'cache'

            with patch.object(extractor, 'LOCAL', cache), patch.object(
                extractor, 'source_files', return_value=[first, second]
            ):
                extractor.extract(root, output)

            records = [json.loads(line) for line in (output / 'records.jsonl').read_text(encoding='utf-8').splitlines()]
            by_path = {Path(record['source_path']).parts[0]: record['manufacturer'] for record in records}
            self.assertEqual({'APC': 'APC', 'Eaton': 'Eaton'}, by_path)


if __name__ == '__main__':
    unittest.main()
