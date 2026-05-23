from pydantic import BaseModel, Field


class PortfolioConfig(BaseModel):
    num_slips: int = Field(default=14, ge=4, le=60)
    legs_per_slip: int = Field(default=4, ge=1, le=20)
    min_player_pool: int = Field(default=40, ge=20)
    max_player_exposure: int = Field(default=2, ge=1, le=20)
    allow_override_max_exposure: bool = False
    max_same_game_legs: int = Field(default=2, ge=1, le=20)
    max_same_team_legs: int = Field(default=2, ge=1, le=20)
    min_time_buckets_per_slip: int = Field(default=2, ge=1, le=20)
    selection_mode: str = Field(default="balanced")
    ownership_penalty_strength: float = Field(default=1.0, ge=0.0, le=2.0)
    anchor_every_n_slips: int = Field(default=0, ge=0, le=20)
    adaptive_learning_enabled: bool = True
    learning_lookback_days: int = Field(default=45, ge=7, le=365)
    learning_min_samples: int = Field(default=8, ge=1, le=100)
    adaptive_archetype_weights: dict[str, float] | None = None
    hr_hitter_filter_enabled: bool = True
    min_hr_score: float = Field(default=0.5, ge=0.0, le=1.0)
    relax_hr_score_floor: float = Field(default=0.35, ge=0.0, le=1.0)
    hr_candidate_pool_size: int = Field(default=56, ge=40, le=300)
    min_selected_leg_hr_score: float = Field(default=0.0, ge=0.0, le=1.0)
    strict_hr_leg_floor: bool = False
    hits_profile: str = Field(default="high-frequency")
    hits_filter_enabled: bool = False
    min_hit_score: float = Field(default=0.0, ge=0.0, le=1.0)
    min_recent_hits: int = Field(default=0, ge=0, le=30)
    min_pitcher_vuln_score: float = Field(default=0.0, ge=0.0, le=1.0)
    min_tb_score: float = Field(default=0.0, ge=0.0, le=1.0)
    min_rbi_score: float = Field(default=0.0, ge=0.0, le=1.0)
    min_hrr_score: float = Field(default=0.0, ge=0.0, le=1.0)
    risk_level: str = Field(default="balanced")
    lineup_locked_only: bool = False
    random_seed: int = 42

    def effective_max_exposure(self) -> int:
        if self.allow_override_max_exposure:
            return self.max_player_exposure
        return min(self.max_player_exposure, 2)
