from fastapi import FastAPI, UploadFile, File, HTTPException, Form
import os
import zipfile
import logging
import urllib.parse
import pandas as pd
import tempfile
import requests
import json
import fitz  # PyMuPDF
import pdfplumber
import boto3
import shutil
import time

import openpyxl
from bs4 import BeautifulSoup
from adobe.pdfservices.operation.auth.service_principal_credentials import ServicePrincipalCredentials
from adobe.pdfservices.operation.pdf_services import PDFServices
from adobe.pdfservices.operation.pdf_services_media_type import PDFServicesMediaType
from adobe.pdfservices.operation.pdfjobs.jobs.extract_pdf_job import ExtractPDFJob
from adobe.pdfservices.operation.pdfjobs.params.extract_pdf.extract_element_type import ExtractElementType
from adobe.pdfservices.operation.pdfjobs.params.extract_pdf.extract_pdf_params import ExtractPDFParams
from adobe.pdfservices.operation.pdfjobs.result.extract_pdf_result import ExtractPDFResult
from adobe.pdfservices.operation.pdfjobs.params.extract_pdf.extract_renditions_element_type import ExtractRenditionsElementType
from apify_client import ApifyClient


# AWS S3 Credentials
AWS_ACCESS_KEY_ID = os.getenv("AWS_ACCESS_KEY_ID")
AWS_SECRET_ACCESS_KEY = os.getenv("AWS_SECRET_ACCESS_KEY")
AWS_DEFAULT_REGION = os.getenv("AWS_DEFAULT_REGION", "us-east-1")
S3_BUCKET_NAME = os.getenv("S3_BUCKET_NAME", "my-textractbucket")
DIFFBOT_API_TOKEN = os.getenv("DIFFBOT_API_TOKEN")
APIFY_API_TOKEN = os.getenv("APIFY_API_TOKEN")
ADOBE_CREDENTIALS_PATH = os.getenv("ADOBE_CREDENTIALS_JSON")
client = ApifyClient(APIFY_API_TOKEN)
# FastAPI app
app = FastAPI()

################################################################################
#                         S3 UPLOAD FUNCTION                                   #
################################################################################

def upload_file_to_s3(file_path):
    """Uploads a file to S3 and returns its URL."""
    s3_client = boto3.client(
        's3',
        aws_access_key_id=AWS_ACCESS_KEY_ID,
        aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
        region_name=AWS_DEFAULT_REGION
    )

    object_key = os.path.basename(file_path)
    try:
        s3_client.upload_file(file_path, S3_BUCKET_NAME, object_key)
        return f"https://{S3_BUCKET_NAME}.s3.{AWS_DEFAULT_REGION}.amazonaws.com/{object_key}"
    except Exception as e:
        logging.error(f"Failed to upload {file_path} to S3: {e}")
        return None

################################################################################
#                         PDF EXTRACTION METHODS                               #
################################################################################

def extract_images_to_md(pdf_path):
    """Extract images from PDF, upload to S3, and return Markdown with image links."""
    doc = fitz.open(pdf_path)
    md_images = "\n## Extracted Images\n"
    for page_num in range(len(doc)):
        page = doc[page_num]
        images = page.get_images(full=True)

        for img_index, img in enumerate(images):
            xref = img[0]
            base_image = doc.extract_image(xref)
            if not base_image:
                continue
            image_bytes = base_image["image"]
            image_ext = base_image["ext"]

            with tempfile.NamedTemporaryFile(delete=False, suffix=f".{image_ext}") as tmp_img:
                tmp_img.write(image_bytes)
                tmp_path = tmp_img.name

            s3_url = upload_file_to_s3(tmp_path)
            os.remove(tmp_path)

            if s3_url:
                md_images += f"![Image page {page_num+1} - {img_index+1}]({s3_url})\n"

    doc.close()
    return md_images if "![Image" in md_images else "\n**No images found in this PDF.**\n"


def extract_text_tables_to_md(pdf_path):
    """Extract text and tables from PDF using pdfplumber and return Markdown."""
    md_text = "\n## Extracted Text\n"
    md_tables = "\n## Extracted Tables\n"
    
    with pdfplumber.open(pdf_path) as pdf:
        for page_num, page in enumerate(pdf.pages):
            page_text = page.extract_text() or ""
            md_text += f"\n### Page {page_num+1}\n```\n{page_text}\n```\n"

            # Extract tables
            tables = page.extract_tables()
            for table in tables:
                md_tables += f"\n### Table (Page {page_num+1})\n"
                for row in table:
                    md_tables += "| " + " | ".join(row) + " |\n"

    return md_text + md_tables


