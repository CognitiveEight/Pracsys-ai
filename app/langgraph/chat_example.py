"""
Intelligence Chat Module - Example Usage
Demonstrates how to use the governed chat interface.
"""

import asyncio
import uuid

from loguru import logger

from app.langgraph.chat_integration import process_workspace_chat
from app.langgraph.chat_models import ChatContext
from app.langgraph.intelligence_chat_agent import intelligence_chat_agent


async def example_basic_chat():
    """Example: Basic chat with full context."""
    logger.info("=" * 70)
    logger.info("EXAMPLE 1: Basic Governed Chat")
    logger.info("=" * 70)

    # Prepare chat context
    chat_context = ChatContext(
        workspace_id="workspace_123",
        user_id="user_456",
        role_context={
            "role": "PETITIONER",
            "reasoning_style": "advocacy-aligned-petitioner",
            "allowed_bias": "petitioner-favorable",
            "forbidden_actions": [
                "Defending respondent position",
                "Minimizing petitioner harm",
            ],
        },
        workspace_state={
            "workspace_id": "workspace_123",
            "documents_count": 5,
            "status": "active",
        },
        active_strategy={
            "strategy_type": "PROSECUTION_OR_RELIEF",
            "title": "Seek Legal Relief",
            "description": "Pursue legal action for damages",
        },
        approved_facts=[
            {
                "fact_id": "fact_001",
                "fact_text": "Contract signed on January 15, 2024",
                "confidence": 0.95,
                "source": "Contract Document",
            },
            {
                "fact_id": "fact_002",
                "fact_text": "Payment not received by agreed date",
                "confidence": 0.90,
                "source": "Bank Statement",
            },
        ],
        applicable_laws=[
            {
                "law_id": "law_001",
                "title": "Contract Act Section 73",
                "description": "Compensation for loss or damage",
            }
        ],
        active_module="analysis_report",
    )

    # User asks a question
    user_message = "What are my strongest arguments for getting compensation?"

    logger.info(f"\n💬 User ({chat_context.role_context['role']}): {user_message}")

    # Process message
    response = await intelligence_chat_agent.process_message(
        workspace_id="workspace_123",
        user_id="user_456",
        message_id=str(uuid.uuid4()),
        user_message=user_message,
        chat_context=chat_context,
    )

    # Display response
    logger.info(f"\n🤖 Assistant:")
    logger.info(f"   {response.response_text}")
    logger.info(f"\n📝 Explanation: {response.explanation}")
    logger.info(f"\n📊 Confidence: {response.confidence}")
    logger.info(f"\n🎯 Intent: {response.detected_intent}")

    if response.sources:
        logger.info(f"\n📚 Sources:")
        for source in response.sources:
            logger.info(f"   - {source.source_name}")

    if response.suggested_actions:
        logger.info(f"\n➡️  Suggested Actions:")
        for action in response.suggested_actions:
            logger.info(f"   - {action}")

    logger.info("\n" + "=" * 70)


async def example_role_aware_responses():
    """Example: Different responses for different roles."""
    logger.info("=" * 70)
    logger.info("EXAMPLE 2: Role-Aware Responses")
    logger.info("=" * 70)

    # Same question, different roles
    user_message = "How strong is the evidence in this case?"

    roles = [
        {
            "role": "PETITIONER",
            "reasoning_style": "advocacy-aligned-petitioner",
            "allowed_bias": "petitioner-favorable",
        },
        {
            "role": "RESPONDENT",
            "reasoning_style": "advocacy-aligned-respondent",
            "allowed_bias": "respondent-favorable",
        },
        {
            "role": "ADJUDICATOR",
            "reasoning_style": "neutral-judicial",
            "allowed_bias": "none",
        },
    ]

    for role_ctx in roles:
        logger.info(f"\n🎭 Role: {role_ctx['role']}")
        logger.info(f"💬 Question: {user_message}")

        chat_context = ChatContext(
            workspace_id="workspace_123",
            user_id="user_456",
            role_context=role_ctx,
            workspace_state={"workspace_id": "workspace_123", "status": "active"},
            approved_facts=[
                {"fact_text": "Strong documentary evidence exists", "confidence": 0.85}
            ],
        )

        response = await intelligence_chat_agent.process_message(
            workspace_id="workspace_123",
            user_id="user_456",
            message_id=str(uuid.uuid4()),
            user_message=user_message,
            chat_context=chat_context,
        )

        logger.info(f"🤖 Response: {response.response_text[:200]}...")
        logger.info("-" * 70)

    logger.info("=" * 70)


