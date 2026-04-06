import { GoogleGenAI, Type, GenerateContentResponse } from "@google/genai";
import { EMPTY_ANALYSIS_REPORT, CaseLaw } from "@/types";

const apiKey = process.env.GEMINI_API_KEY;
const ai = new GoogleGenAI({ apiKey: apiKey ?? "" });

const MODEL_FAST = 'gemini-3-flash-preview';
const MODEL_ADVANCED = 'gemini-3-pro-preview';

/**
 * Enhanced JSON Parser to handle potential LLM conversational prefix/suffix
 */
function cleanAndParseJson(text: string) {
    let processedText = text.trim();
    processedText = processedText.replace(/^```json\s*/, "").replace(/\s*```$/, "");

    const start = Math.min(
        processedText.indexOf('{') === -1 ? Infinity : processedText.indexOf('{'),
        processedText.indexOf('[') === -1 ? Infinity : processedText.indexOf('[')
    );
    const end = Math.max(processedText.lastIndexOf('}'), processedText.lastIndexOf(']'));

    if (start === Infinity || end === -1) return {};

    try {
        return JSON.parse(processedText.substring(start, end + 1));
    } catch (e) {
        console.error("JSON Parse Error:", e);
        return {};
    }
}


// Shared schema for case analysis
const CASE_ANALYSIS_SCHEMA = {
    type: Type.OBJECT,
    properties: {
        documentAssessments: {
            type: Type.ARRAY,
            items: {
                type: Type.OBJECT,
                properties: {
                    fileName: { type: Type.STRING },
                    qualityScore: { type: Type.NUMBER, description: "A score from 0-100 indicating AI readability. Below 40 is poor." },
                    qualityIssues: { type: Type.ARRAY, items: { type: Type.STRING }, description: "List of issues like 'Blurry Scan', 'Handwritten Text'." },
                    summary: { type: Type.STRING, description: "A concise, one-sentence summary of the document's content." },
                    strength: { type: Type.STRING, enum: ["High", "Medium", "Low"], description: "The strength of this document as a piece of evidence for the case." }
                }
            }
        },
        listOfDates: {
            type: Type.ARRAY,
            items: {
                type: Type.OBJECT,
                properties: {
                    date: { type: Type.STRING, description: "YYYY-MM-DD" },
                    title: { type: Type.STRING },
                    description: { type: Type.STRING },
                    sourceDocName: { type: Type.STRING },
                    relevance: { type: Type.STRING, enum: ["High", "Medium", "Low"], description: "The relevance of this event to the core case arguments." },
                    pageNumber: { type: Type.NUMBER, description: "The specific page number in the source document where this event is mentioned." }
                }
            }
        },
        proceduralTimeline: {
            type: Type.ARRAY,
            items: {
                type: Type.OBJECT,
                properties: {
                    date: { type: Type.STRING, description: "YYYY-MM-DD" },
                    title: { type: Type.STRING },
                    description: { type: Type.STRING },
                    sourceDocName: { type: Type.STRING },
                    pageNumber: { type: Type.NUMBER, description: "The specific page number in the source document where this event is mentioned." }
                }
            }
        },
        laws: {
            type: Type.ARRAY,
            items: {
                type: Type.OBJECT,
                properties: {
                    act: { type: Type.STRING },
                    section: { type: Type.STRING },
                    description: { type: Type.STRING },
                    relevance: { type: Type.STRING, enum: ["High", "Medium", "Low"] }
                }
            }
        },
        parties: {
            type: Type.ARRAY,
            items: {
                type: Type.OBJECT,
                properties: {
                    name: { type: Type.STRING },
                    role: { type: Type.STRING, enum: ['Petitioner', 'Respondent', 'Witness', 'Victim', 'Accused', 'Other'] },
                    details: { type: Type.STRING }
                }
            }
        },
        caseLaws: {
            type: Type.ARRAY,
            items: {
                type: Type.OBJECT,
                properties: {
                    citation: { type: Type.STRING, description: "Full citation, e.g., (2020) 1 SCC 1" },
                    court: { type: Type.STRING, description: "e.g., Supreme Court of India" },
                    summary: { type: Type.STRING, description: "A brief summary of the case law's relevance." },
                    relevance: { type: Type.STRING, enum: ["High", "Medium", "Low"], description: "The relevance of this precedent to the current case." }
                }
            }
        },
        nextCriticalDate: { type: Type.STRING, description: "YYYY-MM-DD" },
        criticalDateDescription: { type: Type.STRING },
        analysis: {
            type: Type.OBJECT,
            properties: {
                missingInfo: {
                    type: Type.ARRAY,
                    items: { type: Type.STRING },
                    description: "CRITICAL: Only list information that is truly missing. Always check the 'documents' list and their summaries first. If a document such as an invoice, bill, receipt, contract, or warranty card is present (and its basic details are visible), do NOT ask for it again in 'missingInfo'. If you are uncertain, leave it out"
                },
                pendingActions: { type: Type.ARRAY, items: { type: Type.STRING } },
                strengths: { type: Type.ARRAY, items: { type: Type.OBJECT, properties: { point: { type: Type.STRING }, laws: { type: Type.ARRAY, items: { type: Type.STRING } } } } },
                weaknesses: { type: Type.ARRAY, items: { type: Type.STRING } }, // Corrected from lweaknesses
                risks: { type: Type.ARRAY, items: { type: Type.STRING } },
                relevantJurisdiction: {
                    type: Type.ARRAY,
                    items: { type: Type.STRING },
                    description: "List of all relevant courts or legal jurisdictions identified for this case (e.g., ['High Court of Delhi', 'District Court, Saket'])."
                },
                nextSteps: { type: Type.ARRAY, items: { type: Type.STRING } },
                suggestedDrafts: {
                    type: Type.ARRAY,
                    items: {
                        type: Type.OBJECT,
                        properties: {
                            title: { type: Type.STRING },
                            relevance: { type: Type.STRING, enum: ["High", "Medium", "Low"] }
                        }
                    },
                    description: "Specific legal documents to draft based on the case facts, with relevance."
                },
                caseResolutionScore: {
                    type: Type.NUMBER,
                    description: "Genuine case quality score 0-100 based on missingInfo, strengths/weaknesses, document quality, and timeline clarity. Must vary by case; do not use a fixed default."
                }
            }
        }
    }
};

