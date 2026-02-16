"""
Strategy & Objective Agent - LangGraph Nodes
Implements the strategy suggestion, validation, and locking pipeline.
"""
from typing import Dict, Any, List
from loguru import logger

from app.langgraph.strategy_models import (
    StrategyOption,
    StrategyType,
    StrategyStatus,
    StrategyValidationResult,
    UserObjective,
    is_strategy_compatible_with_role,
    get_incompatibility_reason,
    ROLE_STRATEGY_COMPATIBILITY
)
from app.agents.role_models import Role, RoleEnum
from app.observability.audit import audit_logger


def strategy_suggestion_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """
    Node 1: Strategy Suggestion
    
    Analyzes workspace snapshot and role context to generate
    1-3 strategic options ranked by relevance.
    
    Args:
        state: Current workflow state containing:
            - role_context: RoleContext from Role-Based Agent
            - workspace_snapshot: Documents, facts, timeline
            - readability_scores: Document quality metrics
            
    Returns:
        Updated state with suggested_strategies
    """
    logger.info("🧠 Strategy Suggestion: Analyzing workspace and generating options")
    
    # Extract required inputs
    role_context = state.get("role_context")
    workspace_snapshot = state.get("workspace_snapshot", {})
    workspace_id = state.get("workspace_id")
    
    # Validate mandatory inputs
    if not role_context:
        error_msg = "Strategy Suggestion requires RoleContext from Role-Based Agent"
        logger.error(f"❌ {error_msg}")
        raise ValueError(error_msg)
    
    role = role_context.get("role") if isinstance(role_context, dict) else role_context.role
    
    logger.info(f"📋 Generating strategies for role: {role}")
    
    # Get compatible strategy types for this role
    compatible_strategies = ROLE_STRATEGY_COMPATIBILITY.get(role, [])
    
    if not compatible_strategies:
        logger.warning(f"⚠️ No compatible strategies found for role: {role}")
        return {**state, "suggested_strategies": [], "strategy_error": f"No strategies available for role {role}"}
    
    # Generate strategy options based on role
    suggestions = []
    
    # Convert role to RoleEnum if it's a string
    role_enum = RoleEnum(role) if isinstance(role, str) else role
    
    if role_enum == RoleEnum.PETITIONER:
        suggestions = _generate_petitioner_strategies(workspace_snapshot, workspace_id)
    elif role_enum == RoleEnum.RESPONDENT:
        suggestions = _generate_respondent_strategies(workspace_snapshot, workspace_id)
    elif role_enum == RoleEnum.MEDIATOR:
        suggestions = _generate_mediator_strategies(workspace_snapshot, workspace_id)
    elif role_enum == RoleEnum.ADJUDICATOR:
        suggestions = _generate_adjudicator_strategies(workspace_snapshot, workspace_id)
    
    # Filter and rank by relevance
    suggestions = [s for s in suggestions if is_strategy_compatible_with_role(s.strategy_type, role_enum)]
    suggestions.sort(key=lambda x: x.relevance_score, reverse=True)
    
    # Take top 3
    top_suggestions = suggestions[:3]
    
    logger.success(f"✅ Generated {len(top_suggestions)} strategy options")
    
    # Log audit event
    audit_logger.log_event(
        event_type="strategies_suggested",
        data={
            "workspace_id": workspace_id,
            "role": role.value if hasattr(role, 'value') else str(role),
            "count": len(top_suggestions),
            "strategy_ids": [s.strategy_id for s in top_suggestions]
        },
        workspace_id=workspace_id
    )
    
    return {
        **state,
        "suggested_strategies": top_suggestions,
        "strategy_suggestion_complete": True
    }


