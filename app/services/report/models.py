"""
app/services/report/models.py

Dataclasses used by the ticket report pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from datetime import datetime
from typing import Dict, List, Optional


@dataclass
class CloseHistoryResult:
    selected_close_time:          Optional[datetime]
    effective_close_for_duration: Optional[datetime]
    total_open_seconds:           Optional[float]
    saw_real_reopen:              bool


@dataclass
class TicketRow:
    ticket_id:          int
    summary:            str
    board_name:         str
    board_id:           Optional[int]
    status_name:        str
    status_id:          Optional[int]
    priority_name:      str
    priority_id:        Optional[int]
    priority_level:     Optional[int]
    owner_ident:        str
    resources:          str
    team_name:          str
    source_name:        str
    company_name:       str
    company_identifier: str
    company_canonical:  str
    date_entered:       Optional[datetime]
    date_closed:        Optional[datetime]
    first_contact:      Optional[datetime]
    effective_close:    Optional[datetime]
    closed_flag:        bool
    saw_real_reopen:    bool = False
    total_open_seconds: Optional[float] = None


@dataclass
class Filters:
    date_entered_after_utc:               Optional[str]        = None
    date_entered_before_utc:              Optional[str]        = None
    min_age_days:                         Optional[float]      = None
    max_age_days:                         Optional[float]      = None
    exclude_if_closed_in_month:           Optional[int]        = None
    exclude_if_closed_in_month_over_days: Optional[float]      = None
    company_include:                      List[str]            = dc_field(default_factory=list)
    company_exclude:                      List[str]            = dc_field(default_factory=list)
    assignee_include:                     List[str]            = dc_field(default_factory=list)
    assignee_exclude:                     List[str]            = dc_field(default_factory=list)
    board_include:                        List[str]            = dc_field(default_factory=list)
    board_exclude:                        List[str]            = dc_field(default_factory=lambda: ["Alerts"])
    status_include:                       List[str]            = dc_field(default_factory=list)
    status_exclude:                       List[str]            = dc_field(default_factory=list)
    priority_include:                     List[str]            = dc_field(default_factory=list)
    priority_exclude:                     List[str]            = dc_field(default_factory=list)
    summary_contains_any:                 List[str]            = dc_field(default_factory=list)
    source_include:                       List[str]            = dc_field(default_factory=list)
    team_include:                         List[str]            = dc_field(default_factory=list)


@dataclass
class Metrics:
    total_tickets:        int
    total_closed:         int
    t_first_contact_seconds: List[float]
    t_close_seconds:      List[float]
    by_board:             Dict[str, int]
    by_status:            Dict[str, int]
    by_priority:          Dict[str, int]
    by_assignee:          Dict[str, int]
    by_company:           Dict[str, int]
    entered_by_day:       Dict[str, int]
    closed_by_day:        Dict[str, int]
    tech_priority_counts: Dict[str, Dict[str, int]]