// Helper function for exponential backoff retry logic
async function withRetry<T>(fn: () => Promise<T>, retries = 3, baseDelay = 2000): Promise<T> {
    try {
        return await fn();
    } catch (error: any) {
        const isRateLimit =
            error?.status === 429 ||
            error?.code === 429 ||
            error?.status === 503 ||
            error?.code === 503 ||
            (error?.message && (error.message.includes('429') || error.message.includes('quota') || error.message.includes('exhausted') || error.message.includes('overloaded')));

        if (retries > 0 && isRateLimit) {
            await new Promise(resolve => setTimeout(resolve, baseDelay));
            return withRetry(fn, retries - 1, baseDelay * 2);
        }
        throw error;
    }
}

// Schema for extracting a single case law from an uploaded document
const CASE_LAW_FROM_DOCUMENT_SCHEMA = {
    type: Type.OBJECT,
    properties: {
        caseTitle: { type: Type.STRING, description: "Official case title or party names, e.g. 'ABC vs State of X'." },
        caseDescription: { type: Type.STRING, description: "2-4 sentences summarising the material facts and procedural posture." },
        courtName: { type: Type.STRING, description: "Canonical court name, e.g. 'Supreme Court of India', 'High Court of Delhi'." },
        relevance: { type: Type.STRING, description: "Overall relevance of this precedent: High, Medium, or Low." },
    }
};

// Dedicated helper to compute ONLY missingInfo from a given case context.
// Use the more capable MODEL_ADVANCED so it follows strict instructions better.
export const computeMissingInformation = async (caseContextJSON: string): Promise<string[] | null> => {
    try {
        const prompt = `
        You are a Skeptical Indian Legal Auditor. 
        Your task is to identify TRULY missing information for this case.

        **CASE CONTEXT:**
        ${caseContextJSON}

        **STRICT AUDIT STEPS:**
        1. **Inventory Check:** Look at 'documentAssessments'. If a file is labeled as an Invoice, Bill, Receipt, or Warranty, you are FORBIDDEN from asking for "Proof of Purchase," "Price details," or "Warranty period."
        2. **Status vs. Presence:** Even if a document is "Poor Quality" or "Handwritten," it is still PRESENT. Do not list it as missing.
        3. **Logic Gate:** Only list an item if a lawyer could NOT find it anywhere in the provided summaries or timelines.

        **OUTPUT:** Return ONLY a JSON object: { "missingInfo": [] }
        `;

        const response = await ai.models.generateContent({
            model: MODEL_ADVANCED,
            contents: { parts: [{ text: prompt }] },
            config: { temperature: 0, responseMimeType: "application/json" }
        });

        const parsed = cleanAndParseJson(response.text || "{}") as any;
        return Array.isArray(parsed?.missingInfo) ? parsed.missingInfo : [];
    } catch { return null; }
};

// Secondary verifier: given a candidate missingInfo list from a first pass,
// ask MODEL_ADVANCED to re-check each item against the full case context and drop false positives.
export const verifyMissingInformation = async (caseContextJSON: string, candidates: string[]): Promise<string[] | null> => {
    if (!candidates.length) return [];
    try {
        const prompt = `
        You are an Auditor. Your only job is to REMOVE items from the "Candidate Missing List" if they are already mentioned in the "Case Context".

        **CASE CONTEXT:**
        ${caseContextJSON}

        **CANDIDATE MISSING LIST:**
        ${JSON.stringify(candidates)}

        **DELETION RULES:**
        - Delete the item if any document in 'documentAssessments' covers that topic.
        - Delete if the 'summary' of any document mentions the fact.
        - If you are unsure, DELETE IT. We only want 100% certain gaps.

        **OUTPUT:** Return ONLY the filtered JSON: { "missingInfo": [] }
        `;

        const response = await ai.models.generateContent({
            model: MODEL_ADVANCED,
            contents: { parts: [{ text: prompt }] },
            config: { temperature: 0, responseMimeType: "application/json" }
        });

        const parsed = cleanAndParseJson(response.text || "{}") as any;
        return Array.isArray(parsed?.missingInfo) ? parsed.missingInfo : [];
    } catch { return null; }
};

export const analyzeMultipleDocuments = async (fileParts: any[]) => {
    try {
        const prompt = `
        Analyze these Indian Legal documents. 
        
        **RULES for Document Summary:**
        - Start every summary with the Document Type in CAPS (e.g., TAX INVOICE:, LEGAL NOTICE:, CHAT TRANSCRIPT:).
        - Include specific facts: Dates, Amounts, GST, Parties.
        - This summary is the "Source of Truth" to prevent the system from thinking this info is missing later.

        **RULES for BNS:**
        - NEVER use "IPC". Use "Bharatiya Nyaya Sanhita" or "BNS".
        - Map Section 420 IPC to Section 318 BNS, etc.

        **RULES for Quality:**
        - Assign varied scores. If a doc is a clear digital PDF, 95+. If a photo of a paper, 60-70.
        `;

        const finalPrompt = `${prompt} \n **Output Format:** Return a single JSON object matching this schema: \n ${JSON.stringify(CASE_ANALYSIS_SCHEMA)}`;

        const response = await ai.models.generateContent({
            model: MODEL_FAST,
            contents: { parts: [{ text: finalPrompt }, ...fileParts] },
            config: { temperature: 0 }
        });

        let parsed = cleanAndParseJson(response.text || "{}");

        // Execute Two-Stage Missing Info Logic
        if (parsed?.analysis) {
            const initialMissing = await computeMissingInformation(JSON.stringify(parsed));
            if (initialMissing) {
                const verifiedMissing = await verifyMissingInformation(JSON.stringify(parsed), initialMissing);
                parsed.analysis.missingInfo = verifiedMissing || [];
            }
        }

        return parsed;
    } catch (err) {
        console.error("Analysis Failed:", err);
        return null;
    }
};

