"""
src/agent/tool_definitions.py

Full tool definitions for the dispatch agent, grouped into three categories:

  PERCEPTION (6)  — read the world (tickets, board state, tech availability)
  ACTION     (9)  — change the world (assign, escalate, notify, flag)
  MEMORY     (4)  — read/write dispatch history and tech profiles

Each description tells Claude WHEN and WHY to call the tool, not just what it
does.  The input_schema uses strict JSON Schema (additionalProperties: false,
explicit required fields, enums where the value set is bounded).
"""

from __future__ import annotations

from typing import Any, Dict, List

# ─────────────────────────────────────────────────────────────────────────────
# PERCEPTION tools
# ─────────────────────────────────────────────────────────────────────────────

_PERCEPTION: List[Dict[str, Any]] = [
    {
        "name": "get_new_tickets",
        "description": (
            "Fetch all unassigned / unrouted tickets currently sitting in the "
            "dispatch queue.  Call this FIRST at the start of a dispatch session "
            "to understand what needs to be handled.  Also call it if you are "
            "uncertain whether a specific ticket is still open and unrouted, or "
            "if you want to prioritise multiple tickets before working through "
            "them one at a time.  Returns a list of ticket objects sorted by "
            "priority (Critical → High → Medium → Low) then date entered."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "board_names": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Boards to scan.  Leave empty to use the boards "
                        "configured in portal settings."
                    ),
                },
                "statuses": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Status names to include, e.g. ['New', 'New (Email connector)'].",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Cap on tickets returned.  Defaults to 50.",
                    "minimum": 1,
                    "maximum": 500,
                },
            },
            "required": [],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_dispatch_board",
        "description": (
            "Get a snapshot of the entire dispatch board — all open tickets, "
            "their current assignees, statuses, and ages.  Use this when you "
            "need to understand the overall workload distribution before making "
            "an assignment decision, or when you suspect the board is "
            "backlogged.  Heavier than get_new_tickets; prefer get_new_tickets "
            "when you only need unrouted items."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "board_name": {
                    "type": "string",
                    "description": "Board name to inspect (e.g. 'Support').",
                },
                "include_closed": {
                    "type": "boolean",
                    "description": "Set true to include closed tickets.  Defaults to false.",
                },
            },
            "required": ["board_name"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_technician_schedule",
        "description": (
            "Look up a technician's schedule for today and the next two days — "
            "on-site appointments, PTO, training blocks, etc.  Call this before "
            "assigning a ticket that requires on-site work or that will consume "
            "several hours, to confirm the tech has capacity.  If the schedule "
            "shows the tech is fully booked or on PTO, choose someone else."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "technician_identifier": {
                    "type": "string",
                    "description": "The tech's login identifier (e.g. 'akloss').",
                },
                "days_ahead": {
                    "type": "integer",
                    "description": "How many days ahead to look.  Defaults to 2.",
                    "minimum": 0,
                    "maximum": 14,
                },
            },
            "required": ["technician_identifier"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_technician_workload",
        "description": (
            "Return the number of open tickets currently assigned to a "
            "technician, broken down by priority.  ALWAYS call this before "
            "assigning a ticket to any tech.  If the tech already has 5 or "
            "more open tickets, do not assign to them — pick someone with "
            "lower workload or flag for human review.  Use this to distribute "
            "work evenly across the team."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "technician_identifier": {
                    "type": "string",
                    "description": "The tech's login identifier (e.g. 'akloss').",
                },
            },
            "required": ["technician_identifier"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_ticket_history",
        "description": (
            "Fetch the full history of a ticket: all internal notes, "
            "discussion notes, and the audit trail showing every status/owner "
            "change.  Call this when: (a) the ticket summary is ambiguous and "
            "you need more context; (b) the ticket has been reopened and you "
            "want to know who handled it before; (c) you are considering "
            "reassigning and need to understand prior work done.  Returns notes "
            "in chronological order."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ticket_id": {
                    "type": "integer",
                    "description": "ConnectWise ticket ID.",
                },
                "include_audit": {
                    "type": "boolean",
                    "description": "Include the audit trail (owner/status changes).  Defaults to true.",
                },
            },
            "required": ["ticket_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_tech_availability",
        "description": (
            "Check a technician's real-time Teams presence (Available, Busy, "
            "Away, DoNotDisturb, BeRightBack, Offline).  Call this when you "
            "have narrowed to 1-2 candidates and want to confirm who is "
            "actually at their desk right now, especially for urgent/Critical "
            "tickets.  If both candidates are equally good on skills and "
            "workload, prefer the one who is Available."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "technician_identifier": {
                    "type": "string",
                    "description": "The tech's login identifier (e.g. 'akloss').",
                },
            },
            "required": ["technician_identifier"],
            "additionalProperties": False,
        },
    },
]


