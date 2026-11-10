"""Build the Transfer used by both pipelines from the EDR the UI sends."""
from dataclasses import dataclass, field

from . import policy


@dataclass(frozen=True)
class Transfer:
    endpoint: str
    auth_key: str
    token: str = field(repr=False)
    provider_host: str
    asset_id: str

    def headers(self) -> dict:
        # Sent exactly as EDC supplied it; EDC data planes reject an added "Bearer " prefix.
        return {self.auth_key: self.token}


def from_edr(edr) -> Transfer:
    provider_host = policy.check_endpoint(edr.endpoint, edr.counter_party_address)
    return Transfer(
        endpoint=edr.endpoint.strip(),
        auth_key=edr.auth_key,
        token=edr.auth_value,
        provider_host=provider_host,
        asset_id=edr.asset_id,
    )
