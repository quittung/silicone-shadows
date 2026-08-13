"""Validated request and review-state shapes."""

from typing import Literal

from pydantic import BaseModel, Field, HttpUrl


class MainLength(BaseModel):
    start: tuple[float, float]
    end: tuple[float, float]


class ReviewState(BaseModel):
    status: Literal["pending", "done"] = "pending"
    rating: Literal["unusable", "bad_perspective", "good"] | None = None
    alpha_threshold: int = Field(default=128, ge=1, le=255)
    main_length: MainLength | None = None
    re_review: bool = False


class PrefetchSelection(BaseModel):
    item_ids: list[str] = Field(max_length=5)


class PublicTicket(BaseModel):
    ticket: str = Field(min_length=16, max_length=128)


class GuestSize(BaseModel):
    label: str = Field(default="One size", min_length=1, max_length=100)
    short_label: str = Field(min_length=1, max_length=20)
    price: float | None = Field(default=None, ge=0, le=1_000_000)
    length: float | None = Field(default=None, gt=0, le=10_000)
    circumference: float | None = Field(default=None, gt=0, le=10_000)
    widest_circumference: float | None = Field(default=None, gt=0, le=10_000)
    widest_label: str | None = Field(default=None, max_length=100)
    unit: Literal["in", "cm", "mm"]


class GuestMetadata(BaseModel):
    submission_version: Literal[1] = 1
    catalog_id: int | None = None
    vendor: str = Field(min_length=1, max_length=200)
    product_type: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=200)
    product_url: HttpUrl | None = None
    species: str | None = Field(default=None, max_length=100)
    quality: Literal["good", "bad_perspective"] = "good"
    source: Literal["contributor_photo", "catalog", "alternative"] = "contributor_photo"
    tags: list[str] = Field(default_factory=list, max_length=100)
    features: list[str] = Field(default_factory=list, max_length=50)
    sizes: list[GuestSize] = Field(default_factory=list, max_length=20)
    notes: str | None = Field(default=None, max_length=2_000)


class IndependentSubmission(BaseModel):
    metadata: GuestMetadata
    main_length: MainLength | None = None


class IndependentUpdate(BaseModel):
    record_id: str = Field(min_length=1, max_length=200)
    metadata: GuestMetadata
