import streamlit as st
import requests

BACKEND_URL = "https://big-data-assignment-1.onrender.com"  # from Render

def pdf_to_markdown(file_bytes, file_name, method):
    # Upload PDF as multipart/form-data
    files = {"file": (file_name, file_bytes, "application/pdf")}
    data = {"method": method}  # open-source or enterprise
    resp = requests.post(f"https://big-data-assignment-1.onrender.com/extract/pdf/", files=files, data=data)
    resp.raise_for_status()
    return resp.json()  # returns {"markdown_url": "..."}

def website_to_markdown(url, method):
    data = {"url": url, "method": method}
    resp = requests.post(f"https://big-data-assignment-1.onrender.com/extract/website/", data=data)
    resp.raise_for_status()
    return resp.json()

def main():
    st.title("Markdown Generator")
    choice = st.radio("Select an input type", ["PDF to Markdown", "Website URL to Markdown"])
    
    if choice == "PDF to Markdown":
        uploaded_pdf = st.file_uploader("Upload a PDF file", type=["pdf"])
        extraction_method = st.selectbox("Extraction Method", ["Extract Using Open-Source Tool", "Extract Using Enterprise Tool"])

        if st.button("Generate Markdown"):
            if not uploaded_pdf:
                st.warning("Please upload a PDF first.")
                return
            with st.spinner("Processing..."):
                method_val = "open-source" if extraction_method.startswith("Extract Using Open") else "enterprise"
                response_data = pdf_to_markdown(uploaded_pdf.getvalue(), uploaded_pdf.name, method_val)
                md_url = response_data["markdown_url"]
                st.write(f"**Markdown file uploaded to S3**: {md_url}")
                st.markdown(f"[View Markdown]({md_url})")

    else:
        url_input = st.text_input("Enter a website URL")
        extraction_method = st.selectbox("Extraction Method", ["Extract Using Open-Source Tool", "Extract Using Enterprise Tool"])

        if st.button("Generate Markdown"):
            if not url_input.strip():
                st.warning("Please enter a valid URL.")
                return
            with st.spinner("Processing..."):
                method_val = "open-source" if extraction_method.startswith("Extract Using Open") else "enterprise"
                response_data = website_to_markdown(url_input, method_val)
                md_url = response_data["markdown_url"]
                st.write(f"**Markdown file uploaded to S3**: {md_url}")
                st.markdown(f"[View Markdown]({md_url})")

if __name__ == "__main__":
    main()