def open_source_extract_pdf(pdf_path):
    """Extract images, text, and tables, then format as Markdown."""
    md = f"# Extracted Content from {os.path.basename(pdf_path)}\n"
    md += extract_images_to_md(pdf_path)
    md += extract_text_tables_to_md(pdf_path)
    
    # Save markdown to S3
    with tempfile.NamedTemporaryFile(delete=False, suffix=".md") as md_file:
        md_file.write(md.encode())
        md_file_path = md_file.name

    md_s3_url = upload_file_to_s3(md_file_path)
    os.remove(md_file_path)

    return md_s3_url


logging.basicConfig(level=logging.INFO)

def extract_pdf_elements(pdf_path):
    #Extracts text, tables, and image renditions using Adobe PDF Services.
    try:
        # Load Adobe credentials
        with open(ADOBE_CREDENTIALS_PATH, "r") as f:
            adobe_data = json.load(f)

        credentials = ServicePrincipalCredentials(
            client_id=adobe_data["client_credentials"]["client_id"],
            client_secret=adobe_data["client_credentials"]["client_secret"]
        )
        pdf_services = PDFServices(credentials=credentials)

        # Upload PDF to Adobe API
        with open(pdf_path, "rb") as f:
            input_stream = f.read()
        input_asset = pdf_services.upload(input_stream=input_stream, mime_type=PDFServicesMediaType.PDF)

        # Extract text, tables, and image renditions
        extract_params = ExtractPDFParams(
            elements_to_extract=[ExtractElementType.TEXT, ExtractElementType.TABLES],
            elements_to_extract_renditions=[ExtractRenditionsElementType.FIGURES]  # Extract images as renditions
        )
        job = ExtractPDFJob(input_asset=input_asset, extract_pdf_params=extract_params)
        location = pdf_services.submit(job)
        pdf_services_response = pdf_services.get_job_result(location, ExtractPDFResult)

        # Download the extraction results (ZIP file)
        result_asset = pdf_services_response.get_result().get_resource()
        stream_asset = pdf_services.get_content(result_asset)

        with tempfile.NamedTemporaryFile(delete=False, suffix=".zip") as temp_zip:
            temp_zip.write(stream_asset.get_input_stream())  #  FIXED: No `.read()` needed
            temp_zip_path = temp_zip.name

        # Extract ZIP contents
        output_dir = tempfile.mkdtemp()
        with zipfile.ZipFile(temp_zip_path, "r") as zip_ref:
            zip_ref.extractall(output_dir)

        # Read structuredData.json
        structured_data_path = os.path.join(output_dir, "structuredData.json")
        if not os.path.exists(structured_data_path):
            raise HTTPException(status_code=500, detail="structuredData.json not found in extracted ZIP.")

        with open(structured_data_path, "r", encoding="utf-8") as json_file:
            extracted_data = json.load(json_file)

        return extracted_data, output_dir

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Adobe PDF Services error: {str(e)}")


import logging

def enterprise_extract_pdf(pdf_path):
    """Main function that extracts text, tables, uploads images, and returns Markdown URL."""
    try:
        logging.info("Starting PDF extraction process...")
        extracted_data, output_dir = extract_pdf_elements(pdf_path)

        logging.info("🔹 Extracting images...")
        image_links = upload_images_to_s3(output_dir)
        logging.info(f"Image links: {image_links}")

        logging.info("🔹 Extracting tables from Excel files...")
        table_markdown = extract_tables_from_xlsx(output_dir)
        logging.info(f"Table Markdown content:\n{table_markdown}")

        logging.info("🔹 Generating final Markdown file...")
        md_content = generate_markdown(extracted_data, image_links, table_markdown)

        # Debug: Ensure Markdown content is not empty
        if not md_content.strip():
            logging.error("Markdown content is EMPTY! Something went wrong.")
            raise HTTPException(status_code=500, detail="Generated Markdown is empty!")

        # Save Markdown to S3
        with tempfile.NamedTemporaryFile(delete=False, suffix=".md", mode="w", encoding="utf-8") as md_file:
            md_file.write(md_content)
            md_file_path = md_file.name

        # Debug: Ensure Markdown file exists before upload
        if not os.path.exists(md_file_path):
            logging.error("Markdown file was NOT created!")
            raise HTTPException(status_code=500, detail="Markdown file not found before upload!")

        logging.info(f"🔹 Uploading Markdown file to S3: {md_file_path}")
        md_s3_url = upload_file_to_s3(md_file_path)

        # Debug: Ensure S3 upload was successful
        if not md_s3_url:
            logging.error(" Failed to upload Markdown to S3!")
            raise HTTPException(status_code=500, detail="Markdown upload failed!")

        os.remove(md_file_path)
        logging.info(f" Successfully uploaded Markdown file to S3: {md_s3_url}")

        # Cleanup temporary files
        shutil.rmtree(output_dir)
        logging.info("Cleaned up temporary files.")

        return md_s3_url

    except Exception as e:
        logging.exception(f" Error processing PDF: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error processing PDF: {str(e)}")


