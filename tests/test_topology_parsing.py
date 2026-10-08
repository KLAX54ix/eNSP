"""eNSP .topo parsing regressions for discover_topology.

Real eNSP .topo files declare encoding="UNICODE" (which Python's XML parser
cannot resolve) and use the ``devices/dev`` + ``lines/line`` schema.
"""

from __future__ import annotations

import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from ensp_mcp_server.exceptions import ToolFailure
from ensp_mcp_server.tools.topology import discover_topology, _parse_topo_xml, _extract_device_info

# Mirrors the header and shape of a real eNSP-exported .topo file.
TOPO_XML = """<?xml version="1.0" encoding="UNICODE" ?>
<topo version="1.3.00.100">
    <devices>
        <dev id="id-r1" name="R1" model="AR1220" com_port="2001"/>
        <dev id="id-sw1" name="LSW1" model="S5700" com_port="2000"/>
        <dev id="id-pc1" name="PC1" model="PC" com_port="0"/>
    </devices>
    <lines>
        <line srcDeviceID="id-r1" destDeviceID="id-sw1">
            <interfacePair lineName="Copper" srcIndex="0" tarIndex="1"/>
        </line>
    </lines>
</topo>
"""


class TopoParsingTests(unittest.TestCase):
    def _write(self, content: str, encoding: str = "utf-8") -> str:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "sample.topo"
        path.write_bytes(content.encode(encoding))
        return str(path)

    def test_legacy_unicode_declaration_does_not_crash(self):
        report = discover_topology(self._write(TOPO_XML))
        self.assertNotIn("unknown encoding", report)
        self.assertNotIn("失败", report)

    def test_devices_and_links_are_parsed(self):
        report = discover_topology(self._write(TOPO_XML))
        self.assertIn("设备数量: 3", report)
        self.assertIn("链路数量: 1", report)
        for name in ("R1", "LSW1", "PC1"):
            self.assertIn(name, report)
        # Link endpoints are resolved from IDs to names and expose indices.
        self.assertIn("R1:0", report)
        self.assertIn("LSW1:1", report)

    def test_utf16_topo_with_unicode_declaration(self):
        report = discover_topology(self._write(TOPO_XML, encoding="utf-16"))
        self.assertIn("设备数量: 3", report)
        self.assertIn("R1", report)

    def test_device_type_is_inferred_from_model(self):
        report = discover_topology(self._write(TOPO_XML))
        self.assertIn("router", report)
        self.assertIn("switch", report)

    def test_unicode_names_in_utf8_and_both_utf16_byte_orders(self):
        xml = TOPO_XML.replace('name="R1"', 'name="核心路由器"')
        for encoding, bom in (("utf-8", b""), ("utf-8-sig", b""),
                              ("utf-16-le", b""), ("utf-16-be", b""),
                              ("utf-16-le", b"\xff\xfe"), ("utf-16-be", b"\xfe\xff")):
            with self.subTest(encoding=encoding, bom=bom):
                path = self._write(xml, encoding)
                if bom:
                    Path(path).write_bytes(bom + Path(path).read_bytes())
                report = discover_topology(path)
                self.assertNotIsInstance(report, ToolFailure)
                self.assertIn("核心路由器", report)
                self.assertIn("设备数量: 3", report)

    def test_headerless_utf16_with_leading_whitespace(self):
        xml = '\n<topo><devices><dev name="核心路由器"/></devices></topo>'
        for encoding in ("utf-16-le", "utf-16-be"):
            with self.subTest(encoding=encoding):
                self.assertIn("核心路由器", discover_topology(self._write(xml, encoding)))

    def test_body_encoding_attributes_and_text_are_preserved(self):
        body = '<topo encoding="custom"><note>encoding="UNICODE"</note></topo>'
        for header in ('', '<?xml version="1.0"?>', '<?xml version="1.0" encoding="UNICODE"?>'):
            with self.subTest(header=header):
                root = _parse_topo_xml(self._write(header + body))
                self.assertEqual(root.get("encoding"), "custom")
                self.assertEqual(root.findtext("note"), 'encoding="UNICODE"')

    def test_declared_encoding_is_used_before_removing_declaration(self):
        xml = TOPO_XML.replace('encoding="UNICODE"', 'encoding="GBK"').replace('name="R1"', 'name="核心路由器"')
        self.assertIn("核心路由器", discover_topology(self._write(xml, "gbk")))

    def test_slot_interface_groups_are_reported_without_inventing_cli_names(self):
        xml = '<dev name="R1"><slot number="slot17"><interface interfacename="GE" sztype="Ethernet" count="2"/><interface interfacename="Ethernet" count="8"/></slot><slot number="slot1"><interface interfacename="Serial" count="2"/></slot></dev>'
        info = _extract_device_info(ET.fromstring(xml))
        self.assertEqual(info["interfaces"], ["slot17 / GE (数量: 2)", "slot17 / Ethernet (数量: 8)", "slot1 / Serial (数量: 2)"])
        report = discover_topology(self._write(f"<topo><devices>{xml}</devices></topo>"))
        self.assertIn("slot17 / GE (数量: 2)", report)

    def test_named_legacy_interfaces_are_preserved(self):
        info = _extract_device_info(ET.fromstring('<device name="R1"><interfaces><interface name="GigabitEthernet0/0/0"/></interfaces></device>'))
        self.assertEqual(info["interfaces"], ["GigabitEthernet0/0/0"])

    def test_read_failures_are_typed_errors(self):
        path = self._write('<topo>')
        self.assertIsInstance(discover_topology(path), ToolFailure)
        Path(path).unlink()
        self.assertIsInstance(discover_topology(path), ToolFailure)


if __name__ == "__main__":
    unittest.main()
