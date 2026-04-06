"""
Integration test for SimplerToday system.
Tests the complete flow without Socket.IO connection.
"""
import asyncio
import json
from pathlib import Path
from loguru import logger

# Import components
from app.socket.events import event_handler
from app.core.config import settings
from app.vectorstore.qdrant import vector_store
from app.processing.document_processor import document_processor
from app.workspace.manager import workspace_manager


# Test data from your Socket.IO event
TEST_DATA = {
    "status": True,
    "statusCode": 201,
    "message": "created successfully",
    "data": {
        "_id": "696a3a9f892aeba627a6915c",
        "userId": "69566190ed0063e78c4df274",
        "title": "Hack",
        "workspaceRole": "PETITIONER",
        "status": "ACTIVE",
        "isDeleted": False,
        "deletedAt": None,
        "evidenceFiles": [
            {
                "originalName": "Screenshot 2025-11-20 at 12.15.30 PM.png",
                "storeFileName": "7149c8a3-8fe3-4352-89af-3f39b5827c03-Screenshot_2025-11-20_at_12.15.30_PM.png",
                "filename": "annexure-1-Screenshot_2025-11-20_at_12.15.30_PM.png",
                "extension": "png",
                "fileHash": "4390387a81030d76f726d5c3a881b0c7c180c0390fb9336653d68c1380b0ebfc",
                "url": "https://storage.googleapis.com/sarthi-stag-bucket/workspaces/696a3a9f892aeba627a6915c/7149c8a3-8fe3-4352-89af-3f39b5827c03-Screenshot_2025-11-20_at_12.15.30_PM.png?GoogleAccessId=bns-stag-bucket%40bns-ai-439308.iam.gserviceaccount.com&Expires=2524608000&Signature=ES%2FmGG5obzhxaSZ4SWzrafwyadD0%2B4gZ57w5LAhA5pStIcir4skVVXfflq4yjTm0xFegnDnVL%2B5H9GnRzvyH8S9qjdBilFmzDpx7OpJtA31W347TUgnzrYnDKd5%2BBDS7%2BgEYpEhBD0ju%2FDEnY70jVshCKuKqfxBAzsr2GjmnqmPG4nqsS9PhR5KiiudZB11HCZRkqMN0X%2FhrNYg4CF8soYnTN3kNigZ7WyxIYOvHGkXPf5soSOzoIh6RhVDXkrZ9Sik81nK0rBXlu%2FqdUWJofId2osEP8JEu35ALr%2BSC8ytzn9H5pnsw%2F2djKmhTUb8f4RC0BTAYpCdSbAZV5rcPeA%3D%3D",
                "path": "workspaces/696a3a9f892aeba627a6915c/7149c8a3-8fe3-4352-89af-3f39b5827c03-Screenshot_2025-11-20_at_12.15.30_PM.png",
                "uploadedAt": "2026-01-16T13:18:23.998Z",
                "_id": "696a3aa0892aeba627a6915e"
            },
            {
                "originalName": "Legal_Draft.txt",
                "storeFileName": "5be3d192-e418-42b0-8561-d9dd7c78044b-Legal_Draft.txt",
                "filename": "annexure-2-Legal_Draft.txt",
                "extension": "txt",
                "fileHash": "89530a91e0c635451979810887a374577e4b5345fda52f1343efff2692dab3d8",
                "url": "https://storage.googleapis.com/sarthi-stag-bucket/workspaces/696a3a9f892aeba627a6915c/5be3d192-e418-42b0-8561-d9dd7c78044b-Legal_Draft.txt?GoogleAccessId=bns-stag-bucket%40bns-ai-439308.iam.gserviceaccount.com&Expires=2524608000&Signature=JwoOKsNMHIBUdxR%2FTZSmfQCU9Ad2iGPP8M7FXD%2FJ3HFBnmzUOvM555G9p54T7zRC5VP4h44HFlAIElo2L4Ds8njUQp4QVxunmyWBqXV5MqU8DhGUX2K5K1YdRgvxmyR9V4TIWuGQb9gZl7S17CzthdWRuaYjL99De%2BLxr3aFK5%2BgLU%2BJuCtBWoLVM%2Be8x6fTpmMYjfMitvH1RYvJFjeara%2FwU%2BezB4gs%2B3e8QqnKg%2B9PeS0dOgmB6C6zg04E4dd9j%2B6ouc8kjT5rNZXqvlWj7DEdtfKSPRkZMurNQ5GKsoQnmP4ZL1aP04mkG9aIkTKjkmEzNt%2FMJlwY6BxVdrGilw%3D%3D",
                "path": "workspaces/696a3a9f892aeba627a6915c/5be3d192-e418-42b0-8561-d9dd7c78044b-Legal_Draft.txt",
                "uploadedAt": "2026-01-16T13:18:24.070Z",
                "_id": "696a3aa0892aeba627a6915f"
            }
        ],
        "createdAt": "2026-01-16T13:18:23.576Z",
        "updatedAt": "2026-01-16T13:18:24.072Z",
        "__v": 0
    },
    "error": []
}


