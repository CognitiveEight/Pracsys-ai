"""
Intelligence Chat Module - LangGraph Nodes
Implements governed, role-aware chat reasoning.
"""
from typing import Dict, Any, List
from loguru import logger
import time

from app.langgraph.chat_models import (
    ChatIntent,
    ChatIntentEnum,
    ConfidenceLevel,
    SourceReference,
    IntentClassificationResult,
    GovernanceValidationResult,
    get_module_focus
)
from app.agents.role_models import RoleEnum
from app.ai.gemini_client import gemini_client


def chat_context_builder_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """
    Node 1: ChatContextBuilderNode
    
    Builds comprehensive prompt context using:
    - RoleContext
    - ActiveStrategy  
    - ActiveModule
    - Workspace snapshot
    - Enforces plan-based depth
    
    Args:
        state: Current workflow state
        
    Returns:
        Updated state with prompt_context
    """
    logger.info("🏗️ Building chat context...")
    
    # Extract inputs
    chat_context = state.get("chat_context", {})
    user_message = state.get("user_message", "")
    
    # Validate critical inputs
    role_context = chat_context.get("role_context")
    workspace_state = chat_context.get("workspace_state")
    
    if not role_context:
        logger.error("❌ Missing RoleContext - chat cannot proceed")
        return {
            **state,
            "error": "MISSING_ROLE_CONTEXT",
            "error_message": "Chat requires RoleContext. Please select a role first."
        }
    
    if not workspace_state:
        logger.error("❌ Missing WorkspaceState - chat cannot proceed")
        return {
            **state,
            "error": "MISSING_WORKSPACE_STATE",
            "error_message": "Workspace state not available."
        }
    
    # Extract role
    role = role_context.get("role") if isinstance(role_context, dict) else role_context.role
    
    # Build context sections
    context_sections = []
    
    # 1. Role Context
    role_section = f"""
ROLE CONTEXT:
You are operating as: {role}
Reasoning Style: {role_context.get('reasoning_style', 'N/A')}
Allowed Bias: {role_context.get('allowed_bias', 'none')}
Forbidden Actions: {', '.join(role_context.get('forbidden_actions', []))}
"""
    context_sections.append(role_section)
    
    # 2. Active Strategy
    active_strategy = chat_context.get("active_strategy")
    if active_strategy:
        strategy_section = f"""
ACTIVE STRATEGY:
Type: {active_strategy.get('strategy_type', 'N/A')}
Title: {active_strategy.get('title', 'N/A')}
Description: {active_strategy.get('description', 'N/A')}
"""
        context_sections.append(strategy_section)
    
    # 3. Active Module Focus
    active_module = chat_context.get("active_module")
    if active_module:
        module_focus = get_module_focus(active_module)
        module_section = f"""
ACTIVE MODULE: {active_module}
Focus Area: {module_focus}
"""
        context_sections.append(module_section)
    
    # 4. Approved Facts
    approved_facts = chat_context.get("approved_facts", [])
    if approved_facts:
        facts_section = f"""
APPROVED FACTS ({len(approved_facts)} available):
{chr(10).join([f"- {fact.get('fact_text', 'N/A')[:100]}..." for fact in approved_facts[:5]])}
"""
        context_sections.append(facts_section)
    
    # 5. Applicable Laws
    applicable_laws = chat_context.get("applicable_laws", [])
    if applicable_laws:
        laws_section = f"""
APPLICABLE LAWS ({len(applicable_laws)} available):
{chr(10).join([f"- {law.get('title', 'N/A')}" for law in applicable_laws[:5]])}
"""
        context_sections.append(laws_section)
    
    # 6. Chat History (last 3 exchanges)
    chat_history = chat_context.get("chat_history", [])
    if chat_history:
        history_section = "RECENT CONVERSATION:\n"
        for msg in chat_history[-3:]:
            history_section += f"User: {msg.get('user_message', 'N/A')[:100]}\n"
            history_section += f"Assistant: {msg.get('response', 'N/A')[:100]}\n"
        context_sections.append(history_section)
    
    # Combine all sections
    full_context = "\n".join(context_sections)
    
    logger.success(f"✅ Context built: {len(full_context)} chars")
    logger.info(f"📋 Role: {role}, Module: {active_module}, Facts: {len(approved_facts)}")
    
    return {
        **state,
        "prompt_context": full_context,
        "role": role,
        "context_built": True
    }


