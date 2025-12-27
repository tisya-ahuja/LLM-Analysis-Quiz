import re, time, asyncio, pandas as pd
from urllib.parse import urlparse
from app.scraper import fetch_quiz_page_html
from app.utils import (
    extract_submit_url, extract_download_links, http_get_bytes,
    http_post_json, find_question_text, sum_value_column_in_pdf, decode_atob_blocks
)

async def solve_single(url: str, email: str, secret: str) -> dict:
    html = await fetch_quiz_page_html(url)
    submit_url = extract_submit_url(html, url)
    
    if not submit_url:
        # sometimes in decoded atob text
        for decoded in decode_atob_blocks(html):
            submit_url = extract_submit_url(decoded, url) or submit_url
    if not submit_url:
        raise ValueError("Submit URL not found on quiz page.")

    qtext = find_question_text(html)
    answer = None

    # Pattern 1: Scrape secret code from a data page
    # Use re.DOTALL to make . match newlines, and \s* to match any whitespace including newlines
    if re.search(r'scrape.*secret.*code', qtext, re.I | re.DOTALL):
        # Find the data URL to scrape
        links = extract_download_links(html)
        data_url = None
        
        # Look for relative URLs in the question text or HTML
        # Match /path?params even if followed by newline
        relative_match = re.search(r'(/[\w\-]+\?[^\s\)]*)', qtext)
        if relative_match:
            from urllib.parse import urljoin
            data_url = urljoin(url, relative_match.group(1))
        else:
            # Try to find in links
            for link in links:
                if 'scrape-data' in link or 'data' in link:
                    data_url = link
                    break
        
        if data_url:
            # Fetch the data page
            data_html = await fetch_quiz_page_html(data_url)
            # Look for secret code patterns
            # Pattern 1: "secret code is XXX" or "code is XXX"
            secret_match = re.search(r'(?:secret\s+code|code)\s+is\s+(?:<[^>]+>)?([A-Za-z0-9\-]+)', data_html, re.I)
            if not secret_match:
                # Pattern 2: "secret: XXX" or "code: XXX"
                secret_match = re.search(r'(?:secret|code)[\s:]+([A-Za-z0-9\-]+)', data_html, re.I)
            if not secret_match:
                # Pattern 3: Extract number from <strong> or <em> tags
                secret_match = re.search(r'<strong>(\d+)</strong>', data_html)
            if not secret_match:
                # Pattern 4: Any standalone alphanumeric code
                secret_match = re.search(r'\b([A-Z0-9]{6,})\b', data_html)
            if secret_match:
                answer = secret_match.group(1)

    # Pattern 2: CSV file with cutoff filtering
    if answer is None and re.search(r'csv.*cutoff', qtext, re.I | re.DOTALL):
        # Extract cutoff value
        cutoff_match = re.search(r'cutoff[:\s]+(\d+)', qtext, re.I)
        cutoff = int(cutoff_match.group(1)) if cutoff_match else None
        
        # Find CSV link (check both absolute and relative URLs)
        links = extract_download_links(html)
        csv_link = None
        
        # First try absolute URLs
        for link in links:
            if link.lower().endswith('.csv'):
                csv_link = link
                break
        
        # If not found, look for relative CSV links in href attributes
        if not csv_link:
            csv_href_match = re.search(r'href=["\']([^"\']+\.csv)["\']', html, re.I)
            if csv_href_match:
                from urllib.parse import urljoin
                csv_link = urljoin(url, csv_href_match.group(1))
        
        if csv_link and cutoff is not None:
            csv_bytes = await http_get_bytes(csv_link)
            # Read CSV without header to include all rows
            df = pd.read_csv(pd.io.common.BytesIO(csv_bytes), header=None)
            
            # Filter by cutoff and sum
            # Assume we need to filter rows where a numeric column is > cutoff
            # Then sum another column
            numeric_cols = [c for c in df.columns if pd.api.types.is_numeric_dtype(df[c])]
            if len(numeric_cols) >= 2:
                # First numeric column for filtering, second for summing
                filter_col = numeric_cols[0]
                sum_col = numeric_cols[1]
                filtered_df = df[df[filter_col] > cutoff]
                answer = float(filtered_df[sum_col].sum())
            elif len(numeric_cols) == 1:
                # Single numeric column - filter and sum the same column
                col = numeric_cols[0]
                filtered_df = df[df[col] > cutoff]
                answer = float(filtered_df[col].sum())

    # Pattern 3: PDF with table on page 2
    if answer is None and re.search(r'\btable on page\s*2\b', qtext, re.I) and "value" in qtext.lower():
        links = extract_download_links(html)
        pdf_links = [u for u in links if urlparse(u).path.lower().endswith(".pdf")]
        if not pdf_links:
            for decoded in decode_atob_blocks(html):
                pdf_links += [u for u in re.findall(r'https?://[^\s"<>]+', decoded) if u.lower().endswith(".pdf")]
        if pdf_links: # Only proceed if PDF links are found
            pdf_bytes = await http_get_bytes(pdf_links[0])
            val = sum_value_column_in_pdf(pdf_bytes, page_index=1, column_name="value")
            answer = val

    # Pattern 4: Generic CSV/Excel sum
    if answer is None:
        links = extract_download_links(html)
        data_link = None
        for u in links:
            p = urlparse(u).path.lower()
            if p.endswith(".csv") or p.endswith(".xlsx") or p.endswith(".xls"):
                data_link = u
                break
        if data_link:
            b = await http_get_bytes(data_link)
            if data_link.lower().endswith(".csv"):
                df = pd.read_csv(pd.io.common.BytesIO(b))
            else:
                df = pd.read_excel(pd.io.common.BytesIO(b))
            target = None
            for c in df.columns:
                if str(c).strip().lower() == "value":
                    target = c
                    break
            if target is None:
                for c in df.columns:
                    if pd.api.types.is_numeric_dtype(df[c]):
                        target = c
                        break
            if target: # Only set answer if a target column is found
                answer = float(df[target].fillna(0).sum())

    # Fallback: if still no answer
    if answer is None:
        answer = "unhandled_question"

    payload = {
        "email": email,
        "secret": secret,
        "url": url,
        "answer": answer
    }
    result = await http_post_json(submit_url, payload)
    return {"question": qtext[:280], "submitted_to": submit_url, "answer": answer, "result": result}

async def solve_quiz_chain(start_url: str, email: str, secret: str) -> list[dict]:
    t0 = time.time()
    url = start_url
    out = []
    while url and (time.time() - t0) < 180:
        res = await solve_single(url, email, secret)
        out.append(res)
        nxt = res.get("result", {}).get("url")
        # If incorrect and a new url is provided, prefer the new one
        url = nxt
    return out