# ─────────────────────────────────────────────────────────────────────────────
# ACTION tools
# ─────────────────────────────────────────────────────────────────────────────

_ACTION: List[Dict[str, Any]] = [
    {
        "name": "assign_ticket",
        "description": (
            "Assign an unrouted ticket to a technician and optionally change "
            "its status (e.g. to 'Assigned').  This is the primary dispatch "
            "action.  Only call this after you have: (1) checked the tech's "
            "workload with get_technician_workload, (2) confirmed skills match, "
            "(3) called log_dispatch_decision to record your reasoning.  "
            "In dry_run mode the assignment is previewed but not applied to "
            "ConnectWise."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ticket_id": {"type": "integer"},
                "technician_identifier": {
                    "type": "string",
                    "description": "Tech login identifier (e.g. 'akloss').",
                },
                "new_status": {
                    "type": "string",
                    "description": "Status to set after assignment, e.g. 'Assigned'.  Optional.",
                },
                "new_board": {
                    "type": "string",
                    "description": "Move ticket to this board after assignment.  Optional.",
                },
            },
            "required": ["ticket_id", "technician_identifier"],
            "additionalProperties": False,
        },
    },
    {
        "name": "reassign_ticket",
        "description": (
            "Reassign a ticket that is already owned by someone else to a "
            "different technician.  Use this when: (a) the current owner is "
            "overloaded (5+ tickets) and a better match exists; (b) the owner "
            "is absent; (c) a more specialised tech became available.  Always "
            "add an update_ticket_notes call explaining the reassignment reason "
            "so there is an audit trail."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ticket_id": {"type": "integer"},
                "new_technician_identifier": {
                    "type": "string",
                    "description": "Login identifier of the new owner.",
                },
                "reason": {
                    "type": "string",
                    "description": "Brief reason for the reassignment (logged internally).",
                },
            },
            "required": ["ticket_id", "new_technician_identifier", "reason"],
            "additionalProperties": False,
        },
    },
    {
        "name": "escalate_ticket",
        "description": (
            "Escalate a ticket by raising its priority, optionally moving it "
            "to a different board (e.g. 'Support' → 'Critical'), and "
            "optionally changing its type.  Use this when the ticket content "
            "reveals a severity that does not match the current priority — for "
            "example, a 'Low' ticket that describes a server outage affecting "
            "multiple users.  After escalating, call message_team_channel to "
            "alert the team."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ticket_id": {"type": "integer"},
                "new_priority": {
                    "type": "string",
                    "enum": ["Critical", "High", "Medium", "Low"],
                    "description": "New priority level.",
                },
                "new_board": {
                    "type": "string",
                    "description": "Board to move the ticket to.  Optional.",
                },
                "escalation_reason": {
                    "type": "string",
                    "description": "Why this ticket is being escalated (added as internal note).",
                },
            },
            "required": ["ticket_id", "new_priority", "escalation_reason"],
            "additionalProperties": False,
        },
    },
    {
        "name": "message_technician",
        "description": (
            "Send a direct Teams chat message to a technician.  Use this "
            "after assigning a Critical or High ticket to give them a heads-up "
            "and key context.  Keep messages brief and professional: one "
            "sentence on what the ticket is, one sentence on the client, one "
            "sentence on any urgency.  Do NOT send messages for every routine "
            "assignment — only for urgent or unusual cases."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "technician_identifier": {
                    "type": "string",
                    "description": "Login identifier of the tech to message.",
                },
                "message": {
                    "type": "string",
                    "description": "Message text (plain text, keep under 300 characters).",
                },
                "ticket_id": {
                    "type": "integer",
                    "description": "Optional: ticket ID to include in the message context.",
                },
            },
            "required": ["technician_identifier", "message"],
            "additionalProperties": False,
        },
    },
    {
        "name": "message_team_channel",
        "description": (
            "Post a message to a Teams channel (e.g. the dispatch channel).  "
            "Use this for: (a) Critical ticket escalations that the whole team "
            "should know about; (b) announcing a human-review flag so a senior "
            "dispatcher sees it; (c) end-of-run summaries when running in batch "
            "mode.  Do not spam the channel with routine assignments."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": "Message body.  HTML is supported.",
                },
                "channel": {
                    "type": "string",
                    "description": "Channel name or ID.  Defaults to the configured dispatch channel.",
                },
            },
            "required": ["message"],
            "additionalProperties": False,
        },
    },
    {
        "name": "send_reminder",
        "description": (
            "Send a reminder to a technician about a ticket that is approaching "
            "its SLA deadline or has been idle for too long.  Use this when "
            "you observe (via get_dispatch_board) that a ticket has been "
            "assigned but not touched for an extended period.  The reminder "
            "is posted as an internal note on the ticket AND sent as a Teams "
            "message to the owner."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ticket_id": {"type": "integer"},
                "technician_identifier": {
                    "type": "string",
                    "description": "Tech who owns the ticket.",
                },
                "reminder_reason": {
                    "type": "string",
                    "enum": ["sla_approaching", "idle_too_long", "client_waiting", "custom"],
                    "description": "Why the reminder is being sent.",
                },
                "custom_message": {
                    "type": "string",
                    "description": "Custom message text if reminder_reason is 'custom'.",
                },
            },
            "required": ["ticket_id", "technician_identifier", "reminder_reason"],
            "additionalProperties": False,
        },
    },
    {
        "name": "message_client",
        "description": (
            "Add a customer-visible discussion note to a ticket.  Use this "
            "sparingly — only when the dispatch agent needs to set an "
            "expectation with the client (e.g. 'Your ticket has been received "
            "and assigned; we will be in touch within 2 hours').  The note "
            "will be visible to the client via the ConnectWise portal.  "
            "Proofread carefully — this is external communication."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ticket_id": {"type": "integer"},
                "message": {
                    "type": "string",
                    "description": "Message text visible to the client.",
                },
                "send_email_notification": {
                    "type": "boolean",
                    "description": "Trigger ConnectWise email notification.  Defaults to false.",
                },
            },
            "required": ["ticket_id", "message"],
            "additionalProperties": False,
        },
    },
    {
        "name": "update_ticket_notes",
        "description": (
            "Add an internal (analyst-only) note to a ticket.  Use this to "
            "record your dispatch reasoning, context gathered from the client "
            "record, or any caveats about the assignment.  Good internal notes "
            "help the assigned tech understand why they got the ticket and what "
            "background work was done.  Internal notes are NOT visible to the "
            "client."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ticket_id": {"type": "integer"},
                "note_text": {
                    "type": "string",
                    "description": "Internal note content.",
                },
            },
            "required": ["ticket_id", "note_text"],
            "additionalProperties": False,
        },
    },
    {
        "name": "flag_for_human_review",
        "description": (
            "Flag a ticket for a human dispatcher to review manually.  Call "
            "this when: (a) you are not confident in the assignment (confidence "
            "< 0.6); (b) all available techs are overloaded; (c) the ticket "
            "involves unusual circumstances (legal, HIPAA, executive client, "
            "etc.); (d) the ticket has been bounced between techs multiple "
            "times.  After flagging, post to the team channel so a human sees "
            "it promptly."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ticket_id": {"type": "integer"},
                "reason": {
                    "type": "string",
                    "description": "Why this ticket needs human review.",
                },
                "suggested_technician": {
                    "type": "string",
                    "description": "Your best guess for who should handle it, even if uncertain.",
                },
            },
            "required": ["ticket_id", "reason"],
            "additionalProperties": False,
        },
    },
]