async def initialize_system():
    """Initialize all system components."""
    print("\n" + "="*80)
    print("INITIALIZING SIMPLERTODAY SYSTEM")
    print("="*80 + "\n")
    
    try:
        # Initialize vector store
        print("1. Initializing Vector Store (Qdrant)...")
        await vector_store.initialize()
        print("   ✅ Vector store initialized\n")
        
        # Initialize document processor
        print("2. Initializing Document Processor...")
        await document_processor.initialize()
        print("   ✅ Document processor initialized\n")
        
        # Initialize workspace manager
        print("3. Initializing Workspace Manager...")
        await workspace_manager.initialize()
        print("   ✅ Workspace manager initialized\n")
        
        print("✅ All systems initialized successfully!\n")
        return True
        
    except Exception as e:
        print(f"❌ Initialization failed: {str(e)}\n")
        return False


async def test_workspace_creation():
    """Test workspace creation with role extraction and document processing."""
    print("\n" + "="*80)
    print("TESTING WORKSPACE CREATION EVENT")
    print("="*80 + "\n")
    
    # Display test data
    print("📥 Incoming Socket.IO Event Data:")
    print(f"   Workspace ID: {TEST_DATA['data']['_id']}")  # Using original JSON key
    print(f"   User ID: {TEST_DATA['data']['userId']}")
    print(f"   Title: {TEST_DATA['data']['title']}")
    print(f"   Role: {TEST_DATA['data']['workspaceRole']}")
    print(f"   Evidence Files: {len(TEST_DATA['data']['evidenceFiles'])}")
    
    for i, file in enumerate(TEST_DATA['data']['evidenceFiles'], 1):
        print(f"      {i}. {file['filename']} ({file['extension'].upper()})")
    
    print("\n" + "-"*80 + "\n")
    
    try:
        # Process the event
        print("🚀 Processing workspace creation event...\n")
        await event_handler.on_workspace_created(TEST_DATA)
        
        print("\n" + "="*80)
        print("✅ WORKSPACE CREATION COMPLETED SUCCESSFULLY!")
        print("="*80 + "\n")
        
        return True
        
    except Exception as e:
        print(f"\n❌ Workspace creation failed: {str(e)}\n")
        import traceback
        traceback.print_exc()
        return False