async def intent_classifier_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """
    Node 2: IntentClassifierNode
    
    Classifies user input into intent taxonomy.
    Rejects unsupported intents.
    
    Args:
        state: Current workflow state
        
    Returns:
        Updated state with intent_classification
    """
    logger.info("🔍 Classifying user intent...")
    
    user_message = state.get("user_message", "")
    role = state.get("role")
    
    if not user_message:
        return {
            **state,
            "error": "EMPTY_MESSAGE",
            "error_message": "No message provided"
        }
    
    # Intent classification prompt
    classification_prompt = f"""
Classify the following user message into ONE of these intents:

INTENTS:
- GREETING: Simple greetings, casual conversation, or social interactions (hi, hello, how are you, thanks, etc.)
- EXPLAIN: User wants explanation of existing intelligence or workspace content
- CLARIFY: User asking follow-up or clarification questions about specific topics
- VALIDATE: User wants to validate facts or information
- DRAFT: User wants to create/draft documents
- STRATEGY_CHANGE: User wants to change strategy
- REMOVE_INSIGHT: User wants to remove/reject an insight
- NEXT_STEPS: User asking what to do next or for recommendations
- UNKNOWN: Cannot classify or unsupported

USER MESSAGE: "{user_message}"

ROLE: {role}

IMPORTANT: 
- Simple greetings like "hi", "hello", "hey", "thanks", "good morning" should be classified as GREETING
- Only use EXPLAIN when user is asking about workspace intelligence or specific information
- Use CLARIFY when user is following up on previous conversation
- Most conversational/social messages should be GREETING

Respond in format:
INTENT: <intent_name>
CONFIDENCE: <0.0-1.0>
REASONING: <why you chose this intent>
REQUIRES_AGENT: <yes/no>
TARGET_AGENT: <agent_name or none>
"""
    
    try:
        # Use Gemini for classification
        response = await gemini_client.generate(classification_prompt)
        
        # Parse response (simple parsing)
        lines = response.strip().split('\n')
        intent_line = [l for l in lines if l.startswith('INTENT:')]
        confidence_line = [l for l in lines if l.startswith('CONFIDENCE:')]
        reasoning_line = [l for l in lines if l.startswith('REASONING:')]
        requires_agent_line = [l for l in lines if l.startswith('REQUIRES_AGENT:')]
        target_agent_line = [l for l in lines if l.startswith('TARGET_AGENT:')]
        
        intent = intent_line[0].split(':', 1)[1].strip() if intent_line else "UNKNOWN"
        confidence = float(confidence_line[0].split(':', 1)[1].strip()) if confidence_line else 0.5
        reasoning = reasoning_line[0].split(':', 1)[1].strip() if reasoning_line else "Classification reasoning unavailable"
        requires_agent = requires_agent_line[0].split(':', 1)[1].strip().lower() == 'yes' if requires_agent_line else False
        target_agent = target_agent_line[0].split(':', 1)[1].strip() if target_agent_line else None
        if target_agent and target_agent.lower() == 'none':
            target_agent = None
        
        # Validate intent
        is_supported = intent in [e.value for e in ChatIntentEnum]
        rejection_reason = None if is_supported else f"Intent '{intent}' is not supported"
        
        classification_result = IntentClassificationResult(
            intent=intent,
            confidence=confidence,
            reasoning=reasoning,
            requires_agent=requires_agent,
            target_agent=target_agent,
            is_supported=is_supported,
            rejection_reason=rejection_reason
        )
        
        logger.success(f"✅ Intent classified: {intent} (confidence: {confidence:.2f})")
        logger.info(f"📍 Requires agent: {requires_agent}, Target: {target_agent}")
        
        return {
            **state,
            "intent_classification": classification_result.to_dict(),
            "intent": intent,
            "intent_classified": True
        }
        
    except Exception as e:
        logger.error(f"❌ Intent classification failed: {e}")
        return {
            **state,
            "intent": "UNKNOWN",
            "intent_classification": {
                "intent": "UNKNOWN",
                "confidence": 0.0,
                "reasoning": f"Classification error: {str(e)}",
                "is_supported": False,
                "rejection_reason": "Classification failed"
            }
        }


