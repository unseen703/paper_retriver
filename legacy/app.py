import requests
import xml.etree.ElementTree as ET
import os
import time
from dotenv import load_dotenv

load_dotenv()
download_dir = "./data"

# def fetch_and_download_arxiv_papers_raw(max_results=5, download_dir="./arxiv_papers", max_retries=3):
namespace = {'atom': 'http://www.w3.org/2005/Atom'}
i = 0

print("Fetching metadata and downloading PDFs...")

while True:
    # Your perfectly working request
    url = os.getenv('ARXIV_API_URL') + f"?search_query=({os.getenv('CAT_AI')} OR {os.getenv('CAT_MA')}) AND {os.getenv('TIME_QUERY')}&sortBy=lastUpdatedDate&sortOrder=ascending&start={i}&max_results=20"
    
    res = requests.get(url)
    
    if res.status_code == 200:
        root = ET.fromstring(res.text)
        entries = root.findall('atom:entry', namespace)
        
        if not entries:
            print("No more papers found.")
            break

        for item in entries:
            title = item.find('atom:title', namespace).text.strip().replace('\n', ' ')
            published = item.find('atom:published', namespace).text
            
            # Extract PDF link
            pdf_url = None
            for link in item.findall('atom:link', namespace):
                if link.get('title') == 'pdf':
                    pdf_url = link.get('href')
                    if not pdf_url.endswith('.pdf'):
                        pdf_url += '.pdf'
                    break
            
            print(f"\nTitle: {title}")
            print(f"Published: {published}")
            
            if pdf_url:
                # Extract ID for the filename
                paper_id = pdf_url.split('/')[-1]
                pdf_path = os.path.join(download_dir, f"{paper_id}")
                
                print(f"-> Downloading {paper_id}...")
                
                # arXiv requires a 3-second delay between PDF downloads
                time.sleep(3) 
                
                pdf_res = requests.get(pdf_url)
                if pdf_res.status_code == 200:
                    with open(pdf_path, 'wb') as f:
                        f.write(pdf_res.content)
                    print("-> Download complete.")
                else:
                    print(f"-> Failed to download PDF (Status: {pdf_res.status_code})")
    else:
        print(f"API Error {res.status_code}: {res.text}")
        break
        
    i += 20
    if i > 500:
        break
    time.sleep(3) # Delay before the next metadata batch