def upload_images_to_s3(output_dir):
    """Uploads extracted images from Adobe's `figures/` folder to S3 and returns their URLs."""
    image_links = []
    figures_dir = os.path.join(output_dir, "figures")  # Change from `renditions/` to `figures/`

    if os.path.exists(figures_dir):
        images = [img for img in os.listdir(figures_dir) if img.lower().endswith(('.png', '.jpg', '.jpeg'))]
        logging.info(f"Found {len(images)} images in figures/ folder.")

        for img_file in images:
            img_path = os.path.join(figures_dir, img_file)
            logging.info(f"Uploading image: {img_path} to S3...")

            # Upload image to S3
            s3_url = upload_file_to_s3(img_path)
            if s3_url:
                image_links.append(s3_url)
                logging.info(f"Successfully uploaded {img_file} to S3: {s3_url}")
            else:
                logging.error(f"Failed to upload {img_file} to S3.")

            # Ensure local file cleanup
            try:
                os.remove(img_path)
                logging.info(f"Deleted local file: {img_path}")
            except Exception as e:
                logging.error(f"Error deleting {img_path}: {str(e)}")

    else:
        logging.warning("No figures/ folder found. No images extracted.")

    logging.info(f"Total images uploaded to S3: {len(image_links)}")
    return image_links


def extract_tables_from_xlsx(output_dir):
    """Extracts table data from Excel files in the `tables/` folder and converts it to Markdown format."""
    markdown_tables = ""
    tables_dir = os.path.join(output_dir, "tables")

    if os.path.exists(tables_dir):
        excel_files = [file for file in os.listdir(tables_dir) if file.endswith(".xlsx")]
        logging.info(f"Found {len(excel_files)} table files in tables/ folder.")

        for file in excel_files:
            file_path = os.path.join(tables_dir, file)
            logging.info(f"Processing table file: {file_path}")

            workbook = openpyxl.load_workbook(file_path)
            sheet = workbook.active  # Assume data is in the first sheet

            # Generate Markdown for the table
            markdown_tables += f"## Table from {file}\n\n"
            for i, row in enumerate(sheet.iter_rows(values_only=True)):
                row_data = [str(cell) if cell is not None else "" for cell in row]
                markdown_tables += "| " + " | ".join(row_data) + " |\n"
                if i == 0:  # Add a separator after the header
                    markdown_tables += "| " + " | ".join(["---"] * len(row_data)) + " |\n"
            markdown_tables += "\n"

    else:
        logging.warning(" No tables/ folder found. No tables extracted.")

    return markdown_tables




def generate_markdown(extracted_data, image_links, table_markdown):
    """Generates Markdown content using extracted text, tables, and image links."""
    md_content = "# Extracted PDF Data\n\n"

    # Extract text
    for element in extracted_data.get("elements", []):
        if "Text" in element:
            md_content += f"## Extracted Text\n\n{element['Text']}\n\n"

    # Add tables from Excel files
    md_content += table_markdown

    # Add images to Markdown
    if image_links:
        md_content += "## Extracted Images\n"
        for idx, img_link in enumerate(image_links, start=1):
            md_content += f"![Image {idx}]({img_link})\n"
        md_content += "\n"

    return md_content





