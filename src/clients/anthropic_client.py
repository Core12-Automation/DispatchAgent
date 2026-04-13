"""
src/clients/anthropic_client.py

Reusable wrapper around the Anthropic Messages API.

Covers two usage patterns that exist (or will exist) in this codebase:

1. Simple completion  — system prompt + user message → text response.
   Used by the current router (app/services/router.py) and any future
   one-shot reasoning steps.

2. Tool-use loop  — agentic loop where Claude may call tools repeatedly
   until it emits a final text response.  Used by the future autonomous
   dispatch agent.

Both methods handle:
  - Client instantiation from ANTHROPIC_API_KEY env var
  - Overloadable default model / max_tokens
  - Rate-limit / transient error retries (via the SDK's built-in retry)
  - Normalising the response back to plain text or structured tool calls
"""

from __future__ import annotations

import os
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

import anthropic

# Default model used when callers don't specify one
DEFAULT_MODEL = "claude-sonnet-4-6"
DEFAULT_MAX_TOKENS = 1024


class AnthropicClient:
    """
    Thin wrapper around anthropic.Anthropic with helpers for simple and
    tool-use completion patterns.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        *,
        default_model: str = DEFAULT_MODEL,
        default_max_tokens: int = DEFAULT_MAX_TOKENS,
        max_retries: int = 3,
    ) -> None:
        resolved_key = api_key or os.getenv("ANTHROPIC_API_KEY", "").strip()
        if not resolved_key:
            raise ValueError(
                "Anthropic API key not found. "
                "Set ANTHROPIC_API_KEY in your environment or pass api_key=."
            )
        self._client = anthropic.Anthropic(api_key=resolved_key, max_retries=max_retries)
        self.default_model = default_model
        self.default_max_tokens = default_max_tokens

    # ── Simple completion ─────────────────────────────────────────────────────

    def complete(
        self,
        *,
        system: str,
        messages: List[Dict[str, Any]],
        model: Optional[str] = None,
        max_tokens: Optional[int] = None,
    ) -> str:
        """
        Send a single request and return the text of the first content block.

        Raises ValueError if the response contains no text content.
        """
        response = self._client.messages.create(
            model=model or self.default_model,
            max_tokens=max_tokens or self.default_max_tokens,
            system=system,
            messages=messages,
        )
        for block in response.content:
            if hasattr(block, "text"):
                return block.text
        raise ValueError(f"Claude returned no text content: {response.content!r}")

    # ── Tool-use agentic loop ─────────────────────────────────────────────────

    def run_tool_loop(
        self,
        *,
        system: str,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        tool_executor: Callable[[str, Dict[str, Any]], Any],
        model: Optional[str] = None,
        max_tokens: Optional[int] = None,
        max_iterations: int = 20,
    ) -> Tuple[str, List[Dict[str, Any]]]:
        """
        Run a tool-use loop until Claude emits a final text response.

        Args:
            system:         System prompt.
            messages:       Initial message list (mutated in-place as the
                            loop appends assistant + tool_result turns).
            tools:          Claude tool definitions (JSON schema list).
            tool_executor:  Callable(tool_name, tool_input) → result.
                            Should return a JSON-serialisable value.
            model:          Override model.
            max_tokens:     Override max_tokens.
            max_iterations: Safety limit on tool-call rounds.

        Returns:
            (final_text, updated_messages)
        """
        loop_messages = list(messages)

        for _ in range(max_iterations):
            response = self._client.messages.create(
                model=model or self.default_model,
                max_tokens=max_tokens or self.default_max_tokens,
                system=system,
                messages=loop_messages,
                tools=tools,
            )

            # Append assistant turn
            loop_messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason == "end_turn":
                # Extract the final text
                for block in response.content:
                    if hasattr(block, "text"):
                        return block.text, loop_messages
                raise ValueError("end_turn with no text block in response.")

            if response.stop_reason != "tool_use":
                raise ValueError(f"Unexpected stop_reason: {response.stop_reason!r}")

            # Execute each tool call and collect results
            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                try:
                    result = tool_executor(block.name, block.input)
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": str(result),
                        }
                    )
                except Exception as exc:
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": f"ERROR: {exc}",
                            "is_error": True,
                        }
                    )

            loop_messages.append({"role": "user", "content": tool_results})

        raise RuntimeError(
            f"Tool-use loop exceeded max_iterations={max_iterations} without finishing."
        )
