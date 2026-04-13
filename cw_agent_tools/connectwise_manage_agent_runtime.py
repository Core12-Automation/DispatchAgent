from __future__ import annotations

import json
from typing import Any, Dict, List

from connectwise_manage_actions import ActionResult, TicketActions
from connectwise_manage_client import CWManageClient
from connectwise_manage_resolvers import MappingResolver


COMPANY_ALIASES = {
    "BLUR Workshop": ["blur", "BLUR", "blur workshop", "blurworkshop", "blurworkshop.com"],
    "Willmer Engineering, Inc.": ["Willmer", "willmerengineeringinc", "jcwillmer@willmerengineering.com"],
    "Mann Mechanical": ["Mann Mechanical", "mann", "gthomas@mannmechanical.com"],
}


TOOL_DEFINITIONS: List[Dict[str, Any]] = [
    {
        "name": "get_ticket",
        "description": "Read a ConnectWise service ticket before making a change.",
        "input_schema": {
            "type": "object",
            "properties": {"ticket_id": {"type": "integer"}},
            "required": ["ticket_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_related_resources",
        "description": "List the related resource hrefs available from the ticket _info object.",
        "input_schema": {
            "type": "object",
            "properties": {"ticket_id": {"type": "integer"}},
            "required": ["ticket_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "fetch_related_resource",
        "description": "Fetch a resource linked from ticket._info, such as notes, tasks, configurations, documents, products, timeentries, expenseEntries, activities, or scheduleentries.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticket_id": {"type": "integer"},
                "relation": {"type": "string"},
            },
            "required": ["ticket_id", "relation"],
            "additionalProperties": False,
        },
    },
    {
        "name": "patch_fields",
        "description": "Patch one or more allowed ticket fields with safe resolution for board, status, type, company, and owner.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticket_id": {"type": "integer"},
                "changes": {"type": "object"},
            },
            "required": ["ticket_id", "changes"],
            "additionalProperties": False,
        },
    },
    {
        "name": "assign_owner",
        "description": "Assign a ticket owner by member id or member name from mappings.json.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticket_id": {"type": "integer"},
                "owner": {"anyOf": [{"type": "integer"}, {"type": "string"}]},
            },
            "required": ["ticket_id", "owner"],
            "additionalProperties": False,
        },
    },
    {
        "name": "set_board_status_type",
        "description": "Update board, status, and type together so board-specific status/type names resolve correctly.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticket_id": {"type": "integer"},
                "board": {"anyOf": [{"type": "integer"}, {"type": "string"}]},
                "status": {"anyOf": [{"type": "integer"}, {"type": "string"}]},
                "ticket_type": {"anyOf": [{"type": "integer"}, {"type": "string"}]},
            },
            "required": ["ticket_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "set_company_contact_site",
        "description": "Update company, contact, site, and related address/contact strings on a ticket.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticket_id": {"type": "integer"},
                "company": {"anyOf": [{"type": "integer"}, {"type": "string"}]},
                "contact": {"anyOf": [{"type": "integer"}, {"type": "string"}]},
                "site": {"anyOf": [{"type": "integer"}, {"type": "string"}]},
                "contact_name": {"type": "string"},
                "contact_phone": {"type": "string"},
                "contact_email": {"type": "string"},
                "site_name": {"type": "string"},
                "address_line1": {"type": "string"},
                "address_line2": {"type": "string"},
                "city": {"type": "string"},
                "state_identifier": {"type": "string"},
                "zip_code": {"type": "string"}
            },
            "required": ["ticket_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "set_priority_and_routing",
        "description": "Set priority, service location, source, location, or department.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticket_id": {"type": "integer"},
                "priority": {"anyOf": [{"type": "integer"}, {"type": "object"}]},
                "service_location": {"anyOf": [{"type": "integer"}, {"type": "object"}]},
                "source": {"anyOf": [{"type": "integer"}, {"type": "object"}]},
                "location": {"anyOf": [{"type": "integer"}, {"type": "object"}]},
                "department": {"anyOf": [{"type": "integer"}, {"type": "object"}]}
            },
            "required": ["ticket_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "set_project_fields",
        "description": "Update project-ticket-style fields such as project, phase, wbsCode, budgetHours, opportunity, or summary.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticket_id": {"type": "integer"},
                "project": {"anyOf": [{"type": "integer"}, {"type": "object"}]},
                "phase": {"anyOf": [{"type": "integer"}, {"type": "object"}]},
                "wbs_code": {"type": "string"},
                "budget_hours": {"type": "number"},
                "opportunity": {"anyOf": [{"type": "integer"}, {"type": "object"}]},
                "summary": {"type": "string"}
            },
            "required": ["ticket_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "set_billing_flags",
        "description": "Update booleans and billing options such as approved, closedFlag, billTime, and automatic email flags.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticket_id": {"type": "integer"},
                "approved": {"type": "boolean"},
                "closed_flag": {"type": "boolean"},
                "sub_billing_method": {"type": "string"},
                "bill_time": {"type": "string"},
                "bill_expenses": {"type": "string"},
                "bill_products": {"type": "string"},
                "automatic_email_contact_flag": {"type": "boolean"},
                "automatic_email_resource_flag": {"type": "boolean"},
                "automatic_email_cc_flag": {"type": "boolean"},
                "automatic_email_cc": {"type": "string"},
                "allow_all_clients_portal_view": {"type": "boolean"},
                "customer_updated_flag": {"type": "boolean"}
            },
            "required": ["ticket_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "update_custom_fields",
        "description": "Update customFields by matching id, caption, or connectWiseId and replacing only the value.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticket_id": {"type": "integer"},
                "updates": {
                    "type": "array",
                    "items": {"type": "object"}
                }
            },
            "required": ["ticket_id", "updates"],
            "additionalProperties": False,
        },
    },
    {
        "name": "add_internal_note",
        "description": "Add an internal analysis note to a ticket.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticket_id": {"type": "integer"},
                "text": {"type": "string"}
            },
            "required": ["ticket_id", "text"],
            "additionalProperties": False,
        },
    },
    {
        "name": "add_discussion_note",
        "description": "Add a customer-visible discussion note to a ticket. By default this posts with internalAnalysisFlag off and detailDescriptionFlag on.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticket_id": {"type": "integer"},
                "text": {"type": "string"},
                "detail_description_flag": {"type": "boolean"},
                "resolution_flag": {"type": "boolean"},
                "process_notifications": {"type": "boolean"}
            },
            "required": ["ticket_id", "text"],
            "additionalProperties": False,
        },
    },
    {
        "name": "raw_patch",
        "description": "Apply explicit JSON Patch ops. Keep this as an escape hatch, not the AI's first choice.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticket_id": {"type": "integer"},
                "ops": {"type": "array", "items": {"type": "object"}}
            },
            "required": ["ticket_id", "ops"],
            "additionalProperties": False,
        },
    },
]


