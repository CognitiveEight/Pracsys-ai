# SimplerToday

An intelligent workspace management system with Socket.IO integration, vector store capabilities, and AI-powered agents.

## 🚀 Features

- **Socket.IO Client**: Real-time connection to workspace server with automatic reconnection
- **Workspace Management**: Comprehensive workspace lifecycle management with file processing
- **Vector Store Integration**: Qdrant-based document storage and semantic search
- **AI Agents**: Extensible agent framework for intelligent interactions
- **Observability**: Built-in logging, audit trails, and distributed tracing
- **Security**: JWT-based authentication and authorization
- **Async/Await**: Fully asynchronous architecture for high performance

## 📁 Project Structure

```
simplertoday/
├── app/
│   ├── main.py                 # Application entry point
│   ├── socket/                 # Socket.IO client
│   │   ├── client.py           # Socket client implementation
│   │   ├── events.py           # Event handlers
│   │   └── schemas.py          # Pydantic schemas
│   ├── agents/                 # AI Agents
│   │   ├── base.py             # Base agent class
│   │   ├── router.py           # Agent routing logic
│   │   └── chat_agent.py       # Chat agent implementation
│   ├── workspace/              # Workspace management
│   │   ├── context.py          # Workspace context
│   │   └── manager.py          # Workspace manager
│   ├── vectorstore/            # Vector database
│   │   ├── qdrant.py           # Qdrant integration
│   │   └── memory.py           # Conversation memory
│   ├── observability/          # Monitoring & logging
│   │   ├── logger.py           # Logging configuration
│   │   ├── audit.py            # Audit logging
│   │   └── trace.py            # Distributed tracing
│   ├── core/                   # Core functionality
│   │   ├── config.py           # Configuration management
│   │   ├── lifecycle.py        # Application lifecycle
│   │   └── security.py         # Security utilities
│   └── utils/                  # Utilities
│       └── exceptions.py       # Custom exceptions
├── requirements.txt            # Python dependencies
├── .env.example               # Environment variables template
└── README.md                  # This file
```

## 🛠️ Installation

### Prerequisites

- Python 3.11 or higher
- Qdrant (for vector store)
- Redis (optional, for caching)

### Setup

1. **Clone the repository**:
   ```bash
   cd /media/abhishek/Data1/simplertoday
   ```

2. **Create virtual environment**:
   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   ```

3. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

4. **Configure environment variables**:
   ```bash
   cp .env.example .env
   # Edit .env with your configuration
   ```

5. **Required Environment Variables**:
   - `SOCKET_URL`: Your Socket.IO server URL
   - `SECRET_KEY`: Secret key for JWT tokens
   - `OPENAI_API_KEY`: OpenAI API key (optional, for LLM integration)
   - `QDRANT_URL`: Qdrant server URL (default: http://localhost:6333)

## 🚦 Running the Application

### Start Qdrant (if running locally)

```bash
docker run -p 6333:6333 qdrant/qdrant
```

### Run the application

```bash
python -m app.main
```

Or with uvicorn (if using FastAPI endpoint in future):

```bash
uvicorn app.main:app --reload
```

## 📡 Socket.IO Integration

The application connects to a Socket.IO server and subscribes to workspace events:

### Supported Events

- `workspace_created`: When a new workspace is created
- `workspace_updated`: When workspace data is updated
- `workspace_deleted`: When a workspace is deleted
- `chat_message`: When a chat message is received

### Example Socket Data

```json
{
  "status": true,
  "statusCode": 201,
  "message": "created successfully",
  "data": {
    "_id": "6968e67ef2fd67764c19f57f",
    "userId": "69566190ed0063e78c4df274",
    "title": "Hack",
    "workspaceRole": "PETITIONER",
    "status": "ACTIVE",
    "evidenceFiles": [
      {
        "filename": "Legal_Draft.txt",
        "url": "https://example.com/file.txt",
        "uploadedAt": "2026-01-15T13:07:10.543Z"
      }
    ]
  }
}
```

## 🤖 Agent System

The application includes an extensible agent framework:

### Current Agents

- **ChatAgent**: General conversation and query handling

### Adding New Agents

1. Create agent class extending `BaseAgent`
2. Implement `process()` method
3. Register in `AgentRouter`
4. Add to `AgentType` enum

Example:

```python
from app.agents.base import BaseAgent, AgentConfig, AgentResponse

class CustomAgent(BaseAgent):
    def __init__(self):
        config = AgentConfig(
            name="custom_agent",
            model="gpt-4",
            system_prompt="Your prompt here"
        )
        super().__init__(config)
    
    async def process(self, workspace_context, message, **kwargs):
        # Your implementation
        pass
```

## 🗄️ Vector Store

Documents are automatically processed and stored in Qdrant for semantic search:

- Text files are embedded and indexed
- Semantic search retrieves relevant context
- Workspace-scoped searches ensure data isolation

## 📊 Observability

### Logging

Logs are written to:
- Console (with colors)
- `logs/app.log` (all logs)
- `logs/errors.log` (errors only)
- `logs/structured.log` (JSON format)

### Audit Logging

Audit events are logged to:
- `logs/audit/audit.log`

Tracks:
- Workspace operations
- Agent interactions
- Security events
- System events

### Tracing

Use the `@tracer.trace()` decorator for performance monitoring:

```python
from app.observability.trace import tracer

@tracer.trace("my_operation")
async def my_function():
    # Your code
    pass
```

## 🔒 Security

- JWT-based authentication
- Password hashing with bcrypt
- Input sanitization
- Workspace access control
- Audit logging for compliance

## 🧪 Development

### Code Structure

- **Async/Await**: All I/O operations are asynchronous
- **Type Hints**: Full type annotations for better IDE support
- **Pydantic**: Schema validation for all data structures
- **Loguru**: Beautiful and powerful logging
- **Modular Design**: Clear separation of concerns

### Best Practices

1. Use type hints for all functions
2. Log important events with appropriate levels
3. Handle exceptions gracefully
4. Document complex logic
5. Use async/await consistently

## 📝 Configuration

All configuration is managed through environment variables and `app/core/config.py`:

```python
from app.core.config import settings

# Access configuration
socket_url = settings.socket_url
debug_mode = settings.debug
```

## 🔄 Lifecycle Management

The application handles graceful startup and shutdown:

- Initializes all components
- Connects to external services
- Handles shutdown signals (SIGTERM, SIGINT)
- Cleans up resources properly

## 📚 Next Steps

1. **LLM Integration**: Connect chat agent to OpenAI/Anthropic
2. **Additional Agents**: Implement document analysis, legal research agents
3. **REST API**: Add FastAPI endpoints for HTTP access
4. **WebSocket Server**: Add WebSocket support for direct client connections
5. **Testing**: Add unit and integration tests
6. **Deployment**: Containerize with Docker

## 🐛 Troubleshooting

### Socket Connection Issues

- Verify `SOCKET_URL` is correct
- Check network connectivity
- Ensure Socket.IO server is running

### Vector Store Issues

- Ensure Qdrant is running
- Verify `QDRANT_URL` configuration
- Check Qdrant logs for errors

### Import Errors

- Activate virtual environment
- Reinstall dependencies: `pip install -r requirements.txt`

## 📄 License

[Your License Here]

## 👥 Contributors

[Your Team Information]

## 📧 Support

For issues and questions, please create an issue on GitHub or contact the development team.

---

**Built with ❤️ for intelligent workspace management**
