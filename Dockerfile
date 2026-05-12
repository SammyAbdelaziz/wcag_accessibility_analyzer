FROM mcr.microsoft.com/azure-functions/python:4-python3.11

ENV AzureWebJobsScriptRoot=/home/site/wwwroot \
    AzureFunctionsJobHost__Logging__Console__IsEnabled=true \
    FUNCTIONS_WORKER_RUNTIME=python \
    WCAG_MAX_FILE_SIZE_MB=20

# Install LibreOffice headless (DOCX/PPTX → PDF rendering) and Tesseract OCR
RUN apt-get update && apt-get install -y --no-install-recommends \
    libreoffice-writer \
    libreoffice-impress \
    tesseract-ocr \
    tesseract-ocr-eng \
    poppler-utils \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Tell LibreOffice to use /tmp for its user profile (writable in containers)
ENV HOME=/tmp

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY . /home/site/wwwroot