class AgentRuntime:
    def __init__(self, *, dry_run: bool = True) -> None:
        self.client = CWManageClient()
        self.resolver = MappingResolver(company_aliases=COMPANY_ALIASES)
        self.actions = TicketActions(self.client, self.resolver, dry_run=dry_run)

    def call_tool(self, name: str, arguments: Dict[str, Any]) -> ActionResult | Dict[str, Any]:
        if name == "get_ticket":
            ticket = self.actions.get_ticket(int(arguments["ticket_id"]))
            return {"ok": True, "action": "get_ticket", "ticket_id": int(arguments["ticket_id"]), "ticket": ticket}
        if name == "list_related_resources":
            return {
                "ok": True,
                "action": "list_related_resources",
                "ticket_id": int(arguments["ticket_id"]),
                "resources": self.actions.list_related_resources(int(arguments["ticket_id"])),
            }
        if name == "fetch_related_resource":
            return self.actions.fetch_related_resource(int(arguments["ticket_id"]), str(arguments["relation"]))
        if name == "patch_fields":
            return self.actions.patch_fields(int(arguments["ticket_id"]), dict(arguments["changes"]))
        if name == "assign_owner":
            return self.actions.assign_owner(int(arguments["ticket_id"]), arguments["owner"])
        if name == "set_board_status_type":
            return self.actions.set_board_status_type(
                int(arguments["ticket_id"]),
                board=arguments.get("board"),
                status=arguments.get("status"),
                ticket_type=arguments.get("ticket_type"),
            )
        if name == "set_company_contact_site":
            return self.actions.set_company_contact_site(**arguments)
        if name == "set_priority_and_routing":
            return self.actions.set_priority_and_routing(**arguments)
        if name == "set_project_fields":
            return self.actions.set_project_fields(**arguments)
        if name == "set_billing_flags":
            return self.actions.set_billing_flags(**arguments)
        if name == "update_custom_fields":
            return self.actions.update_custom_fields(int(arguments["ticket_id"]), list(arguments["updates"]))
        if name == "add_internal_note":
            return self.actions.add_internal_note(int(arguments["ticket_id"]), str(arguments["text"]))
        if name == "add_discussion_note":
            return self.actions.add_discussion_note(
                int(arguments["ticket_id"]),
                str(arguments["text"]),
                detail_description_flag=bool(arguments.get("detail_description_flag", True)),
                resolution_flag=bool(arguments.get("resolution_flag", False)),
                process_notifications=arguments.get("process_notifications"),
            )
        if name == "raw_patch":
            return self.actions.raw_patch(int(arguments["ticket_id"]), list(arguments["ops"]))
        raise ValueError(f"Unknown tool: {name}")


def format_result(result: Any) -> str:
    if isinstance(result, ActionResult):
        payload = {
            "ok": result.ok,
            "action": result.action,
            "ticket_id": result.ticket_id,
            "dry_run": result.dry_run,
            "message": result.message,
            "ops": result.ops,
            "data": result.data,
        }
        return json.dumps(payload, indent=2, ensure_ascii=False)
    return json.dumps(result, indent=2, ensure_ascii=False)


if __name__ == "__main__":
    runtime = AgentRuntime(dry_run=True)
    preview = runtime.call_tool(
        "set_board_status_type",
        {
            "ticket_id": 1208274,
            "board": "Support",
            "status": "New",
        },
    )
    import logging as _logging
    _logging.getLogger(__name__).debug("Tool preview result: %s", format_result(preview))