// Analyze a single uploaded judgment/order and extract structured case law details
export const analyzeCaseLawDocument = async (
    filePart: any,
    fileName: string
): Promise<CaseLaw | null> => {
    try {
        const basePrompt = `
You are an expert Indian legal research assistant.

You will be given ONE uploaded judgment or court order as an attached file.
From this document, extract structured case law details in a way that can be reused inside an AI legal workspace.

For the attached document named "${fileName}", do ALL of the following:

1. **Case Title (caseTitle)**:
   - Extract the official reported title or party names from the judgment (e.g., "ABC v. State of X", "Cadila Healthcare Ltd. v. Cadila Pharmaceuticals Ltd.").
   - If you truly cannot identify a clear title from the document, construct a short descriptive title based on the parties or subject matter (e.g., "Cyber Defamation – Employer v. Employee").
   - Do **NOT** output generic placeholders like "PETITIONER vs RESPONDENT".

2. **Case Description (caseDescription)**:
   - In 2–3 short lines, summarise the material facts and brief procedural posture of the case.
   - Keep this concise and scannable for a busy lawyer.

3. **Court Name (courtName)**:
   - Output a clean, canonical court name such as "Supreme Court of India" or "High Court of Delhi".
   - If the court truly cannot be inferred, use "Unknown Court".

4. **Analysis (analysis)**:
   - In 2–3 short lines, explain the key legal holding and how/when this precedent can be useful in similar matters.
   - Focus on practical advocacy value and keep the language plain.

5. **Relevance (relevance)**:
   - Rate how powerful this precedent is in a typical case with similar facts: "High", "Medium", or "Low".

6. **Relevant Case Note (relevantCase)**:
   - Write 1-2 sentences as if under a heading "Relevant Case", in plain language, explaining why this judgment could help the user's matter.

Return **one JSON object** with the following shape (and no extra commentary or markdown):
{
  "caseTitle": "string",
  "caseDescription": "string",
  "courtName": "string",
  "analysis": "string",
  "relevance": "High | Medium | Low",
  "relevantCase": "string"
}
`;

        const isComplexDocument =
            filePart &&
            filePart.inlineData &&
            (
                filePart.inlineData.mimeType === 'application/pdf' ||
                filePart.inlineData.mimeType === 'application/msword' ||
                filePart.inlineData.mimeType === 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
            );

        let config: any;
        let finalPrompt = basePrompt;

        if (isComplexDocument) {
            // For complex docs, rely on prompt-based JSON instructions
            config = {};
            finalPrompt += `

CRITICAL: Respond with a single, valid JSON object ONLY. Do not include any explanations or markdown fences.`;
        } else {
            // For simple text/image, enforce JSON via schema
            config = {
                responseMimeType: "application/json",
                responseSchema: CASE_LAW_FROM_DOCUMENT_SCHEMA
            };
        }

        const parts: any[] = [{ text: finalPrompt }];
        if (filePart) {
            parts.push(filePart);
        }

        const response = await withRetry<GenerateContentResponse>(() =>
            ai.models.generateContent({
                model: MODEL_FAST,
                contents: { parts },
                config
            })
        );

        const responseText = response.text || "{}";
        const raw = cleanAndParseJson(responseText) as any;

        const rawTitle = (raw.caseTitle || "").toString().trim();
        const baseFromFile =
            (fileName || "").toString().replace(/\.[^/.]+$/, "").trim() || "Uploaded Case Law";
        const citation = rawTitle && rawTitle.length > 0 ? rawTitle : baseFromFile;

        const courtName =
            (raw.courtName || "").toString().trim() || "Unknown Court";

        const relevanceRaw = (raw.relevance || "").toString().trim();
        const relevanceNormalized =
            relevanceRaw === "High" || relevanceRaw === "Medium" || relevanceRaw === "Low"
                ? relevanceRaw
                : undefined;

        const description = (raw.caseDescription || "").toString().trim();
        const analysis = (raw.analysis || "").toString().trim();
        const relevantCaseNote = (raw.relevantCase || "").toString().trim();

        const combinedSummary =
            analysis || description || "Case law added from uploaded document.";

        const out: CaseLaw = {
            id: `caselaw_${Date.now()}`,
            citation,
            court: courtName,
            summary: combinedSummary,
            relevance: relevanceNormalized,
            title: rawTitle || baseFromFile,
            description: description || combinedSummary,
            analysis: analysis || combinedSummary,
            relevantCase: relevantCaseNote || ""
        };

        return out;
    } catch {
        return null;
    }
};


