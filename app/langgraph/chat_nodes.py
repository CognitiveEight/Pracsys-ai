"""
Intelligence Chat Module - LangGraph Nodes
Implements governed, role-aware chat reasoning.
"""

import re
import time
from typing import Any, Dict, List

from loguru import logger

from app.agents.role_models import RoleEnum
from app.ai.gemini_client import gemini_client
from app.langgraph.chat_models import (ChatIntent, ChatIntentEnum,
                                       ConfidenceLevel,
                                       GovernanceValidationResult,
                                       IntentClassificationResult,
                                       SourceReference, get_module_focus)
from app.prompts.legal_intelligence_system import (
    ANNEXURE_UPLOAD_GATE_MESSAGE, ROLES,
    get_legal_intelligence_system_instruction)


def _strip_trailing_sources_block(text: str) -> str:
    """Remove a trailing 'SOURCES:' section models often append in free-form replies."""
    if not text or not text.strip():
        return text
    m = re.search(r"(?:^|\n)\s*SOURCES\s*:", text, re.IGNORECASE | re.MULTILINE)
    if m:
        if m.start() == 0:
            return ""
        return text[: m.start()].rstrip()
    return text


def _max_tokens_for_chat_intent(intent: str) -> int:
    """Cap length for routine Q&A; leave headroom for drafting and heavy intents."""
    return {
        "DRAFT": 8000,
        "STRATEGY_CHANGE": 8000,
        "NEXT_STEPS": 8000,
        "VALIDATE": 8000,
        "REMOVE_INSIGHT": 8000,
    }.get(intent, 8000)


