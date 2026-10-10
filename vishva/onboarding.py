#!/usr/bin/env python3
"""
Onboarding — Deterministic questionnaire + modular persona assembly.

Flow:
1. Ask 10 fixed questions (linear, no agent involvement)
2. Save answers to data/onboarding_answers.json
3. Load 10 persona snippets from personas/snippets/
4. Agent selects best matching snippets based on answers
5. Agent creates RAG entries + essentials from answers
6. Combine selected snippets + installer tools into final persona file

Usage:  python3 vishva/onboarding.py [--force]
"""
import os
import sys
import json
import re
from pathlib import Path
#from typing import Dict, List, Optional
from typing import Dict, List

HERE = Path(__file__).parent.resolve()
ROOT = HERE.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from vishva.agent import AgentCore
from vishva.paths import p

MARKER = p("data", ".onboarding_done")
ANSWERS_FILE = p("data", "onboarding_answers.json")
SCAN_FILE = p("data", "installer_scan.json")
SNIPPETS_DIR = ROOT / "personas" / "snippets"

# ---------- 10 Fixed Questions ----------
QUESTIONS = [
    {"id": "name", "q": "What should I call you? (name or nickname)", "type": "text", "required": True},
    {"id": "language", "q": "Which language should I use? (de/en/fr/es/it)", "type": "choice",
     "choices": ["de", "en", "fr", "es", "it"], "default": "de"},
    {"id": "formality", "q": "How formal should I be? (casual/formal)", "type": "choice",
     "choices": ["casual", "formal"], "default": "casual"},
    {"id": "profession", "q": "What's your profession or main activity?", "type": "text", "required": True},
    {"id": "tech_level", "q": "How tech-savvy are you? (beginner/intermediate/expert)", "type": "choice",
     "choices": ["beginner", "intermediate", "expert"], "default": "intermediate"},
    {"id": "response_style", "q": "How should I respond? (short/detailed)", "type": "choice",
     "choices": ["short", "detailed"], "default": "detailed"},
    {"id": "emoji", "q": "Should I use emojis? (yes/no)", "type": "choice",
     "choices": ["yes", "no"], "default": "no"},
    {"id": "projects", "q": "What projects or hobbies are you working on?", "type": "text", "required": False},
    {"id": "schedule", "q": "What's your daily rhythm / when are you usually available?", "type": "text", "required": False},
    {"id": "rules", "q": "Any rules or taboos I should know about?", "type": "text", "required": False},
]


def ask_questions() -> Dict[str, str]:
    print("\n" + "=" * 60)
    print(" VISHVA ONBOARDING — 10 Questions")
    print("=" * 60 + "\n")
    answers = {}
    for i, q in enumerate(QUESTIONS, 1):
        qid = q["id"]
        print(f"[{i}/10] {q['q']}")
        if q["type"] == "choice":
            choices = q["choices"]
            default = q.get("default", choices[0])
            print(f"   Options: {', '.join(choices)}")
            ans = input(f"   Your choice [{default}]: ").strip().lower()
            if not ans:
                ans = default
            while ans not in choices:
                print(f"   Invalid. Choose from: {', '.join(choices)}")
                ans = input(f"   Your choice [{default}]: ").strip().lower()
                if not ans:
                    ans = default
            answers[qid] = ans
        else:
            required = q.get("required", False)
            ans = input(f"   Your answer: ").strip()
            while required and not ans:
                print("   This is required.")
                ans = input(f"   Your answer: ").strip()
            answers[qid] = ans
        print()
    return answers


def load_installer_scan() -> Dict:
    if not os.path.exists(SCAN_FILE):
        print(f"️  No installer data at {SCAN_FILE}")
        return {}
    try:
        with open(SCAN_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"️  Cannot read scan: {e}")
        return {}


