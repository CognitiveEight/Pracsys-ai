"""
File handlers for processing different file types using GCP Vision OCR.
"""
import base64
import os
import tempfile
import subprocess
from io import BytesIO
from typing import Optional, Dict, Any
from loguru import logger

try:
    from google.cloud import vision
    from google.auth.exceptions import DefaultCredentialsError
    GCP_OCR_AVAILABLE = True
except ImportError:
    GCP_OCR_AVAILABLE = False
    logger.warning("Google Cloud Vision API not available")

try:
    import PyPDF2
    PDF_AVAILABLE = True
except ImportError:
    PDF_AVAILABLE = False
    logger.warning("PyPDF2 not available")

try:
    from pdf2image import convert_from_bytes
    PDF2IMAGE_AVAILABLE = True
except ImportError:
    PDF2IMAGE_AVAILABLE = False
    logger.warning("pdf2image not available")

try:
    from PIL import Image
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False
    logger.warning("PIL/Pillow not available")

# Max pixels per image for OCR (avoids DecompressionBomb and GCP Vision "Bad image data")
# PIL default is ~89M; GCP Vision has size limits; keep safe for memory and API
OCR_MAX_IMAGE_PIXELS = 20_000_000

try:
    from docx import Document
    DOCX_AVAILABLE = True
except ImportError:
    DOCX_AVAILABLE = False
    logger.warning("python-docx not available")

try:
    import docx2txt
    DOC_AVAILABLE = True
except ImportError:
    DOC_AVAILABLE = False
    logger.warning("docx2txt not available")


