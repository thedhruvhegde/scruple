"""Fees per lifecycle stage, posted to the ledger in USD minor units.

Every amount here is an ASSUMPTION (docs/ASSUMPTIONS.md A-17). Public sources (S16)
report processor chargeback fees of roughly $15 to $100 and network arbitration
fees of a few hundred dollars, but the actual amounts depend on the acquirer and
change over time. The defaults below are illustrative parameters, not quotes.

Who pays: the merchant, always (it is the merchant's ledger we simulate).
- `on_enter[stage]`: charged when a case enters that stage (e.g. the processor's
  chargeback handling fee when a chargeback lands).
- `arbitration_loss`: charged when the network rules against the merchant
  (simplification: the losing party pays the case fee; A-17).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from scruple.sim.models import Stage
from scruple.sim.networks import Network


class FeeSchedule(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    network: Network
    on_enter: dict[Stage, int] = Field(default_factory=dict)  # USD minor units
    arbitration_loss: int = Field(ge=0)
    assumption: bool = True
    basis: str = "A-17: illustrative parameters, not sourced amounts"

    @model_validator(mode="after")
    def _non_negative(self) -> FeeSchedule:
        if any(v < 0 for v in self.on_enter.values()):
            raise ValueError("fees cannot be negative")
        if Stage.RESOLVED in self.on_enter or Stage.INQUIRY in self.on_enter:
            raise ValueError("no fee is charged on entering inquiry or resolution")
        return self

    def entry_fee(self, stage: Stage) -> int:
        return self.on_enter.get(stage, 0)


DEFAULT_FEES: dict[Network, FeeSchedule] = {
    Network.VISA: FeeSchedule(
        network=Network.VISA,
        on_enter={Stage.CHARGEBACK: 1_500, Stage.PRE_ARBITRATION: 2_500},
        arbitration_loss=60_000,
    ),
    Network.MASTERCARD: FeeSchedule(
        network=Network.MASTERCARD,
        on_enter={Stage.CHARGEBACK: 1_500, Stage.PRE_ARBITRATION: 2_500},
        arbitration_loss=57_500,
    ),
}

NO_FEES: dict[Network, FeeSchedule] = {
    n: FeeSchedule(network=n, arbitration_loss=0) for n in Network
}
