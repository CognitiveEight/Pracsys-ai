"""
Socket.IO event handlers with Intelligence Chat Agent integration.
Updated to match backend Socket.IO specification.
"""
from typing import Dict, Any, Callable, Awaitable, Optional
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

1. **Case Summary** (2-3 sentences): A clear summary of what this case is about, including key facts, parties involved, and the legal issues.

2. **Strategic Objective** (1-2 sentences): A specific, actionable objective that outlines the best course of action for someone in the {workspace_role} role.

3. **Detailed Recommendations** (3-4 bullet points): Specific steps or considerations based on the case facts.

**Format your response as JSON**:
{{
  "caseSummary": "...",
  "strategicObjective": "...",
  "recommendations": ["...", "...", "..."]
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
1. Case summary (2-3 sentences with key facts and parties)
2. Strategic objective (1-2 sentences specific to {workspace_role})
3. Four actionable recommendations

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
  "suggestionChips": [
    "Short actionable chip 1 (2-5 words)",
    "Short actionable chip 2",
    "Short actionable chip 3"
  ]
}}
```

suggestionChips: 3-5 short, actionable next-step labels (2-5 words each) specific to this case and role. Examples: "Review Title Deeds", "Map Legal Heirs", "Draft Demand Notice", "File FIR", "Preserve Digital Evidence". No full sentences."""
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
                        suggestion_chips = analysis_data.get("suggestionChips") or analysis_data.get("suggestion_chips") or []
                        if isinstance(suggestion_chips, str):
                            suggestion_chips = [suggestion_chips] if suggestion_chips else []
                        suggestion_chips = [str(s).strip() for s in suggestion_chips if s][:6]
                        
                        # Validate we got meaningful content
                        if case_summary and strategic_objective and recommendations:
                            logger.success("✅ AI analysis completed successfully")
                            logger.info(f"   - Case Summary: {len(case_summary)} chars")
                            logger.info(f"   - Strategic Objective: {len(strategic_objective)} chars")
                            logger.info(f"   - Recommendations: {len(recommendations)} items")
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
                            if not suggestion_chips:
                                suggestion_chips = ["Review documents", "Analyze provisions", "Draft action plan"]
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
                            
                            # Extract recommendations from the partial JSON
                            recommendations = []
                            suggestion_chips = []
                            if recommendations_match and len(recommendations_match) > 2:
                                # Filter out field names, keep only actual recommendations
                                potential_recs = [r for r in recommendations_match 
                                                if r not in ['caseSummary', 'strategicObjective', 'recommendations']
                                                and len(r) > 20]  # Recommendations should be substantial
                                recommendations = potential_recs[:4]  # Take up to 4
                            
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
                                suggestion_chips = ["Review evidence", "File legal action", "Preserve documents"]
                            
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
                suggestion_chips = ["Review documents", "Analyze BNS provisions", "Assess evidence", "Draft action plan"]
            
            # Format the response (use doc_count from vector store when batch_result not provided, e.g. after delete)
            strategies_payload = {
                "workspaceId": workspace_id,
                "userId": user_id,
                "role": workspace_role,
                "isUpdate": is_update,
                "caseSummary": case_summary,
                "strategicObjective": strategic_objective,
                "recommendations": recommendations,
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

    def _salvage_workspace_metadata_from_truncated_json(self, raw: str) -> Dict[str, Any]:
        """Extract caseTitle, caseCategory and subcategories from truncated Gemini JSON."""
        import re
        result = {"caseTitle": "", "caseCategory": "General", "subcategories": []}
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
        return result

    async def _get_workspace_category_and_subcategory(
        self,
        workspace_id: str,
        user_id: str,
    ) -> Dict[str, Any]:
        """
        Infer case title (from documents), case category and subcategories from workspace documents using AI.
        Returns {"caseTitle": str, "caseCategory": str, "subcategories": List[str]}. caseTitle is fixed once; do not overwrite on sync.
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
                return {"caseTitle": "", "caseCategory": "General", "subcategories": []}
            if not all_docs:
                return {"caseTitle": "", "caseCategory": "General", "subcategories": []}
            parts = []
            for i, doc in enumerate(all_docs, 1):
                content = (doc.get("content") or "").strip()
                if content and len(content) >= 20:
                    parts.append(f"--- Document {i} ---\n{content}")
            combined = "\n\n".join(parts) if parts else ""
            if not combined:
                return {"caseTitle": "", "caseCategory": "General", "subcategories": []}
            combined = (combined[:5000] + "...") if len(combined) > 5000 else combined
            gemini_client.initialize()
            prompt = f"""Based on the document content below, output a JSON object with:
- caseTitle: one short formal case title from the documents (e.g. "Umesh Malik vs Social Media Hacking Complaint" or "Complaint re Hacked Account - Shubham Malik"). Use parties/subject from the docs, not generic text.
- caseCategory: exactly one category in UPPERCASE (e.g. CRIMINAL LAW, CYBER LAW, CIVIL LAW).
- subcategories: array of 1-3 relevant Indian acts (e.g. "Bharatiya Nyaya Sanhita", "Information Technology Act, 2000").

Document content:
{combined}

Output ONLY this JSON, no markdown: {{"caseTitle": "Party/Subject vs Issue", "caseCategory": "CRIMINAL LAW", "subcategories": ["Bharatiya Nyaya Sanhita", "IT Act 2000"]}}"""
            raw = await gemini_client.generate(prompt, temperature=0.2, max_tokens=700)
            if not raw or "Error" in (raw[:80] or ""):
                return {"caseTitle": "", "caseCategory": "General", "subcategories": []}
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
                salvaged = self._salvage_workspace_metadata_from_truncated_json(raw)
                if salvaged.get("caseCategory") != "General" or salvaged.get("subcategories") or salvaged.get("caseTitle"):
                    return salvaged
                return {"caseTitle": "", "caseCategory": "General", "subcategories": []}
            try:
                data = json.loads(raw_clean)
            except json.JSONDecodeError:
                salvaged = self._salvage_workspace_metadata_from_truncated_json(raw)
                if salvaged.get("caseCategory") != "General" or salvaged.get("subcategories") or salvaged.get("caseTitle"):
                    logger.info("Used salvaged workspace metadata from truncated response")
                    return salvaged
                return {"caseTitle": "", "caseCategory": "General", "subcategories": []}
            case_title = (data.get("caseTitle") or "").strip()
            case_category = (data.get("caseCategory") or "General").strip().upper()
            if case_category and "LAW" not in case_category and "ACT" not in case_category:
                case_category = case_category + " LAW"
            subcategories = data.get("subcategories") or data.get("subcategory") or []
            if isinstance(subcategories, str):
                subcategories = [subcategories] if subcategories else []
            subcategories = [str(s).strip() for s in subcategories if s][:5]
            logger.info(
                "Workspace metadata: caseTitle={}, caseCategory={}, subcategories={}",
                case_title[:50] if case_title else "",
                case_category,
                subcategories,
            )
            return {"caseTitle": case_title, "caseCategory": case_category or "General", "subcategories": subcategories}
        except Exception as e:
            logger.warning("Workspace category/subcategory generation failed: {}", str(e))
            return {"caseTitle": "", "caseCategory": "General", "subcategories": []}

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
                                        },
                                    }
                                    await self._socket_client.emit(
                                        "document_feature_response",
                                        workspace_metadata_payload
                                    )
                                    logger.success(
                                        "✅ Workspace metadata sent: title={} (from docs, fixed), category={}, subcategories={}",
                                        (meta.get("caseTitle") or "")[:50],
                                        meta.get("caseCategory"),
                                        meta.get("subcategories"),
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
                "🔄 Workspace sync: {} (User: {})",
                payload.workspaceId,
                payload.userId
            )
            
            # Update workspace metadata
            updates = {}
            if payload.title:
                updates["title"] = payload.title
            if payload.status:
                updates["status"] = payload.status
            
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
                    # Regenerate strategy from remaining documents
                    workspace_role = "ADJUDICATOR"
                    try:
                        ctx = workspace_manager.get_workspace(payload.workspaceId)
                        workspace_role = (ctx.workspace_role or "ADJUDICATOR").upper()
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
                                        },
                                    }
                                    await self._socket_client.emit(
                                        "document_feature_response",
                                        workspace_metadata_payload
                                    )
                                    logger.success(
                                        "✅ Workspace metadata (sync) sent: title=unchanged, category={}, subcategories={}",
                                        meta.get("caseCategory"),
                                        meta.get("subcategories"),
                                    )
                                except Exception as meta_err:
                                    logger.warning("Failed to emit workspace_metadata (sync): {}", str(meta_err))

                            # Regenerate strategies from ALL documents in workspace (including new uploads)
                            if batch_result.get("successful", 0) > 0:
                                logger.info("🎯 Regenerating strategies from all documents in workspace...")
                                try:
                                    workspace_role = "ADJUDICATOR"
                                    try:
                                        ctx = workspace_manager.get_workspace(payload.workspaceId)
                                        workspace_role = (ctx.workspace_role or "ADJUDICATOR").upper()
                                    except Exception:
                                        pass
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
            
            # STEP 1: Get workspace context
            workspace_id = payload.workspaceId or "unknown"
            user_id = payload.userId or "unknown"
            chat_id = payload.chatId or f"chat_{datetime.utcnow().timestamp()}"
            
            # STEP 2: Build role context
            # For now, use a default role context since we don't have persistent state storage
            # TODO: Implement proper workspace state retrieval from database
            role_context = {
                "role": "ADJUDICATOR",  # Default role
                "workspace_id": workspace_id,
                "user_id": user_id
            }
            
            # STEP 3: Build chat context
            chat_context = ChatContext(
                workspace_id=workspace_id,
                user_id=user_id,
                role_context=role_context,
                workspace_state={"workspace_id": workspace_id, "user_id": user_id},
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
            
            # STEP 5: Build AI response payload
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
                )
            )
            
            # STEP 6: Emit ai_response back to server
            if self._socket_client:
                await self._socket_client.emit_ai_response(ai_response.dict(by_alias=True))
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
