"""Analysis DOCX generation request handler.

Feature mixin extracted from events.py — methods moved verbatim.
"""

import asyncio
from typing import Any, Dict

from loguru import logger

from app.socket.schemas import (
    AnalysisDocxGenerateRequestPayload,
)
from agent import process_analysis_docx_request



class DocxExportMixin:
    """Analysis DOCX generation request handler."""

    async def on_analysis_docx_generate_request(self, data: Dict[str, Any]) -> None:
        """
        Handle analysis_docx_generate_request (Server -> AI_Maverick).
        Performs deterministic text processing and emits analysis_docx_generate_response.
        """
        payload_in = data if isinstance(data, dict) else {}
        logger.info(
            "analysis_docx_generate_request <- backend (received): keys={}",
            list(payload_in.keys()) if isinstance(payload_in, dict) else type(payload_in),
        )

        if not self._socket_client:
            logger.warning(
                "analysis_docx_generate_request: socket client unavailable, cannot emit response"
            )
            return

        try:
            req = AnalysisDocxGenerateRequestPayload(**payload_in)
            normalized_request = req.model_dump(exclude_none=True)
        except Exception as e:
            logger.warning("analysis_docx_generate_request: invalid payload: {}", e)
            normalized_request = payload_in

        response_body = await asyncio.to_thread(
            process_analysis_docx_request, normalized_request
        )
        logger.info(
            "analysis_docx_generate_response ready (keys: {})",
            sorted(response_body.keys()) if isinstance(response_body, dict) else type(response_body).__name__,
        )
        await self._socket_client.emit("analysis_docx_generate_response", response_body)
        logger.success(
            "analysis_docx_generate_response emitted for requestId={}",
            response_body.get("requestId"),
        )
