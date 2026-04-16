"""
Strategy & Objective Agent - Example Usage
Demonstrates how to use the Strategy Agent in different scenarios.
"""

import sys
from pathlib import Path

# Add project root to Python path
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

import asyncio

from loguru import logger

from app.agents.role_models import Role
from app.langgraph.role_agent import role_agent
from app.langgraph.strategy_agent import strategy_agent

# ==================== EXAMPLE 1: Suggest Strategies ====================


async def example_1_suggest_strategies():
    """
    Example 1: Generate strategy suggestions for a workspace

    This is typically the first step after role selection.
    """
    print("\n" + "=" * 80)
    print("EXAMPLE 1: Suggest Strategies Based on Role & Context")
    print("=" * 80 + "\n")

    workspace_id = "workspace_001"
    user_id = "user_123"

    # Step 1: Set role (prerequisite)
    logger.info("Step 1: Setting role to PETITIONER")
    role_result = await role_agent.invoke(
        workspace_id=workspace_id,
        user_id=user_id,
        input_data={"action": "select_role", "role": "PETITIONER"},
    )

    role_context = role_result.get("role_context")
    print(f"✅ Role set: {role_context['role']}")

    # Step 2: Suggest strategies
    logger.info("Step 2: Generating strategy suggestions")

    workspace_snapshot = {
        "evidence_files": [
            {"filename": "contract.pdf", "type": "legal_document"},
            {"filename": "email_chain.txt", "type": "correspondence"},
        ],
        "facts": ["Contract breach occurred", "Damages estimated at $50,000"],
        "parties": ["Petitioner: ABC Corp", "Respondent: XYZ Inc"],
    }

    result = strategy_agent.suggest_strategies(
        workspace_id=workspace_id,
        role_context=role_context,
        workspace_snapshot=workspace_snapshot,
    )

    print(f"\n📋 Suggested {len(result['suggested_strategies'])} strategies:\n")

    for i, strategy in enumerate(result["suggested_strategies"], 1):
        print(f"{i}. {strategy.title}")
        print(f"   Type: {strategy.strategy_type}")
        print(f"   Relevance: {strategy.relevance_score*100:.0f}%")
        print(f"   Description: {strategy.description}")
        print(f"   Risks: {', '.join(strategy.risks)}")
        print()

    return result["suggested_strategies"]


# ==================== EXAMPLE 2: Select Strategy ====================


async def example_2_select_strategy():
    """
    Example 2: User selects a suggested strategy

    This validates and locks the selected strategy.
    """
    print("\n" + "=" * 80)
    print("EXAMPLE 2: Select and Lock a Strategy")
    print("=" * 80 + "\n")

    # First get suggestions
    suggested_strategies = await example_1_suggest_strategies()

    # User selects first strategy
    selected_strategy = suggested_strategies[0]
    print(f"\n👤 User selected: {selected_strategy.title}\n")

    workspace_id = "workspace_001"
    user_id = "user_123"

    # Get role context
    role_result = await role_agent.invoke(
        workspace_id=workspace_id, user_id=user_id, input_data={"action": "get_role"}
    )
    role_context = role_result.get("role_context")

    workspace_snapshot = {
        "evidence_files": [{"filename": "contract.pdf"}],
        "facts": ["Contract breach occurred"],
    }

    # Lock the strategy
    result = strategy_agent.select_and_lock_strategy(
        workspace_id=workspace_id,
        role_context=role_context,
        workspace_snapshot=workspace_snapshot,
        strategy_id=selected_strategy.strategy_id,
        suggested_strategies=suggested_strategies,
    )

    if result.get("active_strategy"):
        print(f"✅ Strategy Locked: {result['active_strategy'].title}")
        print(f"\n🔄 Downstream Impact:")
        for key, value in result["active_strategy"].downstream_impact.items():
            print(f"   • {key}: {value}")

        if result.get("requires_reanalysis"):
            print(f"\n⚠️  All downstream agents will reconfigure based on this strategy")
    else:
        print(f"❌ Failed to lock strategy: {result.get('error')}")

    return result


# ==================== EXAMPLE 3: Strategy from User Objective ====================


async def example_3_strategy_from_objective():
    """
    Example 3: Generate strategy from user's stated objective

    This maps user intent to a strategy (existing or new).
    """
    print("\n" + "=" * 80)
    print("EXAMPLE 3: Create Strategy from User Objective")
    print("=" * 80 + "\n")

    workspace_id = "workspace_002"
    user_id = "user_456"

    # Set role to RESPONDENT
    logger.info("Setting role to RESPONDENT")
    role_result = await role_agent.invoke(
        workspace_id=workspace_id,
        user_id=user_id,
        input_data={"action": "select_role", "role": "RESPONDENT"},
    )
    role_context = role_result.get("role_context")

    # User states their objective
    user_objective = (
        "I want to get this case dismissed on procedural grounds as quickly as possible"
    )
    print(f"👤 User Objective: '{user_objective}'\n")

    workspace_snapshot = {
        "evidence_files": [{"filename": "notice.pdf"}],
        "facts": ["Notice served improperly", "Jurisdictional issues"],
    }

    # Generate strategy from objective
    result = strategy_agent.strategy_from_objective(
        workspace_id=workspace_id,
        role_context=role_context,
        workspace_snapshot=workspace_snapshot,
        user_objective=user_objective,
    )

    if result.get("active_strategy"):
        strategy = result["active_strategy"]
        print(f"✅ Strategy Created and Locked:")
        print(f"   Title: {strategy.title}")
        print(f"   Type: {strategy.strategy_type}")
        print(f"   Rationale: {strategy.rationale}")

        if result.get("mapped_strategy_id"):
            print(f"\n📌 Mapped to existing strategy: {result['mapped_strategy_id']}")
        elif result.get("new_strategy"):
            print(f"\n🆕 Created new strategy from user objective")
    else:
        print(f"❌ Error: {result.get('error')}")

    return result


