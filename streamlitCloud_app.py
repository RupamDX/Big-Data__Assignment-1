import streamlit as st
import os
import tempfile
import requests
import urllib.parse
from bs4 import BeautifulSoup
import pandas as pd

# For PDF: open-source
import fitz  # PyMuPDF
import pdfplumber
from tabula import read_pdf

# For PDF: enterprise (Adobe)
import logging
import json
import zipfile
import openpyxl
from datetime import datetime
import boto3
from botocore.exceptions import NoCredentialsError

# Adobe PDF Services SDK imports
from adobe.pdfservices.operation.auth.service_principal_credentials import ServicePrincipalCredentials
from adobe.pdfservices.operation.exception.exceptions import ServiceApiException, ServiceUsageException, SdkException
from adobe.pdfservices.operation.io.cloud_asset import CloudAsset
from adobe.pdfservices.operation.io.stream_asset import StreamAsset
from adobe.pdfservices.operation.pdf_services import PDFServices
from adobe.pdfservices.operation.pdf_services_media_type import PDFServicesMediaType
from adobe.pdfservices.operation.pdfjobs.jobs.extract_pdf_job import ExtractPDFJob
from adobe.pdfservices.operation.pdfjobs.params.extract_pdf.extract_element_type import ExtractElementType
from adobe.pdfservices.operation.pdfjobs.params.extract_pdf.extract_renditions_element_type import ExtractRenditionsElementType
from adobe.pdfservices.operation.pdfjobs.params.extract_pdf.extract_pdf_params import ExtractPDFParams
from adobe.pdfservices.operation.pdfjobs.result.extract_pdf_result import ExtractPDFResult

logging.basicConfig(level=logging.INFO)

################################################################################
#                         A) OPEN-SOURCE PDF EXTRACTION                         #
################################################################################

def extract_images_to_md(pdf_path):
    """
    Extract images from a PDF using PyMuPDF, upload them directly to S3,
    and return Markdown references to those S3 URLs.
    
    If S3 credentials are missing or the upload fails, we'll append a warning
    or skip the image. Adjust that behavior as desired.
    """
    import tempfile
    import os
    
    doc = fitz.open(pdf_path)
    md_images = "\n## Extracted Images\n"
    
    for page_num in range(len(doc)):
        page = doc[page_num]
        images = page.get_images(full=True)

        for img_index, img in enumerate(images):
            xref = img[0]
            base_image = doc.extract_image(xref)
            image_bytes = base_image["image"]
            image_ext = base_image["ext"]

            # 1) Write the image to a temporary file so we can upload to S3
            with tempfile.NamedTemporaryFile(delete=False, suffix=f".{image_ext}") as tmp_img:
                tmp_img.write(image_bytes)
                tmp_path = tmp_img.name

            # 2) Upload to S3 using your existing function
            s3_url = upload_file_to_s3(tmp_path)

            # 3) Remove the local temp file immediately
            os.remove(tmp_path)

            # 4) Append Markdown references
            if s3_url:
                # S3 upload succeeded, reference the remote URL
                md_images += f"![Image page {page_num+1} - {img_index+1}]({s3_url})\n"
            else:
                # If there's no S3 credentials or upload failed, you can either:
                # - Add a warning in your markdown
                # - Or skip the image
                md_images += f"\n**[Warning: Failed to upload image (page {page_num+1}, index {img_index+1}) to S3]**\n"

    doc.close()
    return md_images


def extract_text_to_md(pdf_path):
    """Extract text from PDF using pdfplumber and return Markdown text."""
    md_text = "\n## Extracted Text\n"
    with pdfplumber.open(pdf_path) as pdf:
        for page_num, page in enumerate(pdf.pages):
            page_text = page.extract_text() or ""
            md_text += f"\n### Page {page_num+1}\n```\n{page_text}\n```\n"
    return md_text

def extract_tables_to_md(pdf_path):
    """Extract tables using tabula and return Markdown text."""
    md_tables = "\n## Extracted Tables\n"
    try:
        tables = read_pdf(pdf_path, pages='all', multiple_tables=True)
        for i, table in enumerate(tables):
            md_tables += f"\n### Table {i+1}\n"
            md_tables += table.to_markdown(index=False)
            md_tables += "\n"
    except Exception as e:
        md_tables += f"\nFailed to process tables: {e}\n"
    return md_tables

def open_source_extract_pdf(pdf_path):
    """Combine open-source PDF extraction results into a single Markdown string."""
    md = f"# Extracted Content from {os.path.basename(pdf_path)}\n"
    md += extract_images_to_md(pdf_path)
    md += extract_text_to_md(pdf_path)
    md += extract_tables_to_md(pdf_path)
    return md

