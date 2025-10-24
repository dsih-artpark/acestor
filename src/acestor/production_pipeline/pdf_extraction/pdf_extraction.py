"""
PDF table extraction module for disease surveillance data.

This module provides functionality to extract table data from scanned PDFs
using Claude API's vision capabilities for high-accuracy table recognition.
"""

import base64
import logging
import os
from io import BytesIO, StringIO
from pathlib import Path
from typing import Dict, List, Optional, Union

import pandas as pd

try:
    import anthropic
    from pdf2image import convert_from_path
    from PIL import Image

    DEPENDENCIES_AVAILABLE = True
except ImportError as e:
    DEPENDENCIES_AVAILABLE = False
    MISSING_DEPENDENCY = str(e)

logger = logging.getLogger(__name__)


class PDFTableExtractor:
    """Extract tables from scanned PDFs using Claude API."""

    def __init__(self, api_key: Optional[str] = None):
        """
        Initialize the PDF table extractor.

        Args:
            api_key: Anthropic API key. If None, will try to get from environment.
        """
        if not DEPENDENCIES_AVAILABLE:
            raise ImportError(f"Missing required dependencies: {MISSING_DEPENDENCY}")

        self.api_key = api_key or os.getenv("ANTHROPIC_API_KEY")
        if not self.api_key:
            raise ValueError("Anthropic API key is required. Set ANTHROPIC_API_KEY environment variable or pass api_key parameter.")

        self.client = anthropic.Anthropic(api_key=self.api_key)

    def _convert_pdf_to_images(self, pdf_path: Union[str, Path], dpi: int = 300) -> List[Image.Image]:
        """
        Convert PDF pages to images.

        Args:
            pdf_path: Path to the PDF file
            dpi: DPI for image conversion (higher = better quality)

        Returns:
            List of PIL Images
        """
        try:
            pages = convert_from_path(pdf_path, dpi=dpi)
            logger.info(f"Converted PDF to {len(pages)} images")
            return pages
        except Exception as e:
            logger.error(f"Error converting PDF to images: {e}")
            raise

    def _image_to_base64(self, image: Image.Image) -> str:
        """Convert PIL Image to base64 string with compression."""
        # Resize image if too large to stay under 5MB limit
        max_size = (1200, 1600)  # Max width x height
        if image.size[0] > max_size[0] or image.size[1] > max_size[1]:
            image.thumbnail(max_size, Image.Resampling.LANCZOS)
            logger.info(f"Resized image to {image.size}")

        buffered = BytesIO()
        # Use JPEG with compression for smaller file size
        image.save(buffered, format="PNG", quality=85, optimize=True)
        return base64.b64encode(buffered.getvalue()).decode()

    def _extract_table_from_image(self, image: Image.Image, extraction_prompt: str) -> pd.DataFrame:
        """
        Extract table data from a single image using Claude API.

        Args:
            image: PIL Image containing the table
            extraction_prompt: Prompt for table extraction (required)

        Returns:
            DataFrame with extracted table data
        """
        if extraction_prompt is None:
            raise ValueError("extraction_prompt is required")

        img_base64 = self._image_to_base64(image)

        try:
            response = self.client.messages.create(
                model="claude-3-7-sonnet-20250219",
                max_tokens=4000,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": img_base64}},
                            {"type": "text", "text": extraction_prompt},
                        ],
                    }
                ],
            )

            csv_data = response.content[0].text

            # Save raw Claude output for debugging
            debug_path = "logs/" + f"claude_output_{pd.Timestamp.now().strftime('%Y%m%d_%H%M%S')}.txt"
            with open(debug_path, "w") as f:
                f.write(csv_data)
            logger.info(f"Saved raw Claude output to {debug_path}")

            # Clean the response to get just the CSV data
            csv_data = csv_data.strip()
            if csv_data.startswith("```"):
                csv_data = "\n".join(csv_data.split("\n")[1:-1])

            # Parse CSV data into DataFrame
            df = pd.read_csv(StringIO(csv_data))
            logger.info(f"Extracted table with {len(df)} rows and {len(df.columns)} columns")
            return df

        except Exception as e:
            logger.error(f"Error extracting table from image: {e}")
            raise

    def extract_tables_from_pdf(
        self,
        pdf_path: Union[str, Path],
        extraction_prompt: str,
        output_path: Optional[Union[str, Path]] = None,
        combine_pages: bool = True,
    ) -> Union[pd.DataFrame, List[pd.DataFrame]]:
        """
        Extract all tables from a PDF file.

        Args:
            pdf_path: Path to the PDF file
            output_path: Optional path to save the extracted data as CSV
            extraction_prompt: Prompt for table extraction (required)
            combine_pages: If True, combine all pages into single DataFrame

        Returns:
            DataFrame or list of DataFrames with extracted table data
        """
        pdf_path = Path(pdf_path)
        if not pdf_path.exists():
            raise FileNotFoundError(f"PDF file not found: {pdf_path}")

        logger.info(f"Starting table extraction from: {pdf_path}")

        # Convert PDF to images
        images = self._convert_pdf_to_images(pdf_path)

        # Extract table from first page only (dengue data)
        all_tables = []
        if images:
            logger.info("Processing first page only (dengue data)")
            try:
                df = self._extract_table_from_image(images[0], extraction_prompt)
                if not df.empty:
                    df["page_number"] = 1
                    all_tables.append(df)
                else:
                    logger.warning("No table data extracted from first page")
            except Exception as e:
                logger.error(f"Failed to extract table from first page: {e}")
                raise

        if not all_tables:
            raise ValueError("No tables were successfully extracted from the PDF")

        # Combine or return separate tables
        if combine_pages and len(all_tables) > 1:
            # Remove page_number column before combining if all pages have same structure
            combined_df = pd.concat(all_tables, ignore_index=True)
            result = combined_df
            logger.info(f"Combined {len(all_tables)} tables into single DataFrame with {len(result)} rows")
        else:
            result = all_tables[0] if len(all_tables) == 1 else all_tables

        # Save to CSV if output path provided
        if output_path:
            output_path = Path(output_path)
            if isinstance(result, pd.DataFrame):
                result.to_csv(output_path, index=False)
                logger.info(f"Saved extracted data to: {output_path}")
            else:
                # Save multiple tables with page numbers
                for i, df in enumerate(result):
                    page_output = output_path.parent / f"{output_path.stem}_page_{i + 1}{output_path.suffix}"
                    df.to_csv(page_output, index=False)
                    logger.info(f"Saved page {i + 1} data to: {page_output}")

        return result


