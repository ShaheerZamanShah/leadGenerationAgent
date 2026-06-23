"""
utils/cv_parser.py
------------------
Parses Shaheer's CV PDF to extract skills, projects, and experience
for use in personalised outreach messages.
"""

from __future__ import annotations
import json
import re
from pathlib import Path
from functools import lru_cache

try:
    import pdfplumber
    PDFPLUMBER_AVAILABLE = True
except ImportError:
    PDFPLUMBER_AVAILABLE = False


CV_PATH = Path(__file__).parent.parent / "Shaheer_Zaman_CV_Full.pdf"


@lru_cache(maxsize=1)
def get_cv_text() -> str:
    """Extract full text from the CV PDF."""
    if not PDFPLUMBER_AVAILABLE:
        return _fallback_cv_text()
    if not CV_PATH.exists():
        return _fallback_cv_text()
    try:
        with pdfplumber.open(str(CV_PATH)) as pdf:
            pages = []
            for page in pdf.pages:
                text = page.extract_text()
                if text:
                    pages.append(text)
            return "\n\n".join(pages)
    except Exception:
        return _fallback_cv_text()


@lru_cache(maxsize=1)
def get_cv_summary() -> dict:
    """Return a structured summary of the CV."""
    text = get_cv_text()
    return {
        "full_text": text[:4000],  # Truncate for LLM context
        "skills": _extract_skills(text),
        "projects": _extract_projects(text),
        "experience": _extract_experience(text),
        "education": _extract_education(text),
    }


def _extract_skills(text: str) -> list[str]:
    """Extract technical skills from CV text."""
    skills = []
    skill_patterns = [
        r"Python", r"LangChain", r"LangGraph", r"FastAPI", r"Next\.?js",
        r"React", r"PyTorch", r"TensorFlow", r"OpenCV", r"RAG",
        r"Vector\s*DB", r"PostgreSQL", r"Docker", r"AWS", r"Azure",
        r"GPT", r"Llama", r"Groq", r"Pinecone", r"Chroma",
        r"Multi-?Agent", r"Agentic\s*AI", r"Computer\s*Vision",
        r"NLP", r"Machine\s*Learning", r"Deep\s*Learning",
    ]
    for pattern in skill_patterns:
        if re.search(pattern, text, re.IGNORECASE):
            # Normalize
            clean = re.sub(r"\s+", " ", pattern.replace(r"\.", ".").replace(r"\s*", " ").replace(r"\-?", "-"))
            skills.append(clean.strip())
    return skills[:20]


def _extract_projects(text: str) -> list[dict]:
    """Extract key projects from CV."""
    # These are Shaheer's known projects, enriched from CV text
    projects = [
        {
            "name": "NexusIQ",
            "description": "Enterprise multi-source RAG knowledge agent unifying search across Confluence, Notion, Slack, Jira, Google Drive — sub-2s latency",
            "best_for": ["knowledge management", "enterprise", "internal tools", "document search", "productivity"],
            "proof": "Sub-2-second end-to-end latency, multi-source retrieval",
        },
        {
            "name": "AI SDR (NexusSDR)",
            "description": "Autonomous B2B sales development agent with lead discovery, scoring, deep research, and personalised outreach — built with LangGraph",
            "best_for": ["sales", "b2b", "lead generation", "outreach automation", "marketing"],
            "proof": "6-agent LangGraph pipeline, fully automated",
        },
        {
            "name": "D-VOICE (Sign Language Recognition)",
            "description": "Real-time sign language recognition system achieving 98.25% accuracy using computer vision and deep learning",
            "best_for": ["accessibility", "healthcare", "edtech", "computer vision", "disability tech"],
            "proof": "98.25% accuracy, real-time inference",
        },
        {
            "name": "Agentic Video Generator",
            "description": "Fully autonomous multi-agent pipeline converting text prompts to cinematic visual-novel style videos",
            "best_for": ["content creation", "media", "marketing", "entertainment", "social media"],
            "proof": "End-to-end autonomous generation, zero manual intervention",
        },
        {
            "name": "RAG Customer Support Assistant",
            "description": "Modular production RAG pipeline for 24/7 customer support with hybrid retrieval, reranking, and cited sources",
            "best_for": ["customer support", "saas", "e-commerce", "customer service", "retail"],
            "proof": "Hybrid retrieval + reranking, source citations",
        },
        {
            "name": "Autonomous Research & Coding Agent",
            "description": "Nine-agent pipeline converting natural-language tasks to verified, tested, runnable Python code — 100% test pass rate",
            "best_for": ["research", "automation", "devtools", "productivity", "engineering"],
            "proof": "100% automated test pass rate, 9-agent orchestration",
        },
    ]
    return projects


def _extract_experience(text: str) -> str:
    """Extract experience summary."""
    # Try to find years of experience
    years_match = re.search(r"(\d+)\+?\s*years?\s*(?:of\s*)?experience", text, re.IGNORECASE)
    if years_match:
        return f"{years_match.group(1)}+ years of AI/ML development experience"
    return "2+ years of AI/ML & Agentic AI development experience"


def _extract_education(text: str) -> str:
    """Extract education info."""
    for degree in ["Bachelor", "Master", "BS", "MS", "BE", "Computer Science", "Software Engineering"]:
        if degree.lower() in text.lower():
            return f"Degree in Computer Science / Software Engineering"
    return "Computer Science background"


def _fallback_cv_text() -> str:
    """Fallback CV content when PDF can't be read."""
    return """
Muhammad Shaheer Zaman Shah
AI/ML & Agentic AI Developer | Pakistan
Email: shaheerzaman023@gmail.com
LinkedIn: https://linkedin.com/in/shaheer-zaman
Portfolio: https://shaheer-portfolio.dev

SKILLS
Python, LangChain, LangGraph, FastAPI, Next.js, React, PyTorch, TensorFlow,
OpenCV, RAG, Vector Databases (Pinecone, ChromaDB), PostgreSQL, Docker,
Multi-Agent AI Systems, Computer Vision, NLP, Machine Learning, Deep Learning,
Groq API, OpenAI API, Tavily, Apify

PROJECTS
- NexusIQ: Enterprise RAG knowledge agent, sub-2s latency
- NexusSDR: Autonomous B2B sales agent with LangGraph
- D-VOICE: Sign language recognition, 98.25% accuracy  
- Agentic Video Generator: Text-to-video autonomous pipeline
- RAG Customer Support: Production customer support AI
- Autonomous Research & Coding: 9-agent code generation pipeline

EXPERIENCE
AI/ML Developer with 2+ years building production agentic systems,
RAG pipelines, and computer vision applications.

EDUCATION
Bachelor's in Computer Science / Software Engineering
    """
