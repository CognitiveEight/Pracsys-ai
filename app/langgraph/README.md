# LangGraph Role-Based Main Agent

## 📁 Directory Structure

```
app/langgraph/
├── __init__.py              # Module exports
├── state.py                 # LangGraph state management
├── nodes.py                 # LangGraph nodes (gate, selection, context, validation)
├── role_agent.py            # Main RoleBasedAgent class & graph
├── guards.py                # Decorators and validation guards
├── integration.py           # Integration with workspace system
├── example_usage.py         # Complete usage examples
└── README.md                # This file
```

## 🎯 What This Module Does

This module implements the **Role-Based Main Agent** - the ROOT/ENTRY agent for SimplerToday's legal intelligence system.

### Key Responsibilities

1. **Enforce mandatory role selection** before ANY legal operations
2. **Persist role as workspace-level state** (not session-level)
3. **Condition all downstream agents** with role-specific behavior contracts
4. **Guarantee role-consistent reasoning** across entire workspace lifecycle

## 🎭 Supported Roles

- **ADJUDICATOR**: 100% neutral judicial perspective
- **PETITIONER**: Pro-petitioner advocacy
- **RESPONDENT**: Pro-respondent advocacy
- **MEDIATOR**: Neutral settlement-focused

## 🚨 The Hard Rule

```
NO ROLE = NO OPERATIONS

Without role selection:
  ❌ No legal analysis
  ❌ No document drafting
  ❌ No law mapping
  ❌ No timeline generation
  ❌ No chat reasoning
  
  ✅ ONLY: "Please confirm your role to begin intelligence..."
```

## 🏗️ Architecture

### LangGraph Flow

```
┌─────────────┐
│   START     │
└──────┬──────┘
       │
       ▼
┌─────────────┐
│ Role Gate   │ ◄── BLOCKING CHECKPOINT
└──────┬──────┘
       │
   ┌───┴────┐
   │        │
(blocked)(passed)
   │        │
   ▼        ▼
┌─────┐ ┌──────┐
│ Sel │ │ Ctx  │
└──┬──┘ └───┬──┘
   └────┬───┘
        ▼
   ┌─────────┐
   │Validate │
   └────┬────┘
        │
        ▼
      (END)
```

### Components

#### 1. State Management (`state.py`)

- **WorkspaceState**: Complete LangGraph state with role info
- **RoleState**: Role-specific state (role, context, history)
- Helper functions for state manipulation

#### 2. Nodes (`nodes.py`)

- **role_gate_node**: Absolute blocking checkpoint
- **role_selection_node**: Processes user role selection
- **role_context_builder_node**: Builds context for downstream
- **role_validation_node**: Runtime consistency checks

#### 3. Main Agent (`role_agent.py`)

- **RoleBasedAgent**: Main class with LangGraph
- **role_agent**: Global instance (ready to use)
- Helper functions for downstream integration

#### 4. Guards (`guards.py`)

- **@require_role**: Decorator for agent methods
- **RoleGuard**: Context manager for validation
- **validate_role_context**: Explicit validation
- **assert_role**: Role-specific assertions

#### 5. Integration (`integration.py`)

- **WorkspaceRoleIntegration**: Bridge with workspace system
- Initialization, role changes, message processing
- Sync with existing SimplerToday infrastructure

#### 6. Examples (`example_usage.py`)

- 5 complete working examples
- All usage patterns demonstrated
- Runnable test suite

## 🚀 Quick Start

### 1. Basic Usage

```python
from app.langgraph.role_agent import role_agent

# Compile
role_agent.compile()

# Invoke
state = await role_agent.invoke(
    workspace_id="ws_123",
    user_id="user_456",
    user_input="PETITIONER"
)
```

### 2. Protect Your Agents

```python
from app.langgraph.guards import require_role
from app.langgraph.state import WorkspaceState

class MyAgent:
    @require_role("my_operation")
    async def process(self, state: WorkspaceState):
        # Only runs if role selected
        role = state["role_state"]["role"]
        return f"Processing with {role}"
```

### 3. Get Role Context

```python
from app.langgraph.role_agent import get_role_context_for_downstream

role_context = get_role_context_for_downstream(state)
framing_rules = role_context["framing_rules"]
reasoning_style = role_context["reasoning_style"]
```

## 📚 Documentation

- **Full Documentation**: `/ROLE_AGENT_DOCUMENTATION.md`
- **Quick Start Guide**: `/ROLE_AGENT_QUICKSTART.md`
- **Implementation Summary**: `/LANGGRAPH_SUMMARY.md`

## 🧪 Testing

### Run Examples

```bash
python -m app.langgraph.example_usage
```

### Run Individual Example

```python
from app.langgraph.example_usage import example_basic_role_selection
import asyncio

asyncio.run(example_basic_role_selection())
```

## 🔗 Integration

### With Socket.IO

```python
from app.langgraph.integration import workspace_role_integration

@socket_handler
async def on_message(data):
    state = await workspace_role_integration.process_user_message(
        workspace_id=data["workspace_id"],
        user_id=data["user_id"],
        message=data["message"],
        state=get_state(data["workspace_id"])
    )
    
    await emit("response", {
        "message": state["agent_output"],
        "role": state["role_state"].get("role")
    })
```

### With Existing Agents

```python
from app.langgraph.guards import require_role

class ExistingAgent:
    @require_role("existing_operation")
    async def existing_method(self, state: WorkspaceState):
        # Now protected by role gate
        pass
```

## 🎓 Key Files to Understand

1. **`role_agent.py`**: Start here - main agent implementation
2. **`state.py`**: Understand the state structure
3. **`nodes.py`**: See how each node works
4. **`guards.py`**: Learn how to protect your agents
5. **`example_usage.py`**: See it in action

## 📊 Module Statistics

- **Python Files**: 7
- **Lines of Code**: ~1,900+
- **Nodes**: 4 (gate, selection, context, validation)
- **Guards**: 4 types
- **Roles**: 4 (strict enum)
- **Examples**: 5 complete workflows

## 🎯 Design Principles

1. **Fail-Fast**: Errors raise immediately, no silent failures
2. **Type-Safe**: Full type hints with TypedDict
3. **Auditable**: Every action logged
4. **Extensible**: Easy to add new agents and roles
5. **Non-Bypassable**: Architectural enforcement

## ⚠️ Important Notes

1. **Role is mandatory** - no exceptions, no bypasses
2. **Workspace-level** - role persists across sessions
3. **Role changes** - invalidate all derived intelligence
4. **Always use guards** - protect all downstream operations
5. **Check examples** - see patterns before implementing

## 🔮 Future Enhancements

Potential additions (not yet implemented):

- Multi-user role support
- Role-based document permissions
- Advanced role transition workflows
- Role-specific UI customization
- Custom role validators

## 📞 Support

- Check examples: `python -m app.langgraph.example_usage`
- Read docs: `ROLE_AGENT_DOCUMENTATION.md`
- Review code: All files heavily commented
- Check logs: `logs/audit/audit.log`

---

**Status**: ✅ Production-Ready

**Built with**: LangGraph, Python 3.11+, TypedDict

**Tested**: 5 working examples

**Ready for**: Integration and deployment