################################################################################
#                     B) ENTERPRISE (ADOBE) PDF EXTRACTION                     #
################################################################################

def enterprise_extract_pdf(pdf_path):
    """
    Uses Adobe PDF Services for PDF extraction.
    Fetches credentials from st.secrets["ADOBE_CREDENTIALS_JSON"].
    Returns Markdown string or None if something fails.
    """
    try:
        # Read Adobe credentials JSON from Streamlit secrets
        if "ADOBE_CREDENTIALS_JSON" not in st.secrets:
            raise ValueError("Adobe credentials not found in st.secrets['ADOBE_CREDENTIALS_JSON']")

        adobe_data = json.loads(st.secrets["ADOBE_CREDENTIALS_JSON"])
        client_id = adobe_data["client_credentials"]["client_id"]
        client_secret = adobe_data["client_credentials"]["client_secret"]

        # Initialize PDFServices
        credentials = ServicePrincipalCredentials(client_id=client_id, client_secret=client_secret)
        pdf_services = PDFServices(credentials=credentials)

        # Check PDF path
        if not os.path.exists(pdf_path):
            raise FileNotFoundError(f"PDF file not found: {pdf_path}")

        # Upload PDF
        with open(pdf_path, "rb") as f:
            input_stream = f.read()
        input_asset = pdf_services.upload(input_stream=input_stream, mime_type=PDFServicesMediaType.PDF)

        # Extraction params
        extract_params = ExtractPDFParams(
            elements_to_extract=[ExtractElementType.TEXT, ExtractElementType.TABLES],
            elements_to_extract_renditions=[ExtractRenditionsElementType.FIGURES, ExtractRenditionsElementType.TABLES]
        )

        job = ExtractPDFJob(input_asset=input_asset, extract_pdf_params=extract_params)

        # Submit job
        location = pdf_services.submit(job)
        pdf_services_response = pdf_services.get_job_result(location, ExtractPDFResult)

        # Download result (ZIP)
        result_asset: CloudAsset = pdf_services_response.get_result().get_resource()
        stream_asset: StreamAsset = pdf_services.get_content(result_asset)

        zip_path = create_adobe_zip_path()
        with open(zip_path, "wb") as out:
            out.write(stream_asset.get_input_stream())

        # Convert extracted data to Markdown
        md_output = adobe_zip_to_markdown(zip_path)
        return md_output

    except (ServiceApiException, ServiceUsageException, SdkException) as e:
        logging.exception(f"Adobe PDF Services error: {e}")
        return None
    except Exception as e:
        logging.exception(f"Unexpected error in enterprise_extract_pdf: {e}")
        return None

def create_adobe_zip_path():
    now_str = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    out_dir = "temp_adobe_extractions"
    os.makedirs(out_dir, exist_ok=True)
    return os.path.join(out_dir, f"extraction_{now_str}.zip")

def adobe_zip_to_markdown(zip_file_path):
    """Unzip Adobe extraction result, parse JSON & XLSX, build Markdown string."""
    out_dir = zip_file_path.replace(".zip", "")
    os.makedirs(out_dir, exist_ok=True)

    with zipfile.ZipFile(zip_file_path, "r") as zf:
        zf.extractall(out_dir)

    json_file = os.path.join(out_dir, "structuredData.json")
    if not os.path.exists(json_file):
        raise FileNotFoundError("structuredData.json not found in ZIP extract.")

    with open(json_file, "r", encoding="utf-8") as jf:
        data = json.load(jf)

    md_content = "# Extracted PDF Data (Enterprise)\n\n"

    # Parse text/tables
    for element in data.get("elements", []):
        if "Text" in element:
            md_content += f"## Text Element\n{element['Text']}\n\n"
        if "Table" in element:
            md_content += "## Table Element\n"
            rows = element["Table"].get("Rows", [])
            for i, row in enumerate(rows):
                md_content += "| " + " | ".join(row) + " |\n"
                if i == 0:
                    md_content += "| " + " | ".join(["-" * len(col) for col in row]) + " |\n"
            md_content += "\n"

    # Check XLSX-based tables
    tables_folder = os.path.join(out_dir, "tables")
    if os.path.exists(tables_folder):
        for tbl_file in sorted(os.listdir(tables_folder)):
            if tbl_file.endswith(".xlsx"):
                tbl_path = os.path.join(tables_folder, tbl_file)
                wb = openpyxl.load_workbook(tbl_path)
                sheet = wb.active
                md_content += f"## Table from {tbl_file}\n"
                for i, row in enumerate(sheet.iter_rows(values_only=True)):
                    row_data = [str(cell) if cell else "" for cell in row]
                    md_content += "| " + " | ".join(row_data) + " |\n"
                    if i == 0:
                        md_content += "| " + " | ".join(["-" * len(r) for r in row_data]) + " |\n"
                md_content += "\n"

    # Figures folder (images)
    figures_folder = os.path.join(out_dir, "figures")
    if os.path.exists(figures_folder):
        md_content += "## Images\n\n"
        for fig_file in sorted(os.listdir(figures_folder)):
            fig_path = os.path.join(figures_folder, fig_file)
            if os.path.isfile(fig_path):
                s3_url = upload_file_to_s3(fig_path)
                if s3_url:
                    md_content += f"![Figure]({s3_url})\n\n"
                else:
                    md_content += f"![Figure]({fig_path})\n\n"

    return md_content

