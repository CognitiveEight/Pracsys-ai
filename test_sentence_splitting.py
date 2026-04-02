"""
Test sentence splitting fallback specifically.
Simulates AI returning a single long line with multiple sentences.
"""

import re


def test_sentence_splitting():
    """Test the sentence splitting logic used in description generation."""

    # Simulate AI returning a single long line with multiple sentences
    single_line = "Anonymous petitioner raises an age verification dispute in U16 cricket against BCCI and government officials. The letter alleges BCCI improperly prioritizes less accurate bone age tests over valid Indian passports, leading to player disqualifications. The petitioner requests an immediate review and honoring of passports as primary age proof. The document also mentions Karnataka State Cricket Association and various cricket authorities."

    print("=" * 80)
    print("Testing sentence splitting fallback")
    print("=" * 80)
    print(f"\nInput (single line): {len(single_line)} chars")
    print(f"Content: {single_line}\n")

    # Simulate the splitting logic from the code
    lines = [single_line.strip()]
    print(f"Initial lines: {len(lines)}")

    if len(lines) < 3 and lines:
        combined_text = " ".join(lines)
        print(f"Combined text length: {len(combined_text)} chars")

        # Split on sentence endings
        sentences = re.split(r"([.!?])\s+", combined_text)
        print(f"Split result: {len(sentences)} parts")

        # Reconstruct sentences with punctuation
        reconstructed = []
        for i in range(0, len(sentences) - 1, 2):
            if i + 1 < len(sentences):
                sentence = (sentences[i] + sentences[i + 1]).strip()
            else:
                sentence = sentences[i].strip()
            if sentence and len(sentence) > 8:
                if not re.search(r"[.!?]$", sentence):
                    sentence += "."
                reconstructed.append(sentence)

        print(f"\nReconstructed sentences: {len(reconstructed)}")
        for i, sent in enumerate(reconstructed, 1):
            print(f"  {i}. {sent[:80]}...")

        if len(reconstructed) >= 2:
            lines = reconstructed[:4]
            print(f"\n✅ SUCCESS: Split into {len(lines)} lines")
            result = "\n".join(lines)
            print(f"\nFinal result ({len(result)} chars):")
            print(result)
        else:
            print(f"\n❌ FAILED: Only {len(reconstructed)} sentences after splitting")
    else:
        print("No splitting needed")

    print("\n" + "=" * 80)


if __name__ == "__main__":
    test_sentence_splitting()
