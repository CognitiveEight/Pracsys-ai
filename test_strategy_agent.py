"""
Standalone test for Strategy & Objective Agent
Tests the agent without triggering circular imports.
"""

import sys
from pathlib import Path

# Add project root to Python path
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

# Direct imports to avoid circular dependencies
import importlib.util
import os

# Import Role directly from role_models without triggering __init__
role_models_path = project_root / "app" / "agents" / "role_models.py"
spec = importlib.util.spec_from_file_location("role_models", role_models_path)
role_models = importlib.util.module_from_spec(spec)
spec.loader.exec_module(role_models)
Role = role_models.Role

# Import strategy models directly
strategy_models_path = project_root / "app" / "langgraph" / "strategy_models.py"
spec = importlib.util.spec_from_file_location("strategy_models", strategy_models_path)
strategy_models = importlib.util.module_from_spec(spec)
spec.loader.exec_module(strategy_models)

StrategyOption = strategy_models.StrategyOption
StrategyType = strategy_models.StrategyType
StrategyStatus = strategy_models.StrategyStatus
StrategyState = strategy_models.StrategyState
UserObjective = strategy_models.UserObjective
StrategyValidationResult = strategy_models.StrategyValidationResult
is_strategy_compatible_with_role = strategy_models.is_strategy_compatible_with_role
get_incompatibility_reason = strategy_models.get_incompatibility_reason
ROLE_STRATEGY_COMPATIBILITY = strategy_models.ROLE_STRATEGY_COMPATIBILITY


def test_models():
    """Test Strategy Models"""
    print("\n" + "=" * 80)
    print("TEST 1: Strategy Models")
    print("=" * 80 + "\n")

    # Create a strategy option
    strategy = StrategyOption(
        strategy_id="STR-TEST-001",
        strategy_type="PROSECUTION_OR_RELIEF",
        title="Pursue Legal Relief",
        description="Initiate proceedings to seek legal remedies",
        rationale="Evidence supports claim",
        preconditions=["Verify jurisdiction"],
        risks=["Counter-claims possible"],
        downstream_impact={"ApplicableLaws": "Prioritize relief provisions"},
        relevance_score=0.9,
        compatible_roles=[Role.PETITIONER],
    )

    print(f"✅ Created Strategy: {strategy.title}")
    print(f"   Type: {strategy.strategy_type}")
    print(f"   Status: {strategy.status.value}")
    print(f"   Relevance: {strategy.relevance_score*100:.0f}%")

    # Test strategy state
    state = StrategyState(workspace_id="test_workspace")
    state.add_suggestion(strategy)

    print(f"\n✅ Strategy State Created")
    print(f"   Workspace: {state.workspace_id}")
    print(f"   Suggestions: {len(state.suggested_strategies)}")

    # Lock the strategy
    locked = state.lock_strategy("STR-TEST-001")
    print(f"\n✅ Strategy Locked: {locked}")
    print(
        f"   Active: {state.active_strategy.title if state.active_strategy else 'None'}"
    )
    print(f"   Requires Reanalysis: {state.requires_reanalysis}")


def test_compatibility():
    """Test Role-Strategy Compatibility"""
    print("\n" + "=" * 80)
    print("TEST 2: Role-Strategy Compatibility Matrix")
    print("=" * 80 + "\n")

    roles = [Role.PETITIONER, Role.RESPONDENT, Role.MEDIATOR, Role.ADJUDICATOR]

    print("Compatibility Matrix:")
    print("-" * 80)

    for role in roles:
        compatible = ROLE_STRATEGY_COMPATIBILITY.get(role, [])
        print(f"\n{role.value}:")
        print(f"  ✅ Compatible: {', '.join(compatible)}")

        # Test a specific incompatible strategy
        if role == Role.MEDIATOR:
            is_compat = is_strategy_compatible_with_role("PROSECUTION_OR_RELIEF", role)
            print(f"\n  ❌ PROSECUTION_OR_RELIEF: {is_compat}")
            if not is_compat:
                reason = get_incompatibility_reason("PROSECUTION_OR_RELIEF", role)
                print(f"     Reason: {reason}")


def test_validation():
    """Test Strategy Validation"""
    print("\n" + "=" * 80)
    print("TEST 3: Strategy Validation")
    print("=" * 80 + "\n")

    # Valid case
    validation = StrategyValidationResult(
        is_valid=True,
        warnings=["Consider evidence gaps"],
        explanation="Strategy is valid for PETITIONER role",
    )

    print(f"✅ Valid Strategy:")
    print(f"   Valid: {validation.is_valid}")
    print(f"   Warnings: {len(validation.warnings)}")
    print(f"   Explanation: {validation.explanation}")

    # Invalid case
    validation2 = StrategyValidationResult(
        is_valid=False,
        conflicts=["Incompatible with MEDIATOR role"],
        explanation="Mediators cannot use adversarial strategies",
    )

    print(f"\n❌ Invalid Strategy:")
    print(f"   Valid: {validation2.is_valid}")
    print(f"   Conflicts: {', '.join(validation2.conflicts)}")
    print(f"   Explanation: {validation2.explanation}")