def user_objective_mapper_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """
    Node 2: User Objective Mapper
    
    Maps user's stated objective to a strategy option.
    Validates role compatibility.
    
    Args:
        state: Current workflow state containing:
            - user_objective_text: User's stated goal
            - suggested_strategies: Previously suggested options
            - role_context: User's role
            
    Returns:
        Updated state with mapped_strategy or new_strategy
    """
    logger.info("🎯 User Objective Mapper: Processing user objective")
    
    user_objective_text = state.get("user_objective_text")
    suggested_strategies = state.get("suggested_strategies", [])
    role_context = state.get("role_context")
    workspace_id = state.get("workspace_id")
    
    if not user_objective_text:
        logger.info("ℹ️ No user objective provided, skipping mapping")
        return {**state, "objective_mapping_skipped": True}
    
    # Create UserObjective object
    user_objective = UserObjective(
        objective_text=user_objective_text,
        priority=state.get("objective_priority", "HIGH")
    )
    
    role = role_context.get("role") if isinstance(role_context, dict) else role_context.role
    
    logger.info(f"📝 Mapping objective: '{user_objective_text[:100]}...'")
    
    # Try to map to existing strategy
    mapped_strategy = None
    mapping_confidence = 0.0
    
    for strategy in suggested_strategies:
        # Simple keyword matching (in production, use LLM or semantic matching)
        confidence = _calculate_objective_strategy_match(user_objective_text, strategy)
        
        if confidence > mapping_confidence:
            mapping_confidence = confidence
            mapped_strategy = strategy
    
    if mapped_strategy and mapping_confidence > 0.6:
        logger.success(f"✅ Mapped to strategy: {mapped_strategy.strategy_id} (confidence: {mapping_confidence:.2f})")
        
        return {
            **state,
            "user_objective": user_objective,
            "mapped_strategy_id": mapped_strategy.strategy_id,
            "mapping_confidence": mapping_confidence,
            "objective_mapping_complete": True
        }
    else:
        # Generate new strategy from objective
        logger.info("🆕 Generating new strategy from user objective")
        
        new_strategy = _derive_strategy_from_objective(
            user_objective,
            role,
            workspace_id,
            len(suggested_strategies) + 1
        )
        
        # Validate compatibility
        if not is_strategy_compatible_with_role(new_strategy.strategy_type, role):
            logger.warning(f"⚠️ Derived strategy {new_strategy.strategy_type} incompatible with role {role}")
            
            return {
                **state,
                "user_objective": user_objective,
                "objective_mapping_error": get_incompatibility_reason(new_strategy.strategy_type, role),
                "objective_mapping_complete": False
            }
        
        logger.success(f"✅ Created new strategy from objective: {new_strategy.strategy_id}")
        
        return {
            **state,
            "user_objective": user_objective,
            "new_strategy": new_strategy,
            "objective_mapping_complete": True
        }


def strategy_validation_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """
    Node 3: Strategy Validation
    
    Validates selected strategy against role, facts, and constraints.
    Detects conflicts and generates explanations.
    
    Args:
        state: Current workflow state containing:
            - selected_strategy_id: ID of strategy to validate
            - role_context: User's role
            - workspace_snapshot: Case facts
            
    Returns:
        Updated state with validation_result
    """
    logger.info("✅ Strategy Validation: Checking strategy validity")
    
    selected_strategy_id = state.get("selected_strategy_id")
    suggested_strategies = state.get("suggested_strategies", [])
    new_strategy = state.get("new_strategy")
    role_context = state.get("role_context")
    workspace_snapshot = state.get("workspace_snapshot", {})
    workspace_id = state.get("workspace_id")
    
    # Find the strategy to validate
    strategy_to_validate = None
    
    if new_strategy:
        strategy_to_validate = new_strategy
    else:
        for s in suggested_strategies:
            if s.strategy_id == selected_strategy_id:
                strategy_to_validate = s
                break
    
    if not strategy_to_validate:
        error_msg = f"Strategy {selected_strategy_id} not found for validation"
        logger.error(f"❌ {error_msg}")
        return {
            **state,
            "validation_result": StrategyValidationResult(
                is_valid=False,
                conflicts=[error_msg]
            )
        }
    
    role = role_context.get("role") if isinstance(role_context, dict) else role_context.role
    
    logger.info(f"🔍 Validating strategy: {strategy_to_validate.strategy_id} ({strategy_to_validate.strategy_type})")
    
    conflicts = []
    warnings = []
    suggestions = []
    
    # Check 1: Role compatibility
    if not is_strategy_compatible_with_role(strategy_to_validate.strategy_type, role):
        conflicts.append(get_incompatibility_reason(strategy_to_validate.strategy_type, role))
    
    # Check 2: Preconditions
    for precondition in strategy_to_validate.preconditions:
        if not _check_precondition(precondition, workspace_snapshot):
            warnings.append(f"Precondition may not be met: {precondition}")
    
    # Check 3: Evidence requirements
    available_evidence = workspace_snapshot.get("evidence_files", [])
    for req_type in strategy_to_validate.requires_evidence_types:
        if not _has_evidence_type(req_type, available_evidence):
            warnings.append(f"Missing recommended evidence type: {req_type}")
    
    # Check 4: Contradictory strategies
    active_strategy = state.get("active_strategy")
    if active_strategy and active_strategy.strategy_id != strategy_to_validate.strategy_id:
        if _strategies_conflict(active_strategy, strategy_to_validate):
            suggestions.append(f"This strategy conflicts with current strategy: {active_strategy.title}")
    
    is_valid = len(conflicts) == 0
    
    validation_result = StrategyValidationResult(
        is_valid=is_valid,
        conflicts=conflicts,
        warnings=warnings,
        suggestions=suggestions,
        explanation=_generate_validation_explanation(strategy_to_validate, is_valid, conflicts, warnings)
    )
    
    if is_valid:
        logger.success(f"✅ Strategy validated successfully")
        strategy_to_validate.status = StrategyStatus.VALIDATED
    else:
        logger.error(f"❌ Strategy validation failed: {', '.join(conflicts)}")
        strategy_to_validate.status = StrategyStatus.REJECTED
    
    # Log audit event
    audit_logger.log_event(
        event_type="strategy_validated",
        data={
            "workspace_id": workspace_id,
            "strategy_id": strategy_to_validate.strategy_id,
            "is_valid": is_valid,
            "conflicts": conflicts,
            "warnings": warnings
        },
        workspace_id=workspace_id
    )
    
    return {
        **state,
        "validation_result": validation_result,
        "validated_strategy": strategy_to_validate if is_valid else None,
        "validation_complete": True
    }


