# ConnectWise Manage agent toolbelt

This folder turns the uploaded one-off scripts into reusable actions that an AI dispatcher can call.

The original uploaded scripts post internal notes with `detailDescriptionFlag=False`, `internalAnalysisFlag=True`, and `resolutionFlag=False`. This toolbelt keeps that exact internal-note behavior and adds a separate discussion-note action that posts with `internalAnalysisFlag=False` and defaults `detailDescriptionFlag=True`.

## Files

- `connectwise_manage_client.py`  
  Auth, retries, GET/PATCH/POST, ticket read, ticket list, internal/discussion note add, and generic related-resource fetch.

- `connectwise_manage_resolvers.py`  
  Name-to-id resolution for boards, statuses, types, members, and companies using `mappings.json` and `cw_companies.json`.

- `connectwise_manage_actions.py`  
  Small callable actions like `assign_owner`, `set_board_status_type`, `set_company_contact_site`, `set_project_fields`, and `update_custom_fields`.

- `connectwise_manage_agent_runtime.py`  
  Exposes the action layer as structured tools that an LLM can call with JSON arguments.

## Why this layout is better

Your original scripts are powerful, but they are centered around a giant config block. That is great for batch runs and testing, but not ideal for an AI agent that needs to decide one thing at a time.

This refactor changes the control flow to:

1. Brain reads the ticket.
2. Brain decides a small action.
3. Runtime validates the arguments.
4. Action layer resolves names to ids.
5. Client sends minimal JSON Patch ops.
6. Runtime returns a result back to the brain.

That keeps the model flexible while protecting the API from broad accidental writes.

## Example brain loop

```python
from connectwise_manage_agent_runtime import AgentRuntime, TOOL_DEFINITIONS, format_result

runtime = AgentRuntime(dry_run=True)

# In a real app, this comes from your LLM tool-calling response.
planned_calls = [
    {"name": "get_ticket", "arguments": {"ticket_id": 1208274}},
    {
        "name": "set_board_status_type",
        "arguments": {
            "ticket_id": 1208274,
            "board": "Support",
            "status": "Assigned",
            "ticket_type": "Application"
        }
    },
    {
        "name": "add_internal_note",
        "arguments": {
            "ticket_id": 1208274,
            "text": "Routed by AI dispatcher based on board, type, and client context."
        }
    },
    {
        "name": "add_discussion_note",
        "arguments": {
            "ticket_id": 1208274,
            "text": "We received your request and have routed it to the appropriate team.",
            "process_notifications": True
        }
    }
]

for call in planned_calls:
    result = runtime.call_tool(call["name"], call["arguments"])
    print(format_result(result))
```

## Recommended production pattern

Use two modes:

- `dry_run=True` for planning, previews, testing, and AI self-checks
- `dry_run=False` only after the plan looks correct

For higher safety, keep `raw_patch` disabled at first and let the model use only the named actions.

## Good first tool set for an AI dispatcher

Start with these only:

- `get_ticket`
- `list_related_resources`
- `fetch_related_resource`
- `set_board_status_type`
- `assign_owner`
- `set_priority_and_routing`
- `add_internal_note`
- `add_discussion_note`
- `update_custom_fields`

Then add broader field patching once the agent is stable.
