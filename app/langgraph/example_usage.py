"""
Example usage of the Role-Based Agent system.

This demonstrates how to use the role agent in various scenarios.
"""
import asyncio
from loguru import logger

from app.langgraph.role_agent import role_agent
from app.langgraph.state import create_initial_workspace_state
from app.langgraph.integration import workspace_role_integration
from app.langgraph.guards import require_role, RoleGuard
from app.agents.role_models import RoleEnum


# ============================================================================
# EXAMPLE 1: Basic Role Selection
# ============================================================================

async def example_basic_role_selection():
    """Example: Basic role selection flow."""
    print("\n" + "="*60)
    print("EXAMPLE 1: Basic Role Selection")
    print("="*60 + "\n")
    
    workspace_id = "workspace_123"
    user_id = "user_456"
    
    # Compile the agent
    role_agent.compile()
    
    # Try to send a message without role selection
    print("1. Attempting operation without role...")
    state = await role_agent.invoke(
        workspace_id=workspace_id,
        user_id=user_id,
        user_input="Analyze this case"
    )
    
    print(f"Output: {state['agent_output']}")
    print(f"Blocked: {state['metadata'].get('role_gate_blocked', False)}")
    
    # Select a role
    print("\n2. Selecting role: PETITIONER...")
    state = await role_agent.invoke(
        workspace_id=workspace_id,
        user_id=user_id,
        user_input="PETITIONER",
        existing_state=state
    )
    
    print(f"Output: {state['agent_output']}")
    print(f"Role: {state['role_state']['role']}")
    print(f"Validated: {state['role_state']['role_validated']}")


# ============================================================================
# EXAMPLE 2: Role Change and Re-analysis
# ============================================================================

async def example_role_change():
    """Example: Changing role triggers re-analysis."""
    print("\n" + "="*60)
    print("EXAMPLE 2: Role Change and Re-analysis")
    print("="*60 + "\n")
    
    workspace_id = "workspace_789"
    user_id = "user_101"
    
    # Initialize with role
    print("1. Initializing with ADJUDICATOR role...")
    state = await workspace_role_integration.initialize_workspace_with_role(
        workspace_id=workspace_id,
        user_id=user_id,
        initial_role="ADJUDICATOR"
    )
    
    print(f"Initial role: {state['role_state']['role']}")
    
    # Change role
    print("\n2. Changing role to RESPONDENT...")
    state = await workspace_role_integration.handle_role_change(
        workspace_id=workspace_id,
        user_id=user_id,
        new_role="RESPONDENT",
        state=state
    )
    
    print(f"New role: {state['role_state']['role']}")
    print(f"Requires reanalysis: {state['metadata'].get('requires_reanalysis', False)}")
    print(f"Intelligence invalidated: {state['metadata'].get('intelligence_invalidated', False)}")


# ============================================================================
# EXAMPLE 3: Using Role Guards in Downstream Agents
# ============================================================================

class ExampleLegalAnalysisAgent:
    """Example downstream agent using role guards."""
    
    @require_role("legal_analysis")
    async def analyze_case(self, state):
        """
        Analyze case - PROTECTED by role guard.
        This will only run if role is selected.
        """
        role_context = state["role_state"]["role_context"]
        role = state["role_state"]["role"]
        
        print(f"\n✅ Legal Analysis Agent running with role: {role}")
        print(f"   Reasoning style: {role_context['reasoning_style']}")
        print(f"   Framing rules: {len(role_context['framing_rules'])} rules")
        
        return f"Analysis complete with {role} perspective"


