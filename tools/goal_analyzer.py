import os
import json

def analyze_goal(query: str) -> dict:
    """
    Takes user query; returns 'Goal Fields' and categorizes as 'COMMON' 
    (repeating patterns) or 'UNIQUE' (specific facts).
    """
    print(f"[MCP TOOL: Analyzer] Analyzing query: {query}")
    
    prompt = f"""
    Analyze the following user query for web scraping.
    Determine if the goal requires finding 'COMMON' data (repeating patterns like news articles, products in a list) 
    or 'UNIQUE' data (specific facts like a CEO's name, a single contact email).
    
    Return a JSON with the following structure:
    {{
        "category": "COMMON" or "UNIQUE",
        "goal": "A concise summary of the goal",
        "fields": ["list", "of", "fields", "to", "extract"]
    }}
    
    User Query: {query}
    """
    
    # We will need a way to call the LLM to get this. For now, doing a basic implementation.
    # In the actual implementation, we should use the existing Groq client.
    api_key = str(os.getenv("GROQ_API_KEY", "")).strip().strip('"').strip("'")
    from groq import Groq
    groq_client = Groq(api_key=api_key)
    
    response = groq_client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.1,
        response_format={"type": "json_object"}
    )
    
    content = response.choices[0].message.content
    result = json.loads(content)
    
    print(f"[MCP TOOL: Analyzer] Category: {result.get('category')} | Goal: {result.get('goal')}")
    return result
