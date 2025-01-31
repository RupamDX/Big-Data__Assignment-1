# Markdown Generator (Streamlit Prototype)

This is a Streamlit-based prototype application for extracting content from PDFs and websites and converting it into Markdown format. The application supports both open-source tools and enterprise-grade services (e.g., Adobe PDF Services).

---

## Features

### PDF to Markdown
- Extract text, images, and tables from PDF files.
- Choose between open-source tools (PyMuPDF, pdfplumber, Tabula) and enterprise tools (Adobe PDF Services).
- Upload extracted images and content to AWS S3.

### Website to Markdown
- Scrape website content, including text, images, links, and tables.
- Convert the scraped content into Markdown format.
- Use open-source tools (BeautifulSoup) or enterprise tools (e.g., Diffbot).

### User Interface
- Easy-to-use Streamlit-based UI for selecting input types, uploading PDFs, or providing website URLs.
- View the extracted Markdown content directly in the UI.
- Download the extracted Markdown as a `.md` file.

---

## Technology Stack

- **Frontend**: [Streamlit](https://streamlit.io/)
- **PDF Processing**:
  - Open-source: PyMuPDF, pdfplumber, Tabula
  - Enterprise: Adobe PDF Services SDK
- **Website Scraping**: BeautifulSoup
- **Cloud Storage**: AWS S3

---

## How to Run the Application

1. **Clone the Repository**
   ```bash
   git clone https://github.com/your-repo/markdown-generator.git
   cd markdown-generator
