"""
Ollama LLM Client - Async Wrapper

Provides async interface to Ollama LLM with retry logic and connection verification.
"""
import asyncio
import logging
import time
from typing import Any, Optional

import ollama

from app.core.config import get_settings

logger = logging.getLogger(__name__)


class OllamaClient:
    """
    Singleton Ollama LLM client with async support.
    
    Features:
    - Async wrapper for ollama.Client
    - Connection verification with retries
    - Exponential backoff for failures
    - Thread pool execution for sync operations
    """
    
    _instance: Optional["OllamaClient"] = None
    
    def __new__(cls):
        """Singleton pattern."""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        """Initialize Ollama client (only once)."""
        if self._initialized:
            return
            
        settings = get_settings()
        self.base_url = settings.OLLAMA_BASE_URL
        self.model = settings.OLLAMA_MODEL
        self.timeout = settings.OLLAMA_TIMEOUT
        
        # Create sync client
        self.client = ollama.Client(host=self.base_url, timeout=self.timeout)
        
        self._initialized = True
        logger.info(f"Initialized OllamaClient: {self.base_url}, model={self.model}")
    
    async def verify_model_availability(
        self,
        max_retries: int = 6,
        retry_delay: float = 10.0,
    ) -> bool:
        """
        Verify Ollama model is available with retries.
        
        Args:
            max_retries: Maximum retry attempts (default: 6)
            retry_delay: Base delay between retries in seconds (default: 10s)
            
        Returns:
            True if model available, False otherwise
            
        Note:
            Total wait time: ~60s with exponential backoff
            Delays: 10s, 10s, 10s, 10s, 10s, 10s = 60s
        """
        for attempt in range(max_retries):
            try:
                # Run in executor (ollama.Client is sync)
                loop = asyncio.get_event_loop()
                models = await loop.run_in_executor(
                    None,
                    lambda: self.client.list()
                )
                
                # Check if our model exists
                model_names = [m["name"] for m in models.get("models", [])]
                if self.model in model_names:
                    logger.info(f"Ollama model '{self.model}' is available")
                    return True
                else:
                    logger.warning(
                        f"Model '{self.model}' not found. Available: {model_names}"
                    )
                    
            except Exception as e:
                logger.warning(
                    f"Ollama connection attempt {attempt + 1}/{max_retries} failed: {e}"
                )
            
            # Wait before retry (except on last attempt)
            if attempt < max_retries - 1:
                await asyncio.sleep(retry_delay)
        
        logger.error(f"Failed to verify Ollama model after {max_retries} attempts")
        return False
    
    async def generate(
        self,
        prompt: str,
        system: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
    ) -> dict[str, Any]:
        """
        Generate completion from Ollama with async support.
        
        Args:
            prompt: User prompt
            system: System prompt (optional)
            temperature: Sampling temperature (0.0-1.0)
            max_tokens: Maximum tokens to generate
            
        Returns:
            Response dictionary with 'content' and metadata
            
        Raises:
            Exception: If generation fails after retries
        """
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        
        options = {"temperature": temperature}
        if max_tokens:
            options["num_predict"] = max_tokens
        
        try:
            # Run in executor with exponential backoff
            response = await self._generate_with_retry(messages, options)
            return response
            
        except Exception as e:
            logger.error(f"Ollama generation failed: {e}", exc_info=True)
            raise
    
    async def _generate_with_retry(
        self,
        messages: list[dict],
        options: dict,
        max_retries: int = 3,
    ) -> dict[str, Any]:
        """
        Generate with exponential backoff retry.
        
        Args:
            messages: Chat messages
            options: Generation options
            max_retries: Maximum retry attempts
            
        Returns:
            Response dictionary
        """
        last_error = None
        
        for attempt in range(max_retries):
            try:
                loop = asyncio.get_event_loop()
                response = await loop.run_in_executor(
                    None,
                    lambda: self.client.chat(
                        model=self.model,
                        messages=messages,
                        options=options,
                    )
                )
                
                # Extract content
                content = response.get("message", {}).get("content", "")
                
                return {
                    "content": content,
                    "model": self.model,
                    "done": response.get("done", True),
                    "total_duration": response.get("total_duration"),
                    "prompt_eval_count": response.get("prompt_eval_count"),
                    "eval_count": response.get("eval_count"),
                }
                
            except Exception as e:
                last_error = e
                logger.warning(
                    f"Ollama generation attempt {attempt + 1}/{max_retries} failed: {e}"
                )
                
                # Exponential backoff: 1s, 2s, 4s
                if attempt < max_retries - 1:
                    delay = 2 ** attempt
                    await asyncio.sleep(delay)
        
        raise Exception(f"Ollama generation failed after {max_retries} attempts: {last_error}")
    
    async def generate_json(
        self,
        prompt: str,
        system: Optional[str] = None,
        temperature: float = 0.3,
    ) -> dict[str, Any]:
        """
        Generate JSON response from Ollama.
        
        Args:
            prompt: User prompt (should request JSON output)
            system: System prompt
            temperature: Lower temperature for structured output
            
        Returns:
            Parsed JSON dictionary
            
        Raises:
            ValueError: If response is not valid JSON
        """
        import json
        
        # Add JSON instruction to system prompt
        json_system = (system or "") + "\n\nRespond with valid JSON only."
        
        response = await self.generate(
            prompt=prompt,
            system=json_system,
            temperature=temperature,
        )
        
        content = response["content"].strip()
        
        # Try to extract JSON from markdown code blocks
        if "```json" in content:
            content = content.split("```json")[1].split("```")[0].strip()
        elif "```" in content:
            content = content.split("```")[1].split("```")[0].strip()
        
        try:
            return json.loads(content)
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse JSON from Ollama: {content[:200]}")
            raise ValueError(f"Invalid JSON response: {e}")
    
    async def health_check(self) -> bool:
        """
        Quick health check for Ollama service.
        
        Returns:
            True if service is healthy, False otherwise
        """
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None,
                lambda: self.client.list()
            )
            return True
        except Exception as e:
            logger.warning(f"Ollama health check failed: {e}")
            return False


# Singleton instance
_ollama_client: Optional[OllamaClient] = None


def get_ollama_client() -> OllamaClient:
    """Get singleton Ollama client instance."""
    global _ollama_client
    if _ollama_client is None:
        _ollama_client = OllamaClient()
    return _ollama_client
