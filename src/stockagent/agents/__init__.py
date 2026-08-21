"""The five specialist agents.

======  ==================  ==============================================
Agent   Class               Job
======  ==================  ==============================================
1       ManagerAgent        Supervises, demands evidence, decides
2       RegimeAgent         HMM regime detection and self-audit
3       DiscoveryAgent      Ranks the universe into a watchlist
4       RiskAgent           Sizes, enforces limits, holds a veto
5       DevilsAdvocateAgent Argues against, states falsification
======  ==================  ==============================================

Agents 2-5 analyse independently, then rebut each other; the Manager reviews.
See :mod:`stockagent.debate` for the orchestration.
"""

from __future__ import annotations

from .base import (Agent, AgentReport, AnalysisContext, Decision, Evidence,
                   Finding, Severity, Stance, TradeIdea)
from .devils_advocate import DevilsAdvocateAgent
from .discovery_agent import Candidate, DiscoveryAgent
from .manager import ManagerAgent, ManagerVerdict
from .regime_agent import RegimeAgent
from .risk_agent import RiskAgent

__all__ = [
    "Agent", "AgentReport", "AnalysisContext", "Decision", "Evidence",
    "Finding", "Severity", "Stance", "TradeIdea",
    "ManagerAgent", "ManagerVerdict", "RegimeAgent", "DiscoveryAgent",
    "Candidate", "RiskAgent", "DevilsAdvocateAgent",
]