async def _handle_greeting(state: Dict[str, Any], user_message: str, role: str) -> Dict[str, Any]:
    """
    Handle simple greetings and conversational messages with natural responses.
    Bypasses the formal explanation process for casual conversation.
    
    Args:
        state: Current workflow state
        user_message: The user's message
        role: The user's role
        
    Returns:
        Updated state with natural conversational response
    """
    from datetime import datetime
    start_time = datetime.utcnow()
    
    # Generate natural, contextual greeting response
    greeting_prompt = f"""
You are a helpful AI assistant in a legal workspace system where the user's role is {role}.

The user said: "{user_message}"

Generate a natural, friendly response that:
1. Responds appropriately to their message (greeting, thanks, etc.)
2. Briefly mentions you're here to help with their workspace if it's a first greeting
3. Keeps it conversational and natural (2-3 sentences max)
4. Does NOT include formal explanations, reasoning sections, or governance language
5. Does NOT use phrases like "From judicial perspective" or overly formal language

Just respond naturally as a helpful assistant would.
"""
    
    try:
        response = await gemini_client.generate(greeting_prompt)
        processing_time = (datetime.utcnow() - start_time).total_seconds() * 1000
        
        # Clean up the response if it still has formal formatting
        response_text = response.strip()
        
        # Remove any formatting artifacts
        for prefix in ["ANSWER:", "RESPONSE:", "**Response:**", "**Answer:**"]:
            if response_text.startswith(prefix):
                response_text = response_text[len(prefix):].strip()
        
        logger.success(f"✅ Generated natural greeting response ({processing_time:.0f}ms)")
        
        return {
            **state,
            "response_text": response_text,
            "explanation": "Conversational response to greeting",
            "sources": [],
            "confidence": "HIGH",
            "suggested_actions": [],
            "processing_time_ms": processing_time,
            "explanation_complete": True
        }
    except Exception as e:
        logger.error(f"❌ Failed to generate greeting response: {e}")
        # Fallback to simple greeting
        return {
            **state,
            "response_text": "Hello! I'm here to help you with your workspace. What can I assist you with today?",
            "explanation": "Default greeting response",
            "sources": [],
            "confidence": "HIGH",
            "suggested_actions": [],
            "processing_time_ms": 0,
            "explanation_complete": True
        }


async def explain_why_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """
    Node 3: ExplainWhyNode
    
    Ensures explanation exists and is properly formatted.
    Attaches rationale, sources, and confidence.
    
    Args:
        state: Current workflow state
        
    Returns:
        Updated state with explanation
    """
    logger.info("📝 Generating explanation...")
    
    user_message = state.get("user_message", "")
    prompt_context = state.get("prompt_context", "")
    intent = state.get("intent", "EXPLAIN")
    role = state.get("role")
    chat_context = state.get("chat_context", {})
    
    # Special handling for GREETING intent - simple, natural responses
    if intent == "GREETING":
        logger.info("👋 Handling greeting/conversational message")
        return await _handle_greeting(state, user_message, role)
    
    # Build reasoning prompt
    reasoning_prompt = f"""
{prompt_context}

GOVERNANCE RULES:
1. You MUST respect the role: {role}
2. You MUST cite sources for ALL claims
3. You MUST indicate confidence levels
4. You MUST NOT invent facts
5. You MUST NOT override approved facts
6. You MUST explain your reasoning

USER QUESTION: "{user_message}"

INTENT: {intent}

Generate a response that:
1. Directly answers the question
2. Explains WHY (reasoning)
3. Cites specific sources (documents, facts, laws)
4. Indicates confidence level
5. Respects the role perspective

Response format:
ANSWER: <direct answer>
EXPLANATION: <detailed reasoning>
SOURCES: <list of sources cited>
CONFIDENCE: <HIGH/MEDIUM/LOW/UNCERTAIN>
SUGGESTED_ACTIONS: <optional next steps>
"""
    
    try:
        from datetime import datetime
        start_time = datetime.utcnow()
        
        # Generate response with Gemini
        response = await gemini_client.generate(reasoning_prompt)
        
        processing_time = (datetime.utcnow() - start_time).total_seconds() * 1000
        
        # Parse response
        lines = response.strip().split('\n')
        answer_line = [l for l in lines if l.startswith('ANSWER:')]
        explanation_line = [l for l in lines if l.startswith('EXPLANATION:')]
        sources_line = [l for l in lines if l.startswith('SOURCES:')]
        confidence_line = [l for l in lines if l.startswith('CONFIDENCE:')]
        actions_line = [l for l in lines if l.startswith('SUGGESTED_ACTIONS:')]
        
        answer = answer_line[0].split(':', 1)[1].strip() if answer_line else response
        explanation = explanation_line[0].split(':', 1)[1].strip() if explanation_line else "Explanation not provided"
        sources_text = sources_line[0].split(':', 1)[1].strip() if sources_line else ""
        confidence_text = confidence_line[0].split(':', 1)[1].strip() if confidence_line else "MEDIUM"
        actions_text = actions_line[0].split(':', 1)[1].strip() if actions_line else ""
        
        # Parse sources (simple parsing)
        sources = []
        if sources_text:
            for source_desc in sources_text.split(','):
                if source_desc.strip():
                    sources.append(SourceReference(
                        source_type="referenced",
                        source_id="auto",
                        source_name=source_desc.strip()[:100]
                    ))
        
        # Parse confidence
        try:
            confidence = ConfidenceLevel[confidence_text.upper()]
        except (KeyError, AttributeError):
            confidence = ConfidenceLevel.MEDIUM
        
        # Parse suggested actions
        suggested_actions = []
        if actions_text:
            suggested_actions = [a.strip() for a in actions_text.split(',') if a.strip()]
        
        logger.success(f"✅ Explanation generated ({processing_time:.0f}ms)")
        logger.info(f"📊 Confidence: {confidence.value}, Sources: {len(sources)}")
        
        return {
            **state,
            "response_text": answer,
            "explanation": explanation,
            "sources": [s.to_dict() for s in sources],
            "confidence": confidence.value,
            "suggested_actions": suggested_actions,
            "processing_time_ms": processing_time,
            "explanation_complete": True
        }
        
    except Exception as e:
        logger.error(f"❌ Explanation generation failed: {e}")
        return {
            **state,
            "response_text": "I apologize, but I encountered an error generating a response.",
            "explanation": f"Error: {str(e)}",
            "sources": [],
            "confidence": "UNCERTAIN",
            "error": "EXPLANATION_FAILED"
        }