def load_snippets() -> List[Dict]:
    snippets = []
    if not SNIPPETS_DIR.exists():
        print(f"️  Snippets dir not found: {SNIPPETS_DIR}")
        return snippets
    for md_file in sorted(SNIPPETS_DIR.glob("*.md")):
        try:
            content = md_file.read_text(encoding="utf-8")
            soul = re.search(r"---SOUL---\s*\n(.*?)(?=---ENGINE---|\Z)", content, re.DOTALL)
            engine = re.search(r"---ENGINE---\s*\n(.*?)(?=---ACTIVETOOLS---|\Z)", content, re.DOTALL)
            tools = re.search(r"---ACTIVETOOLS---\s*\n(.*?)\Z", content, re.DOTALL)
            snippets.append({
                "name": md_file.stem,
                "soul": soul.group(1).strip() if soul else "",
                "engine": engine.group(1).strip() if engine else "",
                "activetools": tools.group(1).strip() if tools else "",
            })
        except Exception as e:
            print(f"⚠️  Failed to load {md_file.name}: {e}")
    return snippets


def agent_select_snippets(answers: Dict, snippets: List[Dict]) -> List[str]:
    print("\n🤖 Agent is selecting the best persona snippets...")
    answers_text = "\n".join([f"- {k}: {v}" for k, v in answers.items()])
    snippets_text = "\n\n".join([
        f"### {s['name']}\nSOUL:\n{s['soul']}\n\nENGINE:\n{s['engine']}"
        for s in snippets
    ])
    prompt = f"""PERSONA SNIPPET SELECTION

Based on these user answers:
{answers_text}

Select the 3-4 most appropriate persona snippets from this list:
{snippets_text}

Respond ONLY with a JSON array of snippet names (e.g., ["developer", "casual", "short"]).
Do not include explanations, just the JSON array."""
    try:
        agent = AgentCore(session_id="onboarding", enable_tts=False)
        # Disable meta-helpers during onboarding
        agent.config["rag_meta_enrichment_enabled"] = False
        agent.config["rag_meta_extraction_enabled"] = False
        response = agent.chat(prompt).strip()
        json_match = re.search(r'\[.*?\]', response, re.DOTALL)
        if json_match:
            selected = json.loads(json_match.group(0))
            return [s for s in selected if s in {sn["name"] for sn in snippets}]
        else:
            print(f"️  Could not parse JSON: {response[:200]}")
            return []
    except Exception as e:
        print(f"⚠️  Agent selection failed: {e}")
        return []


def agent_create_rag_entries(answers: Dict, scan: Dict):
    """Agent creates RAG entries + essentials from structured answers."""
    print("\n🧠 Agent is creating RAG entries from answers...")
    try:
        agent = AgentCore(session_id="onboarding_rag", enable_tts=False)
        agent.config["rag_meta_enrichment_enabled"] = False
        agent.config["rag_meta_extraction_enabled"] = False
    except Exception as e:
        print(f"⚠️  Cannot start agent for RAG: {e}")
        return

    answers_text = "\n".join([f"- {k}: {v}" for k, v in answers.items()])
    sys_info = scan.get("system", {}) or {}
    os_info = sys_info.get("os", {}) or {}
    gpu = sys_info.get("gpu", ("?", "?"))

    prompt = f"""RAG ENTRY CREATION

User answers from onboarding:
{answers_text}

System info:
- OS: {os_info.get('PRETTY_NAME', '?')}
- GPU: {gpu[0]} {gpu[1]}
- User: {sys_info.get('user', '?')}
- Project root: {scan.get('config', {}).get('project_root', '?')}
- Agent working dir: {scan.get('config', {}).get('agent_workdir', '?')}

TASK:
1. Create 2-3 rag_save entries (category: user_info, preference, project) from the answers.
   Use tool: rag_save with text, category, priority (0-3), source="onboarding".
2. Create ESSENTIAL entries (max 3 total, including user/system/gpu):
   - Essential 1: "user: <NAME>; system: <OS>; gpu: <GPU>"
   - Essential 2: "agent_home: <working_dir>" (use config.agent_workdir or "working_dir" use the full path)
   Use tool: rag_essential with text.

Execute the tools now. Respond with a summary of what you created."""

    try:
        response = agent.chat(prompt)
        print(f"   Agent response: {response[:300]}")
    except Exception as e:
        print(f"⚠️  RAG creation failed: {e}")