async def example_intent_routing():
    """Example: Different intents trigger different flows."""
    logger.info("=" * 70)
    logger.info("EXAMPLE 3: Intent Detection & Routing")
    logger.info("=" * 70)

    # Base context
    base_context = ChatContext(
        workspace_id="workspace_123",
        user_id="user_456",
        role_context={
            "role": "PETITIONER",
            "reasoning_style": "advocacy-aligned-petitioner",
        },
        workspace_state={"workspace_id": "workspace_123", "status": "active"},
    )

    # Different queries with different intents
    queries = [
        ("Explain why this fact supports my case", "EXPLAIN"),
        ("Can you clarify what Section 73 means?", "CLARIFY"),
        ("I want to draft a legal notice", "DRAFT"),
        ("Should I change my strategy?", "STRATEGY_CHANGE"),
        ("What should I do next?", "NEXT_STEPS"),
    ]

    for query, expected_intent in queries:
        logger.info(f"\n💬 Query: {query}")
        logger.info(f"🎯 Expected Intent: {expected_intent}")

        response = await intelligence_chat_agent.process_message(
            workspace_id="workspace_123",
            user_id="user_456",
            message_id=str(uuid.uuid4()),
            user_message=query,
            chat_context=base_context,
        )

        logger.info(f"✅ Detected Intent: {response.detected_intent}")

        if response.routed_to_agent:
            logger.info(f"🚀 Routed to: {response.routed_to_agent}")
        else:
            logger.info(f"📖 Direct response (no agent routing)")

        logger.info("-" * 70)

    logger.info("=" * 70)


async def example_governance_validation():
    """Example: Governance violations are caught."""
    logger.info("=" * 70)
    logger.info("EXAMPLE 4: Governance Validation")
    logger.info("=" * 70)

    # Try without role context (should fail)
    logger.info("\n❌ Test 1: Missing RoleContext")
    try:
        chat_context = ChatContext(
            workspace_id="workspace_123",
            user_id="user_456",
            role_context=None,  # Missing!
            workspace_state={"workspace_id": "workspace_123"},
        )

        response = await intelligence_chat_agent.process_message(
            workspace_id="workspace_123",
            user_id="user_456",
            message_id=str(uuid.uuid4()),
            user_message="What should I do?",
            chat_context=chat_context,
        )

        if not response.role_compliant:
            logger.error("✅ Correctly caught: Missing role context")
    except ValueError as e:
        logger.success(f"✅ Correctly rejected: {e}")

    # Try without workspace state (should fail)
    logger.info("\n❌ Test 2: Missing WorkspaceState")
    try:
        chat_context = ChatContext(
            workspace_id="workspace_123",
            user_id="user_456",
            role_context={"role": "PETITIONER"},
            workspace_state=None,  # Missing!
        )

        response = await intelligence_chat_agent.process_message(
            workspace_id="workspace_123",
            user_id="user_456",
            message_id=str(uuid.uuid4()),
            user_message="What should I do?",
            chat_context=chat_context,
        )

    except ValueError as e:
        logger.success(f"✅ Correctly rejected: {e}")

    logger.info("\n" + "=" * 70)


async def main():
    """Run all examples."""
    logger.info("\n")
    logger.info("🚀 INTELLIGENCE CHAT MODULE - EXAMPLES")
    logger.info("=" * 70)

    try:
        # Example 1: Basic chat
        await example_basic_chat()

        # Example 2: Role-aware responses
        await example_role_aware_responses()

        # Example 3: Intent routing
        await example_intent_routing()

        # Example 4: Governance validation
        await example_governance_validation()

        logger.info("\n✅ All examples completed successfully!")

    except Exception as e:
        logger.error(f"❌ Example failed: {e}")
        raise


if __name__ == "__main__":
    asyncio.run(main())
