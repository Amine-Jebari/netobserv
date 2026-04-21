"""Pydantic schemas for the three telemetry streams."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

FlowAction = Literal["ACCEPT", "REJECT"]
LogStatus = Literal["OK", "NODATA", "SKIPDATA"]
BgpEventType = Literal["ANNOUNCE", "WITHDRAW", "UPDATE"]
SnmpMetricName = Literal[
    "ifInOctets",
    "ifOutOctets",
    "ifInErrors",
    "ifOutErrors",
    "sysUpTime",
    "cpuLoad",
    "ifUtilizationPct",
    "latencyMs",
]


class VpcFlowRecord(BaseModel):
    """VPC Flow Log v2 schema — fields match the AWS reference exactly."""

    model_config = ConfigDict(extra="forbid")

    version: int = 2
    account_id: str
    interface_id: str
    srcaddr: str
    dstaddr: str
    srcport: int = Field(ge=0, le=65535)
    dstport: int = Field(ge=0, le=65535)
    protocol: int = Field(ge=0, le=255)
    packets: int = Field(ge=0)
    bytes: int = Field(ge=0)
    start_ts: int = Field(description="Start of the flow, epoch seconds UTC.")
    end_ts: int = Field(description="End of the flow, epoch seconds UTC.")
    action: FlowAction
    log_status: LogStatus = "OK"


class BgpEvent(BaseModel):
    """A BGP routing event — ANNOUNCE / WITHDRAW / UPDATE."""

    model_config = ConfigDict(extra="forbid")

    timestamp: datetime
    event_type: BgpEventType
    peer_ip: str
    prefix: str
    as_path: list[int]
    next_hop: str
    local_pref: int = Field(default=100, ge=0, le=4_294_967_295)
    med: int = Field(default=0, ge=0, le=4_294_967_295)


class SnmpSample(BaseModel):
    """One SNMP OID poll sample from a simulated device."""

    model_config = ConfigDict(extra="forbid")

    timestamp: datetime
    device_id: str
    interface: str
    metric: SnmpMetricName
    value: float
