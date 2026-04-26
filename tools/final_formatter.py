import os
import time
from groq import Groq

def format_final_output(raw_data: str, goal: str) -> str:
    """
    Takes raw tool outputs and generates a structured natural language paragraph.
    Implements a chunking Map-Reduce strategy to avoid LLM token limits.
    """
    api_key = str(os.getenv("GROQ_API_KEY", "")).strip().strip('"').strip("'")
    if not api_key:
        return "GROQ_API_KEY is not set."
        
    client = Groq(api_key=api_key)
    
    # Heuristic: 1 token ~= 4 chars. Max TPM is 12000 for 70b, higher for 8b.
    # We want each request to be around 16,000 chars (~4000 tokens).
    CHUNK_SIZE = 16000
    
    if len(raw_data) <= CHUNK_SIZE:
        # Fits in one request easily
        return _ask_llm(client, raw_data, goal)
    
    print(f"[MCP TOOL: Formatter] Data too large ({len(raw_data)} chars). Using Map-Reduce chunking strategy...")
    
    # Split into chunks safely (try to split on newlines if possible)
    chunks = []
    current_chunk = []
    current_length = 0
    for line in raw_data.split('\n'):
        if current_length + len(line) > CHUNK_SIZE and current_chunk:
            chunks.append('\n'.join(current_chunk))
            current_chunk = []
            current_length = 0
        current_chunk.append(line)
        current_length += len(line) + 1
    if current_chunk:
        chunks.append('\n'.join(current_chunk))
        
    intermediate_results = []
    for idx, chunk in enumerate(chunks):
        print(f"[MCP TOOL: Formatter] Processing chunk {idx+1}/{len(chunks)}...")
        
        prompt = f"""
        Goal: "{goal}"
        
        Here is a chunk of raw data extracted from a website. 
        Extract and summarize ONLY the information that is absolutely necessary to answer the Goal.
        If the goal asks for top items (e.g., highest prices), retain all relevant candidates from this chunk so they can be compared later.
        Return the intermediate findings clearly.
        
        Raw Data Chunk:
        {chunk}
        """
        
        try:
            # Using 8b model for intermediate processing to save rate limits
            response = client.chat.completions.create(
                model="llama-3.1-8b-instant",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
            )
            intermediate_results.append(response.choices[0].message.content)
            
            # Small sleep to be nice to rate limits
            if idx < len(chunks) - 1:
                time.sleep(2) 
        except Exception as e:
            print(f"[MCP TOOL: Formatter] Error on chunk {idx+1}: {e}")
            if "rate_limit_exceeded" in str(e):
                print("[MCP TOOL: Formatter] Rate limit hit. Sleeping for 20s and retrying...")
                time.sleep(20)
                try:
                    response = client.chat.completions.create(
                        model="llama-3.1-8b-instant",
                        messages=[{"role": "user", "content": prompt}],
                        temperature=0.1,
                    )
                    intermediate_results.append(response.choices[0].message.content)
                except Exception as retry_e:
                    print(f"[MCP TOOL: Formatter] Retry failed: {retry_e}")
                    
    print("[MCP TOOL: Formatter] Combining intermediate results into final output...")
    combined_data = "\n\n--- INTERMEDIATE CHUNK RESULTS ---\n\n".join(intermediate_results)
    
    # If the combined data is STILL too large, we just truncate it for the final response to guarantee no crash
    # But usually intermediate summaries are very small.
    if len(combined_data) > CHUNK_SIZE * 2:
        combined_data = combined_data[:CHUNK_SIZE * 2] + "\n...[TRUNCATED]"
        
    return _ask_llm(client, combined_data, goal)

def _ask_llm(client, data: str, goal: str) -> str:
    prompt = f"""
    The user wanted to achieve the following goal: "{goal}"
    
    Here is the extracted data (it may be from multiple chunks):
    {data}
    
    Please write a detailed natural language summary of this information, 
    directly addressing the user's goal. You may use a professional structure 
    (e.g., a paragraph followed by a short list if it helps clarity, but keep it elegant).
    """
    
    response = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.2,
    )
    return str(response.choices[0].message.content).strip()
