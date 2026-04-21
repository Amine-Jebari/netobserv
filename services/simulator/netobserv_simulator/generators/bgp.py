"""BGP routing event generator."""

from __future__ import annotations

import random
from datetime import UTC, datetime

from ..models import BgpEvent, BgpEventType
from .topology import ANNOUNCED_PREFIXES, BGP_PEERS, NORMAL_AS_PATHS


class BgpGenerator:
    """Generates plausible BGP ANNOUNCE/WITHDRAW/UPDATE events."""

    def __init__(self, rng: random.Random | None = None) -> None:
        self._rng = rng or random.Random()

    def _next_hop_for(self, peer_ip: str) -> str:
        # Next hop is typically the peer's loopback — derive it deterministically.
        return peer_ip

    def generate(
        self,
        *,
        event_type: BgpEventType | None = None,
        peer_ip: str | None = None,
        prefix: str | None = None,
        as_path: list[int] | None = None,
    ) -> BgpEvent:
        """Generate a BGP event. Overrides let scenarios force anomalies."""

        chosen_peer = peer_ip or self._rng.choice(BGP_PEERS)
        peer_index = BGP_PEERS.index(chosen_peer) if chosen_peer in BGP_PEERS else 0
        chosen_path = (
            list(as_path)
            if as_path is not None
            else list(NORMAL_AS_PATHS[peer_index % len(NORMAL_AS_PATHS)])
        )
        chosen_type: BgpEventType = event_type or self._rng.choices(
            ["UPDATE", "ANNOUNCE", "WITHDRAW"], weights=[70, 20, 10], k=1
        )[0]

        return BgpEvent(
            timestamp=datetime.now(tz=UTC),
            event_type=chosen_type,
            peer_ip=chosen_peer,
            prefix=prefix or self._rng.choice(ANNOUNCED_PREFIXES),
            as_path=chosen_path,
            next_hop=self._next_hop_for(chosen_peer),
            local_pref=self._rng.choice([100, 150, 200]),
            med=self._rng.randint(0, 500),
        )
