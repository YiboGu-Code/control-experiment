"""Designer capability; inject a TextProvider through the common executor."""

from .contracts import DesignDocument, DesignerConfig, DesignerProposal, DesignSection, RequirementDraft
from .role import Designer

__all__ = [
    "DesignDocument", "Designer", "DesignerConfig", "DesignerProposal", "DesignSection", "RequirementDraft",
]