class FileProcessor:
    """Utility class to process different file types and extract text content."""
    
    @staticmethod
    def is_url(content: str) -> bool:
        """Check if content is a URL."""
        return content.startswith(('http://', 'https://'))
    
    @staticmethod
    def is_base64_image(content: str) -> bool:
        """Check if content is a base64 encoded image."""
        return content.startswith('data:image/') and ';base64,' in content
    
    @staticmethod
    def extract_text_from_image(image_data: bytes) -> str:
        """Extract text from image using GCP Vision API OCR."""
        if not GCP_OCR_AVAILABLE:
            logger.warning("GCP Vision API not available. Cannot extract text from image.")
            return "[Image content - GCP Vision API not available]"
        
        try:
            from app.core.config import settings
            import os
            from google.auth.api_key import Credentials as ApiKeyCredentials

            # Vision OCR uses API key only (no service-account credentials).
            api_key_value = (
                (getattr(settings, "google_cloud_vision_api_key", None) or "")
                or os.getenv("GOOGLE_CLOUD_VISION_API_KEY")
                or ""
            ).strip()
            if not api_key_value:
                logger.error("Google Vision API key not configured. Set GOOGLE_CLOUD_VISION_API_KEY.")
                return "[Error: Vision API key not configured (set GOOGLE_CLOUD_VISION_API_KEY)]"

            api_credentials = ApiKeyCredentials(api_key_value)
            client = vision.ImageAnnotatorClient(credentials=api_credentials)

            image = vision.Image(content=image_data)
            response = client.text_detection(image=image)
            if response.error.message:
                raise Exception(f"GCP Vision API error: {response.error.message}")

            texts = response.text_annotations
            if texts:
                extracted_text = texts[0].description
                logger.success(f"Extracted {len(extracted_text)} characters from image using API key")
                return extracted_text.strip()

            logger.warning("No text detected in image")
            return "[No text detected in image]"

        except Exception as e:
            logger.error(f"Error extracting text from image: {str(e)}")
            return f"[Error extracting text from image: {str(e)}]"

    # Service-account OCR auth helpers removed (API key only).

    @staticmethod
    def extract_text_from_pdf(pdf_data: bytes) -> str:
        """Extract text from PDF file using text extraction and OCR for image-based PDFs."""
        if not PDF_AVAILABLE:
            logger.warning("PDF processing library not available. Cannot extract text from PDF.")
            return "[PDF content - PDF processing not available]"
        
        try:
            # First try traditional text extraction
            pdf_file = BytesIO(pdf_data)
            pdf_reader = PyPDF2.PdfReader(pdf_file)
            text = ""
            
            for page in pdf_reader.pages:
                page_text = page.extract_text()
                if page_text:
                    text += page_text + "\n"
            
            # If we got substantial text, return it
            if text.strip() and len(text.strip()) > 50:
                logger.success(f"Extracted {len(text)} characters from PDF using text extraction")
                return text.strip()
            
            # If no text or very little text, try OCR on PDF pages
            if PDF2IMAGE_AVAILABLE and GCP_OCR_AVAILABLE:
                logger.info("Text extraction yielded minimal results, attempting OCR on PDF pages")
                return FileProcessor._extract_text_from_pdf_with_ocr(pdf_data)
            else:
                logger.warning("PDF appears to be image-based but pdf2image or GCP Vision API not available")
                return text.strip() if text.strip() else "[PDF appears to contain images but OCR not available]"
                
        except Exception as e:
            logger.error(f"Error extracting text from PDF: {str(e)}")
            return f"[Error extracting text from PDF: {str(e)}]"
    
    @staticmethod
    def _extract_text_from_pdf_with_ocr(pdf_data: bytes) -> str:
        """Extract text from image-based PDF using OCR."""
        try:
            resample = None
            old_max_pixels = None
            if PIL_AVAILABLE:
                resample = Image.LANCZOS if hasattr(Image, "LANCZOS") else Image.BICUBIC
                if hasattr(Image, "Resampling") and hasattr(Image.Resampling, "LANCZOS"):
                    resample = Image.Resampling.LANCZOS
                old_max_pixels = getattr(Image, "MAX_IMAGE_PIXELS", None)
                if old_max_pixels is not None:
                    Image.MAX_IMAGE_PIXELS = 200_000_000
            try:
                # Convert at 150 DPI to reduce size (default 200 can exceed limits)
                images = convert_from_bytes(pdf_data, dpi=150)
            finally:
                if PIL_AVAILABLE and old_max_pixels is not None:
                    Image.MAX_IMAGE_PIXELS = old_max_pixels

            extracted_texts = []

            for i, image in enumerate(images):
                logger.info(f"Processing PDF page {i+1}/{len(images)} with OCR")

                # Resize if image exceeds safe pixel count (avoids GCP Vision "Bad image data")
                if PIL_AVAILABLE and resample is not None and getattr(image, "width", None) and getattr(image, "height", None):
                    w, h = image.width, image.height
                    pixels = w * h
                    if pixels > OCR_MAX_IMAGE_PIXELS:
                        ratio = (OCR_MAX_IMAGE_PIXELS / pixels) ** 0.5
                        new_w = max(1, int(w * ratio))
                        new_h = max(1, int(h * ratio))
                        image = image.resize((new_w, new_h), resample)
                        logger.warning(
                            "PDF page %s image too large (%s pixels); resized to %sx%s for OCR",
                            i + 1, pixels, new_w, new_h,
                        )

                # Convert PIL image to bytes
                img_byte_arr = BytesIO()
                image.save(img_byte_arr, format='PNG')
                img_bytes = img_byte_arr.getvalue()

                # Extract text using GCP Vision API
                page_text = FileProcessor.extract_text_from_image(img_bytes)

                if page_text and not page_text.startswith("[Error") and not page_text.startswith("[No text"):
                    extracted_texts.append(f"--- Page {i+1} ---\n{page_text}")

            if extracted_texts:
                full_text = "\n\n".join(extracted_texts)
                logger.success(f"Extracted {len(full_text)} characters from PDF using OCR")
                return full_text
            else:
                return "[No text could be extracted from PDF images]"

        except Exception as e:
            logger.error(f"Error extracting text from PDF with OCR: {str(e)}")
            return f"[Error extracting text from PDF with OCR: {str(e)}]"
    
    @staticmethod
    def extract_text_from_doc(doc_data: bytes) -> str:
        """Extract text from legacy DOC file."""
        if not DOC_AVAILABLE:
            logger.warning("DOC processing library not available. Cannot extract text from DOC.")
            return "[DOC content - DOC processing not available]"
        
        try:
            # Save bytes to temporary file for docx2txt processing
            with tempfile.NamedTemporaryFile(suffix='.doc', delete=False) as temp_file:
                temp_file.write(doc_data)
                temp_file_path = temp_file.name
            
            try:
                # First try docx2txt
                try:
                    text = docx2txt.process(temp_file_path)
                    if text and text.strip():
                        logger.success(f"Extracted {len(text)} characters from DOC")
                        return text.strip()
                except Exception as e:
                    logger.warning(f"docx2txt failed: {str(e)}, trying alternative method")
                
                # Fallback: try using antiword command if available
                try:
                    result = subprocess.run(['antiword', temp_file_path], 
                                          capture_output=True, text=True, timeout=30)
                    if result.returncode == 0 and result.stdout.strip():
                        logger.success(f"Extracted {len(result.stdout)} characters from DOC using antiword")
                        return result.stdout.strip()
                except (subprocess.TimeoutExpired, subprocess.CalledProcessError, FileNotFoundError):
                    logger.warning("antiword command not available or failed")
                
                # Final fallback: try to extract as raw text (very basic)
                try:
                    with open(temp_file_path, 'rb') as f:
                        content = f.read()
                    
                    # Look for text patterns in the binary data
                    text_parts = []
                    i = 0
                    while i < len(content) - 1:
                        if content[i] >= 32 and content[i] <= 126:  # Printable ASCII
                            start = i
                            while i < len(content) and content[i] >= 32 and content[i] <= 126:
                                i += 1
                            if i - start > 3:  # Only consider sequences of 4+ characters
                                text_parts.append(content[start:i].decode('ascii', errors='ignore'))
                        i += 1
                    
                    if text_parts:
                        extracted_text = ' '.join(text_parts)
                        # Clean up the extracted text
                        cleaned_text = ' '.join(extracted_text.split())
                        if len(cleaned_text) > 10:  # Only return if we got substantial text
                            logger.success(f"Extracted {len(cleaned_text)} characters from DOC using raw extraction")
                            return cleaned_text
                    
                except Exception as e:
                    logger.warning(f"Raw text extraction failed: {str(e)}")
                
                return "[Could not extract text from DOC file - format may be corrupted or unsupported]"
                
            finally:
                # Clean up temporary file
                try:
                    os.unlink(temp_file_path)
                except:
                    pass
                
        except Exception as e:
            logger.error(f"Error extracting text from DOC: {str(e)}")
            return f"[Error extracting text from DOC: {str(e)}]"
    
    @staticmethod
    def extract_text_from_docx(docx_data: bytes) -> str:
        """Extract text from DOCX file."""
        if not DOCX_AVAILABLE:
            logger.warning("DOCX processing library not available. Cannot extract text from DOCX.")
            return "[DOCX content - DOCX processing not available]"
        
        try:
            docx_file = BytesIO(docx_data)
            doc = Document(docx_file)
            text = ""
            
            for paragraph in doc.paragraphs:
                text += paragraph.text + "\n"
            
            logger.success(f"Extracted {len(text)} characters from DOCX")
            return text.strip()
        except Exception as e:
            logger.error(f"Error extracting text from DOCX: {str(e)}")
            return f"[Error extracting text from DOCX: {str(e)}]"
    
    @staticmethod
    def extract_text_from_txt(txt_data: bytes) -> str:
        """Extract text from TXT file with encoding detection."""
        try:
            # Try UTF-8 first
            try:
                text = txt_data.decode('utf-8')
                logger.success(f"Extracted {len(text)} characters from TXT (UTF-8)")
                return text.strip()
            except UnicodeDecodeError:
                pass
            
            # Try other common encodings
            for encoding in ['latin-1', 'cp1252', 'iso-8859-1']:
                try:
                    text = txt_data.decode(encoding)
                    logger.success(f"Extracted {len(text)} characters from TXT ({encoding})")
                    return text.strip()
                except UnicodeDecodeError:
                    continue
            
            # Last resort: decode with errors='ignore'
            text = txt_data.decode('utf-8', errors='ignore')
            logger.warning(f"Extracted {len(text)} characters from TXT with encoding errors ignored")
            return text.strip()
            
        except Exception as e:
            logger.error(f"Error extracting text from TXT: {str(e)}")
            return f"[Error extracting text from TXT: {str(e)}]"
    
    @staticmethod
    def extract_text_from_md(md_data: bytes) -> str:
        """Extract text from Markdown file."""
        try:
            text = md_data.decode('utf-8', errors='ignore')
            logger.success(f"Extracted {len(text)} characters from Markdown")
            return text.strip()
        except Exception as e:
            logger.error(f"Error extracting text from Markdown: {str(e)}")
            return f"[Error extracting text from Markdown: {str(e)}]"
    
    @staticmethod
    def process_file_by_extension(file_data: bytes, filename: str) -> str:
        """
        Process file based on its extension.
        
        Args:
            file_data: File content as bytes
            filename: Name of the file with extension
            
        Returns:
            Extracted text content
        """
        filename_lower = filename.lower()
        
        # Image files
        if any(ext in filename_lower for ext in ['.jpg', '.jpeg', '.png', '.gif', '.bmp', '.tiff', '.webp']):
            logger.info(f"Processing image file: {filename}")
            return FileProcessor.extract_text_from_image(file_data)
        
        # PDF files
        elif filename_lower.endswith('.pdf'):
            logger.info(f"Processing PDF file: {filename}")
            return FileProcessor.extract_text_from_pdf(file_data)
        
        # DOCX files
        elif filename_lower.endswith('.docx'):
            logger.info(f"Processing DOCX file: {filename}")
            return FileProcessor.extract_text_from_docx(file_data)
        
        # DOC files
        elif filename_lower.endswith('.doc'):
            logger.info(f"Processing DOC file: {filename}")
            return FileProcessor.extract_text_from_doc(file_data)
        
        # Text files
        elif filename_lower.endswith('.txt'):
            logger.info(f"Processing TXT file: {filename}")
            return FileProcessor.extract_text_from_txt(file_data)
        
        # Markdown files
        elif filename_lower.endswith(('.md', '.markdown')):
            logger.info(f"Processing Markdown file: {filename}")
            return FileProcessor.extract_text_from_md(file_data)
        
        # Try to detect by content
        else:
            logger.info(f"Detecting file type by content for: {filename}")
            if file_data.startswith(b'%PDF'):
                return FileProcessor.extract_text_from_pdf(file_data)
            elif file_data.startswith(b'PK'):  # ZIP-based formats like DOCX
                return FileProcessor.extract_text_from_docx(file_data)
            elif file_data.startswith(b'\xd0\xcf\x11\xe0'):  # OLE2 format (DOC files)
                return FileProcessor.extract_text_from_doc(file_data)
            elif file_data.startswith((b'\xff\xd8\xff', b'\x89PNG', b'GIF8')):  # Image files
                return FileProcessor.extract_text_from_image(file_data)
            else:
                # Assume it's a text file
                logger.warning(f"Unknown file type, treating as text: {filename}")
                return FileProcessor.extract_text_from_txt(file_data)


# Global file processor instance
file_processor = FileProcessor()