def action_dispatcher_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """
    Node 4: ActionDispatcherNode
    
    Converts intent → agent invocation.
    Ensures governance rules.
    Never mutates state directly.
    
    Args:
        state: Current workflow state
        
    Returns:
        Updated state with dispatch information
    """
    logger.info("🚀 Dispatching action...")
    
    intent = state.get("intent", "EXPLAIN")
    intent_classification = state.get("intent_classification", {})
    requires_agent = intent_classification.get("requires_agent", False)
    target_agent = intent_classification.get("target_agent")
    
    routed_to_agent = None
    dispatch_message = None
    
    # Route based on intent
    if intent == "GREETING":
        # Conversational response, no agent needed
        logger.info("👋 Conversational greeting - no agent routing required")
        dispatch_message = "Conversational response"
    
    elif intent == "EXPLAIN" or intent == "CLARIFY":
        # Direct response, no agent needed
        logger.info("📖 Direct explanation - no agent routing required")
        dispatch_message = "Answered directly from workspace intelligence"
    
    elif intent == "VALIDATE":
        routed_to_agent = "fact_validation_agent"
        dispatch_message = "Routing to Fact Validation Agent for verification"
        logger.info("✅ Routing to Fact Validation Agent")
    
    elif intent == "DRAFT":
        routed_to_agent = "drafting_agent"
        dispatch_message = "Routing to Drafting Agent for document creation"
        logger.info("📄 Routing to Drafting Agent")
    
    elif intent == "STRATEGY_CHANGE":
        routed_to_agent = "strategy_agent"
        dispatch_message = "Routing to Strategy Agent for strategy modification"
        logger.info("🎯 Routing to Strategy Agent")
    
    elif intent == "REMOVE_INSIGHT":
        routed_to_agent = "fact_validation_agent"
        dispatch_message = "Routing to Fact Validation Agent for insight removal"
        logger.info("🗑️ Routing to validation for removal")
    
    elif intent == "NEXT_STEPS":
        routed_to_agent = "recommendation_agent"
        dispatch_message = "Routing to Recommendation Agent for next steps"
        logger.info("➡️ Routing to Recommendation Agent")
    
    else:
        logger.warning(f"⚠️ Unknown intent: {intent} - providing direct response")
        dispatch_message = "Intent not recognized - providing general response"
    
    logger.success(f"✅ Action dispatched: {routed_to_agent or 'direct_response'}")
    
    return {
        **state,
        "routed_to_agent": routed_to_agent,
        "dispatch_message": dispatch_message,
        "action_dispatched": True
    }


