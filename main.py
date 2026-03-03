from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from transformers import pipeline
import torch
import uvicorn
import asyncio
from playwright.async_api import async_playwright
import playwright_stealth
from bs4 import BeautifulSoup
import html2text
import random
import io
import requests
from pypdf import PdfReader
from fake_useragent import UserAgent

app = FastAPI(title="ConsentIQ Heavy-Duty AI Brain")

# Setup device
device = 0 if torch.cuda.is_available() else -1
print(f"Using device: {'GPU (ROCm/CUDA)' if device == 0 else 'CPU'}")

# Load model
MODEL_NAME = "typeform/distilbert-base-uncased-mnli"
print(f"Loading Brain Model: {MODEL_NAME}...")
classifier = pipeline("zero-shot-classification", model=MODEL_NAME, device=device)
print("Brain loaded and listening on port 8000!")

ua = UserAgent()

# Common Browser Headers
BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,application/pdf,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive"
}

class PolicyRequest(BaseModel):
    url: str

async def extract_pdf_text(url: str):
    try:
        print(f"Brain: Downloading PDF: {url}")
        response = requests.get(url, headers=BROWSER_HEADERS, timeout=30)
        response.raise_for_status()
        with io.BytesIO(response.content) as f:
            reader = PdfReader(f)
            text = ""
            for page in reader.pages:
                text += page.extract_text() + "\n"
            return text
    except Exception as e:
        print(f"PDF Error: {str(e)}")
        raise Exception(f"PDF Parse Error: {str(e)}")

async def run_scrape(url: str, attempt: int = 1):
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=['--disable-dev-shm-usage', '--no-sandbox'])
        
        try:
            # Use random user agent for each attempt
            current_ua = ua.random
            context = await browser.new_context(
                user_agent=current_ua,
                viewport={'width': 1280, 'height': 800}
            )
            
            page = await context.new_page()
            
            # FIX: Use playwright_stealth.stealth(page) - explicitly call the function
            # And removed 'await' because it is a synchronous injection
            playwright_stealth.stealth(page)
            
            print(f"Brain: Attempt {attempt} - Scraping {url}")
            # For S3 browsers and heavy JS, we wait for networkidle
            await page.goto(url, wait_until="networkidle", timeout=90000)
            
            # Extra wait for JS rendering (especially for S3 list population)
            await page.wait_for_timeout(7000)
            
            content = await page.content()
            await browser.close()
            
            soup = BeautifulSoup(content, 'html.parser')
            for junk in soup(["script", "style", "nav", "footer", "header", "aside", "iframe"]):
                junk.extract()
            
            h = html2text.HTML2Text()
            h.ignore_links = True
            h.ignore_images = True
            h.body_width = 0
            markdown_text = h.handle(str(soup))
            
            return markdown_text
            
        except Exception as e:
            await browser.close()
            raise e

async def stealth_scrape(url: str):
    url = url.strip()
    
    # 1. Quick PDF Check
    if url.lower().split('?')[0].endswith('.pdf'):
        return await extract_pdf_text(url)

    # 2. Scrape with retries
    max_retries = 2
    last_error = ""
    for i in range(max_retries):
        try:
            return await run_scrape(url, i + 1)
        except Exception as e:
            last_error = str(e)
            print(f"Scrape attempt {i+1} failed: {last_error}")
            if i < max_retries - 1:
                await asyncio.sleep(3)
    
    raise Exception(last_error)

@app.get("/")
async def health_check():
    return {"status": "Brain Online", "device": "GPU" if device == 0 else "CPU"}

@app.post("/analyze")
async def analyze_policy(request: PolicyRequest):
    url = request.url
    if not url:
        raise HTTPException(status_code=400, detail="URL is required")

    try:
        text = await stealth_scrape(url)
    except Exception as e:
        print(f"Final Brain Error: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Stealth Scrape failed: {str(e)}")

    if not text or len(text.strip()) < 100:
        raise HTTPException(status_code=400, detail="The extracted text is too short. The site might be blocking headless access or requires cookies.")

    # CHUNKED ANALYSIS
    chunks = [
        text[:3000], 
        text[len(text)//2 - 1500 : len(text)//2 + 1500], 
        text[-3000:]
    ]
    combined_context = "\n---\n".join(chunks)

    categories_def = {
        "dataRetention": {
            "labels": ["the company deletes data after use", "the company keeps data forever"],
            "weight": 0.15, "name": "Data Retention"
        },
        "thirdPartySharing": {
            "labels": ["the company keeps data private and secure", "the company sells or shares data with third parties"],
            "weight": 0.30, "name": "Third-party Sharing"
        },
        "biometricUsage": {
            "labels": ["no biometric data is collected", "the company collects face voice or fingerprint data"],
            "weight": 0.25, "name": "Biometric Usage"
        },
        "targetedAdvertising": {
            "labels": ["no targeted advertising is used", "the company uses data for targeted marketing"],
            "weight": 0.15, "name": "Targeted Advertising"
        },
        "dataDeletion": {
            "labels": ["users can delete their data easily", "users cannot easily delete their data"],
            "weight": 0.15, "name": "Data Deletion Rights"
        }
    }

    results = {}
    total_score = 0

    for key, cat in categories_def.items():
        res = classifier(combined_context, candidate_labels=cat["labels"])
        safe_label = cat["labels"][0]
        safe_index = res["labels"].index(safe_label)
        safe_prob = res["scores"][safe_index]
        score = int(safe_prob * 100)
        
        dominant_label = res["labels"][0]
        is_safe = (dominant_label == cat["labels"][0])
        desc = f"Verified: {dominant_label}." if is_safe else f"Warning: {dominant_label}."

        results[key] = {
            "score": score,
            "weight": cat["weight"],
            "label": cat["name"],
            "description": desc
        }
        total_score += score * cat["weight"]

    return {
        "overallScore": int(total_score),
        "categories": results,
        "text_content": text[:1000] + "..."
    }

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