def strategy_lock_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """
    Node 4: Strategy Lock
    
    Locks validated strategy as ACTIVE in workspace state.
    Triggers downstream reconfiguration.
    
    Args:
        state: Current workflow state containing:
            - validated_strategy: Validated strategy to lock
            - validation_result: Must be valid
            
    Returns:
        Updated state with active_strategy and reanalysis_trigger
    """
    logger.info("🔒 Strategy Lock: Activating strategy")
    
    validated_strategy = state.get("validated_strategy")
    validation_result = state.get("validation_result")
    workspace_id = state.get("workspace_id")
    
    if not validated_strategy:
        error_msg = "No validated strategy to lock"
        logger.error(f"❌ {error_msg}")
        return {**state, "lock_error": error_msg}
    
    if not validation_result or not validation_result.is_valid:
        error_msg = "Cannot lock invalid strategy"
        logger.error(f"❌ {error_msg}")
        return {**state, "lock_error": error_msg}
    
    # Supersede old strategy if exists
    old_strategy = state.get("active_strategy")
    if old_strategy:
        old_strategy.status = StrategyStatus.SUPERSEDED
        logger.info(f"📝 Superseded previous strategy: {old_strategy.strategy_id}")
    
    # Activate new strategy
    validated_strategy.status = StrategyStatus.ACTIVE
    
    logger.success(f"✅ Strategy locked: {validated_strategy.strategy_id} - {validated_strategy.title}")
    
    # Trigger downstream reconfiguration
    downstream_trigger = {
        "trigger_type": "STRATEGY_CHANGE",
        "strategy_id": validated_strategy.strategy_id,
        "strategy_type": validated_strategy.strategy_type,
        "downstream_impact": validated_strategy.downstream_impact,
        "requires_reanalysis": True
    }
    
    # Log audit event
    audit_logger.log_event(
        event_type="strategy_locked",
        data={
            "workspace_id": workspace_id,
            "strategy_id": validated_strategy.strategy_id,
            "strategy_type": validated_strategy.strategy_type,
            "title": validated_strategy.title,
            "superseded_strategy": old_strategy.strategy_id if old_strategy else None
        },
        workspace_id=workspace_id
    )
    
    return {
        **state,
        "active_strategy": validated_strategy,
        "strategy_locked": True,
        "downstream_trigger": downstream_trigger,
        "requires_reanalysis": True
    }


# ==================== HELPER FUNCTIONS ====================

