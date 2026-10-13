"""
Workspace manager for handling workspace operations.
"""

import asyncio
from pathlib import Path
from typing import Dict, List, Optional

import aiofiles
import httpx
from loguru import logger

from app.utils.redact import safe_url

from app.core.config import settings
from app.observability.trace import tracer
from app.socket.schemas import EvidenceFile, WorkspaceData
from app.utils.exceptions import (FileProcessingError, WorkspaceError,
                                  WorkspaceNotFoundError)
from app.vectorstore.qdrant import vector_store
from app.workspace.context import WorkspaceContext, context_manager


class WorkspaceManager:
    """
    Manages workspace lifecycle, file processing, and vector store integration.
    """

    def __init__(self):
        self._processing_tasks: Dict[str, asyncio.Task] = {}
        self._http_client: Optional[httpx.AsyncClient] = None

    async def initialize(self) -> None:
        """Initialize workspace manager."""
        # Create workspace data directory
        Path(settings.workspace_data_dir).mkdir(parents=True, exist_ok=True)

        # Initialize HTTP client
        self._http_client = httpx.AsyncClient(timeout=30.0)

        logger.info("Workspace manager initialized")

    async def cleanup(self) -> None:
        """Cleanup workspace manager resources."""
        if self._http_client:
            await self._http_client.aclose()

        # Cancel all processing tasks
        for task in self._processing_tasks.values():
            task.cancel()

        logger.info("Workspace manager cleaned up")

    @tracer.trace("create_workspace")
    async def create_workspace(self, data: WorkspaceData) -> WorkspaceContext:
        """
        Create a new workspace and process its files.

        Args:
            data: WorkspaceData instance

        Returns:
            Created WorkspaceContext
        """
        try:
            logger.info("Creating workspace: {} - {}", data.id, data.title)

            # Create context
            context = context_manager.create_context(data)

            # Process evidence files in background
            if data.evidenceFiles:
                task = asyncio.create_task(
                    self._process_evidence_files(context, data.evidenceFiles)
                )
                self._processing_tasks[data.id] = task

            logger.success("Workspace created: {}", data.id)
            return context

        except Exception as e:
            logger.error("Failed to create workspace: {}", str(e))
            raise WorkspaceError(
                "Failed to create workspace",
                details={"workspace_id": data.id, "error": str(e)},
            )

    @tracer.trace("update_workspace")
    async def update_workspace(self, data: WorkspaceData) -> WorkspaceContext:
        """
        Update an existing workspace.

        Args:
            data: WorkspaceData instance

        Returns:
            Updated WorkspaceContext
        """
        try:
            logger.info("Updating workspace: {}", data.id)

            # Update context
            context = context_manager.update_context(data)

            # Check if new files were added
            existing_files = context.get_evidence_file_urls()
            new_files = [f for f in data.evidenceFiles if f.url not in existing_files]

            if new_files:
                logger.info("Processing {} new files", len(new_files))
                task = asyncio.create_task(
                    self._process_evidence_files(context, new_files)
                )
                self._processing_tasks[data.id] = task

            logger.success("Workspace updated: {}", data.id)
            return context

        except Exception as e:
            logger.error("Failed to update workspace: {}", str(e))
            raise WorkspaceError(
                "Failed to update workspace",
                details={"workspace_id": data.id, "error": str(e)},
            )

    @tracer.trace("delete_workspace")
    async def delete_workspace(self, workspace_id: str) -> bool:
        """
        Delete a workspace.

        Args:
            workspace_id: Workspace identifier

        Returns:
            True if deleted successfully
        """
        try:
            logger.info("Deleting workspace: {}", workspace_id)

            # Cancel processing task if exists
            if workspace_id in self._processing_tasks:
                self._processing_tasks[workspace_id].cancel()
                del self._processing_tasks[workspace_id]

            # Delete from vector store
            await vector_store.delete_workspace(workspace_id)

            # Delete context
            context_manager.delete_context(workspace_id)

            # Delete local files
            workspace_dir = Path(settings.workspace_data_dir) / workspace_id
            if workspace_dir.exists():
                import shutil

                shutil.rmtree(workspace_dir)

            logger.success("Workspace deleted: {}", workspace_id)
            return True

        except Exception as e:
            logger.error("Failed to delete workspace: {}", str(e))
            raise WorkspaceError(
                "Failed to delete workspace",
                details={"workspace_id": workspace_id, "error": str(e)},
            )

    def get_workspace(self, workspace_id: str) -> WorkspaceContext:
        """
        Get workspace context.

        Args:
            workspace_id: Workspace identifier

        Returns:
            WorkspaceContext

        Raises:
            WorkspaceNotFoundError: If workspace not found
        """
        context = context_manager.get_context(workspace_id)
        if not context:
            raise WorkspaceNotFoundError(workspace_id)
        return context

    async def _process_evidence_files(
        self, context: WorkspaceContext, files: List[EvidenceFile]
    ) -> None:
        """
        Process evidence files for a workspace.

        Args:
            context: WorkspaceContext instance
            files: List of evidence files to process
        """
        logger.info(
            "Processing {} evidence files for workspace: {}",
            len(files),
            context.workspace_id,
        )

        for file in files:
            try:
                await self._process_single_file(context, file)
            except Exception as e:
                logger.error("Failed to process file {}: {}", file.filename, str(e))
                # Continue processing other files

        logger.success(
            "Completed processing files for workspace: {}", context.workspace_id
        )

    async def _process_single_file(
        self, context: WorkspaceContext, file: EvidenceFile
    ) -> None:
        """
        Process a single evidence file.

        Args:
            context: WorkspaceContext instance
            file: EvidenceFile to process
        """
        try:
            logger.debug("Processing file: {}", file.filename)

            # Download file
            file_content = await self._download_file(file.url)

            # Save locally
            await self._save_file_locally(
                context.workspace_id, file.filename, file_content
            )

            # Extract text content (for text files)
            if file.filename.endswith(".txt"):
                text_content = file_content.decode("utf-8")

                # Store in vector database
                await vector_store.add_document(
                    workspace_id=context.workspace_id,
                    document_id=file.id,
                    content=text_content,
                    metadata={
                        "filename": file.filename,
                        "url": file.url,
                        "uploaded_at": file.uploadedAt.isoformat(),
                    },
                )

                logger.debug("Stored file in vector database: {}", file.filename)

        except Exception as e:
            logger.error("Error processing file {}: {}", file.filename, str(e))
            raise FileProcessingError(
                f"Failed to process file: {file.filename}",
                details={"filename": file.filename, "error": str(e)},
            )

    async def _download_file(self, url: str) -> bytes:
        """
        Download file from URL.

        Args:
            url: File URL

        Returns:
            File content as bytes
        """
        if not self._http_client:
            raise WorkspaceError("HTTP client not initialized")

        try:
            # Remove the trailing quote if present (from the example data)
            clean_url = url.rstrip('"')

            logger.debug("Downloading file from {}", safe_url(clean_url))
            response = await self._http_client.get(clean_url)
            response.raise_for_status()
            return response.content

        except Exception as e:
            logger.error("Failed to download file from {}: {}", safe_url(url), str(e))
            raise FileProcessingError(
                "Failed to download file", details={"url": url, "error": str(e)}
            )

    async def _save_file_locally(
        self, workspace_id: str, filename: str, content: bytes
    ) -> Path:
        """
        Save file to local storage.

        Args:
            workspace_id: Workspace identifier
            filename: File name
            content: File content

        Returns:
            Path to saved file
        """
        workspace_dir = Path(settings.workspace_data_dir) / workspace_id
        workspace_dir.mkdir(parents=True, exist_ok=True)

        file_path = workspace_dir / filename

        async with aiofiles.open(file_path, "wb") as f:
            await f.write(content)

        logger.debug("Saved file locally: {}", file_path)
        return file_path


# Global workspace manager instance
workspace_manager = WorkspaceManager()
