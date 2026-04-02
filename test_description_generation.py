"""
Test script to verify case description generation works correctly.
Tests the sentence splitting fallback when AI returns single-line responses.
"""

import asyncio
import os
import sys

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.socket.events import SocketEventHandler


async def test_description_generation():
    """Test that description generation handles single-line AI responses."""
    handler = SocketEventHandler()

    # Sample document content (similar to the BCCI case)
    test_content = """
    This is an anonymous letter to BCCI and government officials raising urgent concerns about age verification for the U16 cricket tournament. 
    It alleges that BCCI is improperly prioritizing less accurate bone age tests over valid Indian passports, leading to player disqualifications. 
    The letter requests an immediate review and honoring of passports as primary age proof. 
    The document also mentions Karnataka State Cricket Association and various cricket authorities.
    """

    print("=" * 80)
    print("Testing case description generation with sentence splitting fallback")
    print("=" * 80)
    print(f"\nInput document content length: {len(test_content)} chars")
    print(f"Content preview: {test_content[:200]}...\n")

    # Test main method
    print("Testing _generate_case_summary_from_documents...")
    result = await handler._generate_case_summary_from_documents(test_content)

    if result:
        lines = [ln.strip() for ln in result.split("\n") if ln.strip()]
        print(f"✅ SUCCESS: Generated description with {len(lines)} lines")
        print(f"Description:\n{result}\n")
        print(f"Line count: {len(lines)}")
        print(f"Total length: {len(result)} chars")

        # Validate
        if handler._is_valid_short_summary(result):
            print("✅ Validation passed")
        else:
            print("❌ Validation failed")

        if handler._is_generic_description(result):
            print("❌ Description is generic")
        else:
            print("✅ Description is case-specific")
    else:
        print("❌ FAILED: No description generated")
        print("Testing simple fallback...")
        simple_result = await handler._generate_case_summary_from_documents_simple(
            test_content
        )
        if simple_result:
            lines = [ln.strip() for ln in simple_result.split("\n") if ln.strip()]
            print(f"✅ Simple fallback succeeded: {len(lines)} lines")
            print(f"Description:\n{simple_result}\n")
        else:
            print("❌ Simple fallback also failed")

    print("\n" + "=" * 80)


if __name__ == "__main__":
    asyncio.run(test_description_generation())