export const generateChatMessage = async (
    history: {
        role: 'user' | 'model';
        text: string;
        attachmentData?: string;
        mimeType?: string;
        attachmentDataList?: { data: string; mimeType: string; fileName?: string }[];
    }[],
    context: string,
    systemInstruction: string,
    template?: { mimeType: string; data: string },
    model = MODEL_ADVANCED
): Promise<string> => {
    try {
        const pastHistory = history.slice(0, -1);
        const lastMessage = history[history.length - 1];

        const templateInstruction = template
            ? `\n**CRITICAL DRAFTING INSTRUCTION:** A document template has been provided as an attachment. You MUST generate the new draft following the exact structure, layout, headings, and style of this template. Fill in any placeholders using the relevant case data. The final output should be the completed document based on the template.`
            : '';

        const finalSystemInstruction = `
            You are a helpful, empathetic, and intelligent Indian Legal Assistant. You are fluent in all Indian regional languages.
            
            **CORE RULES:**
            1.  **Language**: ALWAYS respond in the same language the user uses (e.g., Hindi, Tamil, English).
            2.  **Persona**: Be natural, concise, and empathetic like a helpful colleague. Do NOT use formal intros.
            3.  **Brevity**: Keep responses short (under 150 words) unless providing a final draft.
            4.  **STRICT BNS ENFORCEMENT**: The Indian Penal Code (IPC) is defunct. You MUST strictly use ONLY Bharatiya Nyaya Sanhita (BNS) sections for penal offenses. If the user or documents mention IPC, you must translate it and ONLY speak in terms of BNS. Do NOT say "Section 302 IPC", say "Section 103 BNS".
            5.  **Drafting Protocol**: 
                - If the user asks to "draft" something, your FIRST response must be a short, empathetic message followed by a list of 1-3 critical questions needed to complete it.
                - After the user provides the answers, your SECOND response should be the full, complete draft with NO placeholders. Prepend this final draft with the hidden marker: [FINAL_DRAFT].
            6.  **Missing Information Protocol**: If the user asks a question that starts with "Why is" and ends with "considered missing information for this case?", your response MUST end with this exact sentence on a new line: "Kindly provide necessary information or supporting documents to help analyse this further."
            7.  **Strategic Follow-up Protocol**: If the user's query contains phrases like "key strength", "how to leverage it", "weakness", "risk", or "how to mitigate", your answer MUST end with a single, relevant, strategic follow-up question on a new line to guide the lawyer's next steps.
            8.  **Workspace Role Perspective**: In the case data/context there may be a field "workspaceRole" with one of "PETITIONER", "RESPONDENT", "MEDIATOR", or "ADJUDICATOR". ALWAYS respond as if you are advising that role. If "workspaceRole" is missing, assume you are advising the PETITIONER.
            
            ${templateInstruction}

            **USER'S CUSTOM INSTRUCTIONS (Apply if provided):**
            ${systemInstruction}
            
            **CASE DATA (For Context):**
            ${context}
        `;

        const historyParts = pastHistory.map(h => {
            const parts: any[] = [{ text: h.text }];

            // Support multiple inline attachments per message when available.
            if (Array.isArray(h.attachmentDataList) && h.attachmentDataList.length > 0) {
                h.attachmentDataList.forEach(att => {
                    if (att?.data && att?.mimeType) {
                        parts.push({
                            inlineData: {
                                mimeType: att.mimeType,
                                data: att.data
                            }
                        });
                    }
                });
            } else if (h.attachmentData && h.mimeType) {
                parts.push({
                    inlineData: {
                        mimeType: h.mimeType,
                        data: h.attachmentData
                    }
                });
            }

            return {
                role: h.role,
                parts: parts
            };
        });

        const activeChat = ai.chats.create({
            model: model,
            config: {
                systemInstruction: finalSystemInstruction,
            },
            history: historyParts
        });

        const currentParts: any[] = [];
        if (lastMessage.text) {
            currentParts.push({ text: lastMessage.text });
        }
        if (Array.isArray(lastMessage.attachmentDataList) && lastMessage.attachmentDataList.length > 0) {
            lastMessage.attachmentDataList.forEach(att => {
                if (att?.data && att?.mimeType) {
                    currentParts.push({
                        inlineData: {
                            mimeType: att.mimeType,
                            data: att.data
                        }
                    });
                }
            });
        } else if (lastMessage.attachmentData && lastMessage.mimeType) {
            currentParts.push({
                inlineData: {
                    mimeType: lastMessage.mimeType,
                    data: lastMessage.attachmentData
                }
            });
        }
        if (template) {
            currentParts.push({
                inlineData: {
                    mimeType: template.mimeType,
                    data: template.data
                }
            });
        }

        const response = await withRetry<GenerateContentResponse>(() => activeChat.sendMessage({ message: currentParts }));
        // FIX: The `.text` property is a getter, not a method.
        return response.text ?? '';

    } catch (e: any) {
        if (e.status === 429 || e.code === 429 || e.message?.includes('429') || e.message?.includes('quota')) {
            return "⚠️ **System Alert:** I am currently experiencing high traffic (Quota Exceeded). Please wait a moment and try again.";
        }
        return "I encountered an error processing your request. Please check the internet connection or try again.";
    }
};

export const analyzeTextualUpdate = async (
    question: string,
    answer: string,
    filePart: any | null,
    fileName: string | null,
    currentCaseJSON: string
) => {
    try {
        const documentContext = fileName
            ? `The user has also uploaded a document named "${fileName}" as part of their response.`
            : "";
        const sourceDocInstruction = fileName
            ? `- **Source Document**: If a document is provided, you MUST set 'sourceDocName' for ANY extracted event (in 'listOfDates' or 'proceduralTimeline') to its name: "${fileName}".`
            : `- **Source Document**: If a document is provided, set 'sourceDocName' to its name.`;

        const prompt = `
            You are an expert Indian Legal Assistant. The user is providing an update to answer a specific question about an ongoing case.
            
            **Current Case Context (first 10000 chars):**
            ${currentCaseJSON.substring(0, 10000)}

            **The question you previously asked the user was:**
            "${question}"

            **The user's response is:**
            "${answer}"

            ${documentContext}

            **TASK:**
            Analyze the user's response and any attached document. Extract any new, structured information that can be added to the case file.
            - **Focus**: Only extract information directly related to the user's answer.
            - **Timeline Distinction**: You must differentiate between factual incidents for 'listOfDates' and formal procedural steps (hearings, deadlines) for 'proceduralTimeline'.
            - **Date Format**: STRICTLY use YYYY-MM-DD.
            - **Relevance**: If you extract a date (listOfDates), assess its relevance as 'High', 'Medium', or 'Low'.
            ${sourceDocInstruction}
            - **Page Number**: If a document is provided, try to extract the specific page number where the event is mentioned.
            - **STRICT BNS ENFORCEMENT**: The Indian Penal Code (IPC) is defunct. You MUST strictly use ONLY Bharatiya Nyaya Sanhita (BNS) sections. Replace ALL IPC references with BNS.
            - **Relevant Jurisdictions**: Identify ALL relevant courts/jurisdictions if mentioned. Ensure names are consistent and canonical (e.g., 'High Court of Delhi'). Return a de-duplicated list.
            - **Empty Fields**: Do NOT return empty arrays or null fields. Only return fields for which you have found new information.
            - **CRITICAL: Impact Statement**: After extracting the data, you MUST include a field called "impactStatement" in your JSON response. This should be a concise, one-sentence explanation of how the new information helps the case (e.g., "This clarifies the timeline of events," or "This mitigates the risk of 'unverified alibi' by providing a key document.").

            Return strictly ONE JSON object with a structure that is a SUBSET of the following.
        `;

        const isComplexDocument = filePart && (
            filePart.inlineData.mimeType === 'application/pdf' ||
            filePart.inlineData.mimeType === 'application/msword' ||
            filePart.inlineData.mimeType === 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
        );

        const responseSchemaWithImpact = {
            ...CASE_ANALYSIS_SCHEMA,
            properties: {
                ...CASE_ANALYSIS_SCHEMA.properties,
                impactStatement: { type: Type.STRING, description: "Explanation of how the new info impacts the case." }
            }
        };

        let config: any;
        let finalPrompt = prompt;

        if (isComplexDocument) {
            config = {};
            finalPrompt += `
              **Output Format:** Your response MUST be a single, valid JSON object that conforms to the following schema. Do not include any other text or markdown formatting.
              ${JSON.stringify(responseSchemaWithImpact, null, 2)}
            `;
        } else {
            config = {
                responseMimeType: "application/json",
                responseSchema: responseSchemaWithImpact
            };
        }

        const contentParts: any[] = [{ text: finalPrompt }];
        if (filePart) {
            contentParts.push(filePart);
        }

        const response = await withRetry<GenerateContentResponse>(() => ai.models.generateContent({
            model: MODEL_FAST,
            contents: { parts: contentParts },
            config: config
        }));

        // FIX: The `.text` property is a getter, not a method.
        const responseText = response.text || '{}';
        return cleanAndParseJson(responseText);
    } catch (error) {
        return null;
    }
};

