from tools.goal_analyzer import analyze_goal
from tools.html_cleaner import clean_html
from tools.unique_data_finder import find_unique_data
from tools.common_pattern_finder import find_common_patterns
from tools.final_formatter import format_final_output

class MCPServer:
    """
    A Model Context Protocol (MCP) Server that exposes specialized tools for AI agents.
    In a real remote MCP implementation, these would be exposed over an API (like JSON-RPC).
    For this local orchestration, we expose them as callable methods.
    """
    
    def __init__(self):
        self.tools = {
            "goal_analyzer": analyze_goal,
            "html_cleaner": clean_html,
            "unique_data_finder": find_unique_data,
            "common_pattern_finder": find_common_patterns,
            "final_formatter": format_final_output
        }
        
    def list_tools(self):
        return list(self.tools.keys())
        
    def call_tool(self, tool_name: str, **kwargs):
        if tool_name not in self.tools:
            raise ValueError(f"Tool {tool_name} not found.")
        print(f"[MCP SERVER] Executing tool: {tool_name}")
        return self.tools[tool_name](**kwargs)