def upload_file_to_s3(file_path):
    """
    Tries to upload the file to an S3 bucket using credentials from st.secrets.
    Returns the S3 URL or None if missing credentials/bucket or fails to upload.
    """
    bucket_name = st.secrets.get("S3_BUCKET_NAME", "")
    aws_key = st.secrets.get("AWS_ACCESS_KEY_ID", "")
    aws_secret = st.secrets.get("AWS_SECRET_ACCESS_KEY", "")
    aws_region = st.secrets.get("AWS_DEFAULT_REGION", "us-east-1")

    # If missing anything, skip
    if not (bucket_name and aws_key and aws_secret):
        logging.warning("Skipping S3 upload (missing credentials or bucket).")
        return None

    s3_client = boto3.client(
        's3',
        aws_access_key_id=aws_key,
        aws_secret_access_key=aws_secret,
        region_name=aws_region
    )

    object_key = os.path.basename(file_path)
    try:
        s3_client.upload_file(file_path, bucket_name, object_key)
        # Optionally set public-read ACL
        # s3_client.put_object_acl(ACL='public-read', Bucket=bucket_name, Key=object_key)
        return f"https://{bucket_name}.s3.amazonaws.com/{object_key}"

    except FileNotFoundError:
        logging.error(f"File not found: {file_path}")
    except NoCredentialsError:
        logging.error("AWS credentials not available.")
    except Exception as e:
        logging.exception(f"Failed to upload {file_path} to S3: {e}")
    return None


################################################################################
#                   C) OPEN-SOURCE WEBSITE EXTRACTION (BeautifulSoup)          #
################################################################################

def open_source_extract_website(url):
    """
    Scrape text, images, links, and tables from a website using requests + BeautifulSoup.
    Returns a Markdown string (rather than writing to file).
    """
    response = requests.get(url)
    response.raise_for_status()
    soup = BeautifulSoup(response.content, 'html.parser')

    # Extract text (paragraphs)
    paragraphs = soup.find_all('p')
    text_content = '\n'.join([p.get_text(strip=True) for p in paragraphs])

    # Extract images
    images = soup.find_all('img')
    image_urls = []
    for img in images:
        if 'src' in img.attrs:
            src = img['src']
            if src.startswith('//'):
                src = 'https:' + src
            elif not src.startswith(('http:', 'https:')):
                src = urllib.parse.urljoin(url, src)
            image_urls.append(src)

    # Extract links
    links = soup.find_all('a', href=True)
    link_urls = []
    for link in links:
        href = link['href']
        if not href.startswith(('http:', 'https:')):
            href = urllib.parse.urljoin(url, href)
        link_urls.append(href)

    # Extract tables
    found_tables = soup.find_all('table')
    extracted_tables = []
    for table in found_tables:
        rows = table.find_all('tr')
        table_data = []
        for row in rows:
            cols = row.find_all(['td', 'th'])
            table_data.append([col.get_text(strip=True) for col in cols])
        if table_data:
            df = pd.DataFrame(table_data)
            # First row as header if it looks like a header
            if len(df) > 1:
                df.columns = df.iloc[0]
                df = df[1:].reset_index(drop=True)
            extracted_tables.append(df)

    # Build the markdown
    md = f"# Extracted Content from {url}\n\n"
    md += "## Text Content\n\n"
    md += text_content + "\n\n"

    md += "## Images\n\n"
    for img_url in image_urls:
        md += f"![Image]({img_url})\n\n"

    md += "## Links\n\n"
    for lnk in link_urls:
        md += f"- [{lnk}]({lnk})\n"
    md += "\n"

    md += "## Tables\n\n"
    for idx, df_table in enumerate(extracted_tables, start=1):
        md += f"### Table {idx}\n\n"
        md += df_table.to_markdown(index=False) + "\n\n"

    return md

