"""
Socket.IO event handlers with Intelligence Chat Agent integration.
Updated to match backend Socket.IO specification.
"""
from typing import Dict, Any, Callable, Awaitable, Optional, List
from loguru import logger
from datetime import datetime

from app.socket.schemas import (
    WorkspaceCreatedPayload,
    WorkspaceSyncPayload,
    UserMessageRequestPayload,
    AIResponsePayload,
    AIResponseMetadata
)
from app.workspace.manager import workspace_manager
from app.workspace.custom_instructions import set_custom_instructions, build_custom_instruction_suffix
from app.observability.audit import audit_logger
from app.utils.exceptions import SocketEventError
from app.langgraph.integration import workspace_role_integration
from app.agents.role_models import RoleEnum
from app.processing.document_processor import document_processor
from app.core.config import settings


EventHandler = Callable[[Dict[str, Any]], Awaitable[None]]


class SocketEventHandler:
    """
    Handles Socket.IO events and routes them to appropriate handlers.
    
    Per backend specification, AI service handles:
    - workspace_created: New workspace created
    - workspace_sync: Workspace updated
    - user_message_request: User sent chat message
    
    And emits:
    - ai_response: AI response back to server
    """
    
    def __init__(self):
        self._handlers: Dict[str, EventHandler] = {}
        self._socket_client = None  # Will be set by main service
        self._workspace_suggestion_chips: Dict[str, List[str]] = {}  # workspace_id -> 15-16 strategy chips
        self._workspace_chat_suggestion_chips: Dict[str, List[str]] = {}  # workspace_id -> 10-12 contextual chips (refreshed per AI reply; we return random 4 on each request)
        self._last_workspace_id_with_chips: Optional[str] = None  # fallback when request workspace has no chips
        self._register_default_handlers()
    
    def set_socket_client(self, client):
        """Set the socket client for emitting responses."""
        self._socket_client = client
        logger.debug("Socket client registered with event handler")
    
    def _register_default_handlers(self) -> None:
        """Register default event handlers."""
        # Connection events
        self.register("connect", self.on_connect)
        self.register("connected", self.on_connected)
        self.register("disconnect", self.on_disconnect)
        self.register("error", self.on_error)
        
        # Backend-specified events (AI_Maverick listens to these)
        self.register("workspace_created", self.on_workspace_created)
        self.register("workspace_sync", self.on_workspace_sync)
        self.register("user_message_request", self.on_user_message_request)
        self.register("probing_prompts_request", self.on_probing_prompts_request)
        
        # Legacy event handlers
        self.register("workspace_updated", self.on_workspace_updated)
        self.register("workspace_deleted", self.on_workspace_deleted)
        
        logger.info("✅ Event handlers registered")
    
    def register(self, event_name: str, handler: EventHandler) -> None:
        """
        Register an event handler.
        
        Args:
            event_name: Name of the event
            handler: Async function to handle the event
        """
        self._handlers[event_name] = handler
        logger.debug("Registered handler for event: {}", event_name)
    
    async def handle(self, event_name: str, data: Any) -> None:
        """
        Handle an incoming event.
        
        Args:
            event_name: Name of the event
            data: Event data
        """
        handler = self._handlers.get(event_name)
        
        if handler:
            try:
                logger.debug("Handling event: {} with data: {}", event_name, data)
                await handler(data)
            except Exception as e:
                logger.error("Error handling event {}: {}", event_name, str(e))
                logger.exception(e)
                raise SocketEventError(
                    f"Failed to handle event: {event_name}",
                    details={"event": event_name, "error": str(e)}
                )
        else:
            logger.warning("No handler registered for event: {}", event_name)
    
    # ========================================================================
    # Helper Methods
    # ========================================================================
    
    async def _generate_strategies_from_documents(
        self,
        workspace_id: str,
        user_id: str,
        workspace_role: str,
        batch_result: Optional[Dict[str, Any]] = None,
        is_update: bool = False
    ) -> Optional[Dict[str, Any]]:
        """
        Generate detailed, document-specific strategies and objectives using AI analysis.
        Uses ALL documents currently in the workspace (from vector store), in upload order.
        When documents are unrelated, AI is instructed to base strategy on the latest document only.

        Args:
            workspace_id: Workspace identifier
            user_id: User identifier
            workspace_role: User's workspace role
            batch_result: Optional document processing batch result (for readability stats); if None, uses only vector store
            is_update: Whether this is an update (new docs added / doc deleted) or initial generation

        Returns:
            Strategy generation result payload or None if failed
        """
        try:
            from app.vectorstore.qdrant import vector_store
            from app.ai.gemini_client import gemini_client

            logger.info(f"🧠 Generating detailed strategies for workspace {workspace_id} with role {workspace_role}")

            # Build documents_info from batch_result when available (for analysis metadata)
            documents_info = []
            if batch_result:
                for result in batch_result.get("results", []):
                    if result.get("success") and result.get("readable"):
                        documents_info.append({
                            "document_id": result.get("document_id"),
                            "filename": result.get("filename"),
                            "readability_score": result.get("readability_score", 0),
                            "text_length": result.get("extracted_text_length", 0),
                            "stored_in_vector": result.get("stored_in_vector_db", False)
                        })

            # Retrieve ALL document contents from vector store (in upload order: first → latest)
            all_docs = []
            try:
                all_docs = await vector_store.get_all_documents(workspace_id=workspace_id)
                logger.info(f"Retrieved {len(all_docs)} document(s) from vector store for strategy generation")
            except Exception as vector_error:
                logger.warning(f"Could not retrieve documents from vector store: {vector_error}")

            # Build combined content: label documents by order so AI can use "latest" when unrelated
            parts = []
            document_labels = []
            if not all_docs:
                combined_document_content = "No document content available."
            else:
                for i, doc in enumerate(all_docs, 1):
                    content = (doc.get("content") or "").strip()
                    if not content or len(content) < 20:
                        continue
                    label = f"Document {i} (most recently uploaded)" if i == len(all_docs) else f"Document {i}"
                    document_labels.append(label)
                    parts.append(f"--- {label} ---\n{content}")
                combined_document_content = "\n\n".join(parts) if parts else "No document content available."

            # Truncate if too long (Gemini has token limits), keep last document if multiple
            max_content_length = 12000
            if parts and len(combined_document_content) > max_content_length:
                latest_content = parts[-1]
                earlier = "\n\n".join(parts[:-1]) if len(parts) > 1 else ""
                if len(earlier) + len(latest_content) > max_content_length:
                    keep_earlier = max(0, max_content_length - len(latest_content) - 200)
                    combined_document_content = (earlier[:keep_earlier] + "...\n\n" if keep_earlier else "") + latest_content
                else:
                    combined_document_content = earlier + ("\n\n" + latest_content if earlier else latest_content)
                logger.info(f"Truncated combined content to ~{max_content_length} characters (keeping latest document)")
            elif len(combined_document_content) > max_content_length:
                combined_document_content = combined_document_content[:max_content_length] + "..."
                logger.info(f"Truncated document content to {max_content_length} characters")

            # When no documents (e.g. all deleted), return fallback so backend can clear strategy / show empty state
            doc_count = len(all_docs)
            if doc_count == 0:
                fallback_payload = {
                    "workspaceId": workspace_id,
                    "userId": user_id,
                    "role": workspace_role,
                    "isUpdate": is_update,
                    "isEmpty": True,
                    "caseSummary": "",
                    "strategicObjective": "",
                    "recommendations": [],
                    "possibleNextSteps": [],
                    "suggestionChips": [],
                    "analysis": {
                        "documentCount": 0,
                        "averageReadability": 0,
                        "totalDocuments": 0,
                        "successfulDocuments": 0,
                        "basedOnAIAnalysis": False
                    },
                    "timestamp": datetime.utcnow().isoformat()
                }
                logger.info("No documents in workspace; returning empty strategy payload (isEmpty=true)")
                return fallback_payload

            # Generate AI-powered case summary and strategic objective
            logger.info("🤖 Generating AI-powered case analysis (all documents, upload order)...")

            analysis_prompt = f"""You are a legal strategy analyst for Indian law. Analyze the following documents and provide a detailed strategy and objective.

**Role**: {workspace_role}
**Document Content** (documents are in upload order; the last one is the most recently uploaded):
{combined_document_content}

**Task**: Based on the document content above, provide:

1. **Case Summary** (5-6 sentences): A clear summary of what this case is about, including key facts, parties involved, and the legal issues.

2. **Strategic Objective** (5-6 sentences): A specific, actionable objective that outlines the best course of action for someone in the {workspace_role} role.

3. **Detailed Recommendations** (3-4 bullet points): Specific steps or considerations based on the case facts.

4. **Possible Next Steps** (3-5 bullet points): Immediate, actionable next steps that should be taken right away. These should be concrete, specific actions that can be executed immediately (e.g., "File FIR at local police station", "Preserve digital evidence", "Contact legal counsel", "Gather witness statements"). Focus on immediate actions rather than long-term strategy.

**Format your response as JSON**:
{{
  "caseSummary": "...",
  "strategicObjective": "...",
  "recommendations": ["...", "...", "..."],
  "possibleNextSteps": ["...", "...", "...", "..."]
}}

**Important**: 
- Be specific to the case facts in the documents
- Use Indian legal terminology appropriately
- Reference BNS (Bharatiya Nyaya Sanhita) sections, NOT IPC (Indian Penal Code is replaced)
- Reference BNSS (Bharatiya Nagarik Suraksha Sanhita) for procedural matters, NOT CrPC
- Reference BSA (Bharatiya Sakshya Adhiniyam) for evidence matters, NOT Indian Evidence Act
- For IT Act, continue using Information Technology Act, 2000 sections as it's still in force
- Make recommendations actionable and role-specific
- If documents don't contain enough information, acknowledge that and provide general guidance
- **If the documents appear unrelated** (different cases, different matters, or no clear connection), base your analysis ONLY on **Document N (the most recently uploaded)** and ignore the others. Otherwise base your analysis on all documents combined."""

            try:
                # Improved prompt for better JSON generation
                structured_prompt = f"""Analyze these legal documents and provide a JSON response.

DOCUMENT CONTENT:
{combined_document_content}

YOUR ROLE: {workspace_role}

TASK:
1. Case summary (5-6 sentences with key facts and parties)
2. Strategic objective (5-6 sentences specific to {workspace_role})
3. Four actionable recommendations
4. Possible next steps (3-5 immediate, concrete actions that should be taken right away)

LEGAL FRAMEWORK (CRITICAL):
- Use BNS (Bharatiya Nyaya Sanhita) - NOT IPC
- Use BNSS (Bharatiya Nagarik Suraksha Sanhita) - NOT CrPC  
- Use BSA (Bharatiya Sakshya Adhiniyam) - NOT Evidence Act
- IT Act 2000 remains valid

UNRELATED DOCUMENTS: If the documents appear to be from different cases or unrelated matters, base your analysis ONLY on the last document (labeled "most recently uploaded"). Otherwise use all documents combined.

OUTPUT (JSON only, complete all fields):
```json
{{
  "caseSummary": "Concise summary with facts, parties, and key issues",
  "strategicObjective": "Clear objective tailored for {workspace_role} role",
  "recommendations": [
    "Specific action 1 citing BNS/BNSS sections",
    "Specific action 2 with legal basis",
    "Specific action 3 with procedural steps",
    "Specific action 4 with strategic outcome"
  ],
  "possibleNextSteps": [
    "Immediate action 1 (e.g., File FIR, Preserve evidence)",
    "Immediate action 2 (e.g., Contact relevant authority)",
    "Immediate action 3 (e.g., Gather documents)",
    "Immediate action 4 (e.g., Consult legal counsel)",
    "Immediate action 5 (e.g., Secure witness statements)"
  ],
  "suggestionChips": [
    "Chip 1", "Chip 2", "Chip 3", "Chip 4", "Chip 5", "Chip 6", "Chip 7", "Chip 8",
    "Chip 9", "Chip 10", "Chip 11", "Chip 12", "Chip 13", "Chip 14", "Chip 15", "Chip 16"
  ]
}}
```

suggestionChips: exactly 15-16 short, actionable next-step labels (2-5 words each) specific to this case and role. Examples: "Review Title Deeds", "Map Legal Heirs", "Draft Demand Notice", "File FIR", "Preserve Digital Evidence". No full sentences. Provide 15-16 varied chips."""
                custom_suffix = build_custom_instruction_suffix(workspace_id)
                if custom_suffix:
                    structured_prompt = structured_prompt.rstrip() + "\n\n" + custom_suffix
                ai_response = await gemini_client.generate(
                    prompt=structured_prompt,
                    temperature=0.4,  # Lower temperature for more structured output
                    max_tokens=8000  # Increased to allow complete responses
                )
                
                logger.info(f"AI generated {len(ai_response)} characters of analysis")
                logger.info("=" * 80)
                logger.info("🤖 RAW AI RESPONSE:")
                logger.info("-" * 80)
                logger.info(ai_response)
                logger.info("=" * 80)
                
                # Try to parse JSON response with multiple strategies
                import json
                import re
                
                # Clean the response
                ai_response = ai_response.strip()
                
                # Strategy 1: Look for JSON in markdown code blocks (```json ... ```)
                json_match = re.search(r'```json\s*(\{.*?\})\s*```', ai_response, re.DOTALL)
                if json_match:
                    json_str = json_match.group(1)
                    logger.info("✅ Found complete JSON in markdown code block")
                # Strategy 2: Look for JSON starting after ```json (might be incomplete)
                elif '```json' in ai_response:
                    # Extract everything after ```json
                    start = ai_response.find('```json') + 7
                    remaining = ai_response[start:].strip()
                    # Try to find the closing brace
                    if remaining.startswith('{'):
                        # Count braces to find matching close
                        brace_count = 0
                        end_pos = 0
                        for i, char in enumerate(remaining):
                            if char == '{':
                                brace_count += 1
                            elif char == '}':
                                brace_count -= 1
                                if brace_count == 0:
                                    end_pos = i + 1
                                    break
                        
                        if end_pos > 0:
                            json_str = remaining[:end_pos]
                            logger.info("✅ Extracted JSON from markdown block (possibly incomplete response)")
                        else:
                            # Incomplete JSON, try to complete it
                            json_str = remaining
                            if not json_str.rstrip().endswith('}'):
                                # Add closing braces
                                open_braces = json_str.count('{') - json_str.count('}')
                                json_str = json_str.rstrip() + '}' * open_braces
                                logger.warning(f"⚠️ Completed truncated JSON by adding {open_braces} closing braces")
                    else:
                        json_str = None
                        logger.warning("Found ```json marker but no JSON object")
                # Strategy 3: Look for JSON in plain code blocks
                elif re.search(r'```\s*(\{.*?\})\s*```', ai_response, re.DOTALL):
                    json_match = re.search(r'```\s*(\{.*?\})\s*```', ai_response, re.DOTALL)
                    json_str = json_match.group(1)
                    logger.info("✅ Found JSON in plain code block")
                # Strategy 4: Look for standalone JSON
                elif ai_response.startswith('{'):
                    json_str = ai_response
                    # Check if complete
                    if json_str.count('{') == json_str.count('}'):
                        logger.info("✅ Found complete standalone JSON")
                    else:
                        logger.warning("⚠️ Found incomplete standalone JSON, attempting to complete")
                        open_braces = json_str.count('{') - json_str.count('}')
                        json_str = json_str.rstrip() + '}' * open_braces
                # Strategy 5: Extract any JSON object from text
                else:
                    json_match = re.search(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', ai_response, re.DOTALL)
                    if json_match:
                        json_str = json_match.group(0)
                        logger.info("✅ Extracted JSON from text")
                    else:
                        json_str = None
                        logger.warning("❌ No JSON found in AI response")
                
                if json_str:
                    # Try to parse JSON
                    try:
                        analysis_data = json.loads(json_str)
                        
                        case_summary = analysis_data.get("caseSummary", "")
                        strategic_objective = analysis_data.get("strategicObjective", "")
                        recommendations = analysis_data.get("recommendations", [])
                        possible_next_steps = analysis_data.get("possibleNextSteps") or analysis_data.get("possible_next_steps") or []
                        if isinstance(possible_next_steps, str):
                            possible_next_steps = [possible_next_steps] if possible_next_steps else []
                        possible_next_steps = [str(s).strip() for s in possible_next_steps if s][:5]
                        suggestion_chips = analysis_data.get("suggestionChips") or analysis_data.get("suggestion_chips") or []
                        if isinstance(suggestion_chips, str):
                            suggestion_chips = [suggestion_chips] if suggestion_chips else []
                        suggestion_chips = [str(s).strip() for s in suggestion_chips if s][:16]
                        
                        # Validate we got meaningful content
                        if case_summary and strategic_objective and recommendations:
                            logger.success("✅ AI analysis completed successfully")
                            logger.info(f"   - Case Summary: {len(case_summary)} chars")
                            logger.info(f"   - Strategic Objective: {len(strategic_objective)} chars")
                            logger.info(f"   - Recommendations: {len(recommendations)} items")
                            logger.info(f"   - Possible Next Steps: {len(possible_next_steps)} items")
                            logger.info(f"   - Suggestion chips: {len(suggestion_chips)} items")
                        else:
                            logger.warning("⚠️ AI response incomplete, using partial data + fallback")
                            # Use what we got and fill in missing pieces
                            if not case_summary:
                                case_summary = f"Legal matter for {workspace_role} role based on {doc_count} document(s). The documents have been analyzed and contain important case information."
                            if not strategic_objective:
                                strategic_objective = "Conduct a comprehensive legal review to determine the optimal course of action based on the available evidence and applicable BNS provisions."
                            if not recommendations or len(recommendations) < 3:
                                recommendations = recommendations + [
                                    "Review all documentary evidence thoroughly",
                                    "Analyze applicable BNS and BNSS provisions",
                                    "Assess evidence strength under BSA",
                                    "Develop detailed action plan"
                                ][:4]  # Ensure we have at least 4
                            if not possible_next_steps or len(possible_next_steps) < 3:
                                possible_next_steps = possible_next_steps + [
                                    "Review all uploaded documents immediately",
                                    "Preserve and secure all evidence",
                                    "Consult with legal counsel",
                                    "Identify and contact relevant parties",
                                    "Prepare initial documentation"
                                ][:5]  # Ensure we have at least 3-5
                            if not suggestion_chips:
                                suggestion_chips = [
                                    "Review documents", "Analyze provisions", "Draft action plan",
                                    "Review evidence", "File legal action", "Preserve documents",
                                    "Map legal heirs", "Draft demand notice", "Check BNS provisions",
                                    "Assess evidence", "Follow BNSS steps", "Organize documents",
                                    "Consult applicable laws", "Prepare chronology", "Identify parties", "Plan next steps"
                                ][:16]
                            logger.info("✅ Using hybrid AI + fallback content")
                            
                    except json.JSONDecodeError as json_error:
                        logger.error(f"❌ JSON parsing failed: {json_error}")
                        logger.info(f"Failed JSON string (first 500 chars): {json_str[:500]}")
                        
                        # Try to extract partial information using regex
                        # Handle cases where string values are incomplete (no closing quote)
                        case_summary_match = re.search(r'"caseSummary"\s*:\s*"([^"]*)', json_str)
                        objective_match = re.search(r'"strategicObjective"\s*:\s*"([^"]*)', json_str)
                        recommendations_match = re.findall(r'"([^"]+)"\s*(?:,|\])', json_str)
                        
                        if case_summary_match or objective_match:
                            logger.info("✅ Extracted partial data from malformed JSON")
                            case_summary = case_summary_match.group(1) if case_summary_match else ""
                            strategic_objective = objective_match.group(1) if objective_match else ""
                            
                            # Extract recommendations and possible next steps from the partial JSON
                            recommendations = []
                            possible_next_steps = []
                            suggestion_chips = []
                            if recommendations_match and len(recommendations_match) > 2:
                                # Filter out field names, keep only actual recommendations
                                potential_recs = [r for r in recommendations_match 
                                                if r not in ['caseSummary', 'strategicObjective', 'recommendations', 'possibleNextSteps', 'possible_next_steps']
                                                and len(r) > 20]  # Recommendations should be substantial
                                recommendations = potential_recs[:4]  # Take up to 4
                                # Try to extract possible next steps if present
                                possible_next_steps_match = re.search(r'"possibleNextSteps"\s*:\s*\[(.*?)\]', json_str, re.DOTALL)
                                if not possible_next_steps_match:
                                    possible_next_steps_match = re.search(r'"possible_next_steps"\s*:\s*\[(.*?)\]', json_str, re.DOTALL)
                                if possible_next_steps_match:
                                    next_steps_inner = possible_next_steps_match.group(1)
                                    next_steps = re.findall(r'"([^"]+)"', next_steps_inner)
                                    possible_next_steps = [s.strip() for s in next_steps if s.strip()][:5]
                            
                            logger.info(f"   Extracted: Summary={bool(case_summary)}, Objective={bool(strategic_objective)}, Recs={len(recommendations)}")
                            
                            # Fill in missing pieces
                            if not case_summary:
                                raise  # Can't salvage without case summary, use full fallback
                            if not strategic_objective:
                                strategic_objective = "Conduct a comprehensive legal review based on the case facts to determine the optimal course of action under BNS."
                            if not recommendations or len(recommendations) < 3:
                                # Add fallback recommendations
                                default_recs = [
                                    "File appropriate legal action under relevant BNS provisions",
                                    "Preserve and organize all evidence following BSA guidelines",
                                    "Follow BNSS procedural requirements for the case",
                                    "Develop comprehensive strategy based on case specifics"
                                ]
                                recommendations.extend(default_recs)
                                recommendations = recommendations[:4]  # Keep only 4
                            if not suggestion_chips:
                                suggestion_chips = [
                                    "Review evidence", "File legal action", "Preserve documents",
                                    "Review documents", "Analyze provisions", "Draft action plan",
                                    "Map legal heirs", "Draft demand notice", "Check BNS provisions",
                                    "Assess evidence", "Follow BNSS steps", "Organize documents",
                                    "Consult applicable laws", "Prepare chronology", "Identify parties"
                                ][:16]
                            
                            logger.success("✅ Using hybrid: AI-extracted content + fallback where needed")
                        else:
                            raise  # No usable content, use full fallback
                else:
                    logger.error("❌ Could not extract JSON from AI response")
                    raise ValueError("No JSON in response")
                
            except Exception as ai_error:
                logger.error(f"AI analysis failed: {ai_error}")
                logger.warning("Using fallback generic analysis")
                # Fallback to generic analysis
                case_summary = f"Analysis of {doc_count} document(s) for {workspace_role} role. The documents have been processed and are ready for detailed review under Indian legal framework (BNS, BNSS, BSA)."
                strategic_objective = "Conduct a comprehensive legal review to determine the optimal course of action based on the available evidence and applicable provisions under Bharatiya Nyaya Sanhita (BNS) and other relevant laws."
                recommendations = [
                    "Review all uploaded documents thoroughly to identify key facts",
                    "Analyze applicable provisions under BNS (Bharatiya Nyaya Sanhita) and BNSS (Bharatiya Nagarik Suraksha Sanhita)",
                    "Assess the strength of available evidence under BSA (Bharatiya Sakshya Adhiniyam)",
                    "Develop a detailed action plan with specific legal remedies and procedural steps"
                ]
                possible_next_steps = [
                    "Review all uploaded documents immediately",
                    "Preserve and secure all evidence",
                    "Consult with legal counsel",
                    "Identify and contact relevant parties",
                    "Prepare initial documentation"
                ]
                suggestion_chips = [
                    "Review documents", "Analyze BNS provisions", "Assess evidence", "Draft action plan",
                    "Review evidence", "File legal action", "Preserve documents", "Map legal heirs",
                    "Draft demand notice", "Check BNS provisions", "Follow BNSS steps", "Organize documents",
                    "Consult applicable laws", "Prepare chronology", "Identify parties", "Plan next steps"
                ][:16]
            
            # Format the response (use doc_count from vector store when batch_result not provided, e.g. after delete)
            strategies_payload = {
                "workspaceId": workspace_id,
                "userId": user_id,
                "role": workspace_role,
                "isUpdate": is_update,
                "caseSummary": case_summary,
                "strategicObjective": strategic_objective,
                "recommendations": recommendations,
                "possibleNextSteps": possible_next_steps if possible_next_steps else [],
                "suggestionChips": suggestion_chips if suggestion_chips else [],
                "analysis": {
                    "documentCount": doc_count,
                    "averageReadability": (batch_result or {}).get("average_readability_score", 0),
                    "totalDocuments": (batch_result or {}).get("total_files", doc_count),
                    "successfulDocuments": (batch_result or {}).get("successful", doc_count),
                    "basedOnAIAnalysis": True
                },
                "timestamp": datetime.utcnow().isoformat()
            }
            
            logger.success(f"✅ Generated detailed strategy analysis for workspace {workspace_id}")
            
            return strategies_payload
            
        except Exception as e:
            logger.error(f"Error generating strategies: {e}")
            logger.exception(e)
            return None

    def _salvage_workspace_metadata_from_truncated_json(self, raw: str, combined: str = "") -> Dict[str, Any]:
        """Extract caseTitle, caseCategory, subcategories and description from truncated Gemini JSON."""
        import re
        result = {"caseTitle": "", "caseCategory": "General", "subcategories": [], "description": ""}
        if not raw:
            return result
        # Salvage caseTitle: "caseTitle": "VALUE" (for document-based display title)
        title_match = re.search(r'"caseTitle"\s*:\s*"([^"]*)"', raw)
        if not title_match:
            title_match = re.search(r'"caseTitle"\s*:\s*"([^"]*)$', raw)
        if title_match:
            val = title_match.group(1).strip()
            if val:
                result["caseTitle"] = val
        # Salvage caseCategory
        if "caseCategory" in raw:
            cat_match = re.search(r'"caseCategory"\s*:\s*"([^"]*)"', raw)
            if not cat_match:
                cat_match = re.search(r'"caseCategory"\s*:\s*"([^"]*)$', raw)
            if cat_match:
                val = cat_match.group(1).strip().upper()
                if val and len(val) >= 2:
                    result["caseCategory"] = val if ("LAW" in val or "ACT" in val) else (val + " LAW")
        # Salvage subcategories
        sub_match = re.search(r'"subcategories"\s*:\s*\[(.*?)\]', raw, re.DOTALL)
        if not sub_match:
            sub_match = re.search(r'"subcategories"\s*:\s*\[(.*)$', raw, re.DOTALL)
        if sub_match:
            inner = sub_match.group(1)
            subs = re.findall(r'"([^"]+)"', inner)
            result["subcategories"] = [s.strip() for s in subs if s.strip()][:5]
        # Salvage description (short case summary, 3-4 lines only)
        desc_match = re.search(r'"description"\s*:\s*"([^"]*)"', raw)
        if not desc_match:
            desc_match = re.search(r'"description"\s*:\s*"([^"]*)$', raw)
        if desc_match:
            val = desc_match.group(1).strip()
            if val and self._is_valid_short_summary(val):
                lines = [ln.strip() for ln in val.replace("\\n", "\n").split("\n") if ln.strip()]
                if len(lines) < 3:
                    while len(lines) < 3:
                        lines.append("Additional details are provided in the supporting documents.")
                elif len(lines) > 4:
                    lines = lines[:4]
                lines = [ln[:120] if len(ln) > 120 else ln for ln in lines]
                result["description"] = "\n".join(lines)[:500]
        return result

    def _is_valid_short_summary(self, description: str) -> bool:
        """Return False if description looks like raw document paste or is not a short summary."""
        if not description or not description.strip():
            return False
        # Reject if it contains document labels or raw paste
        if "--- Document" in description or "Document 1 ---" in description or "Document 2 ---" in description:
            return False
        # Reject if too long (short summary should be at most ~500 chars for 3-4 lines)
        if len(description) > 550:
            return False
        # Reject if more than 4 lines
        lines = [ln.strip() for ln in description.split("\n") if ln.strip()]
        if len(lines) > 4:
            return False
        # Reject if any single line is very long (likely raw text)
        if any(len(ln) > 200 for ln in lines):
            return False
        return True

    def _is_generic_description(self, text: str) -> bool:
        """Return True if description is the known generic placeholder."""
        if not text or len(text.strip()) < 20:
            return True
        generic_phrases = [
            "Legal matter based on uploaded documents",
            "The case involves parties and issues requiring legal review",
            "The matter requires appropriate legal action",
            "Supporting documents provide further context",
            "parties and issues requiring legal review",  # Partial match
            "appropriate legal action",  # Partial match
        ]
        # Check if it contains the exact generic block or 2+ generic phrases
        exact_generic = "Legal matter based on uploaded documents" in text and "The case involves parties and issues requiring legal review" in text
        phrase_count = sum(1 for p in generic_phrases if p in text)
        return exact_generic or phrase_count >= 2

    async def _generate_case_summary_from_documents(self, combined: str) -> str:
        """Use AI to generate a case-specific 3-4 line summary from document content. Works for any language."""
        import re
        if not combined or len(combined.strip()) < 30:
            return ""
        try:
            from app.ai.gemini_client import gemini_client
            gemini_client.initialize()
            doc_content = combined[:4000].replace("--- Document", "\nDocument")

            for attempt in range(2):  # First attempt + one retry if generic
                if attempt == 0:
                    prompt = f"""You are summarizing a legal case document. The document may be in Hindi, English, or mixed languages.

TASK: Write a SHORT CASE SUMMARY in English. Requirements:
- Exactly 3 or 4 lines. Each line = one short sentence (max 120 characters).
- Line 1: Name the ACTUAL parties (real names of people/organizations) and the SPECIFIC dispute or issue (e.g. "contract breach", "social media hacking", "defamation", "cricket association dispute").
- Line 2: State ONE or TWO concrete facts or allegations from the document (be specific, not vague).
- Line 3: State the SPECIFIC relief, investigation, or action sought (e.g. "seeks FIR", "requests damages", "demands investigation").
- Line 4 (optional): One more specific detail from the case.

FORBIDDEN PHRASES - DO NOT USE THESE EXACT WORDS:
- "Legal matter based on uploaded documents"
- "The case involves parties and issues requiring legal review"
- "The matter requires appropriate legal action"
- "Supporting documents provide further context"

You MUST extract and name the REAL parties and REAL dispute from the document. If the document mentions names like "Umesh Malik", "BCCI", "Karnataka State Cricket Association", "Shubham Malik", etc., USE THOSE NAMES. If it mentions hacking, contract issues, defamation, etc., STATE THAT SPECIFIC ISSUE.

IMPORTANT: Output exactly 3 or 4 lines. Put each sentence on its own line (use a newline after each sentence). Do not output a single paragraph.

Document content:
{doc_content}

Case summary (3-4 lines, one sentence per line):"""
                else:
                    prompt = f"""CRITICAL: Your previous response was too generic. For the SAME document below, write a SPECIFIC case summary.

You MUST extract REAL information:
- Line 1: Find and name the ACTUAL complainant/petitioner and respondent/defendant from the document (e.g. if document says "Umesh Malik" or "BCCI" or "Karnataka State Cricket Association", USE THOSE NAMES). Then state the SPECIFIC dispute type (e.g. "social media account hacking", "contract violation", "defamation", "cricket association dispute").
- Line 2: Extract ONE concrete fact from the document (e.g. "hacked social media account", "edited videos made viral", "contract terms violated", "payment not received").
- Line 3: Extract the SPECIFIC relief sought (e.g. "seeks FIR under IT Act", "requests investigation", "demands compensation", "requests contract enforcement").
- Line 4 (optional): One more specific detail.

DO NOT use these phrases: "Legal matter based on uploaded documents", "parties and issues requiring legal review", "appropriate legal action", "Supporting documents provide further context".

Read the document carefully and extract REAL names and REAL facts. Output exactly 3 or 4 lines. Put each sentence on its own line (newline after each sentence).

Document content:
{doc_content}

Specific case summary (3-4 lines, one sentence per line):"""

                raw = await gemini_client.generate(prompt, temperature=0.2 if attempt == 0 else 0.4, max_tokens=8000)
                if not raw:
                    logger.warning("AI returned empty response for case summary (attempt {})", attempt + 1)
                    continue
                # Split by newline first
                lines = [ln.strip() for ln in raw.strip().split("\n") if ln.strip()][:4]
                # If model returned 1–2 lines (e.g. one paragraph), split by sentence to get 3–4 lines
                if len(lines) < 3 and lines:
                    combined_text = " ".join(lines)
                    logger.debug("Attempting sentence split on: {}", combined_text[:100])
                    # Split on sentence endings: period, exclamation, or question mark followed by space or end
                    # Use lookahead to keep the punctuation
                    sentences = re.split(r"([.!?])\s+", combined_text)
                    # Reconstruct sentences with punctuation
                    reconstructed = []
                    for i in range(0, len(sentences) - 1, 2):
                        if i + 1 < len(sentences):
                            sentence = (sentences[i] + sentences[i + 1]).strip()
                        else:
                            sentence = sentences[i].strip()
                        if sentence and len(sentence) > 8:  # Lower threshold for short valid sentences
                            # Ensure sentence ends with punctuation
                            if not re.search(r"[.!?]$", sentence):
                                sentence += "."
                            reconstructed.append(sentence)
                    # If we didn't get good splits, try simpler period-based split
                    if len(reconstructed) < 2:
                        sentences = re.split(r"\.\s+", combined_text)
                        reconstructed = []
                        for s in sentences:
                            s = s.strip()
                            if s and len(s) > 8:
                                if not s.endswith("."):
                                    s += "."
                                reconstructed.append(s)
                    if len(reconstructed) >= 2:  # Accept 2+ sentences (better than failing)
                        lines = reconstructed[:4]
                        logger.info("Sentence split successful: {} sentences from {} lines", len(lines), len([ln.strip() for ln in raw.strip().split("\n") if ln.strip()]))
                if len(lines) < 2:  # Require at least 2 lines (be more lenient)
                    logger.warning("AI returned only {} lines (need at least 2) for case summary (attempt {})", len(lines), attempt + 1)
                    continue
                summary = "\n".join(lines[:4])
                lines = [ln[:120] if len(ln) > 120 else ln for ln in summary.split("\n")]
                summary = "\n".join(lines)[:500]
                if not self._is_valid_short_summary(summary):
                    logger.warning("AI summary failed validation (attempt {}): {}", attempt + 1, summary[:100])
                    continue
                if self._is_generic_description(summary):
                    logger.warning("AI returned generic description (attempt {}): {}", attempt + 1, summary[:100])
                    if attempt == 0:
                        logger.info("Retrying with stricter prompt")
                    continue
                logger.info("Successfully generated case-specific description (attempt {})", attempt + 1)
                return summary

            logger.error("Failed to generate case-specific description after {} attempts", 2)
            # Try simple fallback before giving up
            logger.info("Attempting simple fallback for case summary generation")
            simple_result = await self._generate_case_summary_from_documents_simple(combined)
            if simple_result:
                logger.info("Simple fallback succeeded: {}", simple_result[:80])
                return simple_result
            return ""
        except Exception as e:
            logger.warning("AI case summary generation failed: {}", str(e))
            # Try simple fallback on exception too
            try:
                simple_result = await self._generate_case_summary_from_documents_simple(combined)
                if simple_result:
                    logger.info("Simple fallback succeeded after exception: {}", simple_result[:80])
                    return simple_result
            except Exception:
                pass
            return ""

    async def _generate_case_summary_from_documents_simple(self, combined: str) -> str:
        """Simpler fallback: generate case summary with minimal prompt when main method fails."""
        import re
        if not combined or len(combined.strip()) < 30:
            return ""
        try:
            from app.ai.gemini_client import gemini_client
            gemini_client.initialize()
            doc_content = combined[:3000].replace("--- Document", "\nDocument")
            prompt = f"""Summarize this legal case in exactly 3-4 short sentences in English. Name the actual parties and the actual dispute. Be specific. Put each sentence on its own line (newline after each sentence).

{doc_content}

Summary (3-4 lines, one sentence per line):"""
            raw = await gemini_client.generate(prompt, temperature=0.5, max_tokens=400)
            if not raw:
                return ""
            lines = [ln.strip() for ln in raw.strip().split("\n") if ln.strip()][:4]
            if len(lines) < 2 and lines:  # More lenient: accept 2+ lines
                combined_text = " ".join(lines)
                # Split on sentence endings
                sentences = re.split(r"([.!?])\s+", combined_text)
                reconstructed = []
                for i in range(0, len(sentences) - 1, 2):
                    if i + 1 < len(sentences):
                        sentence = (sentences[i] + sentences[i + 1]).strip()
                    else:
                        sentence = sentences[i].strip()
                    if sentence and len(sentence) > 8:
                        if not re.search(r"[.!?]$", sentence):
                            sentence += "."
                        reconstructed.append(sentence)
                # Fallback to simple period split
                if len(reconstructed) < 2:
                    sentences = re.split(r"\.\s+", combined_text)
                    reconstructed = []
                    for s in sentences:
                        s = s.strip()
                        if s and len(s) > 8:
                            if not s.endswith("."):
                                s += "."
                            reconstructed.append(s)
                if len(reconstructed) >= 2:
                    lines = reconstructed[:4]
            if len(lines) < 2:  # Require at least 2 lines
                return ""
            summary = "\n".join(lines[:4])
            lines = [ln[:120] if len(ln) > 120 else ln for ln in summary.split("\n")]
            summary = "\n".join(lines)[:500]
            if self._is_valid_short_summary(summary) and not self._is_generic_description(summary):
                return summary
            return ""
        except Exception:
            return ""

    def _generate_case_description_from_content(self, doc_text: str) -> str:
        """Synchronous fallback: generate short summary from content. Prefer _generate_case_summary_from_documents (async) for case-specific text."""
        if not doc_text or len(doc_text.strip()) < 50:
            return "Legal matter based on uploaded documents.\nThe case involves parties and issues requiring legal review.\nThe matter requires appropriate legal action.\nSupporting documents provide further context."
        
        import re
        # Match "--- Document N ---" (avoid [^---] set-difference warning: use [^-]* = any chars except dash)
        cleaned = re.sub(r"---\s*Document\s*\d+[^-]*---", " ", doc_text, flags=re.IGNORECASE)
        cleaned = cleaned.strip()
        if len(cleaned) < 30:
            cleaned = doc_text
        
        sentences = []
        for s in cleaned.split("."):
            s = s.strip()
            if len(s) < 15 or len(s) > 180:
                continue
            if s.startswith("---") or "Document" in s or s.isdigit():
                continue
            sentences.append(s + ".")
        
        short_sentences = [s for s in sentences if len(s) <= 120][:4]
        if len(short_sentences) >= 3:
            return "\n".join(short_sentences[:4])
        return "Legal matter based on uploaded documents.\nThe case involves parties and issues requiring legal review.\nThe matter requires appropriate legal action.\nSupporting documents provide further context."

    def _ensure_metadata_complete(self, metadata: Dict[str, Any], combined: str = "") -> Dict[str, Any]:
        """Ensure all metadata fields have non-empty values."""
        result = metadata.copy()
        # Ensure category
        if not result.get("caseCategory") or result.get("caseCategory") == "General":
            result["caseCategory"] = "CIVIL LAW"
        elif "LAW" not in result["caseCategory"] and "ACT" not in result["caseCategory"]:
            result["caseCategory"] = result["caseCategory"] + " LAW"
        # Ensure subcategories
        if not result.get("subcategories"):
            result["subcategories"] = ["Bharatiya Nyaya Sanhita"] if "CRIMINAL" in result.get("caseCategory", "") else ["Indian Contract Act, 1872"]
        # Ensure description (3-4 lines minimum) - must be a short summary, not raw document text
        desc = result.get("description") or ""
        if not desc.strip() or not self._is_valid_short_summary(desc):
            result["description"] = ""
        if not result.get("description"):
            # Only use generic fallback if we have NO document content
            # If we have document content, async path should have generated description
            # If async failed, leave empty rather than use generic (better than wrong description)
            if not combined or len(combined.strip()) < 50:
                result["description"] = "Legal matter based on uploaded documents.\nThe case involves parties and issues requiring legal review.\nThe matter requires appropriate legal action.\nSupporting documents provide further context."
            else:
                # We have document content but description is empty - async generation likely failed
                # Try sync extraction as last resort, but only if it returns non-generic content
                doc_text = combined[:800].replace("\n", " ")
                extracted = self._generate_case_description_from_content(doc_text)
                # Only use if it's not the generic fallback
                if extracted and not self._is_generic_description(extracted):
                    result["description"] = extracted
                else:
                    # Leave empty - better than generic placeholder
                    logger.warning("Could not generate case-specific description; leaving empty")
                    result["description"] = ""
        else:
            # Ensure existing description has 3-4 lines and is case-specific
            desc_lines = [ln.strip() for ln in result["description"].split("\n") if ln.strip()]
            # Check if description is generic - if so, clear it (don't regenerate with sync method which also returns generic)
            generic_phrases = ["Legal matter based", "The case involves parties", "The matter requires", "Supporting documents"]
            is_generic = any(phrase in result["description"] for phrase in generic_phrases)
            
            if is_generic:
                # Clear generic description - async path should have already tried to generate case-specific
                # Don't use sync _generate_case_description_from_content as it also returns generic
                logger.warning("Description is generic, clearing it (should have been replaced by async AI generation)")
                result["description"] = ""
                desc_lines = []
            
            if len(desc_lines) < 3:
                # Pad with short generic lines only (never use raw document text)
                while len(desc_lines) < 3:
                    desc_lines.append("Additional details are provided in the supporting documents.")
                result["description"] = "\n".join(desc_lines)
            elif len(desc_lines) > 4:
                result["description"] = "\n".join(desc_lines[:4])
            # Ensure no line is too long (short summary only)
            desc_lines = [ln[:120] if len(ln) > 120 else ln for ln in (result["description"].split("\n"))]
            result["description"] = "\n".join(desc_lines)[:500]
        # Ensure title
        if not result.get("caseTitle"):
            result["caseTitle"] = "Petitioner vs Respondent"
        # Ensure title is in "vs" format
        title = result.get("caseTitle", "")
        if title and " vs " not in title and " v " not in title:
            if " vs" in title or "vs " in title:
                title = title.replace(" vs", " vs ").replace("vs ", " vs ")
            else:
                title = f"{title} vs Matter"
            result["caseTitle"] = title
        return result

    async def _generate_fallback_metadata(self, combined: str, workspace_id: str) -> Dict[str, Any]:
        """Generate fallback metadata when AI parsing fails - ensures all fields have values."""
        try:
            from app.ai.gemini_client import gemini_client
            gemini_client.initialize()
            doc_preview = combined[:400].replace("\n", " ")
            fallback_prompt = f"""From this document content, extract ACTUAL CASE DETAILS:
1. Two parties/names for title format "Party1 vs Party2" (or "Party vs Issue" if only one)
2. One legal category (CRIMINAL LAW, CIVIL LAW, CYBER LAW, etc.)
3. One relevant Indian act
4. A SHORT CASE SUMMARY in English only (3-4 lines). Summarize in your own words; do NOT copy-paste document text. Each line one short sentence.

Document: {doc_preview}

Return JSON: {{"caseTitle": "Name1 vs Name2", "caseCategory": "CIVIL LAW", "subcategories": ["Act Name"], "description": "Line 1: Actual party and issue from documents\\nLine 2: Key facts/allegations from documents\\nLine 3: Relief sought from documents\\nLine 4: Additional relevant details"}}"""
            raw = await gemini_client.generate(fallback_prompt, temperature=0.3, max_tokens=300)
            import json
            import re
            start = raw.find("{")
            if start >= 0:
                end = raw.find("}", start) + 1
                if end > start:
                    try:
                        data = json.loads(raw[start:end])
                        title = (data.get("caseTitle") or "").strip()
                        category = (data.get("caseCategory") or "CIVIL LAW").strip().upper()
                        subs = data.get("subcategories") or []
                        desc = (data.get("description") or "").strip()
                        if not title:
                            title = "Petitioner vs Respondent"
                        if " vs " not in title and " v " not in title:
                            title = f"{title} vs Matter"
                        if not category or category == "General":
                            category = "CIVIL LAW"
                        if "LAW" not in category and "ACT" not in category:
                            category = category + " LAW"
                        if not subs:
                            subs = ["Bharatiya Nyaya Sanhita"] if "CRIMINAL" in category else ["Indian Contract Act, 1872"]
                        if not desc and combined and len(combined.strip()) > 50:
                            desc = await self._generate_case_summary_from_documents(combined)
                        if not desc:
                            desc = self._generate_case_description_from_content(doc_preview)
                        else:
                            # Ensure 3-4 lines and case-specific
                            desc_lines = [ln.strip() for ln in desc.split("\n") if ln.strip()]
                            # Check if generic - regenerate from content if so
                            generic_phrases = ["Legal matter based", "The case involves parties", "The matter requires", "Supporting documents"]
                            is_generic = any(phrase in desc for phrase in generic_phrases)
                            
                            if is_generic and doc_preview and len(doc_preview.strip()) > 50:
                                desc = self._generate_case_description_from_content(doc_preview)
                                desc_lines = [ln.strip() for ln in desc.split("\n") if ln.strip()]
                            
                            if len(desc_lines) < 3:
                                # Pad with case-specific content
                                if doc_preview and len(doc_preview.strip()) > 50:
                                    sentences = [s.strip() for s in doc_preview.split(".") if len(s.strip()) > 20]
                                    while len(desc_lines) < 3 and sentences:
                                        new_line = sentences.pop(0) + "."
                                        if new_line not in desc_lines:
                                            desc_lines.append(new_line)
                                    while len(desc_lines) < 3:
                                        desc_lines.append("Additional details are provided in the supporting documents.")
                                else:
                                    while len(desc_lines) < 3:
                                        desc_lines.append("Additional details are provided in the supporting documents.")
                                desc = "\n".join(desc_lines)
                            elif len(desc_lines) > 4:
                                desc = "\n".join(desc_lines[:4])
                        return {"caseTitle": title, "caseCategory": category, "subcategories": subs[:3], "description": desc[:500]}
                    except Exception:
                        pass
        except Exception:
            pass
        # Ultimate fallback - use AI to generate case-specific summary from documents when available
        desc = "Legal matter based on uploaded documents.\nThe case involves parties and issues requiring legal review.\nThe matter requires appropriate legal action.\nSupporting documents provide further context."
        if combined and len(combined.strip()) > 50:
            ai_desc = await self._generate_case_summary_from_documents(combined)
            if ai_desc:
                desc = ai_desc
        return {"caseTitle": "Petitioner vs Respondent", "caseCategory": "CIVIL LAW", "subcategories": ["Indian Contract Act, 1872"], "description": desc}

    async def _get_workspace_category_and_subcategory(
        self,
        workspace_id: str,
        user_id: str,
    ) -> Dict[str, Any]:
        """
        Infer case title (from documents), case category, subcategories and description (3-4 lines) from workspace documents using AI.
        Returns {"caseTitle": str, "caseCategory": str, "subcategories": List[str], "description": str}. 
        Description must be 3-4 lines (minimum 3, maximum 4). caseTitle is fixed once; do not overwrite on sync.
        """
        import json
        import re
        try:
            from app.vectorstore.qdrant import vector_store
            from app.ai.gemini_client import gemini_client
            all_docs = []
            try:
                all_docs = await vector_store.get_all_documents(workspace_id=workspace_id)
            except Exception as e:
                logger.warning("Could not retrieve documents for category: {}", str(e))
                return {"caseTitle": "Petitioner vs Respondent", "caseCategory": "CIVIL LAW", "subcategories": ["Indian Contract Act, 1872"], "description": "Legal matter based on uploaded documents."}
            if not all_docs:
                return {"caseTitle": "Petitioner vs Respondent", "caseCategory": "CIVIL LAW", "subcategories": ["Indian Contract Act, 1872"], "description": "Legal matter based on uploaded documents."}
            parts = []
            for i, doc in enumerate(all_docs, 1):
                content = (doc.get("content") or "").strip()
                if content and len(content) >= 20:
                    parts.append(f"--- Document {i} ---\n{content}")
            combined = "\n\n".join(parts) if parts else ""
            if not combined:
                return {"caseTitle": "Petitioner vs Respondent", "caseCategory": "CIVIL LAW", "subcategories": ["Indian Contract Act, 1872"], "description": "Legal matter based on uploaded documents."}
            combined = (combined[:5000] + "...") if len(combined) > 5000 else combined
            gemini_client.initialize()
            prompt = f"""Based on the document content below, output a JSON object with ALL fields REQUIRED (no empty/null values):

- caseTitle: MUST be in format "Petitioner vs Respondent" or "Party1 vs Party2" (e.g. "Umesh Malik vs Karnataka State Cricket Association", "Shubham Malik vs Social Media Hacking"). Extract actual party names from documents. Format: "[Petitioner/Complainant Name] vs [Respondent/Defendant Name]" or "[Party] vs [Issue/Matter]". This field is MANDATORY - always provide a title.
- caseCategory: exactly one category in UPPERCASE (e.g. CRIMINAL LAW, CYBER LAW, CIVIL LAW, TRADE MARK LAW). MANDATORY - must be one of: CRIMINAL LAW, CIVIL LAW, CYBER LAW, CONTRACT LAW, PROPERTY LAW, LABOUR LAW, TAX LAW, or similar.
- subcategories: array of 1-3 relevant Indian acts (e.g. ["Bharatiya Nyaya Sanhita", "Information Technology Act, 2000", "The Trade Marks Act, 1999"]). MANDATORY - must have at least 1 act.
- description: a SHORT CASE SUMMARY in English only, exactly 3 to 4 lines (minimum 3, maximum 4). You MUST write a SPECIFIC summary for this case: name the actual parties (people or organizations) and the actual dispute. Do NOT use generic phrases like "Legal matter based on uploaded documents" or "The case involves parties and issues requiring legal review". Write in your own words; do NOT copy-paste the document. Each line one short sentence (under 120 characters). Line 1: actual parties and main issue. Line 2: key facts or allegations. Line 3: relief or action sought. Line 4 (optional): one more relevant detail. Output only the summary text, no prefixes like "Line 1:".

Document content:
{combined}

CRITICAL: All four fields (caseTitle, caseCategory, subcategories, description) are MANDATORY. Do not leave any field empty or null.

Output ONLY this JSON, no markdown: {{"caseTitle": "Petitioner Name vs Respondent Name", "caseCategory": "CRIMINAL LAW", "subcategories": ["Bharatiya Nyaya Sanhita", "IT Act 2000"], "description": "MLA Umesh Malik has filed a complaint regarding his son Shubham Malik's hacked social media account.\\nEdited videos and posts were made viral to defame the complainant and damage his political image.\\nThe complainant seeks a high-level investigation and strict action against the perpetrators.\\nThe complaint involves alleged violations under IT Act 2000 and requests preservation of digital evidence."}}"""
            raw = await gemini_client.generate(prompt, temperature=0.2, max_tokens=700)
            if not raw or "Error" in (raw[:80] or ""):
                return await self._generate_fallback_metadata(combined, workspace_id)
            raw_clean = raw.strip()
            if "```" in raw_clean:
                code = re.search(r"```(?:json)?\s*([\s\S]*?)```", raw_clean)
                if code:
                    raw_clean = (code.group(1) or "").strip()
            start = raw_clean.find("{")
            if start >= 0:
                depth = 0
                for i in range(start, len(raw_clean)):
                    if raw_clean[i] == "{":
                        depth += 1
                    elif raw_clean[i] == "}":
                        depth -= 1
                        if depth == 0:
                            raw_clean = raw_clean[start : i + 1]
                            break
            if not raw_clean or not raw_clean.strip().startswith("{"):
                salvaged = self._salvage_workspace_metadata_from_truncated_json(raw, combined)
                salvaged = self._ensure_metadata_complete(salvaged, combined)
                if combined and len(combined.strip()) > 50:
                    ai_desc = await self._generate_case_summary_from_documents(combined)
                    if ai_desc:
                        salvaged["description"] = ai_desc
                if salvaged.get("caseCategory") != "General" or salvaged.get("subcategories") or salvaged.get("caseTitle"):
                    return salvaged
                return await self._generate_fallback_metadata(combined, workspace_id)
            try:
                data = json.loads(raw_clean)
            except json.JSONDecodeError:
                salvaged = self._salvage_workspace_metadata_from_truncated_json(raw, combined)
                salvaged = self._ensure_metadata_complete(salvaged, combined)
                if combined and len(combined.strip()) > 50:
                    ai_desc = await self._generate_case_summary_from_documents(combined)
                    if ai_desc:
                        salvaged["description"] = ai_desc
                if salvaged.get("caseCategory") != "General" or salvaged.get("subcategories") or salvaged.get("caseTitle"):
                    logger.info("Used salvaged workspace metadata from truncated response")
                    return salvaged
                return await self._generate_fallback_metadata(combined, workspace_id)
            case_title = (data.get("caseTitle") or "").strip()
            case_category = (data.get("caseCategory") or "").strip().upper()
            subcategories = data.get("subcategories") or data.get("subcategory") or []
            if isinstance(subcategories, str):
                subcategories = [subcategories] if subcategories else []
            subcategories = [str(s).strip() for s in subcategories if s][:5]
            description = (data.get("description") or "").strip()
            if description:
                # Handle both actual newlines and escaped \n in JSON
                description = description.replace("\\n", "\n")
                # Reject if it looks like raw document paste
                if not self._is_valid_short_summary(description):
                    description = ""
                else:
                    lines = [ln.strip() for ln in description.split("\n") if ln.strip()]
                    # Ensure 3-4 lines: pad if less than 3, truncate if more than 4
                    if len(lines) < 3:
                        while len(lines) < 3:
                            lines.append("Additional details regarding the legal matter.")
                    elif len(lines) > 4:
                        lines = lines[:4]
                    # Truncate any line that is too long (keep summary short)
                    lines = [ln[:120] if len(ln) > 120 else ln for ln in lines]
                    description = "\n".join(lines)[:500]
            
            # Build initial result
            result = {"caseTitle": case_title, "caseCategory": case_category, "subcategories": subcategories, "description": description}
            
            # If description is missing, invalid, or generic, generate case-specific summary from documents using AI
            generic_phrases = ["Legal matter based on uploaded documents", "The case involves parties and issues requiring legal review"]
            need_description = (
                not result.get("description") or
                not self._is_valid_short_summary(result["description"]) or
                any(p in (result.get("description") or "") for p in generic_phrases)
            )
            if need_description:
                if not combined or len(combined.strip()) < 50:
                    logger.warning("Cannot generate case-specific description: no document content available (length: {})", len(combined) if combined else 0)
                else:
                    # Clear generic description before trying AI generation
                    if result.get("description") and any(p in result["description"] for p in generic_phrases):
                        result["description"] = ""
                        logger.info("Cleared generic description, generating case-specific summary from documents ({} chars)", len(combined))
                    ai_summary = await self._generate_case_summary_from_documents(combined)
                    if ai_summary:
                        result["description"] = ai_summary
                        logger.info("✅ Generated case-specific description from documents (AI): {}", ai_summary[:80])
                    elif not result.get("description"):
                        # AI generation failed - try one more time with simpler prompt
                        logger.warning("First AI description attempt failed, retrying with simpler prompt")
                        ai_summary = await self._generate_case_summary_from_documents_simple(combined)
                        if ai_summary:
                            result["description"] = ai_summary
                            logger.info("✅ Generated case-specific description (retry): {}", ai_summary[:80])
                        else:
                            logger.error("❌ Failed to generate case-specific description after retries - description will be empty")
                            result["description"] = ""  # Leave empty rather than use generic
            
            # If title is missing, try to generate it from document content before fallback
            if not result["caseTitle"]:
                title_prompt = f"""From this document content, extract two parties or names and create a case title in format "Party1 vs Party2". If only one party, use "Party vs [Issue]".

Document: {combined[:800]}

Return ONLY the title in format "Name1 vs Name2" or "Name vs Issue", no other text."""
                try:
                    title_raw = await gemini_client.generate(title_prompt, temperature=0.3, max_tokens=100)
                    if title_raw:
                        title_raw = title_raw.strip().strip('"').strip("'")
                        if " vs " in title_raw or " v " in title_raw:
                            result["caseTitle"] = title_raw.split("\n")[0].split(".")[0].strip()
                        else:
                            result["caseTitle"] = f"{title_raw} vs Matter"
                except Exception:
                    pass  # _ensure_metadata_complete will set fallback
            
            # Ensure all fields are complete (fills any missing values)
            result = self._ensure_metadata_complete(result, combined)
            
            logger.info(
                "Workspace metadata: caseTitle={}, caseCategory={}, subcategories={}, description={}",
                result["caseTitle"][:50] if result["caseTitle"] else "",
                result["caseCategory"],
                result["subcategories"],
                result["description"][:50] if result["description"] else "",
            )
            return result
        except Exception as e:
            logger.warning("Workspace category/subcategory generation failed: {}", str(e))
            import traceback
            logger.debug("Traceback: {}", traceback.format_exc())
            desc = "Legal matter based on uploaded documents.\nThe case involves parties and issues requiring legal review.\nThe matter requires appropriate legal action.\nSupporting documents provide further context."
            if 'combined' in locals() and combined and len(combined.strip()) > 50:
                ai_desc = await self._generate_case_summary_from_documents(combined)
                if ai_desc:
                    desc = ai_desc
            fallback = {"caseTitle": "Petitioner vs Respondent", "caseCategory": "CIVIL LAW", "subcategories": ["Indian Contract Act, 1872"], "description": desc}
            # Try to ensure metadata is complete (in case method exists)
            try:
                fallback = self._ensure_metadata_complete(fallback, combined if 'combined' in locals() else "")
            except AttributeError:
                # Method doesn't exist, use fallback as-is
                pass
            return fallback

    # ========================================================================
    # Connection Events
    # ========================================================================
    
    async def on_connect(self, data: Any) -> None:
        """Handle connection event."""
        logger.info("Socket connected")
        await audit_logger.log_event(
            "socket_connect",
            {"status": "connected"}
        )
    
    async def on_connected(self, data: Any) -> None:
        """
        Handle 'connected' event from server.
        Confirms successful connection with userId and clientType.
        """
        logger.success("✅ Server confirmed connection as AI_Maverick")
        logger.info(f"Connection details: {data}")
        await audit_logger.log_event(
            "socket_connected_confirmed",
            {"status": "confirmed", "data": data}
        )
    
    async def on_disconnect(self, data: Any) -> None:
        """Handle disconnection event."""
        logger.info("Socket disconnected")
        await audit_logger.log_event(
            "socket_disconnect",
            {"status": "disconnected"}
        )
    
    async def on_error(self, data: Any) -> None:
        """Handle error event."""
        logger.error("Socket error received: {}", data)
        await audit_logger.log_event(
            "socket_error",
            {"error": str(data)}
        )
    
    # ========================================================================
    # Backend-Specified Events (AI_Maverick → Server)
    # ========================================================================
    
    async def on_workspace_created(self, data: Dict[str, Any]) -> None:
        """
        Handle workspace_created event (Server → AI_Maverick).
        
        Payload:
        {
            workspaceId: str,
            userId: str,
            title: str,
            workspaceRole: str,
            evidenceFiles: [...],
            createdAt: str (ISO timestamp)
        }
        
        Process flow:
        1. Validate payload
        2. Initialize role-based agent with workspaceRole
        3. Process evidence files
        4. Store in vector database
        5. Log audit event
        
        Args:
            data: Workspace creation payload
        """
        try:
            # Validate and parse payload
            payload = WorkspaceCreatedPayload(**data)
            
            # Store custom instructions if provided (structure, tone, description)
            custom = data.get("customInstructions") or {}
            structure = getattr(payload, "structure", None) or custom.get("structure")
            tone = getattr(payload, "tone", None) or custom.get("tone")
            description = getattr(payload, "description", None) or custom.get("description")
            if structure or tone or description:
                set_custom_instructions(
                    payload.workspaceId,
                    structure=structure,
                    tone=tone,
                    description=description,
                )
                logger.info("Custom instructions set: structure={}, tone={}, description={}", structure, tone, (description[:50] + "..." if description and len(description) > 50 else description))
            
            logger.info(
                "📦 Workspace created: {} - {} (User: {}, Role: {})",
                payload.workspaceId,
                payload.title,
                payload.userId,
                payload.workspaceRole
            )
            
            # STEP 1: Validate role (convert to uppercase for validation)
            workspace_role = payload.workspaceRole.upper() if payload.workspaceRole else "ADJUDICATOR"
            if not RoleEnum.validate(workspace_role):
                logger.warning(
                    "Invalid workspace role '{}', defaulting to ADJUDICATOR",
                    payload.workspaceRole
                )
                workspace_role = "ADJUDICATOR"
            
            logger.info("Initializing workspace with role: {}", workspace_role)
            
            # Ensure workspace context exists so chat and strategy can resolve role (get_workspace)
            from app.workspace.context import context_manager as ctx_mgr
            from datetime import datetime as dt
            try:
                created_at = dt.fromisoformat(payload.createdAt.replace("Z", "+00:00")) if getattr(payload, "createdAt", None) else dt.utcnow()
            except Exception:
                created_at = dt.utcnow()
            ctx_mgr.upsert_workspace(
                payload.workspaceId,
                payload.userId,
                title=payload.title,
                status="active",
                workspace_role=workspace_role,
                evidence_files=list(payload.evidenceFiles) if payload.evidenceFiles else [],
                updated_at=created_at,
            )
            
            # STEP 2: Initialize role-based agent
            try:
                state = await workspace_role_integration.initialize_workspace_with_role(
                    workspace_id=payload.workspaceId,
                    user_id=payload.userId,
                    initial_role=workspace_role
                )
                
                logger.success(
                    "✅ Role-based agent initialized for workspace {} with role {}",
                    payload.workspaceId,
                    workspace_role
                )
            except Exception as role_error:
                logger.error("Failed to initialize role agent: {}", str(role_error))
                # Continue with workspace creation even if role init fails
            
            # STEP 3: Process evidence files
            if payload.evidenceFiles:
                logger.info(
                    "Processing {} evidence files for workspace {}",
                    len(payload.evidenceFiles),
                    payload.workspaceId
                )
                
                # Prepare files for batch processing
                files_to_process = []
                for file in payload.evidenceFiles:
                    file_dict = {
                        "url": file.url,
                        "filename": file.filename,
                        "originalName": file.originalName,
                        "storeFileName": file.storeFileName,
                        "extension": file.extension,
                        "fileHash": file.fileHash,
                        "path": file.path,
                        "uploadedAt": file.uploadedAt
                    }
                    # Add _id if it exists (Pydantic field is 'id', maps to '_id' in JSON)
                    if file.id:
                        file_dict["_id"] = file.id
                    files_to_process.append(file_dict)
                
                try:
                    # Process files in batch
                    batch_result = await document_processor.process_batch(
                        files=files_to_process,
                        workspace_id=payload.workspaceId,
                        user_id=payload.userId
                    )
                    
                    logger.success(
                        "✅ Document processing complete: {}/{} successful, avg readability: {:.2f}%",
                        batch_result.get("successful", 0),
                        batch_result.get("total_files", 0),
                        batch_result.get("average_readability_score", 0)
                    )
                    
                    # Send processing results back to backend
                    if self._socket_client:
                        try:
                            # Prepare document results with readability scores, summary, and evidentiary strength
                            document_results = []
                            for result in batch_result.get("results", []):
                                doc_result = {
                                    "documentId": result.get("document_id"),
                                    "filename": result.get("filename"),
                                    "success": result.get("success", False),
                                    "readabilityScore": result.get("readability_score", 0),
                                    "readabilityRating": result.get("readability_rating", "Unknown"),
                                    "readable": result.get("readable", False),
                                    "extractedTextLength": result.get("extracted_text_length", 0),
                                    "storedInVectorDb": result.get("stored_in_vector_db", False),
                                    "summary": result.get("summary", ""),
                                    "evidentiaryStrength": result.get("evidentiary_strength", "N/A"),
                                }
                                if not result.get("success"):
                                    doc_result["error"] = result.get("error", "Unknown error")
                                document_results.append(doc_result)
                            
                            # Emit document_feature_response for document assessments (per backend spec)
                            document_assessment_payload = {
                                "featureType": "document_assessments",
                                "workspaceId": payload.workspaceId,
                                "userId": payload.userId,
                                "payload": {
                                    "documentAssessments": document_results,
                                    "summary": {
                                        "totalFiles": batch_result.get("total_files", 0),
                                        "successfulFiles": batch_result.get("successful", 0),
                                        "failedFiles": batch_result.get("failed", 0),
                                        "averageReadabilityScore": batch_result.get("average_readability_score", 0)
                                    }
                                }
                            }
                            
                            # Emit document_feature_response event (backend spec)
                            logger.info(f"📤 Emitting document_feature_response (document_assessments) with {len(document_results)} documents")
                            
                            # Log detailed document assessment information
                            logger.info("=" * 80)
                            logger.info("📄 DOCUMENT ASSESSMENT PAYLOAD")
                            logger.info("=" * 80)
                            logger.info(f"Workspace ID: {payload.workspaceId}")
                            logger.info(f"User ID: {payload.userId}")
                            logger.info(f"Total Files: {batch_result.get('total_files')}")
                            logger.info(f"Successful: {batch_result.get('successful')}")
                            logger.info(f"Failed: {batch_result.get('failed')}")
                            logger.info(f"Average Readability: {batch_result.get('average_readability_score'):.2f}%")
                            logger.info("-" * 80)
                            
                            for idx, doc in enumerate(document_results, 1):
                                logger.info(f"")
                                logger.info(f"Document {idx}:")
                                logger.info(f"  ID: {doc.get('documentId')}")
                                logger.info(f"  Filename: {doc.get('filename')}")
                                logger.info(f"  Success: {doc.get('success')}")
                                logger.info(f"  Readability Score: {doc.get('readabilityScore')}%")
                                logger.info(f"  Readability Rating: {doc.get('readabilityRating')}")
                                logger.info(f"  Summary: {(doc.get('summary') or '')[:100]}{'...' if len(doc.get('summary') or '') > 100 else ''}")
                                logger.info(f"  Evidentiary Strength: {doc.get('evidentiaryStrength', 'N/A')}")
                                logger.info(f"  Readable: {doc.get('readable')}")
                                logger.info(f"  Extracted Text Length: {doc.get('extractedTextLength')} chars")
                                logger.info(f"  Stored in Vector DB: {doc.get('storedInVectorDb')}")
                                if not doc.get('success'):
                                    logger.info(f"  Error: {doc.get('error')}")
                                logger.info("-" * 80)
                            
                            logger.info("=" * 80)
                            
                            await self._socket_client.emit(
                                "document_feature_response",
                                document_assessment_payload
                            )
                            logger.success("✅ Document assessments sent to backend")
                            logger.info(f"📊 Sent: {batch_result.get('successful')}/{batch_result.get('total_files')} files, avg readability: {batch_result.get('average_readability_score'):.2f}%")

                            # Emit workspace_metadata: title from documents (AI case title), category, subcategories. Title is set once only.
                            if batch_result.get("successful", 0) > 0:
                                try:
                                    meta = await self._get_workspace_category_and_subcategory(
                                        workspace_id=payload.workspaceId,
                                        user_id=payload.userId,
                                    )
                                    workspace_metadata_payload = {
                                        "featureType": "workspace_metadata",
                                        "workspaceId": payload.workspaceId,
                                        "userId": payload.userId,
                                        "payload": {
                                            "workspaceTitle": meta.get("caseTitle") or "",
                                            "updateTitle": True,
                                            "caseCategory": meta.get("caseCategory", "General"),
                                            "subcategories": meta.get("subcategories", []),
                                            "description": meta.get("description", ""),
                                        },
                                    }
                                    await self._socket_client.emit(
                                        "document_feature_response",
                                        workspace_metadata_payload
                                    )
                                    logger.success(
                                        "✅ Workspace metadata sent: title={} (from docs, fixed), category={}, subcategories={}, description={}",
                                        (meta.get("caseTitle") or "")[:50],
                                        meta.get("caseCategory"),
                                        meta.get("subcategories"),
                                        (meta.get("description") or "")[:50],
                                    )
                                except Exception as meta_err:
                                    logger.warning("Failed to emit workspace_metadata: {}", str(meta_err))

                            # STEP 4: Generate strategies and objectives based on processed documents
                            if batch_result.get("successful", 0) > 0:
                                logger.info("🎯 Generating strategies and objectives from processed documents...")
                                try:
                                    strategies_result = await self._generate_strategies_from_documents(
                                        workspace_id=payload.workspaceId,
                                        user_id=payload.userId,
                                        workspace_role=workspace_role,
                                        batch_result=batch_result
                                    )
                                    
                                    if strategies_result:
                                        logger.success("✅ Strategies generated, emitting to backend...")
                                        # Emit as document_feature_response per backend spec
                                        strategy_feature_payload = {
                                            "featureType": "strategy_objective",
                                            "workspaceId": payload.workspaceId,
                                            "userId": payload.userId,
                                            "payload": strategies_result
                                        }
                                        
                                        # Log detailed strategy information
                                        logger.info("=" * 80)
                                        logger.info("📋 STRATEGY & OBJECTIVE PAYLOAD (AI-GENERATED)")
                                        logger.info("=" * 80)
                                        logger.info(f"Workspace ID: {payload.workspaceId}")
                                        logger.info(f"User ID: {payload.userId}")
                                        logger.info(f"Role: {strategies_result.get('role')}")
                                        logger.info(f"Is Update: {strategies_result.get('isUpdate')}")
                                        logger.info("")
                                        logger.info("📄 CASE SUMMARY:")
                                        logger.info(f"{strategies_result.get('caseSummary', 'N/A')}")
                                        logger.info("")
                                        logger.info("🎯 STRATEGIC OBJECTIVE:")
                                        logger.info(f"{strategies_result.get('strategicObjective', 'N/A')}")
                                        logger.info("")
                                        logger.info("📋 RECOMMENDATIONS:")
                                        for idx, rec in enumerate(strategies_result.get('recommendations', []), 1):
                                            logger.info(f"  {idx}. {rec}")
                                        logger.info("")
                                        logger.info("🚀 POSSIBLE NEXT STEPS:")
                                        for idx, step in enumerate(strategies_result.get('possibleNextSteps', []), 1):
                                            logger.info(f"  {idx}. {step}")
                                        logger.info("")
                                        logger.info("📊 ANALYSIS METADATA:")
                                        analysis = strategies_result.get('analysis', {})
                                        logger.info(f"  Document Count: {analysis.get('documentCount', 0)}")
                                        logger.info(f"  Average Readability: {analysis.get('averageReadability', 0):.2f}%")
                                        logger.info(f"  Total Documents: {analysis.get('totalDocuments', 0)}")
                                        logger.info(f"  Successful: {analysis.get('successfulDocuments', 0)}")
                                        logger.info(f"  AI Analysis: {analysis.get('basedOnAIAnalysis', False)}")
                                        logger.info("=" * 80)
                                        
                                        await self._socket_client.emit(
                                            "document_feature_response",
                                            strategy_feature_payload
                                        )
                                        chips = strategies_result.get("suggestionChips") or []
                                        if chips:
                                            self._workspace_suggestion_chips[payload.workspaceId] = list(chips)
                                            self._last_workspace_id_with_chips = payload.workspaceId
                                            logger.info("Stored {} suggestion chips for workspace (probing_prompts)", len(chips))
                                        logger.success("🎉 Strategy & Objectives sent to backend")
                                except Exception as strategy_error:
                                    logger.error(f"Failed to generate strategies: {strategy_error}")
                                    logger.exception(strategy_error)
                                    # Send error to backend
                                    try:
                                        error_payload = {
                                            "featureType": "strategy_objective",
                                            "workspaceId": payload.workspaceId,
                                            "userId": payload.userId,
                                            "error": str(strategy_error)
                                        }
                                        await self._socket_client.emit(
                                            "document_feature_response",
                                            error_payload
                                        )
                                    except Exception as emit_error:
                                        logger.error(f"Failed to emit strategy error: {emit_error}")
                        except Exception as emit_error:
                            logger.error(f"Failed to emit processing results: {emit_error}")
                    
                except Exception as doc_error:
                    logger.error("Error processing documents: {}", str(doc_error))
                    
                    # Send error notification to backend (per backend spec)
                    if self._socket_client:
                        try:
                            error_payload = {
                                "featureType": "document_assessments",
                                "workspaceId": payload.workspaceId,
                                "userId": payload.userId,
                                "error": str(doc_error)
                            }
                            await self._socket_client.emit(
                                "document_feature_response",
                                error_payload
                            )
                            logger.error("❌ Document processing error sent to backend")
                        except Exception as emit_error:
                            logger.error(f"Failed to emit error notification: {emit_error}")
            
            # STEP 4: Log audit event
            await audit_logger.log_event(
                "workspace_created_ai",
                {
                    "workspace_id": payload.workspaceId,
                    "user_id": payload.userId,
                    "title": payload.title,
                    "role": workspace_role,
                    "evidence_files_count": len(payload.evidenceFiles)
                }
            )
            
            logger.success(
                "🎉 Workspace fully initialized: {} with role {}",
                payload.workspaceId,
                workspace_role
            )
            
        except Exception as e:
            logger.error("Error processing workspace_created event: {}", str(e))
            logger.exception(e)
            raise
    
    async def on_workspace_sync(self, data: Dict[str, Any]) -> None:
        """
        Handle workspace_sync event (Server → AI_Maverick).
        
        Payload:
        {
            workspaceId: str,
            userId: str,
            title?: str,
            status?: str,
            evidenceFiles?: [...],
            updatedAt: str (ISO timestamp)
        }
        
        Process flow:
        1. Validate payload
        2. Update workspace metadata
        3. If deletedDocumentIds: remove from vector store, regenerate strategy from remaining docs
        4. Process new evidence files if any (strategy then based on all docs in workspace)
        5. Log audit event
        
        Args:
            data: Workspace sync payload
        """
        try:
            # Log raw websocket input for debugging (delete flags, evidenceFiles, etc.)
            try:
                import json
                raw_preview = json.dumps(data, default=str) if data else "null"
                if len(raw_preview) > 1500:
                    raw_preview = raw_preview[:1500] + "..."
                logger.info("📥 workspace_sync RAW input: {}", raw_preview)
            except Exception:
                logger.info("📥 workspace_sync RAW input (repr): {}", repr(data))

            # Validate and parse payload
            payload = WorkspaceSyncPayload(**data)

            # Update custom instructions if provided (structure, tone, description)
            custom = (data or {}).get("customInstructions") or {}
            structure = getattr(payload, "structure", None) or custom.get("structure")
            tone = getattr(payload, "tone", None) or custom.get("tone")
            description = getattr(payload, "description", None) or custom.get("description")
            if structure is not None or tone is not None or description is not None:
                set_custom_instructions(
                    payload.workspaceId,
                    structure=structure,
                    tone=tone,
                    description=description,
                )
                if structure or tone or description:
                    logger.info("Custom instructions updated: structure={}, tone={}", structure, tone)

            # Normalize deleted IDs: from deletedDocumentIds and from evidenceFiles with isDeleted: true
            deleted_ids = list(payload.deletedDocumentIds or [])
            if not deleted_ids and isinstance(data, dict):
                deleted_ids = list(data.get("deleted_document_ids") or data.get("deletedDocumentIds") or [])
            # Backend may send isDeleted: true on each evidence file instead of/in addition to deletedDocumentIds
            if payload.evidenceFiles:
                for file in payload.evidenceFiles:
                    is_deleted = getattr(file, "isDeleted", None) or (getattr(file, "is_deleted", None) if hasattr(file, "is_deleted") else None)
                    if is_deleted:
                        file_id = getattr(file, "id", None) or (getattr(file, "_id", None) if hasattr(file, "_id") else None)
                        if file_id and str(file_id) not in deleted_ids:
                            deleted_ids.append(str(file_id))
            if deleted_ids:
                logger.info("🗑️ Resolved deleted document IDs (from payload + isDeleted): {}", deleted_ids)
            
            logger.info(
                "🔄 Workspace sync: {} (User: {}, Role: {})",
                payload.workspaceId,
                payload.userId,
                payload.workspaceRole or "unchanged"
            )
            
            # Create or update workspace context so chat and strategy use correct role (e.g. Mediator from Edit Workspace)
            from app.workspace.context import context_manager
            from datetime import datetime as dt
            updated_at = None
            try:
                if payload.updatedAt:
                    updated_at = dt.fromisoformat(payload.updatedAt.replace("Z", "+00:00"))
            except Exception:
                updated_at = dt.utcnow()
            evidence_for_context = []
            if payload.evidenceFiles:
                for f in payload.evidenceFiles:
                    if getattr(f, "isDeleted", False) or getattr(f, "is_deleted", False):
                        continue
                    evidence_for_context.append(f)
            context_manager.upsert_workspace(
                payload.workspaceId,
                payload.userId,
                title=payload.title,
                status=payload.status,
                workspace_role=payload.workspaceRole,
                evidence_files=evidence_for_context,
                updated_at=updated_at,
            )
            
            # Update workspace metadata (for logging)
            updates = {}
            if payload.title:
                updates["title"] = payload.title
            if payload.status:
                updates["status"] = payload.status
            if payload.workspaceRole:
                updates["workspaceRole"] = payload.workspaceRole
            
            logger.info(f"Workspace updates: {updates}")

            # Handle document deletion: remove from vector store and regenerate strategy from remaining docs
            if deleted_ids:
                from app.vectorstore.qdrant import vector_store
                logger.info(
                    "🗑️ Deleting {} document(s) from vector store: {}",
                    len(deleted_ids),
                    deleted_ids
                )
                try:
                    await vector_store.delete_documents(
                        workspace_id=payload.workspaceId,
                        document_ids=deleted_ids,
                    )
                    # Regenerate strategy from remaining documents (use role from payload first, e.g. Mediator)
                    workspace_role = (payload.workspaceRole or "").strip().upper() or "ADJUDICATOR"
                    if workspace_role not in ("ADJUDICATOR", "PETITIONER", "RESPONDENT", "MEDIATOR"):
                        workspace_role = "ADJUDICATOR"
                    try:
                        ctx = workspace_manager.get_workspace(payload.workspaceId)
                        if ctx and ctx.workspace_role:
                            workspace_role = (ctx.workspace_role or workspace_role).upper()
                    except Exception:
                        pass
                    strategies_result = await self._generate_strategies_from_documents(
                        workspace_id=payload.workspaceId,
                        user_id=payload.userId,
                        workspace_role=workspace_role,
                        batch_result=None,
                        is_update=True
                    )
                    if strategies_result and self._socket_client:
                        await self._socket_client.emit(
                            "document_feature_response",
                            {
                                "featureType": "strategy_objective",
                                "workspaceId": payload.workspaceId,
                                "userId": payload.userId,
                                "payload": strategies_result
                            }
                        )
                        logger.success("🎉 Strategy & Objectives updated after document deletion")
                except Exception as del_err:
                    logger.error("Error handling document deletion: {}", str(del_err))
                    logger.exception(del_err)

            # Process new evidence files if any (skip files that are deleted: isDeleted true or in deleted_ids)
            files_to_process = []
            if payload.evidenceFiles:
                for file in payload.evidenceFiles:
                    is_deleted = getattr(file, "isDeleted", False) or getattr(file, "is_deleted", False)
                    if is_deleted:
                        logger.info(
                            "Skipping evidence file (isDeleted=true): {} ({})",
                            getattr(file, "filename", ""),
                            getattr(file, "id", ""),
                        )
                        continue
                    file_id = (getattr(file, "id", None) or (file.__dict__.get("id") if hasattr(file, "__dict__") else None)) or None
                    if file_id and str(file_id) in deleted_ids:
                        logger.info("Skipping evidence file (in deleted list): {} ({})", getattr(file, "filename", ""), file_id)
                        continue
                    file_dict = {
                        "url": file.url,
                        "filename": file.filename,
                        "originalName": file.originalName,
                        "storeFileName": file.storeFileName,
                        "extension": file.extension,
                        "fileHash": file.fileHash,
                        "path": file.path,
                        "uploadedAt": file.uploadedAt
                    }
                    # Add _id if it exists (Pydantic field is 'id', maps to '_id' in JSON)
                    if file_id:
                        file_dict["_id"] = file_id
                    files_to_process.append(file_dict)
                if not files_to_process:
                    logger.info("No new/updated evidence files to process (all skipped or empty list)")
                else:
                    logger.info(
                        "Processing {} new/updated evidence files",
                        len(files_to_process)
                    )

            if payload.evidenceFiles and files_to_process:
                try:
                    batch_result = await document_processor.process_batch(
                        files=files_to_process,
                        workspace_id=payload.workspaceId,
                        user_id=payload.userId
                    )
                    
                    logger.success(
                        "✅ Document sync complete: {}/{} successful",
                        batch_result.get("successful", 0),
                        batch_result.get("total_files", 0)
                    )
                    
                    # Send processing results back to backend
                    if self._socket_client:
                        try:
                            # Prepare document results (with summary and evidentiary strength)
                            document_results = []
                            for result in batch_result.get("results", []):
                                doc_result = {
                                    "documentId": result.get("document_id"),
                                    "filename": result.get("filename"),
                                    "success": result.get("success", False),
                                    "readabilityScore": result.get("readability_score", 0),
                                    "readabilityRating": result.get("readability_rating", "Unknown"),
                                    "readable": result.get("readable", False),
                                    "extractedTextLength": result.get("extracted_text_length", 0),
                                    "storedInVectorDb": result.get("stored_in_vector_db", False),
                                    "summary": result.get("summary", ""),
                                    "evidentiaryStrength": result.get("evidentiary_strength", "N/A"),
                                }
                                if not result.get("success"):
                                    doc_result["error"] = result.get("error", "Unknown error")
                                document_results.append(doc_result)
                            
                            # Emit document_feature_response for document assessments (per backend spec)
                            document_assessment_payload = {
                                "featureType": "document_assessments",
                                "workspaceId": payload.workspaceId,
                                "userId": payload.userId,
                                "payload": {
                                    "documentAssessments": document_results,
                                    "summary": {
                                        "totalFiles": batch_result.get("total_files", 0),
                                        "successfulFiles": batch_result.get("successful", 0),
                                        "failedFiles": batch_result.get("failed", 0),
                                        "averageReadabilityScore": batch_result.get("average_readability_score", 0)
                                    }
                                }
                            }
                            
                            await self._socket_client.emit(
                                "document_feature_response",
                                document_assessment_payload
                            )
                            logger.success("✅ Document assessments (sync) sent to backend")

                            # Emit workspace_metadata on sync: do NOT send title (send empty) so backend keeps existing title unchanged
                            if batch_result.get("successful", 0) > 0:
                                try:
                                    meta = await self._get_workspace_category_and_subcategory(
                                        workspace_id=payload.workspaceId,
                                        user_id=payload.userId,
                                    )
                                    workspace_metadata_payload = {
                                        "featureType": "workspace_metadata",
                                        "workspaceId": payload.workspaceId,
                                        "userId": payload.userId,
                                        "payload": {
                                            "workspaceTitle": "",
                                            "updateTitle": False,
                                            "caseCategory": meta.get("caseCategory", "General"),
                                            "subcategories": meta.get("subcategories", []),
                                            "description": meta.get("description", ""),
                                        },
                                    }
                                    await self._socket_client.emit(
                                        "document_feature_response",
                                        workspace_metadata_payload
                                    )
                                    logger.success(
                                        "✅ Workspace metadata (sync) sent: title=unchanged, category={}, subcategories={}, description={}",
                                        meta.get("caseCategory"),
                                        meta.get("subcategories"),
                                        (meta.get("description") or "")[:50],
                                    )
                                except Exception as meta_err:
                                    logger.warning("Failed to emit workspace_metadata (sync): {}", str(meta_err))

                            # Regenerate strategies from ALL documents in workspace (use role from payload, e.g. Mediator)
                            if batch_result.get("successful", 0) > 0:
                                logger.info("🎯 Regenerating strategies from all documents in workspace...")
                                try:
                                    workspace_role = (payload.workspaceRole or "").strip().upper() or "ADJUDICATOR"
                                    if workspace_role not in ("ADJUDICATOR", "PETITIONER", "RESPONDENT", "MEDIATOR"):
                                        workspace_role = "ADJUDICATOR"
                                    try:
                                        ctx = workspace_manager.get_workspace(payload.workspaceId)
                                        if ctx and ctx.workspace_role:
                                            workspace_role = (ctx.workspace_role or workspace_role).upper()
                                    except Exception:
                                        pass
                                    logger.info("Using workspace role for strategies: {}", workspace_role)
                                    strategies_result = await self._generate_strategies_from_documents(
                                        workspace_id=payload.workspaceId,
                                        user_id=payload.userId,
                                        workspace_role=workspace_role,
                                        batch_result=batch_result,
                                        is_update=True
                                    )
                                    
                                    if strategies_result:
                                        logger.success("✅ Strategies regenerated, emitting to backend...")
                                        # Emit as document_feature_response per backend spec
                                        strategy_feature_payload = {
                                            "featureType": "strategy_objective",
                                            "workspaceId": payload.workspaceId,
                                            "userId": payload.userId,
                                            "payload": strategies_result
                                        }
                                        
                                        # Log detailed strategy information
                                        logger.info("=" * 80)
                                        logger.info("📋 UPDATED STRATEGY & OBJECTIVE PAYLOAD (AI-GENERATED)")
                                        logger.info("=" * 80)
                                        logger.info(f"Workspace ID: {payload.workspaceId}")
                                        logger.info(f"User ID: {payload.userId}")
                                        logger.info(f"Role: {strategies_result.get('role')}")
                                        logger.info(f"Is Update: {strategies_result.get('isUpdate')}")
                                        logger.info("")
                                        logger.info("📄 CASE SUMMARY:")
                                        logger.info(f"{strategies_result.get('caseSummary', 'N/A')}")
                                        logger.info("")
                                        logger.info("🎯 STRATEGIC OBJECTIVE:")
                                        logger.info(f"{strategies_result.get('strategicObjective', 'N/A')}")
                                        logger.info("")
                                        logger.info("📋 RECOMMENDATIONS:")
                                        for idx, rec in enumerate(strategies_result.get('recommendations', []), 1):
                                            logger.info(f"  {idx}. {rec}")
                                        logger.info("")
                                        logger.info("🚀 POSSIBLE NEXT STEPS:")
                                        for idx, step in enumerate(strategies_result.get('possibleNextSteps', []), 1):
                                            logger.info(f"  {idx}. {step}")
                                        logger.info("")
                                        logger.info("📊 ANALYSIS METADATA:")
                                        analysis = strategies_result.get('analysis', {})
                                        logger.info(f"  Document Count: {analysis.get('documentCount', 0)}")
                                        logger.info(f"  Average Readability: {analysis.get('averageReadability', 0):.2f}%")
                                        logger.info(f"  Total Documents: {analysis.get('totalDocuments', 0)}")
                                        logger.info(f"  Successful: {analysis.get('successfulDocuments', 0)}")
                                        logger.info(f"  AI Analysis: {analysis.get('basedOnAIAnalysis', False)}")
                                        logger.info("=" * 80)
                                        
                                        await self._socket_client.emit(
                                            "document_feature_response",
                                            strategy_feature_payload
                                        )
                                        chips = strategies_result.get("suggestionChips") or []
                                        if chips:
                                            self._workspace_suggestion_chips[payload.workspaceId] = list(chips)
                                            self._last_workspace_id_with_chips = payload.workspaceId
                                            logger.info("Stored {} suggestion chips for workspace (probing_prompts)", len(chips))
                                        logger.success("🎉 Updated Strategy & Objectives sent to backend")
                                except Exception as strategy_error:
                                    logger.error(f"Failed to regenerate strategies: {strategy_error}")
                                    logger.exception(strategy_error)
                                    # Send error to backend
                                    try:
                                        error_payload = {
                                            "featureType": "strategy_objective",
                                            "workspaceId": payload.workspaceId,
                                            "userId": payload.userId,
                                            "error": str(strategy_error)
                                        }
                                        await self._socket_client.emit(
                                            "document_feature_response",
                                            error_payload
                                        )
                                    except Exception as emit_error:
                                        logger.error(f"Failed to emit strategy error: {emit_error}")
                        except Exception as emit_error:
                            logger.error(f"Failed to emit sync results: {emit_error}")
                except Exception as doc_error:
                    logger.error("Error syncing documents: {}", str(doc_error))
            
            # Log audit event
            await audit_logger.log_event(
                "workspace_sync_ai",
                {
                    "workspace_id": payload.workspaceId,
                    "user_id": payload.userId,
                    "updates": updates,
                    "new_files_count": len(payload.evidenceFiles) if payload.evidenceFiles else 0,
                    "deleted_document_ids_count": len(deleted_ids),
                    "processed_files_count": len(files_to_process)
                }
            )
            
            logger.success("✅ Workspace sync completed: {}", payload.workspaceId)
            
        except Exception as e:
            logger.error("Error processing workspace_sync event: {}", str(e))
            logger.exception(e)
            raise
    
    async def on_user_message_request(self, data: Dict[str, Any]) -> None:
        """
        Handle user_message_request event (Server → AI_Maverick).
        
        Payload:
        {
            message: str,
            chat_type: ChatType,
            conversation_history: [{role: 'USER'|'AI', content: str}],
            workspaceId?: str,
            userId?: str,
            chatId?: str,
            timestamp: str (ISO timestamp)
        }
        
        Process flow:
        1. Validate payload
        2. Build chat context
        3. Process with Intelligence Chat Agent
        4. Emit ai_response back to server
        
        Args:
            data: User message request payload
        """
        try:
            # Validate and parse payload
            payload = UserMessageRequestPayload(**data)
            
            logger.info(
                "💬 User message request: Chat {} (Type: {}, Workspace: {})",
                payload.chatId,
                payload.chat_type,
                payload.workspaceId
            )
            logger.debug(f"Message: {payload.content[:100]}...")
            
            # Import here to avoid circular dependencies
            from app.langgraph.intelligence_chat_agent import intelligence_chat_agent
            from app.langgraph.chat_models import ChatContext
            from app.langgraph.role_agent import role_agent
            
            # STEP 1: Get workspace context (role, documents count) from workspace manager
            workspace_id = payload.workspaceId or "unknown"
            user_id = payload.userId or "unknown"
            chat_id = payload.chatId or f"chat_{datetime.utcnow().timestamp()}"
            
            role_context = {
                "role": "ADJUDICATOR",
                "workspace_id": workspace_id,
                "user_id": user_id,
            }
            workspace_state = {"workspace_id": workspace_id, "user_id": user_id, "documents_count": 0}
            
            try:
                from app.workspace.manager import workspace_manager
                ctx = workspace_manager.get_workspace(workspace_id)
                if ctx:
                    workspace_role = (ctx.workspace_role or "ADJUDICATOR").upper()
                    if workspace_role not in ("ADJUDICATOR", "PETITIONER", "RESPONDENT", "MEDIATOR"):
                        workspace_role = "ADJUDICATOR"
                    role_context["role"] = workspace_role
                    role_context_from_meta = ctx.get_metadata("role_context")
                    if isinstance(role_context_from_meta, dict) and role_context_from_meta.get("role"):
                        role_context.update(role_context_from_meta)
                    docs_count = ctx.get_evidence_file_count() if hasattr(ctx, "get_evidence_file_count") else len(getattr(ctx, "evidence_files", []))
                    workspace_state["documents_count"] = docs_count
            except Exception as ws_err:
                logger.warning("Could not load workspace for chat (using defaults): {}", str(ws_err))
            
            # Fetch uploaded document content so the model can use it (drafts, answers, etc.)
            try:
                from app.vectorstore.qdrant import vector_store
                all_docs = await vector_store.get_all_documents(workspace_id=workspace_id)
                max_chars_per_doc = 2500
                max_total_chars = 8000
                total = 0
                doc_snippets = []
                for doc in (all_docs or [])[:20]:
                    content = (doc.get("content") or "").strip()
                    if not content:
                        continue
                    snippet = content[:max_chars_per_doc] + ("..." if len(content) > max_chars_per_doc else "")
                    if total + len(snippet) > max_total_chars:
                        snippet = snippet[: max_total_chars - total]
                        doc_snippets.append({"document_id": doc.get("document_id"), "content": snippet})
                        break
                    doc_snippets.append({"document_id": doc.get("document_id"), "content": snippet})
                    total += len(snippet)
                if doc_snippets:
                    workspace_state["uploaded_documents"] = doc_snippets
            except Exception as doc_err:
                logger.warning("Could not load documents for chat context: {}", str(doc_err))
            
            # STEP 2 & 3: Build chat context with role, document count, and uploaded document content
            chat_context = ChatContext(
                workspace_id=workspace_id,
                user_id=user_id,
                role_context=role_context,
                workspace_state=workspace_state,
                chat_history=[
                    {"role": item.role, "content": item.content}
                    for item in payload.conversation_history
                ]
            )
            
            # STEP 4: Process with Intelligence Chat Agent
            logger.info("🤖 Processing message with Intelligence Chat Agent...")
            
            start_time = datetime.utcnow()
            response = await intelligence_chat_agent.process_message(
                workspace_id=workspace_id,
                user_id=user_id,
                message_id=chat_id,
                user_message=payload.content,
                chat_context=chat_context
            )
            processing_time = (datetime.utcnow() - start_time).total_seconds() * 1000
            
            # Generate 10-12 contextual chips, store for this workspace; send random 4 in response (backend polls every few sec and we return random 4 each time)
            import random
            suggestion_chips_for_payload = []
            try:
                role_for_chips = (role_context.get("role") or "ADJUDICATOR").upper()
                generated = await self._generate_contextual_suggestion_chips(
                    workspace_id=workspace_id,
                    role=role_for_chips,
                    user_message=payload.content,
                    ai_response_text=response.response_text,
                    max_chips=12,
                )
                if generated:
                    self._workspace_chat_suggestion_chips[workspace_id] = list(generated)
                    suggestion_chips_for_payload = random.sample(generated, min(4, len(generated)))
                    logger.info("Stored {} contextual chips for workspace; sending random 4 in ai_response (each 4s poll will return random 4 from these {})", len(generated), len(generated))
            except Exception as chip_err:
                logger.warning("Suggestion chips generation failed: {}", str(chip_err))
            
            # STEP 5: Build AI response payload (suggestionChips = random 4 from 10-12; click sends chip as next message)
            ai_response = AIResponsePayload(
                chatId=chat_id,
                workspaceId=workspace_id,
                userId=user_id,
                content=response.response_text,
                metadata=AIResponseMetadata(
                    messageId=response.message_id,
                    aiModel=settings.gemini_model,
                    tokensUsed=response.tokens_used,
                    responseTime=processing_time,
                    confidence=response.confidence.value if hasattr(response.confidence, 'value') else str(response.confidence),
                    detectedIntent=response.detected_intent
                ),
                suggestionChips=suggestion_chips_for_payload if suggestion_chips_for_payload else None,
            )
            
            # STEP 6: Emit ai_response back to server
            if self._socket_client:
                payload_dict = ai_response.dict(by_alias=True)
                if payload_dict.get("suggestionChips") is None:
                    payload_dict.pop("suggestionChips", None)
                await self._socket_client.emit_ai_response(payload_dict)
                logger.success("✅ AI response sent successfully")
            else:
                logger.error("❌ Socket client not available to emit response")
            
            # STEP 7: Log audit event
            await audit_logger.log_event(
                "user_message_processed",
                {
                    "workspace_id": workspace_id,
                    "user_id": user_id,
                    "chat_id": chat_id,
                    "message_length": len(payload.content),
                    "response_length": len(response.response_text),
                    "processing_time_ms": processing_time,
                    "intent": response.detected_intent
                }
            )
            
        except Exception as e:
            logger.error("Error processing user_message_request: {}", str(e))
            logger.exception(e)
            
            # Try to send error response back (only if payload was successfully parsed)
            try:
                # Get basic info from data dict for error response
                chat_id = data.get("chatId")
                workspace_id = data.get("workspaceId", "unknown")
                user_id = data.get("userId", "unknown")
                
                if self._socket_client and chat_id:
                    error_response = AIResponsePayload(
                        chatId=chat_id,
                        workspaceId=workspace_id,
                        userId=user_id,
                        content=f"I apologize, but I encountered an error processing your message: {str(e)}",
                        metadata=AIResponseMetadata(
                            messageId=chat_id,
                            aiModel=settings.gemini_model,
                            error=str(e)
                        )
                    )
                    await self._socket_client.emit_ai_response(error_response.dict(by_alias=True))
                    logger.info("Error response sent to user")
            except Exception as emit_error:
                logger.error(f"Failed to emit error response: {emit_error}")
    
    def _fallback_chips_for_role(self, role: str, needed: int) -> List[str]:
        """Return generic role-appropriate chips to pad when the model returns fewer than 10. Need 12+ for rotation."""
        role_upper = (role or "ADJUDICATOR").upper()
        pools = {
            "ADJUDICATOR": [
                "Summarise key issues", "Identify applicable laws", "Assess evidence strength",
                "Outline procedural steps", "List open questions", "Suggest further inquiry",
                "Compare parties' positions", "Note gaps in evidence", "Recommend next steps",
                "Initiate cyber investigation", "Examine edited content", "Collect victim statements",
                "Consult cyber forensics", "Review jurisdictional aspects", "Order preservation of evidence",
            ],
            "PETITIONER": [
                "Draft relief sought", "List supporting evidence", "Cite applicable provisions",
                "Strengthen arguments", "Address counter-arguments", "Request interim relief",
                "Outline chronology", "Identify witnesses", "Suggest documents to file",
                "Summarise harm suffered", "Map legal remedies", "Draft cause of action",
            ],
            "RESPONDENT": [
                "Identify defence grounds", "Challenge petitioner evidence", "Cite exceptions",
                "Propose dismissal grounds", "List procedural objections", "Suggest counter-evidence",
                "Outline limitations", "Address each claim", "Draft reply points",
                "Highlight inconsistencies", "Invoke limitation", "Request security for costs",
            ],
            "MEDIATOR": [
                "List common ground", "Identify sticking points", "Suggest compromise options",
                "Outline risks for each party", "Draft settlement framework", "Clarify interests",
                "Propose confidence-building steps", "Summarise positions", "Suggest next round",
                "Propose joint fact-finding", "Draft term sheet", "Suggest cooling-off period",
            ],
        }
        generic = pools.get(role_upper, pools["ADJUDICATOR"])
        return list(generic)[:needed]

    async def _generate_contextual_suggestion_chips(
        self,
        workspace_id: str,
        role: str,
        user_message: str,
        ai_response_text: str,
        max_chips: int = 12,
        conversation_summary: Optional[str] = None,
    ) -> List[str]:
        """
        Generate 10-12 short, role-based, case-related suggestion chips.
        If the model returns fewer than 10, we pad with generic role-based chips so we have 10-12 to sample from.
        """
        import json
        try:
            from app.ai.gemini_client import gemini_client
            gemini_client.initialize()
            role_upper = (role or "ADJUDICATOR").upper()
            user_preview = (user_message or "")[:300]
            ai_preview = (ai_response_text or "")[:400]
            prompt = f"""You are suggesting follow-up prompts for a legal workspace chat. The workspace role is: {role_upper}.

Last user message: "{user_preview}"
Last assistant reply (summary): "{ai_preview}"
{f'Recent context: {conversation_summary[:200]}' if conversation_summary else ''}

You MUST output a JSON array of exactly 10, 11, or 12 short follow-up prompts. Minimum 10 items. Not 4. Not 5. At least 10 strings in the array.
Each prompt: 2-8 words, role-appropriate, case-related, actionable. User will click one and it gets sent as their next message.
Vary types: draft, explain, list, risks, next steps, clarify, evidence, options.

Return ONLY a JSON array, no other text. Example with 10+ items: ["Draft a settlement proposal", "List key issues for mediation", "What are the risks for each party?", "Suggest compromise options", "Summarise evidence so far", "Outline procedural next steps", "Identify gaps in the complaint", "Recommend documents to collect", "Clarify jurisdiction", "Compare both sides' positions", "Suggest interim measures"]
"""
            raw = await gemini_client.generate(prompt, temperature=0.7, max_tokens=600)
            if not raw or "Error" in (raw[:80] or ""):
                return []
            raw = raw.strip()
            start = raw.find("[")
            if start < 0:
                return []
            depth = 0
            end = -1
            for i in range(start, len(raw)):
                if raw[i] == "[":
                    depth += 1
                elif raw[i] == "]":
                    depth -= 1
                    if depth == 0:
                        end = i + 1
                        break
            if end <= start:
                return []
            arr = json.loads(raw[start:end])
            if not isinstance(arr, list):
                arr = []
            chips = [str(x).strip() for x in arr if x and 2 <= len(str(x).strip()) <= 120][:max_chips]
            from_model = len(chips)
            # Always ensure we have 10–12 chips so every 4s we can emit a different random 4
            target_count = 12
            if len(chips) < target_count:
                seen = {c.lower() for c in chips}
                fallbacks = self._fallback_chips_for_role(role, target_count + 5)
                for pad in fallbacks:
                    if len(chips) >= target_count:
                        break
                    if pad and pad.lower() not in seen:
                        chips.append(pad)
                        seen.add(pad.lower())
            result = chips[:target_count]
            logger.info("Contextual chips: model gave {}, after padding have {} (return random 4 each poll)", from_model, len(result))
            return result
        except Exception as e:
            logger.warning("Contextual suggestion chips generation failed: {}", str(e))
            # Still return 10–12 fallback chips so rotation works
            return self._fallback_chips_for_role(role, 12)
    
    async def on_probing_prompts_request(self, data: Dict[str, Any]) -> None:
        """
        Handle probing_prompts_request from backend.
        - If conversation context is provided (lastUserMessage, lastAiResponse, or conversation_history),
          generate contextual, role-based suggestion chips so they change with the chat.
        - Otherwise return a random 4 from the stored 15-16 strategy chips (role/case-related).
        Chips are sent as prompts[]; when user clicks one, that text goes into the chat as the user message.
        """
        import random
        try:
            workspace_id = (data.get("workspaceId") or data.get("workspace_id") or "").strip()
            chat_type = (data.get("chatType") or "").strip()
            if chat_type != "STRATEGY_OBJECTIVE":
                logger.debug("probing_prompts_request: chatType is not STRATEGY_OBJECTIVE, skipping")
                return
            max_prompts = data.get("maxPrompts")
            if max_prompts is None:
                max_prompts = 4
            try:
                max_prompts = int(max_prompts)
            except (TypeError, ValueError):
                max_prompts = 4
            
            # 1) If we have 10-12 stored contextual chips for this workspace, return random 4 each time (so each poll shows different chips)
            stored_chat_chips = self._workspace_chat_suggestion_chips.get(workspace_id) or []
            if stored_chat_chips:
                count = min(max_prompts, len(stored_chat_chips))
                prompts = random.sample(stored_chat_chips, count) if count > 0 else []
                logger.info("probing_prompts: returning random {} from {} stored chips (rotation)", len(prompts), len(stored_chat_chips))
            else:
                # 2) No stored chat chips: if we have conversation context, generate 10-12, store, return random 4
                last_user = (data.get("lastUserMessage") or data.get("last_user_message") or "").strip()
                last_ai = (data.get("lastAiResponse") or data.get("last_ai_response") or "").strip()
                conv_history = data.get("conversation_history") or data.get("conversationHistory") or []
                if conv_history and (not last_user and not last_ai):
                    for m in reversed(conv_history):
                        r = (m.get("role") or "").upper()
                        c = (m.get("content") or "").strip()
                        if not c:
                            continue
                        if r == "USER" and not last_user:
                            last_user = c[:500]
                        elif r == "AI" and not last_ai:
                            last_ai = c[:500]
                        if last_user and last_ai:
                            break
                prompts = []
                if last_user or last_ai or (conv_history and len(conv_history) >= 2):
                    role = "ADJUDICATOR"
                    try:
                        ctx = workspace_manager.get_workspace(workspace_id)
                        if ctx and ctx.workspace_role:
                            role = (ctx.workspace_role or "ADJUDICATOR").upper()
                    except Exception:
                        pass
                    if last_user or last_ai:
                        conv_summary = None
                        if conv_history and len(conv_history) > 2:
                            parts = []
                            for m in conv_history[-4:]:
                                r = m.get("role") or m.get("role", "")
                                c = (m.get("content") or "")[:80]
                                if c:
                                    parts.append(f"{r}: {c}")
                            conv_summary = " | ".join(parts) if parts else None
                        generated = await self._generate_contextual_suggestion_chips(
                            workspace_id=workspace_id,
                            role=role,
                            user_message=last_user,
                            ai_response_text=last_ai,
                            max_chips=12,
                            conversation_summary=conv_summary,
                        )
                        if generated:
                            self._workspace_chat_suggestion_chips[workspace_id] = generated
                            count = min(max_prompts, len(generated))
                            prompts = random.sample(generated, count) if count > 0 else []
                            logger.info("probing_prompts: generated {} contextual chips, returning random {}", len(generated), len(prompts))
                # 3) Fallback: strategy chips (random 4 from 15-16)
                if not prompts:
                    chips = self._workspace_suggestion_chips.get(workspace_id) or []
                    if not chips and self._last_workspace_id_with_chips:
                        chips = self._workspace_suggestion_chips.get(self._last_workspace_id_with_chips) or []
                    if not chips:
                        logger.warning("probing_prompts_request: no suggestion chips for workspace {}", workspace_id)
                    count = min(max_prompts, len(chips)) if chips else 0
                    prompts = random.sample(chips, count) if count > 0 else []
            
            response = {
                "workspaceId": data.get("workspaceId"),
                "userId": data.get("userId"),
                "actionableId": data.get("actionableId"),
                "chatId": data.get("chatId"),
                "chatType": data.get("chatType"),
                "viewType": data.get("viewType"),
                "prompts": prompts,
            }
            if data.get("timestamp") is not None:
                response["timestamp"] = data.get("timestamp")
            if self._socket_client:
                await self._socket_client.emit("probing_prompts_response", response)
                logger.success(
                    "probing_prompts_response emitted: workspaceId={}, count={}",
                    workspace_id[:12] if workspace_id else "",
                    len(prompts),
                )
            else:
                logger.warning("No socket client to emit probing_prompts_response")
        except Exception as e:
            logger.exception(e)
            logger.error("probing_prompts_request failed: {}", str(e))
    
    # ========================================================================
    # Legacy Event Handlers (for backward compatibility)
    # ========================================================================
    
    async def on_workspace_updated(self, data: Dict[str, Any]) -> None:
        """Handle workspace update event (legacy)."""
        try:
            logger.debug("Processing legacy workspace_updated event")
            workspace_id = data.get("workspace_id") or data.get("workspaceId")
            logger.info(f"Workspace updated (legacy): {workspace_id}")
            
            await audit_logger.log_event(
                "workspace_updated_legacy",
                {"workspace_id": workspace_id}
            )
        except Exception as e:
            logger.error(f"Error processing workspace_updated: {e}")
            raise
    
    async def on_workspace_deleted(self, data: Dict[str, Any]) -> None:
        """Handle workspace deletion event (legacy)."""
        try:
            workspace_id = data.get("workspace_id") or data.get("workspaceId")
            logger.info(f"Workspace deleted (legacy): {workspace_id}")
            
            # Delete the workspace
            await workspace_manager.delete_workspace(workspace_id)
            
            await audit_logger.log_event(
                "workspace_deleted",
                {"workspace_id": workspace_id}
            )
        except Exception as e:
            logger.error(f"Error processing workspace_deleted: {e}")
            raise


# Global event handler instance
event_handler = SocketEventHandler()