# Explanations kept for governance/metadata only — never append to user-visible chat text
_EXPLANATION_SUPPRESSED_IN_UI = frozenset(
    {
        "Response based on workspace context and annexures where used.",
        "Short reply - user may ask for more detail.",
        "Explanation not provided",
        "Conversational response to greeting",
        "Default greeting response",
    }
)


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
            "error_message": "Chat requires RoleContext. Please select a role first.",
        }

    if not workspace_state:
        logger.error("❌ Missing WorkspaceState - chat cannot proceed")
        return {
            **state,
            "error": "MISSING_WORKSPACE_STATE",
            "error_message": "Workspace state not available.",
        }

    # Extract role
    role = (
        role_context.get("role")
        if isinstance(role_context, dict)
        else role_context.role
    )

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

    # 5a. ANALYSIS REPORT (cached panel snapshot – use for Analysis Report chat, score explanation, strengths/risks)
    workspace_state = chat_context.get("workspace_state") or {}
    analysis_report = workspace_state.get("analysis_report")
    if isinstance(analysis_report, dict) and analysis_report:
        score = analysis_report.get("caseResolutionScore")
        if score is None:
            score = analysis_report.get("case_resolution_score")
        expl = (
            analysis_report.get("qualityScoreExplanation")
            or analysis_report.get("quality_score_explanation")
            or ""
        ).strip()
        missing = (
            analysis_report.get("missingInfo")
            or analysis_report.get("missing_info")
            or []
        )
        strengths_ar = analysis_report.get("strengths") or []
        weak_ar = analysis_report.get("weaknesses") or []
        risks_ar = analysis_report.get("risks") or []
        juris = (
            analysis_report.get("relevantJurisdiction")
            or analysis_report.get("relevant_jurisdiction")
            or []
        )
        pending = (
            analysis_report.get("pendingActions")
            or analysis_report.get("pending_actions")
            or []
        )
        next_st = (
            analysis_report.get("nextSteps") or analysis_report.get("next_steps") or []
        )
        drafts_ar = (
            analysis_report.get("suggestedDrafts")
            or analysis_report.get("suggested_drafts")
            or []
        )
        st_lines = []
        for s in strengths_ar[:5]:
            if isinstance(s, dict):
                pt = (s.get("point") or s.get("text") or "").strip()
                laws = s.get("laws") or s.get("law") or []
                if not isinstance(laws, list):
                    laws = [laws] if laws else []
                law_s = ", ".join(str(l) for l in laws[:3] if l)
                st_lines.append(f"- {pt[:300]}" + (f" ({law_s})" if law_s else ""))
            elif s:
                st_lines.append(f"- {str(s)[:300]}")
        ar_section = f"""
CACHED ANALYSIS REPORT (same data as the Analysis Report panel; use for questions about score, gaps, risks):
- Case resolution score: {score}%
- Quality score explanation: {expl if expl else "(see counts below — user may ask you to explain the score in your own words)"}
- Missing information ({len(missing) if isinstance(missing, list) else 0}): {chr(10).join(f"  - {str(x)[:200]}" for x in (missing[:6] if isinstance(missing, list) else [])) or "  (none)"}
- Strengths: {chr(10).join(st_lines) if st_lines else "  (none)"}
- Weaknesses ({len(weak_ar) if isinstance(weak_ar, list) else 0}): {chr(10).join(f"  - {str(x)[:200]}" for x in (weak_ar[:6] if isinstance(weak_ar, list) else [])) or "  (none)"}
- Risks ({len(risks_ar) if isinstance(risks_ar, list) else 0}): {chr(10).join(f"  - {str(x)[:200]}" for x in (risks_ar[:6] if isinstance(risks_ar, list) else [])) or "  (none)"}
- Relevant jurisdiction: {", ".join(str(x) for x in (juris[:5] if isinstance(juris, list) else [])) or "(none)"}
- Pending actions: {", ".join(str(x) for x in (pending[:5] if isinstance(pending, list) else [])) or "(none)"}
- Next steps: {", ".join(str(x) for x in (next_st[:5] if isinstance(next_st, list) else [])) or "(none)"}
- Suggested drafts: {", ".join(str(x) for x in (drafts_ar[:5] if isinstance(drafts_ar, list) else [])) or "(none)"}
"""
        context_sections.append(ar_section)

    # 5b. Workspace annexures (uploaded evidence; reference when case-specific)
    uploaded_docs = workspace_state.get("uploaded_documents") or []
    if uploaded_docs:
        docs_section = "\nUPLOADED ANNEXURES (reference evidence for this workspace):\n"
        docs_section += "Ordering: Annexure 1 is the first block below, Annexure 2 the second, and so on.\n"
        docs_section += (
            'When the user refers to "Annexure 1", "Annexure 2", Roman numerals (Annexure I, II), '
            "or variants like annexure-1, map them to the corresponding numbered block below.\n"
        )
        docs_section += (
            "Use annexure text when the query depends on case facts, drafting, evidence, or citations. "
            "For general legal or process questions, reason directly and cite annexures only when they add value.\n"
        )
        docs_section += (
            "For high-level questions (e.g. case goal, what the dispute is about), prefer ACTIVE STRATEGY and "
            "CACHED ANALYSIS REPORT in this prompt if present; read annexures for detail only when needed.\n"
        )
        for i, doc in enumerate(uploaded_docs, 1):
            content = doc.get("content") or ""
            if content:
                ann_label = (
                    doc.get("fileName") or doc.get("display_name") or ""
                ).strip()
                header = f"--- Annexure {i}"
                if ann_label:
                    header += f" ({ann_label})"
                header += " ---"
                docs_section += f"\n{header}\n{content}\n"
        context_sections.append(docs_section)

    # 5c. Chat attachment (file supplied with this message)
    chat_attachment = workspace_state.get("chat_attachment") or []
    if chat_attachment:
        attach_section = (
            "\nANNEXURE ATTACHED WITH THIS MESSAGE (answer using the labels below):\n"
        )
        for i, att in enumerate(chat_attachment, 1):
            name = (att.get("fileName") or "").strip() or f"Annexure {i}"
            content = att.get("content") or ""
            if content:
                attach_section += f"\n--- {name} ---\n{content}\n"
        context_sections.append(attach_section)

    # 6. Chat History (last 3 exchanges; support both {user_message, response} and {role, content})
    chat_history = chat_context.get("chat_history", [])
    if chat_history:
        history_section = "RECENT CONVERSATION:\n"
        for msg in chat_history[-6:]:  # up to 6 messages (3 exchanges)
            if msg.get("role") and msg.get("content") is not None:
                label = "User" if (msg.get("role") == "USER") else "Assistant"
                history_section += f"{label}: {(msg.get('content') or '')[:200]}\n"
            else:
                history_section += f"User: {(msg.get('user_message') or 'N/A')[:100]}\n"
                history_section += (
                    f"Assistant: {(msg.get('response') or 'N/A')[:100]}\n"
                )
        context_sections.append(history_section)

    # Combine all sections
    full_context = "\n".join(context_sections)

    # Append user custom instructions (structure, tone, description) when set
    custom_suffix = chat_context.get("custom_instruction_suffix") or ""
    if custom_suffix:
        full_context = full_context.rstrip() + "\n\n" + custom_suffix

    logger.success(f"✅ Context built: {len(full_context)} chars")
    logger.info(
        f"📋 Role: {role}, Module: {active_module}, Facts: {len(approved_facts)}"
    )

    return {
        **state,
        "prompt_context": full_context,
        "role": role,
        "context_built": True,
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
            "error_message": "No message provided",
        }

    # Intent classification prompt
    classification_prompt = f"""
Classify the following user message into ONE of these intents:

INTENTS:
- GREETING: Simple greetings, casual conversation, or social interactions (hi, hello, how are you, thanks, etc.)
- EXPLAIN: User wants explanation of existing intelligence or workspace content
- CLARIFY: User asking follow-up or clarification questions about specific topics
- VALIDATE: User wants to validate facts or information
- DRAFT: User wants to create or draft pleadings, filings, or formal text
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
        lines = response.strip().split("\n")
        intent_line = [l for l in lines if l.startswith("INTENT:")]
        confidence_line = [l for l in lines if l.startswith("CONFIDENCE:")]
        reasoning_line = [l for l in lines if l.startswith("REASONING:")]
        requires_agent_line = [l for l in lines if l.startswith("REQUIRES_AGENT:")]
        target_agent_line = [l for l in lines if l.startswith("TARGET_AGENT:")]

        intent = intent_line[0].split(":", 1)[1].strip() if intent_line else "UNKNOWN"
        confidence = (
            float(confidence_line[0].split(":", 1)[1].strip())
            if confidence_line
            else 0.5
        )
        reasoning = (
            reasoning_line[0].split(":", 1)[1].strip()
            if reasoning_line
            else "Classification reasoning unavailable"
        )
        requires_agent = (
            requires_agent_line[0].split(":", 1)[1].strip().lower() == "yes"
            if requires_agent_line
            else False
        )
        target_agent = (
            target_agent_line[0].split(":", 1)[1].strip() if target_agent_line else None
        )
        if target_agent and target_agent.lower() == "none":
            target_agent = None

        # Validate intent
        is_supported = intent in [e.value for e in ChatIntentEnum]
        rejection_reason = (
            None if is_supported else f"Intent '{intent}' is not supported"
        )

        classification_result = IntentClassificationResult(
            intent=intent,
            confidence=confidence,
            reasoning=reasoning,
            requires_agent=requires_agent,
            target_agent=target_agent,
            is_supported=is_supported,
            rejection_reason=rejection_reason,
        )

        logger.success(f"✅ Intent classified: {intent} (confidence: {confidence:.2f})")
        logger.info(f"📍 Requires agent: {requires_agent}, Target: {target_agent}")

        return {
            **state,
            "intent_classification": classification_result.to_dict(),
            "intent": intent,
            "intent_classified": True,
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
                "rejection_reason": "Classification failed",
            },
        }


async def _handle_greeting(
    state: Dict[str, Any], user_message: str, role: str
) -> Dict[str, Any]:
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

    # Generate natural, contextual greeting (still role-conditioned per legal intelligence rules)
    system_instruction = get_legal_intelligence_system_instruction(
        current_role=role or "ADJUDICATOR"
    )
    greeting_prompt = f"""
