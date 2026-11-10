"""Pydantic models: pagination union, schema.source, request bodies, overrides."""
import json
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Annotated, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator

from . import config

PARAM = r"^[A-Za-z0-9_.\-\[\]]{1,64}$"
RECORD_PATH = r"^$|^[A-Za-z0-9_\-]+(\.[A-Za-z0-9_\-]+)?$"
XML_ROW_PATH = r"^[A-Za-z0-9_\-.:/]{1,512}$"

Param = Annotated[str, Field(pattern=PARAM)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BodyNext(_Strict):
    type: Literal["body_next"]
    next_path: Literal["next", "links.next"] = "next"


class LinkHeader(_Strict):
    type: Literal["link_header"]


class OffsetPagination(_Strict):
    type: Literal["offset"]
    param: Param = "offset"
    limit_param: Param = "limit"
    page_size: int = Field(gt=0, le=100_000)


class PagePagination(_Strict):
    type: Literal["page"]
    param: Param = "page"
    start: int = Field(default=1, ge=0)


Pagination = Annotated[
    Union[BodyNext, LinkHeader, OffsetPagination, PagePagination],
    Field(discriminator="type"),
]
PAGINATION_ADAPTER = TypeAdapter(Pagination)


class Source(_Strict):
    format: Literal["csv", "tsv", "json", "geojson", "xml"]
    delimiter: Optional[Literal[",", ";", "\t", "|"]] = None
    zip_inner: Optional[str] = None
    xml_row_path: Optional[Annotated[str, Field(pattern=XML_ROW_PATH)]] = None
    json_record_path: Optional[Annotated[str, Field(pattern=RECORD_PATH)]] = None
    geojson: bool = False
    pagination: Optional[Pagination] = None

    @field_validator("zip_inner")
    @classmethod
    def _plain_filename(cls, v):
        if v is not None and (not v or len(v) > 255 or "/" in v or "\\" in v or ".." in v):
            raise ValueError("must be a plain filename")
        return v


class Schema(_Strict):
    source: Source


_AUTH_KEYS = {"authorization": "Authorization", "x-api-key": "X-Api-Key"}


class Edr(_Strict):
    """Current EDR of a STARTED consumer HttpData-PULL transfer, resolved by the UI."""
    endpoint: str = Field(min_length=1, max_length=4096)
    auth_key: Literal["Authorization", "X-Api-Key"]
    auth_value: str = Field(min_length=1, max_length=16384, repr=False)
    counter_party_address: str = Field(min_length=1, max_length=4096)
    asset_id: str = Field(min_length=1, max_length=256)

    @field_validator("auth_key", mode="before")
    @classmethod
    def _canonical_key(cls, v):
        return _AUTH_KEYS.get(v.strip().lower(), v) if isinstance(v, str) else v

    @field_validator("auth_value")
    @classmethod
    def _single_line(cls, v):
        if "\r" in v or "\n" in v:
            raise ValueError("must not contain line breaks")
        return v


class ExploreRequest(_Strict):
    edr: Edr


class DownloadRequest(_Strict):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    edr: Edr
    format: Literal["csv", "tsv", "json", "xlsx", "parquet", "geojson", "xml", "zip"]
    schema_: Schema = Field(alias="schema")


@dataclass(frozen=True)
class Override:
    pagination: Optional[object]
    json_record_path: Optional[str]


@lru_cache(maxsize=1)
def load_overrides() -> dict:
    """PAGINATION_OVERRIDES: {assetId: {<pagination object>, "json_record_path"?}}."""
    raw = config.PAGINATION_OVERRIDES_RAW.strip()
    if not raw:
        return {}
    out = {}
    for asset_id, entry in json.loads(raw).items():
        entry = dict(entry)
        path = entry.pop("json_record_path", None)
        if path is not None and not re.fullmatch(RECORD_PATH, path):
            raise ValueError(f"PAGINATION_OVERRIDES[{asset_id}]: invalid json_record_path")
        pagination = PAGINATION_ADAPTER.validate_python(entry) if entry else None
        out[asset_id] = Override(pagination=pagination, json_record_path=path)
    return out
