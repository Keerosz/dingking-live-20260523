from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, model_validator

from .config import PortfolioConfig


class PlayerRecord(BaseModel):
    player_name: str
    team: str
    bats: str
    lineup_slot: int
    recent_hr_form: float
    hard_hit_pct: float
    barrel_pct: float
    pull_pct: float
    fly_ball_pct: float
    iso: float
    hr_pa: float
    recent_hits: int
    recent_hr_streak: int

    pitcher_name: str
    pitcher_hand: str
    hr_allowed: float
    opp_fly_ball_tendency: float
    pitcher_split_weakness: str
    hard_contact_allowed: float
    barrel_rate_allowed: float
    pitcher_fatigue: float
    bullpen_quality: float

    wind_speed: float
    wind_direction: str
    temperature: float
    humidity: float
    air_density: float

    park_name: str
    park_hr_factor: float
    park_lhb_boost: float
    park_rhb_boost: float
    short_porch: int
    dome: int

    implied_total: float
    moneyline: float
    projected_ownership: float
    leverage_score_seed: float
    start_time_bucket: str
    game_id: str

    position: str | None = None


class GeneratePortfolioRequest(BaseModel):
    players: list[PlayerRecord] = Field(default_factory=list)
    config: PortfolioConfig = Field(default_factory=PortfolioConfig)
    run_label: str | None = None


class Slip(BaseModel):
    slip_id: str
    legs: list[str]
    archetype: str
    story: str
    avg_portfolio_value: float


class PortfolioSummary(BaseModel):
    total_slips: int
    legs_per_slip: int
    unique_players_used: int
    player_pool_size: int
    hard_rule_max_exposure: int
    max_actual_exposure: int
    hard_2x_respected: bool


class PortfolioOutput(BaseModel):
    run_id: str
    created_at: datetime
    summary: dict[str, Any]
    exposure_report: dict[str, Any]
    parlays: list[dict[str, Any]]
    archetype_tags: dict[str, int]
    pairing_frequency: list[dict[str, Any]]
    time_slot_distribution: dict[str, int]
    rr_survivability_metrics: dict[str, Any]
    chalk_vs_leverage_balance: dict[str, Any]


class StoredRun(BaseModel):
    run_id: str
    created_at: datetime
    payload: dict[str, Any]


class GeneratePortfolioRequestFile(BaseModel):
    csv_path: str
    config: PortfolioConfig = Field(default_factory=PortfolioConfig)

    @model_validator(mode="after")
    def require_csv(self) -> "GeneratePortfolioRequestFile":
        if not self.csv_path:
            raise ValueError("csv_path is required")
        return self


class DecisionOutcomeRequest(BaseModel):
    run_id: str | None = None
    playbook_name: str = Field(min_length=1)
    category: str = Field(min_length=1)
    subcategory: str | None = None
    market_type: str | None = None
    book: str | None = None
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    stake_units: float = Field(default=1.0, ge=0.0)
    odds_price: float | None = None
    win_flag: bool
    payout_multiple: float | None = None
    reward_score: float | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class TwitterSignalRequest(BaseModel):
    source_account: str = Field(min_length=1)
    signal_text: str = Field(min_length=1)
    signal_type: str | None = None
    player_name: str | None = None
    team: str | None = None
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    occurred_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class SlipHitEstimateRequest(BaseModel):
    run_id: str = Field(min_length=1)
    winning_players: list[str] = Field(default_factory=list)
    min_legs_hit: int | None = Field(default=None, ge=1)


class TwitterScreenshotPlaybookRequest(BaseModel):
    screenshot_text: str = Field(min_length=1)
    num_slips: int = Field(default=20, ge=1, le=40)
    legs_per_slip: int = Field(default=4, ge=1, le=20)
    mode: str = Field(default="balanced")
    hits_profile: str = Field(default="high-frequency")
    risk_level: str = Field(default="balanced")
    lineup_locked_only: bool = False
    allow_live: bool = False
    max_pinned_players: int = Field(default=10, ge=1, le=40)