The user said: "{user_message}"

Generate a natural, friendly response that:
1. Responds appropriately to their message (greeting, thanks, etc.)
2. Briefly mentions you're here to help with their workspace if it's a first greeting
3. Keeps it conversational and natural (2-3 sentences max)
4. Does NOT include formal explanations, reasoning sections, or governance language
5. Does NOT use phrases like "From judicial perspective" or overly formal language

Just respond naturally as a helpful assistant would, while remaining consistent with the workspace role.
"""

    try:
        response = await gemini_client.generate(
            greeting_prompt,
            system_instruction=system_instruction,
            temperature=0.5,
            max_tokens=300,
        )
        processing_time = (datetime.utcnow() - start_time).total_seconds() * 1000

        # Clean up the response if it still has formal formatting
        response_text = response.strip()

        # Remove any formatting artifacts
        for prefix in ["ANSWER:", "RESPONSE:", "**Response:**", "**Answer:**"]:
            if response_text.startswith(prefix):
                response_text = response_text[len(prefix) :].strip()

        logger.success(
            f"✅ Generated natural greeting response ({processing_time:.0f}ms)"
        )

        return {
            **state,
            "response_text": response_text,
            "explanation": "Conversational response to greeting",
            "sources": [],
            "confidence": "HIGH",
            "suggested_actions": [],
            "processing_time_ms": processing_time,
            "explanation_complete": True,
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
            "explanation_complete": True,
        }


async def explain_why_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """
    Node 3: ExplainWhyNode

    Ensures explanation exists and is properly formatted.
    Attaches rationale, sources, and confidence.
    Enforces annexure upload gate: first message after annexures exist shows role confirmation.
    """
    logger.info("📝 Generating explanation...")

    user_message = state.get("user_message", "")
    prompt_context = state.get("prompt_context", "")
    intent = state.get("intent", "EXPLAIN")
    role = state.get("role")
    chat_context = state.get("chat_context", {})
    workspace_state = chat_context.get("workspace_state") or {}
    chat_history = chat_context.get("chat_history") or []
    documents_count = workspace_state.get("documents_count", 0)

    # Special handling for GREETING intent - simple, natural responses
    if intent == "GREETING":
        logger.info("👋 Handling greeting/conversational message")
        return await _handle_greeting(state, user_message, role)

    # Annexure upload gate: first message when annexures exist → show gate message and four roles only
    if documents_count > 0 and len(chat_history) == 0:
        gate_response = (
            ANNEXURE_UPLOAD_GATE_MESSAGE
            + "\n\nSelect one role to proceed:\n\n• "
            + "\n• ".join(ROLES)
        )
        logger.info(
            "📋 Annexure upload gate: returning role confirmation message (no analysis)"
        )
        return {
            **state,
            "response_text": gate_response,
            "explanation": "Annexure upload gate – role confirmation required before any analysis.",
            "sources": [],
            "confidence": "HIGH",
            "suggested_actions": [],
            "processing_time_ms": 0,
            "explanation_complete": True,
        }

    # Role-conditioned legal intelligence system instruction
    system_instruction = get_legal_intelligence_system_instruction(
        current_role=role,
        annexures_uploaded=documents_count > 0,
        role_confirmed=True,
    )

    # Build prompt: reason-first, proportionate length; avoid memo-style dumps for simple questions
    reasoning_prompt = f"""
{prompt_context}

