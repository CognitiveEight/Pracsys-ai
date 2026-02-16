"""
Google Gemini AI client for SimplerToday.
Handles all interactions with Gemini 2.0 Flash model.
"""
from typing import List, Dict, Any, Optional
import google.generativeai as genai
from loguru import logger

from app.core.config import settings
from app.observability.trace import tracer


class GeminiClient:
    """
    Client for Google Gemini AI services.
    
    Features:
    - Text generation with Gemini 2.0 Flash
    - Embeddings for vector storage
    - Streaming support
    - Role-based conditioning
    """
    
    def __init__(self):
        self.model_name = settings.gemini_model  # gemini-2.5-flash
        self.embedding_model_name = "models/gemini-embedding-001"
        self.model = None
        self.embedding_model = None
        self._initialized = False
    
    def initialize(self):
        """Initialize Gemini client."""
        if self._initialized:
            return
        
        try:
            # Configure API key
            if not settings.google_api_key:
                logger.warning("GOOGLE_API_KEY not set - Gemini features disabled")
                return
            
            genai.configure(api_key=settings.google_api_key)
            
            # Initialize generation model
            self.model = genai.GenerativeModel(self.model_name)
            
            logger.success("✅ Gemini client initialized with model: {}", self.model_name)
            self._initialized = True
            
        except Exception as e:
            logger.error("Failed to initialize Gemini client: {}", str(e))
            raise
    
    @tracer.trace("gemini_generate")
    async def generate(
        self,
        prompt: str,
        system_instruction: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: int = 2000,
        **kwargs
    ) -> str:
        """
        Generate text using Gemini.
        
        Args:
            prompt: User prompt
            system_instruction: System instruction for model
            temperature: Temperature (0.0-1.0)
            max_tokens: Maximum tokens to generate
            **kwargs: Additional generation parameters
            
        Returns:
            Generated text
        """
        if not self._initialized:
            self.initialize()
        
        if not self.model:
            logger.error("Gemini model not initialized")
            return "Error: Gemini model not available"
        
        try:
            logger.debug("Generating with Gemini 2.0 Flash...")
            
            # Configure generation
            generation_config = genai.types.GenerationConfig(
                temperature=temperature,
                max_output_tokens=max_tokens,
                **kwargs
            )
            
            # Build full prompt with system instruction
            if system_instruction:
                full_prompt = f"{system_instruction}\n\n{prompt}"
            else:
                full_prompt = prompt
            
            # Generate
            response = self.model.generate_content(
                full_prompt,
                generation_config=generation_config
            )
            
            result = response.text
            
            logger.success("✅ Generated {} characters with Gemini", len(result))
            return result
            
        except Exception as e:
            logger.error("Gemini generation error: {}", str(e))
            return f"Error generating response: {str(e)}"
    
    @tracer.trace("gemini_chat")
    async def chat(
        self,
        messages: List[Dict[str, str]],
        system_instruction: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: int = 2000,
        **kwargs
    ) -> str:
        """
        Chat with Gemini using conversation history.
        
        Args:
            messages: List of message dicts with 'role' and 'content'
            system_instruction: System instruction
            temperature: Temperature
            max_tokens: Max tokens
            **kwargs: Additional parameters
            
        Returns:
            Generated response
        """
        if not self._initialized:
            self.initialize()
        
        if not self.model:
            logger.error("Gemini model not initialized")
            return "Error: Gemini model not available"
        
        try:
            # Start chat session
            chat = self.model.start_chat(history=[])
            
            # Configure generation
            generation_config = genai.types.GenerationConfig(
                temperature=temperature,
                max_output_tokens=max_tokens,
                **kwargs
            )
            
            # Build conversation
            conversation_text = ""
            
            if system_instruction:
                conversation_text += f"System: {system_instruction}\n\n"
            
            for msg in messages:
                role = msg.get('role', 'user')
                content = msg.get('content', '')
                
                if role == 'system':
                    conversation_text += f"System: {content}\n\n"
                elif role == 'user':
                    conversation_text += f"User: {content}\n\n"
                elif role == 'assistant':
                    conversation_text += f"Assistant: {content}\n\n"
            
            conversation_text += "Assistant:"
            
            # Generate response
            response = chat.send_message(
                conversation_text,
                generation_config=generation_config
            )
            
            result = response.text
            
            logger.success("✅ Chat response: {} characters", len(result))
            return result
            
        except Exception as e:
            logger.error("Gemini chat error: {}", str(e))
            return f"Error in chat: {str(e)}"
    
    @tracer.trace("gemini_embed")
    async def embed_text(self, text: str) -> List[float]:
        """
        Generate embeddings for text using Google's embedding model.
        
        Args:
            text: Text to embed
            
        Returns:
            Embedding vector (768 dimensions)
        """
        if not self._initialized:
            self.initialize()
        
        try:
            # Use Google's embedding API
            result = genai.embed_content(
                model=self.embedding_model_name,
                content=text,
                task_type="retrieval_document"
            )
            
            embedding = result['embedding']
            
            logger.debug("✅ Generated embedding: {} dimensions", len(embedding))
            return embedding
            
        except Exception as e:
            logger.error("Gemini embedding error: {}", str(e))
            # Return zero vector on error
            return [0.0] * 768
    
    @tracer.trace("gemini_embed_batch")
    async def embed_texts(self, texts: List[str]) -> List[List[float]]:
        """
        Generate embeddings for multiple texts.
        
        Args:
            texts: List of texts to embed
            
        Returns:
            List of embedding vectors
        """
        embeddings = []
        
        for text in texts:
            embedding = await self.embed_text(text)
            embeddings.append(embedding)
        
        logger.debug("✅ Generated {} embeddings", len(embeddings))
        return embeddings
    
    def is_available(self) -> bool:
        """Check if Gemini is available."""
        return self._initialized and self.model is not None


# Global Gemini client instance
gemini_client = GeminiClient()

