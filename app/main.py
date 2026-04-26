import os
import sys
import shutil
import streamlit as st
from dotenv import load_dotenv

# Ensure root directory is in path so we can import from tools
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from tools.goal_analyzer import analyze_goal
from tools.html_cleaner import clean_html
from tools.unique_data_finder import find_unique_data
from tools.common_pattern_finder import find_common_patterns
from tools.final_formatter import format_final_output

load_dotenv()

st.set_page_config(page_title="MCP AI Scraper", page_icon="🕵️", layout="wide")
st.title("MCP AI Web Scraper")
st.caption("A Model Context Protocol implementation for dynamic scraping and extraction.")

with st.sidebar:
    st.header("Scraping Settings")
    max_pages = st.number_input("Navigation limit", min_value=1, max_value=1000, value=50, step=1)

url = st.text_input("Target URL", placeholder="https://example.com")
extraction_goal = st.text_area(
    "Extraction requirement",
    placeholder="Find the CEO's name OR Find recent news articles...",
    height=120,
)

run_button = st.button("Scrape and Extract", type="primary")

if run_button:
    if not url or not extraction_goal:
        st.error("Please provide both a Target URL and an Extraction requirement.")
        st.stop()
        
    # Clear the resources folder before starting a new scrape
    res_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'resources'))
    if os.path.exists(res_path):
        shutil.rmtree(res_path, ignore_errors=True)
    os.makedirs(res_path, exist_ok=True)
        
    st.write("### Execution Progress")
    progress_bar = st.progress(0)
    status_text = st.empty()
    
    # Step 1: Goal Analyzer
    status_text.text("Step 1/5: Analyzing Goal...")
    analysis_result = analyze_goal(extraction_goal)
    category = analysis_result.get("category", "COMMON").upper()
    goal_summary = analysis_result.get("goal", extraction_goal)
    progress_bar.progress(20)
    
    st.info(f"**Analysis Result:** Categorized as `{category}`\n\n**Goal Summary:** {goal_summary}")
    
    # Step 2: HTML Cleaner (Optional for base URL to warm up resources)
    status_text.text(f"Step 2/5: Cleaning initial HTML for {url}...")
    clean_html(url)
    progress_bar.progress(40)
    
    # Step 3: Routing
    status_text.text("Step 3/5: Routing to appropriate MCP Tool...")
    progress_bar.progress(60)
    
    # Step 4: Extraction
    status_text.text(f"Step 4/5: Running {category} Finder Tool...")
    raw_data = ""
    if category == "UNIQUE":
        raw_data = find_unique_data(url, extraction_goal, max_pages=max_pages)
    else:
        raw_data = find_common_patterns(url, extraction_goal, max_pages=max_pages)
    progress_bar.progress(80)
    
    with st.expander("Raw Extracted Data"):
        st.text_area("Data", raw_data, height=200)
        
    # Step 5: Formatting
    status_text.text("Step 5/5: Formatting Final Output...")
    final_paragraph = format_final_output(raw_data, extraction_goal)
    progress_bar.progress(100)
    status_text.text("Complete!")
    
    st.subheader("Final Result")
    st.success(final_paragraph)