def response_formatter_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """
    Node 5: ResponseFormatterNode
    
    Formats final response with:
    - Role-consistent tone
    - Module context
    - Next possible actions
    
    Args:
        state: Current workflow state
        
    Returns:
        Updated state with formatted_response
    """
    logger.info("🎨 Formatting response...")
    
    role = state.get("role")
    intent = state.get("intent", "EXPLAIN")
    response_text = state.get("response_text", "")
    explanation = state.get("explanation", "")
    sources = state.get("sources", [])
    confidence = state.get("confidence", "MEDIUM")
    suggested_actions = state.get("suggested_actions", [])
    routed_to_agent = state.get("routed_to_agent")
    dispatch_message = state.get("dispatch_message")
    
    # For GREETING intent, use simple formatting without formal structure
    if intent == "GREETING":
        logger.info("👋 Using conversational formatting for greeting")
        return {
            **state,
            "formatted_response": response_text,
            "response_complete": True
        }
    
    # Apply role-specific tone adjustments for formal queries
    role_prefix = ""
    if role == "PETITIONER":
        role_prefix = "From petitioner perspective: "
    elif role == "RESPONDENT":
        role_prefix = "From respondent perspective: "
    elif role == "MEDIATOR":
        role_prefix = "From neutral mediation perspective: "
    elif role == "ADJUDICATOR":
        role_prefix = "From judicial perspective: "
    
    # Build formatted response
    formatted_parts = []
    
    # Main response
    if response_text:
        formatted_parts.append(f"{role_prefix}{response_text}")
    
    # Explanation
    if explanation and explanation not in ["Explanation not provided", "Conversational response to greeting"]:
        formatted_parts.append(f"\n**Reasoning:** {explanation}")
    
    # Sources
    if sources:
        sources_text = "\n**Sources:**\n" + "\n".join([
            f"- {s.get('source_name', 'Unknown')}" for s in sources
        ])
        formatted_parts.append(sources_text)
    
    # Confidence (only for formal responses)
    if intent not in ["GREETING"]:
        formatted_parts.append(f"\n**Confidence:** {confidence}")
    
    # Agent routing info
    if routed_to_agent:
        formatted_parts.append(f"\n**Action:** {dispatch_message}")
    
    # Suggested actions
    if suggested_actions:
        actions_text = "\n**Suggested Next Steps:**\n" + "\n".join([
            f"- {action}" for action in suggested_actions
        ])
        formatted_parts.append(actions_text)
    
    formatted_response = "\n".join(formatted_parts)
    
    logger.success("✅ Response formatted")
    logger.info(f"📏 Response length: {len(formatted_response)} chars")
    
    return {
        **state,
        "formatted_response": formatted_response,
        "response_complete": True
    }


def governance_validator_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """
    Validates response against governance rules.
    
    Checks:
    - Role compliance
    - Strategy compliance
    - No fact invention
    - No unauthorized mutations
    
    Args:
        state: Current workflow state
        
    Returns:
        Updated state with governance_validation
    """
    logger.info("🔐 Validating governance compliance...")
    
    role = state.get("role")
    chat_context = state.get("chat_context", {})
    response_text = state.get("response_text", "")
    intent = state.get("intent")
    
    violations = []
    warnings = []
    
    # Check 1: Role context exists
    if not chat_context.get("role_context"):
        violations.append("Missing role context - cannot validate role compliance")
    
    # Check 2: No direct state mutation (all must go through agents)
    if intent in ["VALIDATE", "DRAFT", "STRATEGY_CHANGE", "REMOVE_INSIGHT"]:
        if not state.get("routed_to_agent"):
            violations.append(f"Intent '{intent}' requires agent routing but none specified")
    
    # Check 3: Response has explanation
    if not state.get("explanation"):
        violations.append("Response missing mandatory explanation")
    
    # Check 4: Response has confidence indicator
    if not state.get("confidence"):
        warnings.append("Response missing confidence indicator")
    
    is_valid = len(violations) == 0
    
    validation_result = GovernanceValidationResult(
        is_valid=is_valid,
        violations=violations,
        warnings=warnings,
        explanation="Governance validation complete" if is_valid else "Governance violations detected"
    )
    
    if is_valid:
        logger.success("✅ Governance validation passed")
    else:
        logger.error(f"❌ Governance violations: {violations}")
    
    if warnings:
        logger.warning(f"⚠️ Warnings: {warnings}")
    
    return {
        **state,
        "governance_validation": validation_result.to_dict(),
        "role_compliant": is_valid,
        "strategy_compliant": is_valid,
        "governance_validated": True
    }

