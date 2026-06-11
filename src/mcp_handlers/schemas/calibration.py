from typing import Optional, Union, Literal
from pydantic import Field, model_validator
from .mixins import AgentIdentityMixin

class CheckCalibrationParams(AgentIdentityMixin):
    """
    Check calibration of confidence estimates
    """
    focus: Literal["all", "ethics", "stability", "complexity", "knowledge"] = Field(
        default="all",
        description="Focus area. Usually 'all'"
    )

class RebuildCalibrationParams(AgentIdentityMixin):
    """
    Rebuild calibration from scratch
    """
    min_age_hours: Union[float, str, None] = Field(
        default=0.5,
        description="Minimum age of decisions to evaluate"
    )
    max_decisions: Union[int, str, None] = Field(
        default=0,
        description="Maximum decisions to process (0=all)"
    )

    @model_validator(mode='after')
    def coerce_types(self):
        if isinstance(self.min_age_hours, str):
            try:
                self.min_age_hours = float(self.min_age_hours)
            except ValueError:
                self.min_age_hours = 0.5
        if isinstance(self.max_decisions, str):
            try:
                self.max_decisions = int(self.max_decisions)
            except ValueError:
                self.max_decisions = 0
        return self

class UpdateCalibrationGroundTruthParams(AgentIdentityMixin):
    """
    Update calibration with an external truth signal
    """
    confidence: Optional[float] = Field(None, description="Reported confidence")
    predicted_correct: Optional[bool] = Field(None, description="System's prediction")
    actual_correct: Optional[bool] = Field(None, description="Actual ground truth")
    decision_index: Optional[int] = Field(None, description="Index in history")
    session_id: Optional[str] = Field(None, description="Dialectic session ID")
    is_correct: Optional[bool] = Field(None, description="Whether session output was correct")

class BackfillCalibrationFromDialecticParams(AgentIdentityMixin):
    """
    Retroactively update calibration from historical sessions
    """
    limit: Union[int, str, None] = Field(default=50, description="Max sessions to process")

    @model_validator(mode='after')
    def coerce_types(self):
        if isinstance(self.limit, str):
            try:
                self.limit = int(self.limit)
            except ValueError:
                self.limit = 50
        return self

class CalibrationParams(AgentIdentityMixin):
    """Parameters for calibration"""
    action: Literal["check", "update", "backfill", "rebuild"] = Field("check", description="Operation to perform")
    actual_correct: Optional[bool] = Field(None, description="Ground truth (for action=update)")
    confidence: Optional[float] = Field(None, description="Confidence value (for action=update)")
    dry_run: Optional[bool] = Field(None, description="Dry run mode (for action=rebuild)")