MERGE_PROMPT = """PERSONA MERGE — Create a single, unified persona file.

You will receive N persona snippets. Your task is to MERGE them into ONE
coherent persona file. DO NOT concatenate — merge the traits.

RULES:
1. SOUL section:
   - Exactly ONE line per field: Persona, Style, Context, Code, Feedback
   - Combine adjectives/traits from all snippets (comma-separated)
   - No contradictions — if snippets conflict, pick the dominant one
   - Example: "Persona: Friendly, relaxed, analytical, assumes knowledge"

2. ENGINE section:
   - Each directive group (e.g. [COGNITIVE_PROCESS]) appears EXACTLY ONCE
   - Merge all bullet points from all snippets into one list
   - Remove duplicates, keep the most specific wording
   - Add [USER_SPECIFIC] section with 2-3 lines derived from user answers

3. ACTIVETOOLS section:
   - Union of all tools from snippets + installer tools
   - One tool per line, sorted alphabetically
   - No duplicates

4. Format:
---SOUL---
Persona: <merged>
Style: <merged>
Context: <merged>
Code: <merged>
Feedback: <merged>

---ENGINE---
[COGNITIVE_PROCESS]
<merged directives>

[USER_SPECIFIC]
<derived from user answers>

---ACTIVETOOLS---
<union of tools>

OUTPUT: Only the merged file content. No explanations, no markdown fences."""


def agent_merge_snippets(selected_names: List[str], snippets: List[Dict],
                         answers: Dict, installer_tools: List[str]) -> str:
    """LLM merges selected snippets into one coherent persona file."""
    print("\n Agent is merging snippets into unified persona...")

    selected = [s for s in snippets if s["name"] in selected_names]
    if not selected:
        selected = snippets[:3]

    # Build snippets text
    snippets_text = "\n\n".join([
        f"### Snippet: {s['name']}\n{s['soul']}\n\n{s['engine']}"
        for s in selected
    ])

    # Installer tools
    tools_text = "\n".join(sorted(installer_tools)) if installer_tools else "(none)"

    # User answers
    answers_text = "\n".join([f"- {k}: {v}" for k, v in answers.items()])

    prompt = f"""{MERGE_PROMPT}

SNIPPETS TO MERGE:
{snippets_text}

INSTALLER TOOLS (include these in ACTIVETOOLS):
{tools_text}

USER ANSWERS (use for [USER_SPECIFIC] section):
{answers_text}

Now create the merged persona file."""

    try:
        agent = AgentCore(session_id="onboarding_v2_merge", enable_tts=False)
        agent.config["rag_meta_enrichment_enabled"] = False
        agent.config["rag_meta_extraction_enabled"] = False
        response = agent.chat(prompt).strip()

        # Clean up: remove markdown fences if present
        if response.startswith("```"):
            lines = response.split("\n")
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            response = "\n".join(lines)

        # Validate structure
        if "---SOUL---" not in response or "---ENGINE---" not in response:
            print("⚠️  Merge output missing required sections, using fallback")
            return _fallback_merge(selected, installer_tools, answers)

        return response + "\n"
    except Exception as e:
        print(f"⚠️  Merge failed: {e}, using fallback")
        return _fallback_merge(selected, installer_tools, answers)