async def example_role_guards():
    """Example: Using role guards in downstream agents."""
    print("\n" + "="*60)
    print("EXAMPLE 3: Role Guards in Downstream Agents")
    print("="*60 + "\n")
    
    workspace_id = "workspace_456"
    user_id = "user_789"
    
    # Create agent
    analysis_agent = ExampleLegalAnalysisAgent()
    
    # Initialize without role
    print("1. Attempting analysis without role...")
    state = create_initial_workspace_state(workspace_id, user_id)
    
    try:
        await analysis_agent.analyze_case(state)
    except Exception as e:
        print(f"❌ Blocked: {str(e)}")
    
    # Initialize with role
    print("\n2. Selecting role and attempting analysis...")
    state = await workspace_role_integration.initialize_workspace_with_role(
        workspace_id=workspace_id,
        user_id=user_id,
        initial_role="PETITIONER"
    )
    
    # Now it will work
    result = await analysis_agent.analyze_case(state)
    print(f"\nResult: {result}")


# ============================================================================
# EXAMPLE 4: Role Context for Different Roles
# ============================================================================

async def example_role_contexts():
    """Example: Different role contexts."""
    print("\n" + "="*60)
    print("EXAMPLE 4: Role Contexts for Different Roles")
    print("="*60 + "\n")
    
    from app.agents.role_models import RoleBehaviorContract
    
    for role in RoleEnum.list_roles():
        print(f"\n{'='*50}")
        print(f"ROLE: {role}")
        print('='*50)
        
        context = RoleBehaviorContract.get_context(role)
        
        print(f"\nReasoning Style: {context.reasoning_style}")
        print(f"Allowed Bias: {context.allowed_bias}")
        
        print("\nFraming Rules:")
        for rule in context.framing_rules:
            print(f"  • {rule}")
        
        print("\nForbidden Actions:")
        for action in context.forbidden_actions:
            print(f"  ✗ {action}")


# ============================================================================
# EXAMPLE 5: Complete Workflow
# ============================================================================

async def example_complete_workflow():
    """Example: Complete workflow from initialization to analysis."""
    print("\n" + "="*60)
    print("EXAMPLE 5: Complete Workflow")
    print("="*60 + "\n")
    
    workspace_id = "workspace_complete"
    user_id = "user_complete"
    
    # Step 1: Initialize workspace
    print("Step 1: Initialize workspace")
    state = create_initial_workspace_state(workspace_id, user_id)
    print(f"✓ Workspace initialized: {workspace_id}")
    
    # Step 2: User sends message without role
    print("\nStep 2: User sends message without role")
    state = await workspace_role_integration.process_user_message(
        workspace_id=workspace_id,
        user_id=user_id,
        message="Can you analyze my case?",
        state=state
    )
    print(f"Response: {state['agent_output']}")
    
    # Step 3: User selects role
    print("\nStep 3: User selects role")
    state = await workspace_role_integration.process_user_message(
        workspace_id=workspace_id,
        user_id=user_id,
        message="MEDIATOR",
        state=state
    )
    print(f"Response: {state['agent_output']}")
    print(f"Role set: {state['role_state']['role']}")
    
    # Step 4: Now downstream agents can run
    print("\nStep 4: Downstream agents can now run")
    print("✓ Role gate passed")
    print("✓ Role context available")
    print("✓ All agents are role-conditioned")
    
    # Step 5: Show role context
    role_context = state['role_state']['role_context']
    print(f"\nRole Context:")
    print(f"  Role: {role_context['role']}")
    print(f"  Reasoning: {role_context['reasoning_style']}")
    print(f"  Bias: {role_context['allowed_bias']}")


# ============================================================================
# MAIN RUNNER
# ============================================================================

async def main():
    """Run all examples."""
    print("\n" + "╔" + "="*58 + "╗")
    print("║" + " "*10 + "Role-Based Agent System Examples" + " "*15 + "║")
    print("╚" + "="*58 + "╝")
    
    try:
        # Run examples
        await example_basic_role_selection()
        await example_role_change()
        await example_role_guards()
        await example_role_contexts()
        await example_complete_workflow()
        
        print("\n" + "="*60)
        print("✅ All examples completed successfully!")
        print("="*60 + "\n")
        
    except Exception as e:
        logger.error("❌ Example failed: {}", str(e))
        raise


if __name__ == "__main__":
    # Run examples
    asyncio.run(main())

