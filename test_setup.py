"""
Test script to verify SimplerToday setup and configuration.
"""

import importlib.util
import sys
from pathlib import Path
from typing import List, Tuple


def test_python_version() -> Tuple[bool, str]:
    """Test if Python version is compatible."""
    version = sys.version_info
    if version.major == 3 and version.minor >= 11:
        return True, f"✅ Python {version.major}.{version.minor}.{version.micro}"
    return False, f"❌ Python {version.major}.{version.minor} (requires 3.11+)"


def test_imports() -> List[Tuple[bool, str]]:
    """Test if all required packages can be imported."""
    packages = [
        "pydantic",
        "pydantic_settings",
        "socketio",
        "aiohttp",
        "qdrant_client",
        "loguru",
        "jose",
        "passlib",
        "httpx",
        "aiofiles",
    ]

    results = []
    for package in packages:
        try:
            # Import the package directly without replacing underscores
            importlib.import_module(package)
            results.append((True, f"✅ {package}"))
        except ImportError:
            results.append((False, f"❌ {package} (not installed)"))

    return results


def test_project_structure() -> List[Tuple[bool, str]]:
    """Test if all required files and directories exist."""
    base_path = Path(__file__).parent

    required_items = [
        ("app/", "directory"),
        ("app/main.py", "file"),
        ("app/socket/client.py", "file"),
        ("app/agents/base.py", "file"),
        ("app/workspace/manager.py", "file"),
        ("app/vectorstore/qdrant.py", "file"),
        ("app/observability/logger.py", "file"),
        ("app/core/config.py", "file"),
        ("requirements.txt", "file"),
        ("env.example", "file"),
    ]

    results = []
    for item, item_type in required_items:
        path = base_path / item
        if item_type == "directory":
            exists = path.is_dir()
        else:
            exists = path.is_file()

        if exists:
            results.append((True, f"✅ {item}"))
        else:
            results.append((False, f"❌ {item} (missing)"))

    return results


def test_app_imports() -> List[Tuple[bool, str]]:
    """Test if application modules can be imported."""
    modules = [
        "app.core.config",
        "app.socket.client",
        "app.agents.base",
        "app.workspace.manager",
        "app.vectorstore.qdrant",
        "app.observability.logger",
    ]

    results = []
    for module in modules:
        try:
            importlib.import_module(module)
            results.append((True, f"✅ {module}"))
        except Exception as e:
            results.append((False, f"❌ {module} ({str(e)[:50]})"))

    return results


def test_env_file() -> Tuple[bool, str]:
    """Test if .env file exists."""
    env_path = Path(__file__).parent / ".env"
    if env_path.exists():
        return True, "✅ .env file exists"
    return False, "⚠️  .env file not found (copy from env.example)"


def main():
    """Run all tests and display results."""
    print("=" * 60)
    print("SimplerToday Setup Test")
    print("=" * 60)

    # Test Python version
    print("\n📦 Python Version:")
    success, msg = test_python_version()
    print(f"  {msg}")

    # Test package imports
    print("\n📦 Required Packages:")
    import_results = test_imports()
    for success, msg in import_results:
        print(f"  {msg}")

    # Test project structure
    print("\n📁 Project Structure:")
    structure_results = test_project_structure()
    for success, msg in structure_results:
        print(f"  {msg}")

    # Test .env file
    print("\n⚙️  Configuration:")
    success, msg = test_env_file()
    print(f"  {msg}")

    # Test app imports
    print("\n🐍 Application Modules:")
    app_import_results = test_app_imports()
    for success, msg in app_import_results:
        print(f"  {msg}")

    # Summary
    print("\n" + "=" * 60)
    all_results = (
        [test_python_version()]
        + import_results
        + structure_results
        + [test_env_file()]
        + app_import_results
    )

    total = len(all_results)
    passed = sum(1 for success, _ in all_results if success)
    failed = total - passed

    print(f"Results: {passed}/{total} passed")

    if failed > 0:
        print(f"⚠️  {failed} tests failed. Please fix the issues above.")
        return 1
    else:
        print("✅ All tests passed! You're ready to run the application.")
        print("\nNext steps:")
        print("  1. Ensure .env is configured with your settings")
        print("  2. Start services: docker-compose up -d")
        print("  3. Run the app: ./run.sh or python -m app.main")
        return 0


if __name__ == "__main__":
    sys.exit(main())
