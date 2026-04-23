"""InfluxDB line-protocol serialisation — verify escaping and field typing."""

from __future__ import annotations

from netobserv_collector.clients.influx import InfluxPoint


def test_basic_point():
    p = InfluxPoint(
        measurement="flows",
        tags={"srcaddr": "10.0.0.1", "dstaddr": "8.8.8.8"},
        fields={"packets": 10, "bytes": 1234},
        timestamp_ns=1_710_000_000_000_000_000,
    )
    line = p.to_line()
    assert line.startswith("flows,")
    assert "srcaddr=10.0.0.1" in line
    assert "dstaddr=8.8.8.8" in line
    # Integer fields get the 'i' suffix in line protocol.
    assert "packets=10i" in line
    assert "bytes=1234i" in line
    assert line.endswith(" 1710000000000000000")


def test_tag_value_escaping():
    p = InfluxPoint(
        measurement="snmp",
        tags={"interface": "Gig 0/0,1"},
        fields={"value": 1.0},
    )
    line = p.to_line()
    assert r"interface=Gig\ 0/0\,1" in line


def test_string_field_quoted():
    p = InfluxPoint(
        measurement="m",
        tags={"a": "b"},
        fields={"msg": 'hello "world"'},
    )
    line = p.to_line()
    assert r'msg="hello \"world\""' in line


def test_empty_fields_rejected():
    p = InfluxPoint(measurement="m", tags={"a": "b"}, fields={})
    try:
        p.to_line()
    except ValueError:
        return
    raise AssertionError("expected ValueError for empty fields")