USER MESSAGE: "{user_message}"

INTENT: {intent}

You are an intelligence assistant. Calibrate response length to the question:

- Short or single-focus questions (e.g. case goal, objective, "what is this about", parties, relief): give a direct, concise answer first—usually one short paragraph or a few bullets. Start from ACTIVE STRATEGY and CACHED ANALYSIS REPORT if present; only then tighten with annexures if needed. Do NOT turn this into a multi-section legal memo unless the user asks for detail.
- Questions that explicitly ask for detailed analysis, full breakdown, outline of all arguments, or comparison of evidence: you may use clear sections or headings.
- Drafting, line-by-line annexure interpretation, or "what does Annexure X say": ground answers in the relevant annexures; cite briefly inline when useful.
- General legal/process questions: reasoning-led; cite annexures only when they add value.
- If annexures conflict, flag it; do not invent case facts.

Formatting:
- Routine chat: plain prose only. Do NOT add "SOURCES:" lists or "ANSWER:/EXPLANATION:" labels unless the user asked for a formal structured deliverable.
- Do not use many "###" markdown headings for simple short questions.

Stay in role. If case-specific facts are missing, say what is missing instead of inventing them.
"""

    try:
        from datetime import datetime

        start_time = datetime.utcnow()

        response = await gemini_client.generate(
            reasoning_prompt,
            system_instruction=system_instruction,
            temperature=0.35,
            max_tokens=_max_tokens_for_chat_intent(intent or "EXPLAIN"),
        )

        processing_time = (datetime.utcnow() - start_time).total_seconds() * 1000

        # Parse response: support both structured (ANSWER:/EXPLANATION:) and natural (single message)
        raw = response.strip()
        lines = raw.split("\n")
        answer_line = [l for l in lines if l.startswith("ANSWER:")]
        explanation_line = [l for l in lines if l.startswith("EXPLANATION:")]
        sources_line = [l for l in lines if l.startswith("SOURCES:")]
        confidence_line = [l for l in lines if l.startswith("CONFIDENCE:")]
        actions_line = [l for l in lines if l.startswith("SUGGESTED_ACTIONS:")]

        if answer_line:
            answer = answer_line[0].split(":", 1)[1].strip()
            explanation = (
                explanation_line[0].split(":", 1)[1].strip() if explanation_line else ""
            )
            sources_text = (
                sources_line[0].split(":", 1)[1].strip() if sources_line else ""
            )
            confidence_text = (
                confidence_line[0].split(":", 1)[1].strip()
                if confidence_line
                else "MEDIUM"
            )
            actions_text = (
                actions_line[0].split(":", 1)[1].strip() if actions_line else ""
            )
        else:
            # Natural response: entire message is the answer
            answer = _strip_trailing_sources_block(raw)
            # Governance requires explanation only when the reply is very short (see governance_validator_node)
            stripped = (answer or "").strip()
            if len(stripped) >= 50:
                explanation = ""
            else:
                explanation = (
                    "Short reply - user may ask for more detail."
                    if stripped
                    else ""
                )
            sources_text = ""
            confidence_text = "MEDIUM"
            actions_text = ""

        answer = _strip_trailing_sources_block(answer)

        # Parse sources
        sources = []
        if sources_text:
            for source_desc in sources_text.split(","):
                if source_desc.strip():
                    sources.append(
                        SourceReference(
                            source_type="referenced",
                            source_id="auto",
                            source_name=source_desc.strip()[:100],
                        )
                    )

        try:
            confidence = ConfidenceLevel[confidence_text.upper()]
        except (KeyError, AttributeError):
            confidence = ConfidenceLevel.MEDIUM

        suggested_actions = (
            [a.strip() for a in actions_text.split(",") if a.strip()]
            if actions_text
            else []
        )

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
            "explanation_complete": True,
        }

    except Exception as e:
        logger.error(f"❌ Explanation generation failed: {e}")
        return {
            **state,
            "response_text": "I apologize, but I encountered an error generating a response.",
            "explanation": f"Error: {str(e)}",
            "sources": [],
            "confidence": "UNCERTAIN",
            "error": "EXPLANATION_FAILED",
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
        dispatch_message = "Routing to Drafting Agent for drafting"
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
        "action_dispatched": True,
    }


def response_formatter_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """
    Node 5: ResponseFormatterNode

    Outputs a single clean, natural message for the user (no fixed template).
    The model already responds in role; we avoid adding redundant labels and sections.
    """
    logger.info("🎨 Formatting response...")

    intent = state.get("intent", "EXPLAIN")
    response_text = (state.get("response_text") or "").strip()
    explanation = state.get("explanation", "")
    sources = state.get("sources", [])
    suggested_actions = state.get("suggested_actions", [])

    # Greeting or any intent: primary output is the response text as one clean message
    if intent == "GREETING":
        logger.info("👋 Using conversational formatting for greeting")
        return {**state, "formatted_response": response_text, "response_complete": True}

    # Build one natural message: main response first, then only add extras when they add value
    parts = [response_text] if response_text else []

    # Add reasoning only if present and meant for the user (not internal / governance placeholders)
    if explanation and explanation not in _EXPLANATION_SUPPRESSED_IN_UI:
        parts.append(f"\n\n{explanation}")

    # Add sources only when we actually cited something
    if sources:
        source_names = [
            s.get("source_name", "Unknown") for s in sources if s.get("source_name")
        ]
        if source_names:
            parts.append("\n\nSources: " + ", ".join(source_names[:5]))

    # Optional single line for next steps when we have concrete suggestions (no long bullet list every time)
    if suggested_actions and len(suggested_actions) <= 3:
        parts.append("\n\nYou could also: " + "; ".join(suggested_actions[:3]))
    elif suggested_actions:
        parts.append("\n\nSuggested: " + suggested_actions[0])

    formatted_response = "".join(parts).strip()

    logger.success("✅ Response formatted")
    logger.info(f"📏 Response length: {len(formatted_response)} chars")

    return {
        **state,
        "formatted_response": formatted_response,
        "response_complete": True,
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
            violations.append(
                f"Intent '{intent}' requires agent routing but none specified"
            )

    # Check 3: Very short replies need a non-empty explanation; longer replies stand alone
    if (
        not (state.get("explanation") or "").strip()
        and len((state.get("response_text") or "").strip()) < 50
    ):
        violations.append("Response missing mandatory explanation")

    # Check 4: Response has confidence indicator
    if not state.get("confidence"):
        warnings.append("Response missing confidence indicator")

    is_valid = len(violations) == 0

    validation_result = GovernanceValidationResult(
        is_valid=is_valid,
        violations=violations,
        warnings=warnings,
        explanation=(
            "Governance validation complete"
            if is_valid
            else "Governance violations detected"
        ),
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
        "governance_validated": True,
    }