def _fallback_merge(selected: List[Dict], installer_tools: List[str],
                    answers: Dict) -> str:
    """Fallback: simple merge if LLM fails."""
    # SOUL: merge traits line by line
    fields = ["Persona", "Style", "Context", "Code", "Feedback"]
    soul_lines = []
    for field in fields:
        values = []
        for s in selected:
            if s["soul"]:
                for line in s["soul"].split("\n"):
                    if line.startswith(f"{field}:"):
                        val = line.split(":", 1)[1].strip()
                        if val:
                            values.append(val)
        if values:
            # Deduplicate while preserving order
            seen = set()
            unique = []
            for v in values:
                for item in v.split(","):
                    item = item.strip()
                    if item and item not in seen:
                        seen.add(item)
                        unique.append(item)
            soul_lines.append(f"{field}: {', '.join(unique)}")

    # ENGINE: merge all directives, dedup by section header
    engine_sections = {}
    for s in selected:
        if s["engine"]:
            current_section = None
            for line in s["engine"].split("\n"):
                if line.startswith("[") and line.endswith("]"):
                    current_section = line
                    if current_section not in engine_sections:
                        engine_sections[current_section] = []
                elif current_section and line.strip():
                    if line.strip() not in engine_sections[current_section]:
                        engine_sections[current_section].append(line.strip())

    engine_lines = []
    for section, bullets in engine_sections.items():
        engine_lines.append(section)
        engine_lines.extend(bullets)
        engine_lines.append("")

    # Add USER_SPECIFIC
    engine_lines.append("[USER_SPECIFIC]")
    profession = answers.get("profession", "")
    tech_level = answers.get("tech_level", "")
    if profession:
        engine_lines.append(f"- User works as: {profession}")
    if tech_level:
        engine_lines.append(f"- Tech level: {tech_level}")
    engine_lines.append("")

    # ACTIVETOOLS: union
    all_tools = set(installer_tools)
    for s in selected:
        if s["activetools"]:
            tools = [t.strip() for t in s["activetools"].split("\n") if t.strip()]
            all_tools.update(tools)

    final = "---SOUL---\n"
    final += "\n".join(soul_lines)
    final += "\n\n---ENGINE---\n"
    final += "\n".join(engine_lines)
    final += "\n---ACTIVETOOLS---\n"
    final += "\n".join(sorted(all_tools))
    final += "\n"
    return final


def save_persona(name: str, content: str):
    target = ROOT / "personas" / f"{name}.md"
    target.write_text(content, encoding="utf-8")
    print(f"✅ Persona saved: {target}")


def main():
    force = "--force" in sys.argv
    if os.path.exists(MARKER) and not force:
        print("✅ Onboarding already completed. (--force to repeat)")
        return

    # Step 1: Ask questions
    answers = ask_questions()

    # Step 2: Save answers
    answers_path = Path(ANSWERS_FILE)
    answers_path.parent.mkdir(parents=True, exist_ok=True)
    with open(answers_path, "w", encoding="utf-8") as f:
        json.dump(answers, f, indent=2, ensure_ascii=False)
    print(f"💾 Answers saved to {answers_path}")

    # Step 3: Load installer data + snippets
    scan = load_installer_scan()
    snippets = load_snippets()
    if not snippets:
        print("❌ No snippets found. Exiting.")
        return 1
    print(f"📚 Loaded {len(snippets)} snippets")

    installer_tools = scan.get("config", {}).get("enabled_tools", [])

    # Step 4: Agent selects snippets
    selected_names = agent_select_snippets(answers, snippets)
    if not selected_names:
        print("️  No snippets selected. Using fallback.")
        selected_names = [s["name"] for s in snippets[:3]]
    print(f"🎯 Selected snippets: {selected_names}")

    # Step 5: Agent creates RAG entries + essentials
    agent_create_rag_entries(answers, scan)

    # Step 6: Agent merges snippets into unified persona
    final_persona = agent_merge_snippets(selected_names, snippets, answers, installer_tools)
    persona_name = answers.get("name", "user").lower().replace(" ", "_")
    save_persona(persona_name, final_persona)

    # Step 7: Set marker
    with open(MARKER, "w", encoding="utf-8") as f:
        f.write(json.dumps(answers, indent=2))

    print("\n" + "=" * 60)
    print(f"✅ ONBOARDING COMPLETE")
    print(f"   Persona: {persona_name}")
    print(f"   Activate with: /personality {persona_name}")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
