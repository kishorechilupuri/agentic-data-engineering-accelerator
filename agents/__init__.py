"""Agent package for the Agentic Data Engineering Accelerator.

Six specialist agents, one per stage of the data-engineering SDLC, plus the
orchestrator that runs them in order.
"""

from agents.base_agent import (
    STATUS_DEGRADED,
    STATUS_FAILED,
    STATUS_OK,
    BaseAgent,
    StageResult,
)
from agents.codegen_agent import PipelineCodeGenAgent
from agents.dq_agent import DataQualityAgent
from agents.mapping_agent import SourceToTargetMappingAgent
from agents.orchestrator import STAGE_ORDER, Orchestrator
from agents.requirement_agent import RequirementRefinerAgent
from agents.review_agent import CodeReviewAgent
from agents.test_agent import TestGenerationAgent

AGENT_REGISTRY = {
    RequirementRefinerAgent.key: RequirementRefinerAgent,
    SourceToTargetMappingAgent.key: SourceToTargetMappingAgent,
    PipelineCodeGenAgent.key: PipelineCodeGenAgent,
    DataQualityAgent.key: DataQualityAgent,
    TestGenerationAgent.key: TestGenerationAgent,
    CodeReviewAgent.key: CodeReviewAgent,
    Orchestrator.key: Orchestrator,
}

__all__ = [
    "AGENT_REGISTRY",
    "STAGE_ORDER",
    "STATUS_DEGRADED",
    "STATUS_FAILED",
    "STATUS_OK",
    "BaseAgent",
    "CodeReviewAgent",
    "DataQualityAgent",
    "Orchestrator",
    "PipelineCodeGenAgent",
    "RequirementRefinerAgent",
    "SourceToTargetMappingAgent",
    "StageResult",
    "TestGenerationAgent",
]
