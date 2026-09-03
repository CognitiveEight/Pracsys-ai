"""Per-feature mixin classes composed into SocketEventHandler."""

from app.socket.handlers._shared import JsonHelpersMixin
from app.socket.handlers.documents import DocumentsMixin
from app.socket.handlers.state_roles import StateRolesMixin
from app.socket.handlers.extraction import ExtractionMixin
from app.socket.handlers.strategy import StrategyMixin
from app.socket.handlers.analysis_report import AnalysisReportMixin
from app.socket.handlers.list_of_dates import ListOfDatesMixin
from app.socket.handlers.applicable_laws import ApplicableLawsMixin
from app.socket.handlers.case_laws import CaseLawsMixin
from app.socket.handlers.relevant_parties import RelevantPartiesMixin
from app.socket.handlers.procedural_timeline import ProceduralTimelineMixin
from app.socket.handlers.drafting import DraftingMixin
from app.socket.handlers.objective_change import ObjectiveChangeMixin
from app.socket.handlers.law_relevance import LawRelevanceMixin
from app.socket.handlers.panel_edits import PanelEditsMixin
from app.socket.handlers.user_facts import UserFactsMixin
from app.socket.handlers.docx_export import DocxExportMixin
from app.socket.handlers.template_sync import TemplateSyncMixin
from app.socket.handlers.workspace_metadata import WorkspaceMetadataMixin
from app.socket.handlers.workspace_events import WorkspaceEventsMixin
from app.socket.handlers.chat import ChatMixin
from app.socket.handlers.suggestion_chips import SuggestionChipsMixin

__all__ = [
    "JsonHelpersMixin",
    "DocumentsMixin",
    "StateRolesMixin",
    "ExtractionMixin",
    "StrategyMixin",
    "AnalysisReportMixin",
    "ListOfDatesMixin",
    "ApplicableLawsMixin",
    "CaseLawsMixin",
    "RelevantPartiesMixin",
    "ProceduralTimelineMixin",
    "DraftingMixin",
    "ObjectiveChangeMixin",
    "LawRelevanceMixin",
    "PanelEditsMixin",
    "UserFactsMixin",
    "DocxExportMixin",
    "TemplateSyncMixin",
    "WorkspaceMetadataMixin",
    "WorkspaceEventsMixin",
    "ChatMixin",
    "SuggestionChipsMixin",
]
