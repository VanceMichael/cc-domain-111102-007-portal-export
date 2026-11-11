import unittest
import json
from app.wire import Replay

from app.catalogue import export_csv, load_catalogues
class DomainTests(unittest.TestCase):
    def test_csv_identity(self):
        b=export_csv("museum-a","zh",[{"service":"讲解,导览"}])
        self.assertIn(b"museum-a",b);self.assertTrue(b.startswith(bytes([239,187,191])))
    def test_venues(self):
        self.assertEqual(set(load_catalogues()),{"museum-a","museum-b"})
    def test_quoting(self):
        self.assertIn('"讲解,导览"',export_csv("a","zh",[{"service":"讲解,导览"}]).decode("utf-8-sig"))


class WireTests(unittest.TestCase):
    def test_unknown_route(self):
        from tempfile import TemporaryDirectory
        from pathlib import Path
        with TemporaryDirectory() as d:
            p=Path(d)/"routes.json";p.write_text("{}")
            self.assertEqual(Replay(p).get("https://missing.invalid").status,404)
