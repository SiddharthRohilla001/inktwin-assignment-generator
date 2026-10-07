from typing import Literal

from pydantic import BaseModel, Field


class StyleProfile(BaseModel):
    script_type: str = Field(description="Observed writing style, such as print, cursive, or mixed.")
    slant: str = Field(description="Observed letter slant.")
    letter_shape: str = Field(description="Distinctive visible letter-shape characteristics.")
    stroke_weight: str = Field(description="Observed pen or pencil stroke weight.")
    spacing: str = Field(description="Observed spacing between letters and words.")
    line_spacing: str = Field(description="Observed spacing and alignment between lines.")
    confidence: Literal["low", "medium", "high"]
    limitations: list[str] = Field(
        description="Uncertain or unobservable details; never infer traits hidden by image quality."
    )


class Page(BaseModel):
    page_number: int = Field(ge=1)
    title: str
    content: list[str] = Field(min_length=1)


class AgentResult(BaseModel):
    language: str
    style_profile: StyleProfile
    pages: list[Page] = Field(min_length=1)
    rendering_note: str = Field(
        description="Explain that style_profile is guidance for the client renderer, not a handwriting font."
    )


class HealthResponse(BaseModel):
    status: Literal["ok", "not_configured"]
    provider_configured: bool