def extract_dengue_surveillance_data(
    pdf_path: Union[str, Path], output_path: Optional[Union[str, Path]] = None, api_key: Optional[str] = None
) -> pd.DataFrame:
    """
    Convenience function to extract dengue surveillance data from PDF reports.

    Args:
        pdf_path: Path to the dengue surveillance PDF report
        output_path: Optional path to save extracted data as CSV
        api_key: Anthropic API key

    Returns:
        DataFrame with dengue surveillance data
    """
    extractor = PDFTableExtractor(api_key=api_key)

    dengue_prompt = """
    Extract the dengue surveillance table data and output ONLY raw CSV data with no explanatory text,
    no markdown formatting, no code blocks, and no additional commentary.

    Use these exact column headers in this order:
    SL_NO,District,Total_No_of_Taluks_Blocks_in_the_District,Taluks,PHC,Villages_Houses,
    Population_of_affected_Villages,Total_Suspected_Cases,Total_Blood_samples_collected,
    Total_IgM_Mac_Elisa,Total_NS1_Antigen,Total_+ves,Total_Death,Daily_Suspected_Cases,
    Daily_Blood_samples_collected,Daily_IgM_Mac_Elisa,Daily_NS1_Antigen,Daily_Total_+ves,
    Daily_Death,Cumulative_Suspected_Cases,Cumulative_Blood_samples_collected,
    Cumulative_IgM_Mac_Elisa,Cumulative_NS1_Antigen,Cumulative_Total_+ves,Cumulative_Death

    Requirements:
    - Map the table columns to these exact headers
    - Preserve district names exactly as shown
    - For numeric columns, use 0 for empty/blank cells
    - Output ONLY the CSV data starting with the header row
    - No text before or after the CSV data
    """

    return extractor.extract_tables_from_pdf(
        pdf_path=pdf_path, extraction_prompt=dengue_prompt, output_path=output_path, combine_pages=True
    )


# Example usage for integration with existing pipeline
def integrate_with_pipeline(pdf_path: str, config: Dict) -> pd.DataFrame:
    """
    Integration function for the existing Acestor pipeline.

    Args:
        pdf_path: Path to PDF file
        config: Pipeline configuration dictionary

    Returns:
        Processed DataFrame ready for pipeline use
    """
    try:
        # Extract data from PDF
        df = extract_dengue_surveillance_data(pdf_path)

        # Apply any pipeline-specific transformations
        if "date_column" in config:
            # Add date column if specified in config
            df["date"] = config.get("report_date", pd.Timestamp.now())

        if "required_columns" in config:
            # Ensure required columns are present
            missing_cols = set(config["required_columns"]) - set(df.columns)
            if missing_cols:
                logger.warning(f"Missing required columns: {missing_cols}")

        logger.info(f"Successfully processed PDF data: {len(df)} records")
        return df

    except Exception as e:
        logger.error(f"Failed to integrate PDF data into pipeline: {e}")
        raise