// New function to analyze a single new document in the context of an existing case
export const analyzeSingleDocument = async (
    filePart: any,
    fileName: string,
    existingCaseJSON: string
) => {
    try {
        const prompt = `
            You are an expert Indian Legal Assistant. Your task is to analyze a new document and extract information to add to an existing case file, avoiding any duplicates.

            **Existing Case Context (first 10000 chars of JSON):**
            ${existingCaseJSON.substring(0, 10000)}

            **New Document to Analyze:**
            The document is named: "${fileName}"

            **TASK:**
            1.  Read the new document provided.
            2.  Compare the information within it against the existing case context.
            3.  Extract ONLY new, unique information that is not already present in the case data provided.
            4.  **CRITICAL - DOCUMENT QUALITY & STRENGTH ASSESSMENT:**
                For the document named "${fileName}", provide a quality assessment.
                - **qualityScore**: An integer score from 0 to 100 representing the document's readability for an AI. (0-39: Poor, 40-69: Fair, 70-89: Good, 90-100: Excellent).
                - **qualityIssues**: A list of specific issues found (e.g., "Blurry Scan", "Heavily Handwritten", "Low Resolution"). Return an empty array if quality is good.
                - **summary**: A concise, one-sentence summary of what the document is about.
                - **strength**: The evidentiary strength of the document for the case, rated as 'High', 'Medium', or 'Low'.
                This assessment should be in a top-level array 'documentAssessments'.
            5.  **Timeline Distinction**: You MUST differentiate between factual incidents for 'listOfDates' and formal procedural steps (hearings, deadlines) for 'proceduralTimeline'.
            6.  **Source Attribution**: For every extracted event, you MUST set 'sourceDocName' to "${fileName}".
            7.  **Page Number**: Extract the specific 'pageNumber' where the information is found.
            8.  **Relevance**: Assess relevance ('High', 'Medium', 'Low') for list of dates.
            9.  **Strict BNS**: Use Bharatiya Nyaya Sanhita (BNS) sections only. No IPC.
            10. **Case Laws**: Use your internal knowledge to suggest relevant Indian case law precedents based on this new document.
            11. **Jurisdictions**: Identify any relevant courts. Ensure names are canonical and de-duplicated (e.g., use 'High Court of Delhi' consistently, not 'Delhi High Court').

            Return strictly ONE JSON object with the shared case schema structure (listOfDates, laws, parties, analysis, caseLaws, etc.).
        `;

        const isComplexDocument = filePart && (
            filePart.inlineData.mimeType === 'application/pdf' ||
            filePart.inlineData.mimeType === 'application/msword' ||
            filePart.inlineData.mimeType === 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
        );

        let config: any;
        let finalPrompt = prompt;

        if (isComplexDocument) {
            config = {};
            finalPrompt += `
              **Output Format:** Your response MUST be a single, valid JSON object that conforms to the following schema. Do not include any other text or markdown formatting.
              ${JSON.stringify(CASE_ANALYSIS_SCHEMA, null, 2)}
            `;
        } else {
            config = {
                responseMimeType: "application/json",
                responseSchema: CASE_ANALYSIS_SCHEMA
            };
        }

        const contentParts: any[] = [{ text: finalPrompt }];
        if (filePart) {
            contentParts.push(filePart);
        }

        const response = await withRetry<GenerateContentResponse>(() => ai.models.generateContent({
            model: MODEL_FAST, // Use faster model for quicker updates
            contents: { parts: contentParts },
            config: config
        }));

        // FIX: The `.text` property is a getter, not a method.
        const responseText = response.text || '{}';
        return cleanAndParseJson(responseText);
    } catch (error) {
        return null;
    }
};