# ─────────────────────────────────────────────────────────────────────────────
# MEMORY tools
# ─────────────────────────────────────────────────────────────────────────────

_MEMORY: List[Dict[str, Any]] = [
    {
        "name": "get_tech_profile",
        "description": (
            "Load a technician's stored profile: their skills, specialties, "
            "average resolution time, total tickets handled, and any notes "
            "recorded by previous dispatch runs.  Call this when you need to "
            "go beyond the roster description and assess a tech's actual track "
            "record — especially for complex, multi-skill tickets."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "technician_identifier": {
                    "type": "string",
                    "description": "Login identifier (e.g. 'akloss').",
                },
            },
            "required": ["technician_identifier"],
            "additionalProperties": False,
        },
    },
    {
        "name": "update_tech_profile",
        "description": (
            "Update a technician's profile after a dispatch decision.  Call "
            "this when you learn something new about a tech's skills or "
            "capabilities — for example, after a ticket reveals they handle "
            "firewall work well.  Do NOT call this every dispatch; only when "
            "genuinely new information warrants a profile update."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "technician_identifier": {
                    "type": "string",
                    "description": "Login identifier of the tech.",
                },
                "updates": {
                    "type": "object",
                    "description": (
                        "Fields to update.  Supported keys: "
                        "skills (array), specialties (array), notes (string)."
                    ),
                    "properties": {
                        "skills":      {"type": "array", "items": {"type": "string"}},
                        "specialties": {"type": "array", "items": {"type": "string"}},
                        "notes":       {"type": "string"},
                    },
                    "additionalProperties": False,
                },
            },
            "required": ["technician_identifier", "updates"],
            "additionalProperties": False,
        },
    },
    {
        "name": "log_dispatch_decision",
        "description": (
            "Persist your dispatch decision to the audit log.  ALWAYS call "
            "this after every assignment, reassignment, escalation, or "
            "flag_for_human_review — even in dry_run mode.  This is the "
            "institutional memory of the dispatch system; future runs use it "
            "via get_similar_past_tickets.  Include the full reason and any "
            "alternatives you considered."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ticket_id": {"type": "integer"},
                "ticket_summary": {
                    "type": "string",
                    "description": "One-line summary of the ticket (for future similarity search).",
                },
                "assigned_technician": {
                    "type": "string",
                    "description": "Login identifier of the assigned tech (or 'human_review').",
                },
                "reason": {
                    "type": "string",
                    "description": "Full reasoning for the assignment.",
                },
                "confidence": {
                    "type": "number",
                    "minimum": 0.0,
                    "maximum": 1.0,
                    "description": "Your confidence in this decision (0.0 = total guess, 1.0 = certain).",
                },
                "alternatives_considered": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "technician": {"type": "string"},
                            "reason_not_chosen": {"type": "string"},
                        },
                        "required": ["technician", "reason_not_chosen"],
                        "additionalProperties": False,
                    },
                    "description": "Other techs you considered and why you ruled them out.",
                },
            },
            "required": ["ticket_id", "ticket_summary", "assigned_technician", "reason", "confidence"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_similar_past_tickets",
        "description": (
            "Search the dispatch history for tickets similar to the current "
            "one based on keyword similarity in the summary.  Call this when "
            "the ticket is unusual, involves a recurring client issue, or when "
            "you want to confirm that your intended assignee has handled this "
            "type of work successfully before.  Returns up to 10 past "
            "decisions with who handled them and the outcomes."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Keywords or short phrase describing the ticket type.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum results to return.  Defaults to 5.",
                    "minimum": 1,
                    "maximum": 20,
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
]


# ─────────────────────────────────────────────────────────────────────────────
# CONTEXTUAL / INCIDENT tools
# ─────────────────────────────────────────────────────────────────────────────

_CONTEXTUAL: List[Dict[str, Any]] = [
    {
        "name": "suppress_alert",
        "description": (
            "Suppress further tickets matching this incident's fingerprint for a "
            "specified number of hours.  Suppressed tickets will be auto-acknowledged "
            "in ConnectWise with an internal note, but NOT assigned to anyone.  "
            "Use when: an operator note authorises suppression, or a repeat alert "
            "storm has been assigned and further duplicates should be silenced.  "
            "Always log your decision with log_dispatch_decision after calling this."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "incident_id": {
                    "type": "integer",
                    "description": "The active_incidents.id of the incident to suppress.",
                },
                "duration_hours": {
                    "type": "number",
                    "description": "How many hours to suppress.  Use 0 for indefinite (until manually resolved).",
                    "minimum": 0,
                },
                "reason": {
                    "type": "string",
                    "description": "Why this incident is being suppressed (added as internal note).",
                },
            },
            "required": ["incident_id", "duration_hours", "reason"],
            "additionalProperties": False,
        },
    },
    {
        "name": "group_with_incident",
        "description": (
            "Link a new ticket to an existing active incident instead of treating it "
            "as a separate issue.  The ticket will NOT get a new tech assignment — "
            "it is already being handled under the incident.  Use when: a ticket's "
            "_context shows is_repeat=true and the incident has a tech already_assigned_to.  "
            "This is preferred over assign_ticket for duplicate tickets."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ticket_id": {
                    "type": "integer",
                    "description": "The CW ticket ID to group.",
                },
                "incident_id": {
                    "type": "integer",
                    "description": "The active_incidents.id to group this ticket under.",
                },
            },
            "required": ["ticket_id", "incident_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_active_incidents",
        "description": (
            "Get all active (non-resolved) incidents, optionally filtered by client name.  "
            "Use this to check whether an ongoing issue is already tracked before "
            "deciding how to handle a new ticket.  Returns id, incident_key, status, "
            "occurrence_count, assigned_tech, and ticket_ids."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "client_name": {
                    "type": "string",
                    "description": "Optional: filter incidents by company name.",
                },
            },
            "required": [],
            "additionalProperties": False,
        },
    },
    {
        "name": "resolve_incident",
        "description": (
            "Mark an incident as resolved.  Future tickets with the same fingerprint "
            "will start a fresh incident.  Call this when the root cause is fixed and "
            "no more related tickets are expected.  Clears any active suppression."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "incident_id": {
                    "type": "integer",
                    "description": "The active_incidents.id to resolve.",
                },
                "resolution_notes": {
                    "type": "string",
                    "description": "Optional notes describing how the incident was resolved.",
                },
            },
            "required": ["incident_id"],
            "additionalProperties": False,
        },
    },
]


# ─────────────────────────────────────────────────────────────────────────────
# Combined export
# ─────────────────────────────────────────────────────────────────────────────

TOOL_DEFINITIONS: List[Dict[str, Any]] = _PERCEPTION + _ACTION + _MEMORY + _CONTEXTUAL

# Quick lookup by name
TOOL_NAMES = {t["name"] for t in TOOL_DEFINITIONS}

# Contextual tool names (for reference)
CONTEXTUAL_TOOL_NAMES = {t["name"] for t in _CONTEXTUAL}
