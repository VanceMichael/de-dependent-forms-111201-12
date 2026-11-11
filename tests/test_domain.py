import json
from pathlib import Path
import unittest

from collector.domain import *


class DomainTests(unittest.TestCase):
    def setUp(self):
        self.payload = json.loads(Path('fixtures/sample.json').read_text())

    def test_domain_behavior(self):
        parser = Form()
        parser.feed('<form action="/step"><input name="csrf" value="synthetic"></form>')
        self.assertEqual(parser.fields['csrf'], 'synthetic')
        drafts = Drafts(self.payload['options'])
        row = self.payload['requests'][0]
        drafts.save(row['id'], row['content'])
        with self.assertRaises(ValueError):
            drafts.save(row['id'], row['content'])

    def test_deterministic_observations(self):
        self.assertEqual(collect(self.payload), collect(self.payload))
