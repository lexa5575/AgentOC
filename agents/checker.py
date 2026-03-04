"""
Reply Checker
-------------

Two-layer validation of AI-generated replies before sending:

1. Rule-Based Checker (Python, 0 tokens):
   - No fabricated tracking numbers
   - No unauthorized discounts
   - No AI self-reference
   - Ends with "Thank you!"
   - No competitor mentions
   - No "check the website" phrases

2. LLM Checker (4th LLM call):
   - Fact consistency with ConversationState
   - Promise compliance
   - Tone matching
   - Answers the actual question
   - Hallucination detection

Returns CheckResult with is_ok, warnings, suggestions.
"""

import logging
import re
from dataclasses import dataclass, field
from typing import Optional

from agno.agent import Agent
from agno.models.openai import OpenAIResponses

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Check Result
# ---------------------------------------------------------------------------
@dataclass
class CheckResult:
    """Result of reply validation."""
    
    is_ok: bool = True
    warnings: list[str] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)
    rule_violations: list[str] = field(default_factory=list)
    llm_issues: list[str] = field(default_factory=list)
    
    def add_warning(self, msg: str, source: str = "rule") -> None:
        """Add a warning and mark as not OK."""
        self.is_ok = False
        self.warnings.append(msg)
        if source == "rule":
            self.rule_violations.append(msg)
        else:
            self.llm_issues.append(msg)
    
    def add_suggestion(self, msg: str) -> None:
        """Add a suggestion (doesn't affect is_ok)."""
        self.suggestions.append(msg)


# ---------------------------------------------------------------------------
# Rule-Based Checker (0 tokens)
# ---------------------------------------------------------------------------

# Patterns for fake tracking numbers (made up by LLM)
FAKE_TRACKING_PATTERNS = [
    r"9[24]00\s*1234\s*5678",  # Common fake patterns
    r"usps\d{22}",  # USPS format but might be fake
    r"tracking.*will\s+be\s+updated",  # Placeholder phrases
    r"tracking.*soon",
]

# AI self-reference patterns
AI_SELF_REFERENCE = [
    r"as an ai",
    r"i am an ai",
    r"i'm an ai",
    r"artificial intelligence",
    r"language model",
    r"chatgpt",
    r"claude",
    r"openai",
    r"anthropic",
]

# Competitor mentions (should not appear)
COMPETITOR_PATTERNS = [
    r"iqos\.com",
    r"heets\.com",
    r"amazon",
    r"ebay",
    r"alibaba",
]

# Phrases we should not use
FORBIDDEN_PHRASES = [
    r"check\s+(the|our)\s+website",  # We check FOR them
    r"visit\s+(the|our)\s+website",
    r"go\s+to\s+(the|our)\s+website",
    r"i don'?t have\s+access",
    r"i cannot\s+access",
    r"i'm unable to",
]

# Discount patterns (to check if unauthorized)
DISCOUNT_PATTERN = r"(\d+)\s*%\s*(off|discount)"


def check_rules(
    draft: str,
    result: dict,
    conversation_state: Optional[dict] = None,
) -> CheckResult:
    """Apply rule-based checks to the draft reply.
    
    Zero LLM tokens — pure Python validation.
    
    Args:
        draft: The generated reply text
        result: Processing result dict with client_data, etc.
        conversation_state: Optional ConversationState for context
        
    Returns:
        CheckResult with any violations found
    """
    check = CheckResult()
    draft_lower = draft.lower()
    
    # Rule 1: No fake tracking numbers
    for pattern in FAKE_TRACKING_PATTERNS:
        if re.search(pattern, draft_lower):
            check.add_warning(
                "Possible fake tracking number detected — verify before sending",
                source="rule"
            )
            break
    
    # Rule 2: Must end with "Thank you!"
    # Allow some variations
    thank_patterns = [
        r"thank\s+you!?\s*$",
        r"thanks!?\s*$",
        r"thank\s+you\s+so\s+much!?\s*$",
    ]
    has_thank_you = any(
        re.search(p, draft_lower.strip()) for p in thank_patterns
    )
    if not has_thank_you:
        check.add_suggestion("Reply should end with 'Thank you!'")
    
    # Rule 3: No AI self-reference
    for pattern in AI_SELF_REFERENCE:
        if re.search(pattern, draft_lower):
            check.add_warning(
                f"AI self-reference detected: '{pattern}'",
                source="rule"
            )
            break
    
    # Rule 4: No competitor mentions
    for pattern in COMPETITOR_PATTERNS:
        if re.search(pattern, draft_lower):
            check.add_warning(
                f"Competitor mention detected: '{pattern}'",
                source="rule"
            )
            break
    
    # Rule 5: No forbidden phrases
    for pattern in FORBIDDEN_PHRASES:
        match = re.search(pattern, draft_lower)
        if match:
            check.add_warning(
                f"Forbidden phrase detected: '{match.group()}'",
                source="rule"
            )
            break
    
    # Rule 6: Check discount authorization
    discount_match = re.search(DISCOUNT_PATTERN, draft_lower)
    if discount_match:
        mentioned_discount = int(discount_match.group(1))
        client_data = result.get("client_data", {})
        authorized_discount = client_data.get("discount_percent", 0) if client_data else 0
        
        if mentioned_discount > authorized_discount:
            check.add_warning(
                f"Unauthorized discount: {mentioned_discount}% offered, "
                f"client has {authorized_discount}% authorized",
                source="rule"
            )
    
    return check