################################################################################
#                         URL EXTRACTION METHODS                               #
################################################################################
def open_source_extract_website(url: str):
    """Scrape text from a website using BeautifulSoup and upload Markdown to S3."""
    response = requests.get(url)
    response.raise_for_status()
    soup = BeautifulSoup(response.content, 'html.parser')

    paragraphs = soup.find_all('p')
    text_content = '\n'.join([p.get_text(strip=True) for p in paragraphs])

    md_content = f"# Extracted Content from {url}\n\n## Text Content\n\n{text_content}\n"

    # Save Markdown file
    with tempfile.NamedTemporaryFile(delete=False, suffix=".md", mode="w", encoding="utf-8") as md_file:
        md_file.write(md_content)
        md_file_path = md_file.name

    # Upload to S3
    md_s3_url = upload_file_to_s3(md_file_path)
    os.remove(md_file_path)

    return md_s3_url  # Return the S3 URL instead of the raw Markdown text


def extract_website_content(url):
    """
    Extracts text, images, links, and tables from a website.
    
    Parameters:
    - url (str): The URL of the website to scrape.

    Returns:
    - Extracted text
    - List of image URLs
    - List of all hyperlinks
    - List of tables in Markdown format
    """
    response = requests.get(url)
    response.raise_for_status()  
    soup = BeautifulSoup(response.content, 'html.parser')

    # Extract text
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
    tables = soup.find_all('table')
    extracted_tables = []
    for table in tables:
        rows = table.find_all('tr')
        table_data = []
        for row in rows:
            cols = row.find_all(['td', 'th'])
            table_data.append([col.get_text(strip=True) for col in cols])
        if table_data:  
            df = pd.DataFrame(table_data)
            if len(df) > 1:
                df.columns = df.iloc[0]  
                df = df[1:].reset_index(drop=True)
            extracted_tables.append(df)

    return text_content, image_urls, link_urls, extracted_tables


def save_to_markdown(url, text, image_urls, links, tables):
    """
    Saves the extracted website content into a Markdown file and uploads to S3.
    
    Returns:
    - S3 URL of the uploaded Markdown file
    """
    with tempfile.NamedTemporaryFile(delete=False, suffix=".md", mode="w", encoding="utf-8") as md_file:
        md_file.write(f"# Extracted Content from {url}\n\n")

        # Add text
        md_file.write("## Text Content\n\n")
        md_file.write(text + "\n\n")

        # Add images
        md_file.write("## Images\n\n")
        for img_url in image_urls:
            md_file.write(f"![Image]({img_url})\n\n")

        # Add links
        md_file.write("## Links\n\n")
        for link in links:
            md_file.write(f"- [{link}]({link})\n")

        # Add tables
        md_file.write("## Tables\n\n")
        for i, table in enumerate(tables):
            md_file.write(f"### Table {i + 1}\n\n")
            md_file.write(table.to_markdown(index=False) + "\n\n")

        md_file_path = md_file.name

    # Upload to S3
    md_s3_url = upload_file_to_s3(md_file_path)
    os.remove(md_file_path)

    return md_s3_url  # Return the S3 URL





def wait_for_apify_run(run_id, poll_interval=5):
    """Waits indefinitely for an Apify run to complete before proceeding."""
    while True:
        run = client.run(run_id).get()
        run_status = run["status"]

        logging.info(f"Apify Run Status: {run_status}")

        if run_status == "SUCCEEDED":
            return run  # Run completed successfully
        elif run_status in ["FAILED", "ABORTED"]:
            # Get error details if available
            run_info = client.run(run_id).get()
            error_message = run_info.get("errorMessage", "No detailed error message provided.")
            logging.error(f"Apify run failed! Status: {run_status}, Error: {error_message}")
            raise HTTPException(status_code=500, detail=f"Apify run failed with status: {run_status}. Error: {error_message}")

        time.sleep(poll_interval)  # Keep waiting indefinitely




