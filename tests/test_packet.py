"""核对演练资料的公开输入约定。"""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from inspect_packet import read_packet


class PacketTest(unittest.TestCase):
    def test_related_records(self):
        doc = read_packet(Path("资料/演练.json"))
        segments={(row['shipment'],row['segment']) for row in doc['grids']}
        self.assertTrue({(row['shipment'],row['segment']) for row in doc['boxes']} <= segments)
        self.assertTrue({(row['shipment'],row['segment']) for row in doc['segment_limits']} == segments)
        self.assertTrue(len({(row['shipment'],row['segment'],row['revision']) for row in doc['grids']}) == len(doc['grids']))
        self.assertTrue(all(len(row['cells']) == 3 and all(len(line)==3 for line in row['cells']) for row in doc['grids']))
        self.assertTrue({row['box'] for row in doc['checks']} <= {row['box'] for row in doc['boxes']})
        self.assertTrue({row['shipment'] for row in doc['payments']} <= {row['id'] for row in doc['orders']})

    def test_reject_wrong_container(self):
        with TemporaryDirectory() as folder:
            packet = Path(folder) / "unexpected.json"
            packet.write_text(json.dumps({"invalid": 3}), encoding="utf-8")
            with self.assertRaises(ValueError):
                read_packet(packet)