export const refreshCaseContext = async (
    caseContextJSON: string,
    documents?: { name?: string; summary?: string }[]
) => {
    try {
        const docListText =
            documents && documents.length > 0
                ? `\n\n**CRITICAL - DOCUMENTS ALREADY IN THE CASE (do NOT list these or their core details as missing):** The following documents are already uploaded. Do NOT include in 'missingInfo' any item that refers to: Tax Invoice, Retail Bill, Purchase receipt, Warranty card/booklet, or correspondence/chat that these documents represent. Document names: ${documents.map((d) => d.name || "").join(", ")}.`
                : "";

        const prompt = `
            You are an expert Indian Legal Assistant.
            The user wants to refresh the case analysis based on the core facts (timelines).
            ${docListText}

            **Workspace Role Perspective**
            Inside the JSON below there may be a field "workspaceRole" with one of "PETITIONER", "RESPONDENT", "MEDIATOR", or "ADJUDICATOR". You MUST analyse the case and write all analysis as if you are advising that role. If "workspaceRole" is missing, assume you are advising the PETITIONER.
            
            **Case Facts & Timelines:**
            ${caseContextJSON.substring(0, 10000)}

            **TASK: FULL CONTEXT HYDRATION**
            1.  **Analysis Report**: Generate a comprehensive 'analysis' object (strengths, weaknesses, risks, missingInfo, etc.) based on the provided facts. Cover ALL aspects (Criminal, Civil, Procedural). Do NOT filter for a specific strategy yet. Do NOT list as missing any document type already listed above as "DOCUMENTS ALREADY IN THE CASE".
            2.  **Laws**: Identify ALL applicable laws (BNS/Acts) based on the events described in the timelines.
            3.  **Parties**: List ALL relevant parties mentioned in the timelines.
            4.  **Drafting Assistant**: Suggest a broad range of relevant drafts with their relevance ('High', 'Medium', 'Low').
            5.  **Case Laws**: Suggest broadly relevant Indian precedents.
            6.  **Strict BNS**: Use BNS sections only.

            Return strictly ONE JSON object with 'analysis', 'laws', 'parties', 'caseLaws' fields.
        `;

        const response = await withRetry<GenerateContentResponse>(() =>
            ai.models.generateContent({
                model: MODEL_ADVANCED,
                contents: { parts: [{ text: prompt }] },
                config: {
                    temperature: 0,
                    responseMimeType: "application/json",
                    responseSchema: CASE_ANALYSIS_SCHEMA
                }
            })
        );

        const responseText = response.text || "{}";
        const result = cleanAndParseJson(responseText);
        return result;
    } catch (error) {
        return { analysis: EMPTY_ANALYSIS_REPORT, laws: [], parties: [], caseLaws: [] };
    }
};

export const generateProbingPrompts = async (context: string, activeViewTitle: string) => {
    try {
        const prompt = `
            Generate 3 short, punchy, strategic follow-up actions (max 3-4 words each) for a lawyer using an AI workspace.
            
            **Context:**
            Active View: ${activeViewTitle}
            Case Data: ${context}
            
            **CRITICAL:**
            - Look at the "Recent Conversation Focus" in the data. If the user just clicked a specific law, case law, or document, generate actions specific to THAT item.
            - Example: If user clicked "Section 302", prompts should be "Check Bail", "Find Judgments", "Draft Defense".
            - If context is generic, suggest standard legal steps.

            Output strictly a JSON array of strings: ["Action 1", "Action 2", "Action 3"]
        `;

        const response = await withRetry<GenerateContentResponse>(() => ai.models.generateContent({
            model: MODEL_FAST,
            contents: { parts: [{ text: prompt }] },
            config: {
                responseMimeType: "application/json",
                responseSchema: { type: Type.ARRAY, items: { type: Type.STRING } }
            }
        }));

        // FIX: The `.text` property is a getter, not a method.
        const responseText = response.text || '[]';
        return cleanAndParseJson(responseText);
    } catch (error) {
        return ["Analyze Risks", "Draft Notice", "Find Precedents"];
    }
};

/** Infer relevance ("High" | "Medium" | "Low") of each law to the case. Returns same order as input laws. */
export const inferLawRelevance = async (
    caseContextJSON: string,
    laws: { act: string; section: string; title?: string; actCode?: string }[]
): Promise<string[]> => {
    if (!laws.length) return [];
    try {
        const lawsDesc = laws.map((l, i) => `${i + 1}. ${l.actCode || ""} ${l.section} — ${l.act}${l.title ? ` (${l.title})` : ""}`).join("\n");
        const prompt = `
You are an expert Indian Legal Assistant. Given the case context and a list of acts/sections, rate how relevant each law is to this specific case.

**Case context (summary, category, parties, existing laws, analysis):**
${caseContextJSON.substring(0, 6000)}

**Laws to rate (one relevance per law, same order):**
${lawsDesc}

**TASK:** For each law above, output exactly one of: "High", "Medium", or "Low".
- High: directly applicable to the core legal issues or charges.
- Medium: related or supportive to the case.
- Low: tangentially related or background.

Output a JSON array of strings only, in the same order as the list above. Example: ["High", "Medium", "Low"]
`;

        const response = await withRetry<GenerateContentResponse>(() => ai.models.generateContent({
            model: MODEL_FAST,
            contents: { parts: [{ text: prompt }] },
            config: {
                responseMimeType: "application/json",
                responseSchema: { type: Type.ARRAY, items: { type: Type.STRING } }
            }
        }));
        const responseText = response.text || "[]";
        const arr = cleanAndParseJson(responseText);
        if (!Array.isArray(arr)) return laws.map(() => "Medium");
        const out: string[] = [];
        for (let i = 0; i < laws.length; i++) {
            const v = arr[i];
            const s = typeof v === "string" ? v.trim() : "";
            out.push(s === "High" || s === "Medium" || s === "Low" ? s : "Medium");
        }
        return out;
    } catch {
        return laws.map(() => "Medium");
    }
};

export const generateShortTitle = async (text: string) => {
    try {
        const prompt = `Generate a very short title (max 4 words) for this note: "${text.substring(0, 200)}"`;
        const response = await ai.models.generateContent({
            model: MODEL_FAST,
            contents: { parts: [{ text: prompt }] }
        });
        // FIX: The `.text` property is a getter, not a method.
        return response.text?.trim().replace(/^"|"$/g, '') || "Working Note";
    } catch {
        return "Working Note";
    }
};

