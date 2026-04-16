"""
Example: Ollama LLM Client Usage

Demonstrates how to use OllamaClient for various tasks.
"""
import asyncio
from app.ai.intelligence.llm_client import get_ollama_client


async def example_basic_generation():
    """Example: Basic text generation."""
    client = get_ollama_client()
    
    # Verify model is available
    available = await client.verify_model_availability()
    if not available:
        print("❌ Ollama model not available")
        return
    
    # Generate text
    response = await client.generate(
        prompt="Explain what a trading signal is in 2 sentences.",
        system="You are a financial expert.",
        temperature=0.7,
    )
    
    print(f"Response: {response['content']}")
    print(f"Model: {response['model']}")
    print(f"Tokens: {response.get('eval_count', 0)}")


async def example_json_generation():
    """Example: Generate structured JSON output."""
    client = get_ollama_client()
    
    prompt = """
    Classify this news headline into a category and sentiment:
    "Apple announces record quarterly earnings, stock surges 5%"
    
    Return JSON with keys: category, sentiment, confidence
    """
    
    try:
        result = await client.generate_json(
            prompt=prompt,
            system="You are a financial news classifier.",
            temperature=0.3,  # Lower temp for structured output
        )
        
        print(f"Category: {result.get('category')}")
        print(f"Sentiment: {result.get('sentiment')}")
        print(f"Confidence: {result.get('confidence')}")
        
    except ValueError as e:
        print(f"Failed to parse JSON: {e}")


async def example_event_classification():
    """Example: Classify trading event with Ollama."""
    client = get_ollama_client()
    
    event_text = """
    Federal Reserve announces 0.25% interest rate hike.
    Markets react negatively with tech stocks down 2%.
    """
    
    prompt = f"""
    Classify this trading event:
    {event_text}
    
    Return JSON with:
    - event_type: one of [earnings, fed_announcement, geopolitical, market_data]
    - impact: one of [high, medium, low]
    - sentiment: one of [bullish, bearish, neutral]
    - confidence: float between 0 and 1
    - reasoning: brief explanation
    """
    
    result = await client.generate_json(
        prompt=prompt,
        system="You are an expert at classifying financial events.",
    )
    
    print(f"Event Type: {result['event_type']}")
    print(f"Impact: {result['impact']}")
    print(f"Sentiment: {result['sentiment']}")
    print(f"Confidence: {result['confidence']}")
    print(f"Reasoning: {result['reasoning']}")


async def example_fake_news_detection():
    """Example: Detect fake news with Ollama."""
    client = get_ollama_client()
    
    headline = "Elon Musk announces Tesla will accept Dogecoin for all purchases"
    
    prompt = f"""
    Analyze this headline for credibility:
    "{headline}"
    
    Return JSON with:
    - is_credible: boolean
    - credibility_score: float 0-1
    - red_flags: list of concerns
    - reasoning: explanation
    """
    
    result = await client.generate_json(
        prompt=prompt,
        system="You are a fact-checker analyzing financial news.",
    )
    
    print(f"Credible: {result['is_credible']}")
    print(f"Score: {result['credibility_score']}")
    print(f"Red Flags: {result['red_flags']}")
    print(f"Reasoning: {result['reasoning']}")


async def example_health_check():
    """Example: Check Ollama service health."""
    client = get_ollama_client()
    
    healthy = await client.health_check()
    
    if healthy:
        print("✅ Ollama service is healthy")
    else:
        print("❌ Ollama service is down")


async def example_with_retry():
    """Example: Generation with automatic retry on failure."""
    client = get_ollama_client()
    
    try:
        response = await client.generate(
            prompt="What is the current market sentiment?",
            temperature=0.5,
        )
        print(f"Response: {response['content']}")
        
    except Exception as e:
        print(f"Generation failed after retries: {e}")


async def example_startup_verification():
    """Example: Verify Ollama on application startup."""
    client = get_ollama_client()
    
    print("Verifying Ollama model availability...")
    print("This will retry 6 times over ~60 seconds...")
    
    available = await client.verify_model_availability(
        max_retries=6,
        retry_delay=10.0,
    )
    
    if available:
        print("✅ Ollama is ready for use")
    else:
        print("❌ Ollama not available - LLM features will be disabled")


if __name__ == "__main__":
    print("=== Example 1: Basic Generation ===")
    asyncio.run(example_basic_generation())
    
    print("\n=== Example 2: JSON Generation ===")
    asyncio.run(example_json_generation())
    
    print("\n=== Example 3: Event Classification ===")
    asyncio.run(example_event_classification())
    
    print("\n=== Example 4: Fake News Detection ===")
    asyncio.run(example_fake_news_detection())
    
    print("\n=== Example 5: Health Check ===")
    asyncio.run(example_health_check())
    
    print("\n=== Example 6: Startup Verification ===")
    asyncio.run(example_startup_verification())
