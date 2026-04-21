"""VPC flow record generator."""

from __future__ import annotations

import random
import time

from ..models import VpcFlowRecord
from .topology import DESTINATION_IPS, PROTOCOL_MIX, SOURCE_IPS


class FlowGenerator:
    """Produces VPC Flow Log v2 records with realistic traffic mixes."""

    def __init__(
        self,
        account_id: str,
        interface_id_prefix: str = "eni-",
        rng: random.Random | None = None,
    ) -> None:
        self._account_id = account_id
        self._interface_ids = tuple(
            f"{interface_id_prefix}{i:016x}" for i in range(1, 11)
        )
        self._rng = rng or random.Random()
        self._mix_weights = [p.weight for p in PROTOCOL_MIX]

    def _pick_protocol(self) -> tuple[int, int]:
        """Return (protocol_number, dst_port) for a plausible connection."""

        choice = self._rng.choices(PROTOCOL_MIX, weights=self._mix_weights, k=1)[0]
        return choice.protocol, choice.dst_port

    def generate(
        self,
        *,
        srcaddr: str | None = None,
        dstaddr: str | None = None,
        bytes_multiplier: float = 1.0,
        force_reject: bool = False,
    ) -> VpcFlowRecord:
        """Generate a single flow record. Overrides support anomaly injection."""

        now = int(time.time())
        start_ts = now - self._rng.randint(0, 5)
        end_ts = now

        protocol, dport = self._pick_protocol()

        pkt_count = max(1, int(self._rng.randint(4, 600) * bytes_multiplier))
        byte_count = max(pkt_count * 64, int(self._rng.randint(256, 750_000) * bytes_multiplier))

        action = "REJECT" if force_reject else (
            "REJECT" if self._rng.random() < 0.01 else "ACCEPT"
        )

        return VpcFlowRecord(
            version=2,
            account_id=self._account_id,
            interface_id=self._rng.choice(self._interface_ids),
            srcaddr=srcaddr or self._rng.choice(SOURCE_IPS),
            dstaddr=dstaddr or self._rng.choice(DESTINATION_IPS),
            srcport=self._rng.randint(32_768, 60_999),
            dstport=dport,
            protocol=protocol,
            packets=pkt_count,
            bytes=byte_count,
            start_ts=start_ts,
            end_ts=end_ts,
            action=action,
            log_status="OK",
        )