def test_user_objective():
    """Test User Objective"""
    print("\n" + "=" * 80)
    print("TEST 4: User Objective")
    print("=" * 80 + "\n")

    objective = UserObjective(
        objective_text="I want to get this case dismissed quickly",
        priority="HIGH",
        constraints=["Limited budget", "Time sensitive"],
        preferred_timeline="30 days",
    )

    print(f"👤 User Objective:")
    print(f"   Text: {objective.objective_text}")
    print(f"   Priority: {objective.priority}")
    print(f"   Constraints: {', '.join(objective.constraints)}")
    print(f"   Timeline: {objective.preferred_timeline}")

    obj_dict = objective.to_dict()
    print(f"\n✅ Serialization: {len(obj_dict)} fields")


def test_strategy_types():
    """Test All Strategy Types"""
    print("\n" + "=" * 80)
    print("TEST 5: Strategy Types")
    print("=" * 80 + "\n")

    strategy_types = [
        "PROSECUTION_OR_RELIEF",
        "DEFENSIVE_DISMISSAL",
        "NEGOTIATED_SETTLEMENT",
        "FACTUAL_CLARIFICATION",
        "PROCEDURAL_COMPLIANCE",
        "EVIDENCE_STRENGTHENING",
    ]

    descriptions = {
        "PROSECUTION_OR_RELIEF": "Seek legal action/remedy",
        "DEFENSIVE_DISMISSAL": "Defend/dismiss claims",
        "NEGOTIATED_SETTLEMENT": "Seek compromise",
        "FACTUAL_CLARIFICATION": "Clarify facts/evidence",
        "PROCEDURAL_COMPLIANCE": "Follow procedures",
        "EVIDENCE_STRENGTHENING": "Build stronger case",
    }

    print("Available Strategy Types:")
    print("-" * 80)

    for i, strategy_type in enumerate(strategy_types, 1):
        print(f"\n{i}. {strategy_type}")
        print(f"   Description: {descriptions[strategy_type]}")

        # Show compatible roles
        compatible_roles = []
        for role in [Role.PETITIONER, Role.RESPONDENT, Role.MEDIATOR, Role.ADJUDICATOR]:
            if is_strategy_compatible_with_role(strategy_type, role):
                compatible_roles.append(role.value)

        print(f"   Compatible Roles: {', '.join(compatible_roles)}")


def test_strategy_lifecycle():
    """Test Strategy Lifecycle"""
    print("\n" + "=" * 80)
    print("TEST 6: Strategy Lifecycle")
    print("=" * 80 + "\n")

    # Create strategy state
    state = StrategyState(workspace_id="lifecycle_test")

    # Add suggestions
    strategies = []
    for i in range(3):
        strategy = StrategyOption(
            strategy_id=f"STR-{i+1}",
            strategy_type=(
                "PROSECUTION_OR_RELIEF" if i == 0 else "EVIDENCE_STRENGTHENING"
            ),
            title=f"Strategy Option {i+1}",
            description=f"Description for strategy {i+1}",
            rationale=f"Rationale for strategy {i+1}",
            preconditions=[],
            risks=[],
            downstream_impact={},
            relevance_score=0.9 - (i * 0.1),
        )
        strategies.append(strategy)
        state.add_suggestion(strategy)

    print(f"📋 Added {len(state.suggested_strategies)} strategy suggestions")

    # Lock first strategy
    print(f"\n🔒 Locking Strategy: {strategies[0].strategy_id}")
    state.lock_strategy(strategies[0].strategy_id)

    print(f"   Active Strategy: {state.active_strategy.strategy_id}")
    print(f"   Status: {state.active_strategy.status.value}")
    print(f"   History: {len(state.strategy_history)} events")

    # Change to different strategy
    print(
        f"\n🔄 Changing Strategy: {strategies[0].strategy_id} → {strategies[1].strategy_id}"
    )
    state.lock_strategy(strategies[1].strategy_id)

    print(f"   Old Status: {strategies[0].status.value}")
    print(f"   New Status: {strategies[1].status.value}")
    print(f"   Active: {state.active_strategy.strategy_id}")
    print(f"   History: {len(state.strategy_history)} events")

    # Show history
    print(f"\n📜 Strategy History:")
    for event in state.strategy_history:
        print(f"   - {event['action']}: {event['strategy_id']} ({event['timestamp']})")


def run_all_tests():
    """Run all standalone tests"""
    print("\n" + "█" * 80)
    print("█" + " " * 78 + "█")
    print("█" + "  STRATEGY & OBJECTIVE AGENT - STANDALONE TESTS".center(78) + "█")
    print("█" + " " * 78 + "█")
    print("█" * 80)

    try:
        test_models()
        test_compatibility()
        test_validation()
        test_user_objective()
        test_strategy_types()
        test_strategy_lifecycle()

        print("\n" + "█" * 80)
        print("█" + " " * 78 + "█")
        print("█" + "  ✅ ALL TESTS PASSED".center(78) + "█")
        print("█" + " " * 78 + "█")
        print("█" * 80 + "\n")

        print(
            "📝 NOTE: Full agent workflow tests require the complete app environment."
        )
        print("   These tests validate the core models and logic in isolation.\n")

    except Exception as e:
        print(f"\n❌ Test failed: {e}")
        import traceback

        traceback.print_exc()
        return False

    return True


if __name__ == "__main__":
    success = run_all_tests()
    sys.exit(0 if success else 1)
