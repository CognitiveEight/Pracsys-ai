"""
Socket.IO event handlers with role-based agent integration.
"""
from typing import Dict, Any, Callable, Awaitable
from loguru import logger

from app.socket.schemas import SocketResponse, WorkspaceUpdateEvent
from app.workspace.manager import workspace_manager
from app.observability.audit import audit_logger
from app.utils.exceptions import SocketEventError
from app.langgraph.integration import workspace_role_integration
from app.agents.role_models import RoleEnum
from app.processing.document_processor import document_processor


EventHandler = Callable[[Dict[str, Any]], Awaitable[None]]


class SocketEventHandler:
    """Handles Socket.IO events and routes them to appropriate handlers."""
    
    def __init__(self):
        self._handlers: Dict[str, EventHandler] = {}
        self._register_default_handlers()
    
    def _register_default_handlers(self) -> None:
        """Register default event handlers."""
        self.register("connect", self.on_connect)
        self.register("disconnect", self.on_disconnect)
        self.register("workspace_created", self.on_workspace_created)
        self.register("workspace_updated", self.on_workspace_updated)
        self.register("workspace_deleted", self.on_workspace_deleted)
        self.register("chat_message", self.on_chat_message)
        self.register("error", self.on_error)
    
    def register(self, event_name: str, handler: EventHandler) -> None:
        """
        Register an event handler.
        
        Args:
            event_name: Name of the event
            handler: Async function to handle the event
        """
        self._handlers[event_name] = handler
        logger.debug("Registered handler for event: {}", event_name)
    
    async def handle(self, event_name: str, data: Any) -> None:
        """
        Handle an incoming event.
        
        Args:
            event_name: Name of the event
            data: Event data
        """
        handler = self._handlers.get(event_name)
        
        if handler:
            try:
                logger.debug("Handling event: {} with data: {}", event_name, data)
                await handler(data)
            except Exception as e:
                logger.error("Error handling event {}: {}", event_name, str(e))
                raise SocketEventError(
                    f"Failed to handle event: {event_name}",
                    details={"event": event_name, "error": str(e)}
                )
        else:
            logger.warning("No handler registered for event: {}", event_name)
    
    async def on_connect(self, data: Any) -> None:
        """Handle connection event."""
        logger.info("Socket connected")
        await audit_logger.log_event(
            "socket_connect",
            {"status": "connected"}
        )
    
    async def on_disconnect(self, data: Any) -> None:
        """Handle disconnection event."""
        logger.info("Socket disconnected")
        await audit_logger.log_event(
            "socket_disconnect",
            {"status": "disconnected"}
        )
    
    async def on_workspace_created(self, data: Dict[str, Any]) -> None:
        """
        Handle workspace creation event with role-based agent integration.
        
        Process flow:
        1. Extract role from workspaceRole
        2. Initialize role-based agent with the role
        3. Download and process evidence files
        4. Calculate readability scores
        5. Store in vector database (user-scoped)
        
        Args:
            data: Workspace creation data
        """
        try:
            # Validate and parse the response
            response = SocketResponse(**data)
            workspace_data = response.data
            
            workspace_id = workspace_data.id  # Changed from _id to id
            user_id = workspace_data.userId
            
            logger.info(
                "Workspace created: {} - {} (User: {}, Role: {})",
                workspace_id,
                workspace_data.title,
                user_id,
                workspace_data.workspaceRole
            )
            
            # STEP 1: Extract and validate role
            workspace_role = workspace_data.workspaceRole
            
            # Map workspaceRole to our Role enum
            if not RoleEnum.validate(workspace_role):
                logger.warning(
                    "Invalid workspace role '{}', defaulting to ADJUDICATOR",
                    workspace_role
                )
                workspace_role = "ADJUDICATOR"
            
            logger.info("Initializing workspace with role: {}", workspace_role)
            
            # STEP 2: Initialize role-based agent with role
            try:
                state = await workspace_role_integration.initialize_workspace_with_role(
                    workspace_id=workspace_id,
                    user_id=user_id,
                    initial_role=workspace_role
                )
                
                logger.success(
                    "✅ Role-based agent initialized for workspace {} with role {}",
                    workspace_id,
                    workspace_role
                )
                
            except Exception as role_error:
                logger.error("Failed to initialize role agent: {}", str(role_error))
                # Continue with workspace creation even if role init fails
            
            # STEP 3: Create workspace context
            context = await workspace_manager.create_workspace(workspace_data)
            
            # STEP 4: Process evidence files with readability scoring
            if workspace_data.evidenceFiles:
                logger.info(
                    "Processing {} evidence files for workspace {}",
                    len(workspace_data.evidenceFiles),
                    workspace_id
                )
                
                # Prepare files for batch processing
                files_to_process = [
                    {
                        "url": file.url,
                        "filename": file.filename,
                        "_id": file.id,  # Changed from file._id to file.id
                        "uploadedAt": file.uploadedAt
                    }
                    for file in workspace_data.evidenceFiles
                ]
                
                # Process files in batch
                batch_result = await document_processor.process_batch(
                    files=files_to_process,
                    workspace_id=workspace_id,
                    user_id=user_id
                )
                
                logger.success(
                    "✅ Document processing complete: {}/{} successful, avg readability: {:.2f}%",
                    batch_result["successful"],
                    batch_result["total_files"],
                    batch_result["average_readability_score"]
                )
                
                # Store batch result in context
                context.set_metadata("document_processing_result", batch_result)
            
            # STEP 5: Log comprehensive audit event
            await audit_logger.log_event(
                "workspace_created_with_role",
                {
                    "workspace_id": workspace_id,
                    "user_id": user_id,
                    "title": workspace_data.title,
                    "role": workspace_role,
                    "evidence_files_count": len(workspace_data.evidenceFiles),
                    "readability_avg": context.get_metadata("document_processing_result", {}).get("average_readability_score", 0)
                }
            )
            
            logger.success(
                "🎉 Workspace fully initialized: {} with role {} and {} documents processed",
                workspace_id,
                workspace_role,
                len(workspace_data.evidenceFiles)
            )
            
        except Exception as e:
            logger.error("Error processing workspace creation: {}", str(e))
            raise
    
    async def on_workspace_updated(self, data: Dict[str, Any]) -> None:
        """
        Handle workspace update event.
        
        Args:
            data: Workspace update data
        """
        try:
            response = SocketResponse(**data)
            workspace_data = response.data
            
            logger.info("Workspace updated: {}", workspace_data.id)
            
            # Update the workspace
            await workspace_manager.update_workspace(workspace_data)
            
            # Log audit event
            await audit_logger.log_event(
                "workspace_updated",
                {
                    "workspace_id": workspace_data.id,
                    "title": workspace_data.title
                }
            )
            
        except Exception as e:
            logger.error("Error processing workspace update: {}", str(e))
            raise
    
    async def on_workspace_deleted(self, data: Dict[str, Any]) -> None:
        """
        Handle workspace deletion event.
        
        Args:
            data: Workspace deletion data
        """
        try:
            workspace_id = data.get("workspace_id")
            
            logger.info("Workspace deleted: {}", workspace_id)
            
            # Delete the workspace
            await workspace_manager.delete_workspace(workspace_id)
            
            # Log audit event
            await audit_logger.log_event(
                "workspace_deleted",
                {"workspace_id": workspace_id}
            )
            
        except Exception as e:
            logger.error("Error processing workspace deletion: {}", str(e))
            raise
    
    async def on_chat_message(self, data: Dict[str, Any]) -> None:
        """
        Handle chat message event.
        
        Args:
            data: Chat message data
        """
        try:
            logger.debug("Received chat message: {}", data)
            
            # TODO: Route to appropriate agent
            workspace_id = data.get("workspace_id")
            message = data.get("message")
            
            logger.info("Processing chat message for workspace: {}", workspace_id)
            
            # Log audit event
            await audit_logger.log_event(
                "chat_message_received",
                {
                    "workspace_id": workspace_id,
                    "message_length": len(message) if message else 0
                }
            )
            
        except Exception as e:
            logger.error("Error processing chat message: {}", str(e))
            raise
    
    async def on_error(self, data: Any) -> None:
        """
        Handle error event.
        
        Args:
            data: Error data
        """
        logger.error("Socket error received: {}", data)
        await audit_logger.log_event(
            "socket_error",
            {"error": str(data)}
        )


# Global event handler instance
event_handler = SocketEventHandler()