def _generate_petitioner_strategies(snapshot: Dict[str, Any], workspace_id: str) -> List[StrategyOption]:
    """Generate strategies for PETITIONER role."""
    from datetime import datetime
    
    strategies = [
        StrategyOption(
            strategy_id=f"STR-{workspace_id}-001",
            strategy_type="PROSECUTION_OR_RELIEF",
            title="Pursue Legal Relief/Remedies",
            description="Initiate proceedings to seek legal remedies based on available evidence",
            rationale="Documents show potential grounds for relief. Proactive approach maximizes chances.",
            preconditions=["Verify jurisdiction", "Confirm statute of limitations"],
            risks=["Counter-claims possible", "Evidence may be challenged"],
            downstream_impact={
                "ApplicableLaws": "Prioritize relief-granting provisions",
                "Drafting": "Enable petition/complaint templates",
                "Timeline": "Calculate procedural deadlines"
            },
            relevance_score=0.9,
            compatible_roles=[RoleEnum.PETITIONER],
            requires_evidence_types=["supporting_documents", "proof_of_claim"],
            estimated_complexity="HIGH",
            created_at=datetime.utcnow()
        ),
        StrategyOption(
            strategy_id=f"STR-{workspace_id}-002",
            strategy_type="EVIDENCE_STRENGTHENING",
            title="Strengthen Evidentiary Foundation",
            description="Focus on building stronger documentary and factual evidence before proceeding",
            rationale="Current evidence may benefit from additional supporting documentation",
            preconditions=["Identify evidence gaps", "Confirm evidence admissibility"],
            risks=["Delay in proceedings", "Statute limitations pressure"],
            downstream_impact={
                "Evidence": "Identify gaps and requirements",
                "Timeline": "Extended preparation phase"
            },
            relevance_score=0.7,
            compatible_roles=[RoleEnum.PETITIONER, RoleEnum.RESPONDENT],
            requires_evidence_types=["all_available"],
            estimated_complexity="MEDIUM",
            created_at=datetime.utcnow()
        )
    ]
    
    return strategies


def _generate_respondent_strategies(snapshot: Dict[str, Any], workspace_id: str) -> List[StrategyOption]:
    """Generate strategies for RESPONDENT role."""
    from datetime import datetime
    
    strategies = [
        StrategyOption(
            strategy_id=f"STR-{workspace_id}-101",
            strategy_type="DEFENSIVE_DISMISSAL",
            title="Seek Dismissal on Procedural/Merit Grounds",
            description="Challenge the claim based on procedural defects or lack of merit",
            rationale="Defensive strategy to minimize exposure and terminate proceedings early",
            preconditions=["Identify procedural violations", "Assess merit deficiencies"],
            risks=["May strengthen opponent's case if unsuccessful"],
            downstream_impact={
                "ApplicableLaws": "Focus on dismissal provisions",
                "Drafting": "Enable dismissal motion templates"
            },
            relevance_score=0.85,
            compatible_roles=[RoleEnum.RESPONDENT],
            requires_evidence_types=["procedural_records"],
            estimated_complexity="HIGH",
            created_at=datetime.utcnow()
        ),
        StrategyOption(
            strategy_id=f"STR-{workspace_id}-102",
            strategy_type="NEGOTIATED_SETTLEMENT",
            title="Explore Settlement Options",
            description="Pursue negotiated resolution to minimize costs and exposure",
            rationale="Settlement may reduce legal costs and provide certainty",
            preconditions=["Assess settlement value range", "Identify compromise areas"],
            risks=["May appear weak", "Settlement costs"],
            downstream_impact={
                "Negotiation": "Enable settlement framework",
                "Timeline": "Flexible based on negotiations"
            },
            relevance_score=0.75,
            compatible_roles=[RoleEnum.RESPONDENT, RoleEnum.PETITIONER, RoleEnum.MEDIATOR],
            requires_evidence_types=["valuation_documents"],
            estimated_complexity="MEDIUM",
            created_at=datetime.utcnow()
        )
    ]
    
    return strategies


def _generate_mediator_strategies(snapshot: Dict[str, Any], workspace_id: str) -> List[StrategyOption]:
    """Generate strategies for MEDIATOR role."""
    from datetime import datetime
    
    strategies = [
        StrategyOption(
            strategy_id=f"STR-{workspace_id}-201",
            strategy_type="NEGOTIATED_SETTLEMENT",
            title="Facilitate Mutual Resolution",
            description="Guide parties toward mutually acceptable resolution",
            rationale="Neutral facilitation can preserve relationships and reduce costs",
            preconditions=["Both parties willing to negotiate", "Common ground exists"],
            risks=["Negotiations may fail", "Power imbalance issues"],
            downstream_impact={
                "Communication": "Enable neutral facilitation mode",
                "Documentation": "Track compromise proposals"
            },
            relevance_score=0.9,
            compatible_roles=[RoleEnum.MEDIATOR],
            requires_evidence_types=["all_party_positions"],
            estimated_complexity="MEDIUM",
            created_at=datetime.utcnow()
        )
    ]
    
    return strategies