################################################################################
#             D) ENTERPRISE WEBSITE EXTRACTION (Diffbot example)               #
################################################################################

def enterprise_extract_website(url):
    """
    Uses Diffbot (enterprise example) to get text, images, tables from a webpage.
    Token is read from st.secrets["DIFFBOT_API_TOKEN"].
    Returns Markdown string or None if something fails.
    """
    diffbot_token = st.secrets.get("DIFFBOT_API_TOKEN", "")
    if not diffbot_token:
        logging.warning("Diffbot token not set in st.secrets['DIFFBOT_API_TOKEN'].")
        return None

    api_endpoint = f"https://api.diffbot.com/v3/article?token={diffbot_token}&url={url}"
    resp = requests.get(api_endpoint)
    if resp.status_code != 200:
        logging.error(f"Diffbot request failed: {resp.status_code}")
        return None

    data = resp.json()
    objects = data.get("objects", [])

    markdown = ""
    for obj in objects:
        title = obj.get("title", "Untitled")
        text = obj.get("text", "")
        images = obj.get("images", [])
        tables = obj.get("tables", [])

        markdown += f"# {title}\n\n"
        markdown += f"{text}\n\n"

        if images:
            markdown += "## Images\n\n"
            for img in images:
                img_url = img.get("url", "")
                caption = img.get("caption", "Image")
                markdown += f"![{caption}]({img_url})\n\n"

        if tables:
            markdown += "## Tables\n\n"
            for table in tables:
                rows = table.get("rows", [])
                if rows:
                    # table header
                    markdown += "| " + " | ".join(rows[0]) + " |\n"
                    markdown += "| " + " | ".join(["-" * len(col) for col in rows[0]]) + " |\n"
                    for row in rows[1:]:
                        markdown += "| " + " | ".join(row) + " |\n"
                    markdown += "\n"

    return markdown if markdown else None


################################################################################
#                               E) STREAMLIT UI                                #
################################################################################

def main():
    st.title("Markdown Generator")

    # Step 1: Radio / Selectbox to choose PDF vs. Website
    choice = st.radio("Select an input type", ["PDF to Markdown", "Website URL to Markdown"])

    if choice == "PDF to Markdown":
        # UI for PDF
        uploaded_pdf = st.file_uploader("Upload a PDF file", type=["pdf"])
        extraction_method = st.selectbox("Extraction Method", ["Extract Using Open-Source Tool", "Extract Using Enterprise Tool"])

        if st.button("Generate Markdown"):
            if not uploaded_pdf:
                st.warning("Please upload a PDF first.")
                return

            # Write the PDF to a temp file
            with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as temp_pdf:
                temp_pdf.write(uploaded_pdf.read())
                temp_pdf_path = temp_pdf.name

            with st.spinner("Extracting content from PDF..."):
                if extraction_method == "Extract Using Open-Source Tool":
                    md_result = open_source_extract_pdf(temp_pdf_path)
                else:
                    md_result = enterprise_extract_pdf(temp_pdf_path)
                    if not md_result:
                        st.error("Enterprise PDF extraction failed. Check logs or secrets.")
                        return

            # Show preview
            st.markdown(md_result)

            # Download button
            st.download_button(
                label="Download Markdown",
                data=md_result,
                file_name="extracted_markdown.md",
                mime="text/markdown"
            )

    else:
        # UI for Website
        url_input = st.text_input("Enter a website URL")
        extraction_method = st.selectbox("Extraction Method", ["Extract Using Open-Source Tool", "Extract Using Enterprise Tool"])

        if st.button("Generate Markdown"):
            if not url_input.strip():
                st.warning("Please enter a valid URL.")
                return

            with st.spinner("Extracting content from website..."):
                if extraction_method == "Extract Using Open-Source Tool":
                    try:
                        md_result = open_source_extract_website(url_input)
                    except Exception as e:
                        st.error(f"Open-source website extraction failed: {e}")
                        return
                else:
                    md_result = enterprise_extract_website(url_input)
                    if not md_result:
                        st.error("Enterprise website extraction failed. Check logs or Diffbot token in secrets.")
                        return

            st.markdown(md_result)

            # Download button
            st.download_button(
                label="Download Markdown",
                data=md_result,
                file_name="extracted_website.md",
                mime="text/markdown"
            )


if __name__ == "__main__":
    main()