export const getDraftingRequirements = async (
    suggestedDraftTitle: string,
    currentCaseJSON: string
): Promise<string[]> => {
    try {
        const prompt = `
            You are an expert Indian Legal Assistant. Based on the provided case data, identify 3-5 critical pieces of information (specific facts, dates, party details, etc.) that are currently missing from the case context but would be essential to draft a complete and effective "${suggestedDraftTitle}".
            Focus on actionable items the user can provide.

            **Current Case Context:**
            ${currentCaseJSON.substring(0, 12000)}

            Return a JSON object with a single field 'missingRequirements' which is an array of strings.
        `;
        const response = await withRetry<GenerateContentResponse>(() =>
            ai.models.generateContent({
                model: MODEL_ADVANCED,
                contents: { parts: [{ text: prompt }] },
                config: {
                    responseMimeType: "application/json",
                    responseSchema: {
                        type: Type.OBJECT,
                        properties: {
                            missingRequirements: {
                                type: Type.ARRAY,
                                items: { type: Type.STRING },
                            },
                        },
                    },
                },
            })
        );
        const parsed = cleanAndParseJson(response.text || "{}");
        return Array.isArray(parsed?.missingRequirements) ? parsed.missingRequirements : [];
    } catch (err) {
        console.error("getDraftingRequirements error", err);
        return [];
    }
};

export const generateInitialStrategy = async (caseMetadata: any) => {
    try {
        const prompt = `
      You are an expert Indian Legal Strategist.
      
      **Workspace Role Perspective**
      The case context JSON may contain a field "workspaceRole" with one of "PETITIONER", "RESPONDENT", "MEDIATOR", or "ADJUDICATOR". You MUST write the summary, strategic objective, and outcomes from the perspective of that role (what THEY should do). If "workspaceRole" is missing, assume you are advising the PETITIONER.
      
      **Case Context:**
      ${JSON.stringify(caseMetadata).substring(0, 8000)}

      **TASK:**
      1. **Summary**: Write a detailed, case-oriented summary (3–6 sentences, 80–180 words) that:
         - Starts with "This case is about..." and covers parties, core legal issues, key facts from the documents, and procedural context.
         - Is specific to the case and uploaded files—reference documents, dates, and laws where relevant.
         - Gives enough detail for a lawyer to understand the matter without re-reading the files.
      2. **Strategic Objective**: Write a longer, case-oriented strategic objective paragraph (4–8 sentences, 100–200 words) that:
         - Describes the recommended strategic direction for this specific case based on the documents and context.
         - References key facts, parties, applicable laws, and procedural posture.
         - Outlines priorities, risks to address, and how the evidence and timeline support (or limit) the strategy.
         - Is tailored to this case only—no generic advice.
      3. **Outcomes**: List 3–5 specific, distinct strategic outcomes or legal courses of action available for this case (e.g., "File Quashing Petition", "Seek Anticipatory Bail", "Initiate Civil Recovery", "Negotiate Settlement").
         - You MUST provide at least 3 distinct outcomes.
         - Do not return an empty list.
         - Each outcome should be concrete and tied to the case context above.
    `;

        const response = await withRetry<GenerateContentResponse>(() => ai.models.generateContent({
            model: MODEL_ADVANCED,
            contents: { parts: [{ text: prompt }] },
            config: {
                responseMimeType: "application/json",
                responseSchema: {
                    type: Type.OBJECT,
                    properties: {
                        summary: {
                            type: Type.STRING,
                            description: "A detailed, case-oriented summary: 3–6 sentences, 80–180 words, covering parties, legal issues, key facts from documents, and procedural context."
                        },
                        strategicObjective: {
                            type: Type.STRING,
                            description: "A longer, case-oriented strategic objective paragraph: 4–8 sentences, 100–200 words, describing recommended strategy, key facts, risks, and how evidence supports the approach for this specific case."
                        },
                        outcomes: {
                            type: Type.ARRAY,
                            items: { type: Type.STRING },
                            description: "A list of 3 to 5 distinct, actionable strategic outcomes."
                        }
                    }
                }
            }
        }));

        // FIX: The `.text` property is a getter, not a method.
        const result = cleanAndParseJson(response.text || '{}');

        // Fallback logic if summary or strategicObjective are missing
        if (!result.summary) result.summary = "This case requires a detailed review of the provided documents.";
        if (!result.strategicObjective) result.strategicObjective = "A detailed strategic objective will be formed after further review of the case documents and context.";

        if (!Array.isArray(result.outcomes)) {
            result.outcomes = [];
        } else {
            result.outcomes = result.outcomes.filter((o: any) => typeof o === 'string' && o.trim().length > 0);
        }

        if (result.outcomes.length === 0) {
            result.outcomes = [
                "Conduct Detailed Legal Review",
                "Identify Key Evidentiary Gaps",
                "Formulate Comprehensive Defense Strategy",
                "Evaluate Settlement Possibilities"
            ];
        }

        return result;
    } catch (error) {
        return {
            summary: "This case requires a detailed review of the provided documents.",
            strategicObjective: "A detailed strategic objective will be formed after further review of the case documents and context.",
            outcomes: [
                "Conduct Detailed Legal Review",
                "Identify Key Evidentiary Gaps",
                "Formulate Comprehensive Defense Strategy",
                "Evaluate Settlement Possibilities"
            ]
        };
    }
};