def _generate_adjudicator_strategies(snapshot: Dict[str, Any], workspace_id: str) -> List[StrategyOption]:
    """Generate strategies for ADJUDICATOR role."""
    from datetime import datetime
    
    strategies = [
        StrategyOption(
            strategy_id=f"STR-{workspace_id}-301",
            strategy_type="FACTUAL_CLARIFICATION",
            title="Clarify Factual Issues",
            description="Identify and clarify disputed factual matters objectively",
            rationale="Clear factual record is essential for sound adjudication",
            preconditions=["Access to all evidence", "Parties have presented positions"],
            risks=["None - neutral analytical approach"],
            downstream_impact={
                "Analysis": "Focus on fact-finding",
                "Timeline": "May require additional hearings"
            },
            relevance_score=0.85,
            compatible_roles=[RoleEnum.ADJUDICATOR, RoleEnum.MEDIATOR],
            requires_evidence_types=["all_evidence"],
            estimated_complexity="MEDIUM",
            created_at=datetime.utcnow()
        )
    ]
    
    return strategies


def _calculate_objective_strategy_match(objective_text: str, strategy: StrategyOption) -> float:
    """Calculate match score between user objective and strategy (simple keyword matching)."""
    objective_lower = objective_text.lower()
    
    # Check for keywords in strategy title and description
    keywords = [
        strategy.title.lower(),
        strategy.description.lower(),
        strategy.strategy_type.lower()
    ]
    
    matches = sum(1 for keyword in keywords if keyword in objective_lower)
    return min(matches / len(keywords), 1.0)


def _derive_strategy_from_objective(objective: UserObjective, role: Role, 
                                    workspace_id: str, sequence: int) -> StrategyOption:
    """Derive a new strategy from user objective."""
    from datetime import datetime
    
    # Simple heuristic-based strategy derivation
    # In production, this should use LLM
    objective_lower = objective.objective_text.lower()
    
    if "dismiss" in objective_lower or "defend" in objective_lower:
        strategy_type = "DEFENSIVE_DISMISSAL"
    elif "settle" in objective_lower or "negotiate" in objective_lower:
        strategy_type = "NEGOTIATED_SETTLEMENT"
    elif "evidence" in objective_lower or "strengthen" in objective_lower:
        strategy_type = "EVIDENCE_STRENGTHENING"
    elif "clarify" in objective_lower or "facts" in objective_lower:
        strategy_type = "FACTUAL_CLARIFICATION"
    else:
        # Default based on role
        # Convert role to RoleEnum if it's a string
        role_enum = RoleEnum(role) if isinstance(role, str) else role
        if role_enum == RoleEnum.PETITIONER:
            strategy_type = "PROSECUTION_OR_RELIEF"
        elif role_enum == RoleEnum.RESPONDENT:
            strategy_type = "DEFENSIVE_DISMISSAL"
        else:
            strategy_type = "FACTUAL_CLARIFICATION"
    
    return StrategyOption(
        strategy_id=f"STR-{workspace_id}-USR-{sequence:03d}",
        strategy_type=strategy_type,
        title=f"User-Defined: {objective.objective_text[:50]}",
        description=objective.objective_text,
        rationale="Strategy derived from user's stated objective",
        preconditions=objective.constraints,
        risks=["User-defined strategy may need refinement"],
        downstream_impact={"Custom": "User-defined strategy"},
        relevance_score=1.0,  # User's choice gets highest score
        compatible_roles=[role],
        estimated_complexity="MEDIUM",
        created_at=datetime.utcnow()
    )


def _check_precondition(precondition: str, snapshot: Dict[str, Any]) -> bool:
    """Check if a precondition is met (simplified check)."""
    # In production, this should be more sophisticated
    return True  # Optimistic assumption


def _has_evidence_type(evidence_type: str, available_evidence: List[Any]) -> bool:
    """Check if required evidence type is available."""
    # In production, check actual evidence metadata
    return len(available_evidence) > 0


def _strategies_conflict(strategy1: StrategyOption, strategy2: StrategyOption) -> bool:
    """Check if two strategies conflict."""
    # Simplified conflict detection
    conflicting_pairs = [
        ("PROSECUTION_OR_RELIEF", "DEFENSIVE_DISMISSAL"),
        ("DEFENSIVE_DISMISSAL", "PROSECUTION_OR_RELIEF")
    ]
    
    return (strategy1.strategy_type, strategy2.strategy_type) in conflicting_pairs


def _generate_validation_explanation(strategy: StrategyOption, is_valid: bool, 
                                     conflicts: List[str], warnings: List[str]) -> str:
    """Generate human-readable validation explanation."""
    if is_valid:
        if warnings:
            return f"Strategy '{strategy.title}' is valid but has {len(warnings)} warnings to consider."
        return f"Strategy '{strategy.title}' is fully validated and ready to activate."
    else:
        return f"Strategy '{strategy.title}' cannot be used: {'; '.join(conflicts)}"