def enterprise_extract_website(url):
    """Extracts content from a website using Apify and uploads it as Markdown to S3."""
    try:
        logging.info(f"Starting website extraction for: {url}")

        # Prepare the Actor input (WITHOUT PROXY)
        run_input = {
            "startUrls": [url],  
            "maxDepth": 1,
            "sameDomain": True,
            "maxResults": 10,
            "waitForLoad": 5000,  # Allow JavaScript-heavy pages to load
        }

        # Start the Apify Actor
        run = client.actor("OutlPf9SFs5BPflRj").start(run_input=run_input)
        run_id = run["id"]
        logging.info(f"Apify actor started with Run ID: {run_id}")

        # Manually wait for Apify to complete
        timeout = 600  # Max wait time (seconds)
        start_time = time.time()

        while True:
            # Get current status of the Apify run
            run_status = client.run(run_id).get()["status"]
            logging.info(f"Apify run status: {run_status}")

            if run_status == "SUCCEEDED":
                logging.info("Apify run completed successfully!")
                break
            elif run_status in ["FAILED", "ABORTED"]:
                # Get Apify error message if available
                run_info = client.run(run_id).get()
                error_message = run_info.get("errorMessage", "No detailed error message provided.")
                logging.error(f"Apify run failed! Error: {error_message}")
                raise HTTPException(status_code=500, detail=f"Apify run failed: {error_message}")

            # Check if timeout reached
            if time.time() - start_time > timeout:
                raise HTTPException(status_code=500, detail="Apify run timed out!")

            time.sleep(5)  # Wait 5 seconds before checking again

        # Retrieve extracted dataset
        dataset_id = client.run(run_id).get()["defaultDatasetId"]
        dataset_items = list(client.dataset(dataset_id).iterate_items())

        # Debug: Print extracted dataset
        logging.info(f"Extracted {len(dataset_items)} items from Apify.")

        if not dataset_items:
            logging.error("Apify returned an empty dataset!")
            raise HTTPException(status_code=500, detail="Extracted dataset is empty!")

        # Prepare Markdown content
        md_content = f"# Extracted Content from {url}\n\n"

        for item in dataset_items:
            title = item.get("title", "No Title")
            page_url = item.get("url", "#")
            markdown_text = item.get("markdown") or item.get("textContent") or "No Content Available"

            md_content += f"## {title}\n"
            md_content += f"[Source Link]({page_url})\n\n"
            md_content += f"{markdown_text}\n\n---\n\n"

        # Ensure Markdown is not empty
        if not md_content.strip():
            logging.error("Extracted Markdown is empty!")
            raise HTTPException(status_code=500, detail="Extracted Markdown is empty!")

        # Save Markdown to a temporary file
        with tempfile.NamedTemporaryFile(delete=False, suffix=".md", mode="w", encoding="utf-8") as md_file:
            md_file.write(md_content)
            md_file_path = md_file.name

        # Upload the Markdown file to S3
        logging.info(f"Uploading extracted Markdown to S3: {md_file_path}")
        md_s3_url = upload_file_to_s3(md_file_path)

        # Ensure S3 upload was successful
        if not md_s3_url:
            logging.error("Markdown upload to S3 failed!")
            raise HTTPException(status_code=500, detail="Markdown upload failed!")

        # Cleanup local file
        os.remove(md_file_path)

        logging.info(f"Successfully uploaded Markdown to S3: {md_s3_url}")

        return md_s3_url

    except Exception as e:
        logging.exception(f"Error extracting website content: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error extracting website content: {str(e)}")

################################################################################
#                          API ENDPOINTS                                       #
################################################################################

@app.post("/extract/pdf/")
async def extract_pdf(file: UploadFile = File(...), method: str = Form(...)):
    """Extract content from a PDF using Open-Source or Enterprise method."""
    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as temp_pdf:
        temp_pdf.write(file.file.read())
        temp_pdf_path = temp_pdf.name

    if method == "open-source":
        md_s3_url = open_source_extract_pdf(temp_pdf_path)
    elif method == "enterprise":
        md_s3_url = enterprise_extract_pdf(temp_pdf_path)
    else:
        raise HTTPException(status_code=400, detail="Invalid extraction method.")

    return {"markdown_url": md_s3_url}

@app.post("/extract/website/")
async def extract_website(url: str = Form(...), method: str = Form(...)):
    """Extract content from a website and upload Markdown to S3 based on the selected method."""
    try:
        if method == "open-source":
            extracted_text, image_urls, extracted_links, extracted_tables = extract_website_content(url)
            md_s3_url = save_to_markdown(url, extracted_text, image_urls, extracted_links, extracted_tables)

        elif method == "enterprise":
            md_s3_url = enterprise_extract_website(url)  # Calls the enterprise extraction function

        else:
            raise HTTPException(status_code=400, detail="Invalid extraction method. Choose 'open-source' or 'enterprise'.")

        return {"markdown_url": md_s3_url}  # Return the S3 URL of the extracted Markdown file

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error extracting website content: {str(e)}")


if __name__ == "__main__":
    url = "https://en.wikipedia.org/wiki/DeepSeek"
    result = enterprise_extract_website(url)
    print(result)