# ---------------------------------------------------------------------------
# LLM Checker Agent
# ---------------------------------------------------------------------------

checker_instructions = """\
You are a quality checker for customer service replies at shipmecarton.com.

You will receive:
1. CONVERSATION STATE — facts, promises, previous exchanges
2. DRAFT REPLY — the response to be sent
3. POLICY RULES — rules that must be followed

Your task: Check the draft reply for issues.

CHECK FOR:

1. **Fact Consistency**
   - Does the reply match facts in CONVERSATION STATE?
   - If it mentions order_id, price, items — are they correct?
   - No contradictions with what we said before

2. **Promise Compliance**
   - If we made promises (e.g., "ship today", "3-5 days delivery")
   - Does this reply honor or contradict them?

3. **Tone Matching**
   - Is the tone casual and friendly like James?
   - Does it match our previous messages (if shown)?

4. **Answers the Question**
   - Does the reply actually address what the customer asked?
   - Is it helpful or evasive?

5. **Hallucination Detection**
   - Any facts mentioned that are NOT in the context?
   - Made-up details about products, prices, shipping?

RESPONSE FORMAT:
Return ONLY a JSON object:
{
  "is_ok": true/false,
  "issues": ["list", "of", "issues"],
  "suggestions": ["optional", "improvements"]
}

If everything is fine: {"is_ok": true, "issues": [], "suggestions": []}
"""

checker_agent = Agent(
    id="reply-checker",
    name="Reply Checker",
    model=OpenAIResponses(id="gpt-5.2"),
    instructions=checker_instructions,
    markdown=False,
)


def check_with_llm(
    draft: str,
    conversation_state: Optional[dict],
    policy_rules: str = "",
) -> CheckResult:
    """Run LLM-based checks on the draft reply.
    
    This is the 4th LLM call in the pipeline.
    
    Args:
        draft: The generated reply text
        conversation_state: ConversationState JSON
        policy_rules: Relevant policy rules for context
        
    Returns:
        CheckResult with LLM-identified issues
    """
    check = CheckResult()
    
    # Build prompt
    state_text = "No conversation state available."
    if conversation_state:
        import json
        state_text = json.dumps(conversation_state, ensure_ascii=False, indent=2)
    
    prompt = f"""
=== CONVERSATION STATE ===
{state_text}

=== POLICY RULES ===
{policy_rules or "Standard policies apply."}

=== DRAFT REPLY ===
{draft}

Check this reply and return JSON result:
"""
    
    try:
        response = checker_agent.run(prompt)
        raw = response.content
        
        # Parse JSON response
        import json
        # Strip markdown if present
        json_str = re.sub(r"^```json\s*|\s*```$", "", raw.strip())
        data = json.loads(json_str)
        
        if not data.get("is_ok", True):
            for issue in data.get("issues", []):
                check.add_warning(issue, source="llm")
        
        for suggestion in data.get("suggestions", []):
            check.add_suggestion(suggestion)
            
    except Exception as e:
        logger.warning("LLM checker failed: %s", e)
        check.add_suggestion(f"LLM check skipped: {e}")
    
    return check


# ---------------------------------------------------------------------------
# Combined Checker
# ---------------------------------------------------------------------------

def check_reply(
    draft: str,
    result: dict,
    conversation_state: Optional[dict] = None,
    policy_rules: str = "",
    run_llm_check: bool = True,
) -> CheckResult:
    """Full two-layer check of a reply draft.
    
    Layer 1: Rule-based (Python, 0 tokens)
    Layer 2: LLM-based (optional, ~500 tokens)
    
    Args:
        draft: The generated reply text
        result: Processing result dict
        conversation_state: ConversationState JSON
        policy_rules: Relevant policy rules
        run_llm_check: Whether to run LLM check (can be disabled for speed)
        
    Returns:
        Combined CheckResult from both layers
    """
    # Layer 1: Rule-based
    rule_result = check_rules(draft, result, conversation_state)
    
    # Layer 2: LLM-based (if enabled and no critical rule violations)
    if run_llm_check and len(rule_result.rule_violations) < 3:
        llm_result = check_with_llm(draft, conversation_state, policy_rules)
        
        # Merge results
        rule_result.warnings.extend(llm_result.warnings)
        rule_result.suggestions.extend(llm_result.suggestions)
        rule_result.llm_issues.extend(llm_result.llm_issues)
        
        if not llm_result.is_ok:
            rule_result.is_ok = False
    
    return rule_result


def format_check_result_for_telegram(check: CheckResult) -> str:
    """Format check result for Telegram notification."""
    if check.is_ok:
        return "✅ Reply check passed"
    
    lines = ["⚠️ <b>Reply Check Issues:</b>"]
    
    if check.rule_violations:
        lines.append("\n<b>Rule Violations:</b>")
        for v in check.rule_violations:
            lines.append(f"• {v}")
    
    if check.llm_issues:
        lines.append("\n<b>LLM Issues:</b>")
        for i in check.llm_issues:
            lines.append(f"• {i}")
    
    if check.suggestions:
        lines.append("\n<b>Suggestions:</b>")
        for s in check.suggestions:
            lines.append(f"💡 {s}")
    
    return "\n".join(lines)