# ==================== EXAMPLE 4: Change Strategy ====================


async def example_4_change_strategy():
    """
    Example 4: Change from one strategy to another

    This supersedes the old strategy and triggers reconfiguration.
    """
    print("\n" + "=" * 80)
    print("EXAMPLE 4: Change Active Strategy")
    print("=" * 80 + "\n")

    # First set initial strategy
    initial_result = await example_2_select_strategy()
    old_strategy = initial_result["active_strategy"]

    print(f"\n🔄 User wants to change strategy...")
    print(f"   Current: {old_strategy.title}")

    # Get suggestions again
    workspace_id = "workspace_001"
    user_id = "user_123"

    role_result = await role_agent.invoke(
        workspace_id=workspace_id, user_id=user_id, input_data={"action": "get_role"}
    )
    role_context = role_result.get("role_context")

    workspace_snapshot = {"evidence_files": [], "facts": []}

    suggestions_result = strategy_agent.suggest_strategies(
        workspace_id=workspace_id,
        role_context=role_context,
        workspace_snapshot=workspace_snapshot,
    )

    # Select different strategy
    new_strategy = suggestions_result["suggested_strategies"][1]  # Pick 2nd option
    print(f"   New: {new_strategy.title}\n")

    # Change strategy
    result = strategy_agent.change_strategy(
        workspace_id=workspace_id,
        old_strategy_id=old_strategy.strategy_id,
        new_strategy_id=new_strategy.strategy_id,
        role_context=role_context,
        workspace_snapshot=workspace_snapshot,
        suggested_strategies=suggestions_result["suggested_strategies"],
    )

    if result.get("active_strategy"):
        print(f"✅ Strategy Changed Successfully")
        print(f"   Old: {old_strategy.strategy_id} (SUPERSEDED)")
        print(f"   New: {result['active_strategy'].strategy_id} (ACTIVE)")
        print(f"\n⚠️  Full re-analysis triggered for all downstream agents")
    else:
        print(f"❌ Failed to change strategy: {result.get('error')}")

    return result


# ==================== EXAMPLE 5: Role-Strategy Validation ====================


async def example_5_role_strategy_validation():
    """
    Example 5: Demonstrate role-strategy compatibility validation

    Shows what happens when incompatible strategy is attempted.
    """
    print("\n" + "=" * 80)
    print("EXAMPLE 5: Role-Strategy Compatibility Validation")
    print("=" * 80 + "\n")

    workspace_id = "workspace_003"
    user_id = "user_789"

    # Set role to MEDIATOR (neutral role)
    logger.info("Setting role to MEDIATOR")
    role_result = await role_agent.invoke(
        workspace_id=workspace_id,
        user_id=user_id,
        input_data={"action": "select_role", "role": "MEDIATOR"},
    )
    role_context = role_result.get("role_context")
    print(f"✅ Role: MEDIATOR (neutral facilitation)\n")

    # Try to use adversarial objective (should fail)
    adversarial_objective = "I want to aggressively pursue maximum damages"
    print(f"❌ Attempting incompatible objective: '{adversarial_objective}'\n")

    workspace_snapshot = {"evidence_files": [], "facts": []}

    result = strategy_agent.strategy_from_objective(
        workspace_id=workspace_id,
        role_context=role_context,
        workspace_snapshot=workspace_snapshot,
        user_objective=adversarial_objective,
    )

    if result.get("error"):
        print(f"🚫 Validation Failed (as expected):")
        print(f"   Error: {result['error']}")
        print(
            f"\n💡 Explanation: Mediators must remain neutral and cannot pursue adversarial strategies"
        )

    # Now try compatible objective
    print(f"\n✅ Attempting compatible objective:\n")
    compatible_objective = (
        "I want to help both parties find a mutually acceptable resolution"
    )
    print(f"   '{compatible_objective}'\n")

    result2 = strategy_agent.strategy_from_objective(
        workspace_id=workspace_id,
        role_context=role_context,
        workspace_snapshot=workspace_snapshot,
        user_objective=compatible_objective,
    )

    if result2.get("active_strategy"):
        print(f"✅ Strategy Validated and Locked:")
        print(f"   {result2['active_strategy'].title}")
        print(f"   Type: {result2['active_strategy'].strategy_type}")

    return result2


# ==================== RUN ALL EXAMPLES ====================


async def run_all_examples():
    """Run all strategy agent examples."""
    print("\n" + "█" * 80)
    print("█" + " " * 78 + "█")
    print("█" + "  STRATEGY & OBJECTIVE AGENT - COMPLETE EXAMPLES".center(78) + "█")
    print("█" + " " * 78 + "█")
    print("█" * 80)

    try:
        await example_1_suggest_strategies()
        await example_2_select_strategy()
        await example_3_strategy_from_objective()
        await example_4_change_strategy()
        await example_5_role_strategy_validation()

        print("\n" + "█" * 80)
        print("█" + " " * 78 + "█")
        print("█" + "  ✅ ALL EXAMPLES COMPLETED SUCCESSFULLY".center(78) + "█")
        print("█" + " " * 78 + "█")
        print("█" * 80 + "\n")

    except Exception as e:
        logger.error(f"Example failed: {e}")
        raise


if __name__ == "__main__":
    asyncio.run(run_all_examples())
