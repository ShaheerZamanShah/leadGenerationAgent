"""
config/settings.py
------------------
Centralised, typed configuration loaded from .env.
All agents import from here - no scattered os.getenv() calls.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path
from dotenv import load_dotenv

# Load .env from project root
load_dotenv(Path(__file__).parent.parent / ".env")


@dataclass
class Settings:
    # LLM
    groq_api_key: str = field(default_factory=lambda: os.getenv("GROQ_API_KEY", ""))
    fast_model: str = field(default_factory=lambda: os.getenv("FAST_MODEL", "llama-3.1-8b-instant"))
    smart_model: str = field(default_factory=lambda: os.getenv("SMART_MODEL", "llama-3.3-70b-versatile"))

    # Search
    tavily_api_key: str = field(default_factory=lambda: os.getenv("TAVILY_API_KEY", ""))

    # Scraping
    apify_api_key: str = field(default_factory=lambda: os.getenv("APIFY_API_KEY", ""))
    apollo_api_key: str = field(default_factory=lambda: os.getenv("APOLLO_API_KEY", ""))

    # Email
    gmail_user: str = field(default_factory=lambda: os.getenv("GMAIL_USER", ""))
    gmail_app_password: str = field(default_factory=lambda: os.getenv("GMAIL_APP_PASSWORD", ""))

    # Pipeline config
    lead_score_threshold: int = field(default_factory=lambda: int(os.getenv("LEAD_SCORE_THRESHOLD", "60")))
    max_leads_per_run: int = field(default_factory=lambda: int(os.getenv("MAX_LEADS_PER_RUN", "20")))
    human_in_loop: bool = field(default_factory=lambda: os.getenv("HUMAN_IN_LOOP", "false").lower() == "true")
    output_dir: Path = field(default_factory=lambda: Path(os.getenv("OUTPUT_DIR", "data/output")))

    # Shaheer's profile
    developer_name: str = "Muhammad Shaheer Zaman Shah"
    developer_first_name: str = "Shaheer"
    developer_title: str = "AI/ML & Agentic AI Developer"
    developer_email: str = "shaheerzaman023@gmail.com"
    developer_linkedin: str = "https://linkedin.com/in/shaheer-zaman"
    developer_portfolio: str = "https://shaheer-portfolio.dev"

    # Key projects — enriched from CV
    projects: list = field(default_factory=lambda: [
        {
            "name": "NexusIQ",
            "description": "Enterprise RAG knowledge agent unifying search across Confluence, Notion, Slack, Jira, Google Drive, PDFs — sub-2-second latency",
            "best_for": ["knowledge management", "enterprise", "internal tools", "document search", "productivity"],
            "proof": "Sub-2-second end-to-end latency, multi-source retrieval",
        },
        {
            "name": "NexusSDR (AI Sales Agent)",
            "description": "Autonomous B2B sales development agent with lead discovery, qualification scoring, deep research, and personalised outreach — built with LangGraph 6-agent pipeline",
            "best_for": ["sales", "b2b", "lead generation", "outreach automation", "marketing", "growth"],
            "proof": "Fully autonomous 6-agent LangGraph pipeline",
        },
        {
            "name": "D-VOICE (Sign Language AI)",
            "description": "Real-time sign language recognition system with 98.25% accuracy using computer vision and deep learning",
            "best_for": ["accessibility", "healthcare", "edtech", "computer vision", "disability tech"],
            "proof": "98.25% accuracy, real-time inference at 30fps",
        },
        {
            "name": "Agentic Video Generator",
            "description": "Fully autonomous multi-agent pipeline converting text prompts to cinematic visual-novel style videos — zero manual intervention",
            "best_for": ["content creation", "media", "marketing", "entertainment", "social media", "advertising"],
            "proof": "End-to-end autonomous generation, zero manual steps",
        },
        {
            "name": "RAG Customer Support Assistant",
            "description": "Production-grade RAG pipeline for 24/7 AI customer support with hybrid retrieval, reranking, and source citations",
            "best_for": ["customer support", "saas", "e-commerce", "customer service", "retail", "helpdesk"],
            "proof": "Hybrid retrieval + reranking, source-cited answers",
        },
        {
            "name": "Autonomous Research & Coding Agent",
            "description": "Nine-agent LangGraph pipeline that converts natural-language tasks to verified, tested, runnable Python code — 100% automated test pass rate",
            "best_for": ["research", "automation", "devtools", "productivity", "engineering", "data science"],
            "proof": "100% automated test pass rate, 9-agent orchestration",
        },
    ])

    # Target lead criteria
    target_industries: list = field(default_factory=lambda: [
        "SaaS", "E-commerce", "Real Estate", "Healthcare", "Legal Tech",
        "Finance", "Logistics", "HR Tech", "Marketing Agency", "EdTech",
        "PropTech", "InsurTech", "Retail", "Consulting", "Media",
        "Customer Service", "FinTech", "AgriTech", "TravelTech", "FoodTech",
    ])

    target_roles: list = field(default_factory=lambda: [
        "Founder", "CEO", "CTO", "Co-Founder", "Head of Operations",
        "VP of Sales", "Director of Engineering", "Head of Product",
        "Operations Manager", "Chief Revenue Officer", "Head of Customer Success",
        "Director of Marketing", "VP of Marketing", "Chief Operating Officer",
        "Head of Customer Service", "Director of Customer Experience",
    ])

    def validate(self) -> list[str]:
        """Return list of missing required keys."""
        missing = []
        if not self.groq_api_key:
            missing.append("GROQ_API_KEY")
        if not self.tavily_api_key:
            missing.append("TAVILY_API_KEY")
        return missing


# Singleton
settings = Settings()