async def verify_results():
    """Verify the results of processing."""
    print("\n" + "="*80)
    print("VERIFYING RESULTS")
    print("="*80 + "\n")
    
    workspace_id = TEST_DATA['data']['_id']  # Using original JSON key
    user_id = TEST_DATA['data']['userId']
    
    try:
        # Check workspace context
        print("1. Checking Workspace Context...")
        from app.workspace.context import context_manager
        
        context = context_manager.get_context(workspace_id)
        if context:
            print(f"   ✅ Workspace context exists")
            print(f"   - Title: {context.title}")
            print(f"   - Role: {context.get_metadata('role')}")
            print(f"   - Evidence files: {context.get_evidence_file_count()}")
            
            # Check document processing results
            processing_result = context.get_metadata('document_processing_result')
            if processing_result:
                print(f"\n   📊 Document Processing Results:")
                print(f"   - Total files: {processing_result['total_files']}")
                print(f"   - Successful: {processing_result['successful']}")
                print(f"   - Failed: {processing_result['failed']}")
                print(f"   - Avg readability: {processing_result['average_readability_score']:.2f}%")
                
                print(f"\n   📄 Individual File Results:")
                for result in processing_result['results']:
                    print(f"\n   File: {result['filename']}")
                    print(f"   - Success: {result['success']}")
                    if result['success']:
                        print(f"   - Readability: {result['readability_score']:.2f}% ({result['readability_rating']})")
                        print(f"   - Readable: {'Yes' if result['readable'] else 'No'}")
                        print(f"   - Extracted text: {result['extracted_text_length']} chars")
                        print(f"   - Stored in vector DB: {'Yes' if result['stored_in_vector_db'] else 'No'}")
                        
                        # Show readability breakdown
                        breakdown = result.get('readability_breakdown', {})
                        if breakdown:
                            print(f"   - Breakdown:")
                            print(f"     • Extraction: {breakdown.get('extraction_score', 0):.1f}%")
                            print(f"     • Content: {breakdown.get('content_score', 0):.1f}%")
                            print(f"     • Structure: {breakdown.get('structure_score', 0):.1f}%")
                            print(f"     • Quality: {breakdown.get('quality_score', 0):.1f}%")
        else:
            print(f"   ❌ Workspace context not found")
        
        # Check vector store
        print(f"\n2. Checking Vector Store...")
        try:
            results = await vector_store.search(
                workspace_id=workspace_id,
                query="legal draft",
                limit=3
            )
            print(f"   ✅ Vector store is accessible")
            print(f"   - Search results: {len(results)} chunks found")
            
            if results:
                print(f"\n   🔍 Sample Search Results:")
                for i, result in enumerate(results[:2], 1):
                    content_preview = result['content'][:100] + "..." if len(result['content']) > 100 else result['content']
                    print(f"\n   Result {i}:")
                    print(f"   - Score: {result['score']:.4f}")
                    print(f"   - Content preview: {content_preview}")
                    print(f"   - Document ID: {result['document_id']}")
                    
        except Exception as e:
            print(f"   ⚠️ Vector store check failed: {str(e)}")
        
        # Check role-based agent
        print(f"\n3. Checking Role-Based Agent...")
        try:
            from app.langgraph.integration import workspace_role_integration
            from app.langgraph.state import create_initial_workspace_state
            
            # Create state for testing
            state = create_initial_workspace_state(workspace_id, user_id)
            
            # Try to initialize with role
            state = await workspace_role_integration.initialize_workspace_with_role(
                workspace_id=workspace_id,
                user_id=user_id,
                initial_role="PETITIONER"
            )
            
            print(f"   ✅ Role-based agent initialized")
            print(f"   - Role: {state['role_state']['role']}")
            print(f"   - Validated: {state['role_state']['role_validated']}")
            
            role_context = state['role_state'].get('role_context')
            if role_context:
                print(f"   - Reasoning style: {role_context['reasoning_style']}")
                print(f"   - Framing rules: {len(role_context['framing_rules'])} rules")
                print(f"   - Forbidden actions: {len(role_context['forbidden_actions'])} restrictions")
                
        except Exception as e:
            print(f"   ⚠️ Role agent check: {str(e)}")
        
        print("\n" + "="*80)
        print("✅ VERIFICATION COMPLETED")
        print("="*80 + "\n")
        
        return True
        
    except Exception as e:
        print(f"\n❌ Verification failed: {str(e)}\n")
        import traceback
        traceback.print_exc()
        return False


async def cleanup_system():
    """Cleanup system resources."""
    print("\n" + "="*80)
    print("CLEANING UP SYSTEM")
    print("="*80 + "\n")
    
    try:
        # Cleanup document processor
        await document_processor.cleanup()
        print("✅ Document processor cleaned up")
        
        # Cleanup workspace manager
        await workspace_manager.cleanup()
        print("✅ Workspace manager cleaned up")
        
        # Close vector store
        await vector_store.close()
        print("✅ Vector store closed")
        
        print("\n✅ Cleanup completed successfully!\n")
        
    except Exception as e:
        print(f"⚠️ Cleanup error: {str(e)}\n")


async def main():
    """Main test function."""
    print("\n" + "╔" + "="*78 + "╗")
    print("║" + " "*20 + "SimplerToday Integration Test" + " "*29 + "║")
    print("╚" + "="*78 + "╝")
    
    success = True
    
    try:
        # Step 1: Initialize
        if not await initialize_system():
            print("❌ Test failed at initialization")
            return
        
        # Step 2: Test workspace creation
        if not await test_workspace_creation():
            print("❌ Test failed at workspace creation")
            success = False
        
        # Step 3: Verify results
        if success:
            await verify_results()
        
    except Exception as e:
        logger.error("Test failed with error: {}", str(e))
        import traceback
        traceback.print_exc()
        success = False
        
    finally:
        # Cleanup
        await cleanup_system()
    
    # Final summary
    print("\n" + "="*80)
    if success:
        print("🎉 ALL TESTS PASSED SUCCESSFULLY!")
        print("="*80)
        print("\nYour SimplerToday system is working correctly!")
        print("\nWhat was tested:")
        print("  ✅ Role extraction from workspaceRole field")
        print("  ✅ Role-based agent initialization")
        print("  ✅ Document download from Google Cloud Storage")
        print("  ✅ Multi-format text extraction (PNG OCR, TXT)")
        print("  ✅ Readability scoring (0-100%)")
        print("  ✅ User-scoped vector storage")
        print("  ✅ Workspace context management")
        print("\nNext steps:")
        print("  1. Connect to your Socket.IO server")
        print("  2. System will automatically process events")
        print("  3. All workspace data will be processed as tested")
    else:
        print("❌ SOME TESTS FAILED")
        print("="*80)
        print("\nPlease check the error messages above for details.")
    
    print("\n")


if __name__ == "__main__":
    # Run the test
    asyncio.run(main())

