"""
Readability scoring for documents.
Calculates how readable and well-structured a document is (0-100%).
"""
from typing import Dict, Any
import re
from loguru import logger


class ReadabilityScorer:
    """
    Calculate readability score for documents.
    
    Score factors:
    - Text extraction success (40%)
    - Content quality (30%)
    - Structure and formatting (20%)
    - Character/encoding quality (10%)
    """
    
    def calculate_score(
        self,
        extracted_text: str,
        metadata: Dict[str, Any],
        original_size: int
    ) -> Dict[str, Any]:
        """
        Calculate comprehensive readability score.
        
        Args:
            extracted_text: Extracted text content
            metadata: File metadata
            original_size: Original file size in bytes
            
        Returns:
            Dictionary with score and breakdown
        """
        logger.debug("Calculating readability score...")
        
        # If extraction failed
        if metadata.get("error"):
            return {
                "total_score": 0,
                "extraction_score": 0,
                "content_score": 0,
                "structure_score": 0,
                "quality_score": 0,
                "error": metadata["error"],
                "readable": False
            }
        
        # Calculate component scores
        extraction_score = self._score_extraction(extracted_text, metadata, original_size)
        content_score = self._score_content(extracted_text)
        structure_score = self._score_structure(extracted_text, metadata)
        quality_score = self._score_quality(extracted_text)
        
        # Weighted total score
        total_score = (
            extraction_score * 0.40 +
            content_score * 0.30 +
            structure_score * 0.20 +
            quality_score * 0.10
        )
        
        result = {
            "total_score": round(total_score, 2),
            "extraction_score": round(extraction_score, 2),
            "content_score": round(content_score, 2),
            "structure_score": round(structure_score, 2),
            "quality_score": round(quality_score, 2),
            "readable": total_score >= 50,
            "rating": self._get_rating(total_score),
            "details": {
                "character_count": len(extracted_text),
                "word_count": len(extracted_text.split()),
                "line_count": len(extracted_text.split('\n')),
                "format": metadata.get("format", "unknown")
            }
        }
        
        logger.success(
            "Readability score: {:.2f}% ({})",
            result["total_score"],
            result["rating"]
        )
        
        return result
    
    def _score_extraction(
        self,
        text: str,
        metadata: Dict[str, Any],
        original_size: int
    ) -> float:
        """
        Score text extraction success (0-100).
        
        Factors:
        - Text was extracted (base 50)
        - Text length vs file size ratio (up to 30)
        - Metadata completeness (up to 20)
        """
        if not text:
            return 0.0
        
        score = 50.0  # Base score for successful extraction
        
        # Text length ratio
        if original_size > 0:
            text_ratio = len(text) / original_size
            # Good ratio is 0.5-2.0 for text files
            if 0.5 <= text_ratio <= 2.0:
                score += 30
            elif 0.1 <= text_ratio < 0.5:
                score += 15
            elif text_ratio > 2.0:
                score += 20
        
        # Metadata completeness
        important_fields = ["character_count", "word_count", "format"]
        present_fields = sum(1 for field in important_fields if field in metadata)
        score += (present_fields / len(important_fields)) * 20
        
        return min(score, 100.0)
    
    def _score_content(self, text: str) -> float:
        """
        Score content quality (0-100).
        
        Factors:
        - Adequate length (up to 40)
        - Sentence structure (up to 30)
        - Vocabulary diversity (up to 30)
        """
        if not text:
            return 0.0
        
        score = 0.0
        words = text.split()
        word_count = len(words)
        
        # Length scoring
        if word_count >= 100:
            score += 40
        elif word_count >= 50:
            score += 30
        elif word_count >= 20:
            score += 20
        elif word_count >= 10:
            score += 10
        
        # Sentence structure (presence of punctuation)
        sentences = re.split(r'[.!?]+', text)
        if len(sentences) > 1:
            score += 30
        elif len(sentences) > 0 and len(text) > 50:
            score += 15
        
        # Vocabulary diversity (unique words ratio)
        if word_count > 0:
            unique_words = len(set(words))
            diversity_ratio = unique_words / word_count
            score += diversity_ratio * 30
        
        return min(score, 100.0)
    
    def _score_structure(self, text: str, metadata: Dict[str, Any]) -> float:
        """
        Score document structure (0-100).
        
        Factors:
        - Paragraph breaks (up to 40)
        - Headers/sections (up to 30)
        - Lists/tables (up to 30)
        """
        if not text:
            return 0.0
        
        score = 0.0
        
        # Paragraph breaks
        paragraphs = [p for p in text.split('\n\n') if p.strip()]
        if len(paragraphs) > 5:
            score += 40
        elif len(paragraphs) > 2:
            score += 30
        elif len(paragraphs) > 1:
            score += 20
        else:
            score += 10
        
        # Headers (markdown style or numbered)
        headers = re.findall(r'^#+\s+.+$|^\d+\.\s+.+$', text, re.MULTILINE)
        if len(headers) > 5:
            score += 30
        elif len(headers) > 2:
            score += 20
        elif len(headers) > 0:
            score += 10
        
        # Lists and tables
        has_lists = bool(re.search(r'^\s*[-*•]\s+', text, re.MULTILINE))
        has_tables = metadata.get("table_count", 0) > 0 or bool(re.search(r'\|.+\|', text))
        
        if has_lists and has_tables:
            score += 30
        elif has_lists or has_tables:
            score += 15
        
        return min(score, 100.0)
    
    def _score_quality(self, text: str) -> float:
        """
        Score character and encoding quality (0-100).
        
        Factors:
        - No encoding errors (40)
        - Reasonable special character ratio (30)
        - No excessive whitespace (30)
        """
        if not text:
            return 0.0
        
        score = 0.0
        
        # Encoding quality (no replacement characters)
        if '�' not in text and '\ufffd' not in text:
            score += 40
        else:
            replacement_ratio = (text.count('�') + text.count('\ufffd')) / len(text)
            score += 40 * (1 - replacement_ratio)
        
        # Special character ratio
        special_chars = re.findall(r'[^\w\s.,!?;:()\-\'"@#$%&*+=\[\]{}|\\/<>~`]', text)
        if len(text) > 0:
            special_ratio = len(special_chars) / len(text)
            if special_ratio < 0.05:  # Less than 5% special chars
                score += 30
            elif special_ratio < 0.10:
                score += 20
            elif special_ratio < 0.20:
                score += 10
        
        # Whitespace quality (not excessive)
        lines = text.split('\n')
        empty_lines = sum(1 for line in lines if not line.strip())
        if len(lines) > 0:
            empty_ratio = empty_lines / len(lines)
            if empty_ratio < 0.3:  # Less than 30% empty
                score += 30
            elif empty_ratio < 0.5:
                score += 20
            elif empty_ratio < 0.7:
                score += 10
        
        return min(score, 100.0)
    
    def _get_rating(self, score: float) -> str:
        """Get text rating based on score."""
        if score >= 90:
            return "Excellent"
        elif score >= 75:
            return "Good"
        elif score >= 60:
            return "Fair"
        elif score >= 40:
            return "Poor"
        else:
            return "Very Poor"


# Global scorer instance
readability_scorer = ReadabilityScorer()

