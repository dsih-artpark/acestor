#!/usr/bin/env python3
"""
Test script for PDF extraction functionality.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

from acestor.production_pipeline.pdf_extraction import extract_dengue_surveillance_data


def test_pdf_extraction():
    """Test PDF extraction with sample dengue report."""

    # Set up paths
    pdf_path = Path("data/DR1-9-25.pdf")
    output_path = Path("data/extracted_dengue_data.csv")

    # Check if PDF exists
    if not pdf_path.exists():
        print(f"Error: PDF file not found at {pdf_path}")
        return 1

    # Load environment variables from .env file
    load_dotenv(Path(__file__).parent / ".env")

    # Get API key from environment
    api_key = os.getenv('ANTHROPIC_API_KEY')
    if not api_key:
        print("Error: ANTHROPIC_API_KEY not found in environment")
        print("Make sure it's set in the .env file")
        return 1

    try:
        print(f"Extracting tables from: {pdf_path}")
        print("This may take a moment...")

        # Extract data
        df = extract_dengue_surveillance_data(
            pdf_path=pdf_path,
            output_path=output_path,
            api_key=api_key
        )

        print("\nExtraction completed successfully!")
        print(f"Extracted {len(df)} rows and {len(df.columns)} columns")
        print(f"Data saved to: {output_path}")

        # Display preview
        print("\nData preview:")
        print("=" * 50)
        print(df.head())

        print("\nColumn names:")
        print(list(df.columns))

        if 'District' in df.columns:
            print(f"\nFound {df['District'].nunique()} unique districts")

        return 0

    except Exception as e:
        print(f"Error during extraction: {e}")
        return 1


if __name__ == "__main__":
    exit_code = test_pdf_extraction()
    exit(exit_code)