export const realignWorkspaceToStrategy = async (caseContextJSON: string, strategy: string) => {
    try {
        const prompt = `
            You are an expert Indian Legal Assistant. The user has decided to pursue ONLY this specific outcome: "${strategy}".

            **Workspace Role Perspective**
            Inside the case data there may be a field "workspaceRole" with one of "PETITIONER", "RESPONDENT", "MEDIATOR", or "ADJUDICATOR". You MUST re-contextualise and filter the workspace as if you are advising that role. If "workspaceRole" is missing, assume you are advising the PETITIONER.

            **Current Case Data:**
            ${caseContextJSON.substring(0, 10000)}

            **TASK: RE-CONTEXTUALIZE AND FILTER**
            You must REWRITE the workspace data to focus EXCLUSIVELY on achieving "${strategy}".
            **CRITICAL:** FILTER OUT any information (laws, analysis points, drafts, parties) that is NOT relevant to this specific strategy.

            1.  **Analysis Report**:
                - Strengths/Weaknesses/Risks: KEEP ONLY those relevant to "${strategy}". REMOVE others.
                - Missing Info: Ask ONLY for info needed for "${strategy}".
            2.  **Drafting Assistant**:
                - Update 'suggestedDrafts' to list ONLY documents required for "${strategy}". Remove generic drafts. Include relevance levels.
            3.  **Applicable Laws**:
                - List ONLY laws that are legally relevant for "${strategy}". REMOVE irrelevant sections.
            4.  **Relevant Parties**:
                - List ONLY parties crucial for "${strategy}".
            5.  **Case Laws**:
                - Suggest precedents ONLY if they support "${strategy}". REMOVE irrelevant case laws.
            6.  **List of Dates**:
                - You MUST return the complete list of dates provided in the context.
                - DO NOT REMOVE ANY EVENTS from 'listOfDates'.
                - ONLY update the 'relevance' field ('High', 'Medium', 'Low') for each event based on the new strategy: "${strategy}".

            **EXCLUSIONS:**
            - DO NOT change 'proceduralTimeline' (Upcoming Dates).
            - DO NOT change 'documents' (Supporting Material).
            - DO NOT add new jurisdictions; only return those from the context that are relevant to the strategy.
            - STRICTLY use BNS sections.

            Return the updated JSON object with fields: 'analysis', 'laws', 'parties', 'caseLaws', 'listOfDates'.
        `;

        const response = await withRetry<GenerateContentResponse>(() => ai.models.generateContent({
            model: MODEL_ADVANCED,
            contents: { parts: [{ text: prompt }] },
            config: {
                responseMimeType: "application/json",
                responseSchema: CASE_ANALYSIS_SCHEMA
            }
        }));

        // FIX: The `.text` property is a getter, not a method.
        const responseText = response.text || '{}';
        return cleanAndParseJson(responseText);
    } catch (error) {
        return null;
    }
};

export const recalculateCaseScore = async (caseContextJSON: string): Promise<number | null> => {
    try {
        const prompt = `
            You are a senior legal analyst. Your task is to provide a genuine "Case Quality Score" from 0-100 based ONLY on the completeness and quality of the provided case data. The score MUST vary case-by-case—do NOT return a fixed or default number (e.g. 65) for every case.

            **Scoring Criteria (apply strictly):**
            - 80-100: Well-documented, strong evidence, clear timelines, few missing items, more strengths than weaknesses.
            - 50-79: Decent foundation but notable gaps, some missing documents, or unclear events; balance of strengths and weaknesses.
            - 0-49: Poorly documented, critical information missing, significant weaknesses, or very few documents/dates.

            **You must derive the score from:** count and severity of missingInfo, number and weight of strengths vs weaknesses/risks, document quality/readability if present, clarity and completeness of listOfDates/proceduralTimeline, and parties/laws completeness. Output a single integer that reflects THIS case only.

            **Case Data:**
            ${caseContextJSON}

            **TASK:**
            Analyze the case data above and return a single JSON object with one property: caseResolutionScore (number 0-100). The score must be justified by the data; do not use a default.
        `;

        const response = await withRetry<GenerateContentResponse>(() => ai.models.generateContent({
            model: MODEL_FAST, // Fast model is sufficient for this scoring task
            contents: { parts: [{ text: prompt }] },
            config: {
                responseMimeType: "application/json",
                responseSchema: {
                    type: Type.OBJECT,
                    properties: {
                        caseResolutionScore: { type: Type.NUMBER }
                    }
                }
            }
        }));

        // FIX: The `.text` property is a getter, not a method.
        const result = cleanAndParseJson(response.text || '{}');
        return result.caseResolutionScore ?? null;

    } catch (error) {
        return null;
    }
};

export const getQualityScoreExplanation = async (analysisJSON: string): Promise<string> => {
    try {
        const prompt = `
            You are a senior legal analyst. Based on the following case analysis data, provide a concise, 2-3 sentence explanation for the current "Case Quality Score".
            
            **Key Factors to Consider:**
            - **Missing Information**: A high number of missing items lowers the score significantly.
            - **Strengths vs. Weaknesses**: A good balance or more strengths improves the score.
            - **Document Quality**: Mention if poor document readability (inferred from context) could be a factor.
            - **Timeline Clarity**: A well-defined timeline of dates is positive.

            **Case Analysis Data:**
            ${analysisJSON}

            **TASK:**
            Explain the score in simple terms for a lawyer. Start with "The current score is X because..."
        `;
        const response = await ai.models.generateContent({
            model: MODEL_FAST,
            contents: { parts: [{ text: prompt }] }
        });
        // FIX: The `.text` property is a getter, not a method.
        return response.text ?? "Could not generate an explanation at this time.";
    } catch (error) {
        return "An error occurred while generating the explanation.";
    }
};

export const getRiskExplanation = async (riskText: string, caseContextJSON: string): Promise<string> => {
    try {
        const prompt = `
            You are a senior legal analyst. A user wants to understand a specific risk/weakness.

            **Full Case Context (first 5000 chars):**
            ${caseContextJSON.substring(0, 5000)}

            **Risk/Weakness in Question:**
            "${riskText}"

            **TASK:**
            Provide a concise, 2-3 sentence explanation for why this is considered a risk or weakness. If possible, link it to specific missing information, known facts from the 'listOfDates', or lack of supporting evidence mentioned in the context.
        `;
        const response = await ai.models.generateContent({
            model: MODEL_FAST,
            contents: { parts: [{ text: prompt }] }
        });
        // FIX: The `.text` property is a getter, not a method.
        return response.text ?? "Could not generate an explanation at this time.";
    } catch (error) {
        return "An error occurred while generating the explanation.";
    }
}
