"""
One-shot site inspector: opens browser, navigates to eCourts, selects dropdowns,
then dumps cookies, hidden fields, captcha image info, and POST request details.

Run:  uv run python inspect_site.py
Output: /tmp/ecourts_inspect.json  +  /tmp/ecourts_captcha_sample.png
"""
import asyncio
import json
import time
from scraper import ECourtsScraper


async def inspect():
    scraper = ECourtsScraper(headless=False)
    await scraper.setup_driver()

    page = scraper.page
    context = scraper.context

    captured_requests = []
    captured_responses = []

    # Intercept all network requests
    async def on_request(req):
        captured_requests.append({
            "url": req.url,
            "method": req.method,
            "post_data": req.post_data,
            "headers": dict(req.headers),
        })

    async def on_response(resp):
        captured_responses.append({
            "url": resp.url,
            "status": resp.status,
        })

    page.on("request", on_request)
    page.on("response", on_response)

    print("\n[1] Navigating and selecting dropdowns...")
    await scraper.navigate_and_select()

    print("[2] Capturing cookies...")
    cookies = await context.cookies()
    cookie_map = {c["name"]: c["value"] for c in cookies}

    print("[3] Inspecting page form fields...")
    form_fields = await page.evaluate("""() => {
        const out = {};
        document.querySelectorAll('input, select, textarea').forEach(el => {
            const key = el.name || el.id || el.className || 'unnamed';
            out[key] = {
                tag: el.tagName,
                type: el.type || '',
                value: el.value || '',
                id: el.id || '',
                name: el.name || '',
                class: el.className || '',
            };
        });
        return out;
    }""")

    print("[4] Checking captcha image source...")
    captcha_info = await page.evaluate("""() => {
        const img = document.querySelector('img[src*="securimage"], img[src*="captcha"], #captcha_image, img.captcha');
        if (!img) {
            // Find all images
            const all = Array.from(document.querySelectorAll('img')).map(i => ({
                src: i.src, id: i.id, class: i.className
            }));
            return {found: false, all_images: all};
        }
        return {found: true, src: img.src, id: img.id, class: img.className};
    }""")

    print("[5] Downloading captcha image sample...")
    captcha_bytes = None
    if captcha_info.get("found"):
        try:
            captcha_el = page.locator(f'img[src*="securimage"], img[src*="captcha"]').first
            captcha_bytes = await captcha_el.screenshot()
            with open("/tmp/ecourts_captcha_sample.png", "wb") as f:
                f.write(captcha_bytes)
            print("    Captcha screenshot saved to /tmp/ecourts_captcha_sample.png")
        except Exception as e:
            print(f"    Captcha screenshot failed: {e}")

    print("[6] Doing a full search to capture submitPartyName POST...")
    # Fill the search form and submit
    await page.fill("#petres_name", "Sharma")
    await page.fill("#rgyearP", "2019")

    # Get captcha image URL from page
    captcha_src = await page.evaluate(
        "() => document.getElementById('captcha_image')?.src || ''"
    )
    print(f"    Captcha image src: {captcha_src}")

    # Download and solve captcha
    from captcha_solver import solve as solve_captcha
    cap_resp2 = await page.request.get(captcha_src)
    with open("/tmp/inspect_captcha.png", "wb") as f:
        f.write(await cap_resp2.body())
    solved = solve_captcha("/tmp/inspect_captcha.png")
    print(f"    Captcha solved: '{solved}'")

    await page.fill("#fcaptcha_code", solved)

    # Click Go and wait for response
    for sel in ["button:has-text('Go')", "button[type='submit']", "input[type='submit']"]:
        try:
            await page.locator(sel).first.click(timeout=3000)
            print(f"    Clicked: {sel}")
            break
        except Exception:
            continue

    await page.wait_for_timeout(4000)

    post_reqs = [r for r in captured_requests if r["method"] == "POST"]
    submit_reqs = [r for r in post_reqs if "submitPartyName" in r["url"]]

    print(f"\n{'='*60}")
    print(f"ALL POST REQUESTS ({len(post_reqs)} total):")
    for r in post_reqs:
        print(f"  {r['url']}")
        print(f"  body: {r['post_data']}")
        print()

    print(f"submitPartyName body:")
    for r in submit_reqs:
        print(f"  {r['post_data']}")

    print("[7] Full page URL and title...")
    page_info = {
        "url": page.url,
        "title": await page.title(),
    }

    # Compile report
    report = {
        "page": page_info,
        "cookies": cookie_map,
        "cookie_names": list(cookie_map.keys()),
        "form_fields": form_fields,
        "captcha_info": captcha_info,
        "post_requests_during_navigation": post_reqs,
        "all_requests_count": len(captured_requests),
        "ajax_requests": [
            r for r in captured_requests
            if "ajax" in r["url"].lower() or r["headers"].get("x-requested-with") == "XMLHttpRequest"
        ],
    }

    out_path = "/tmp/ecourts_inspect.json"
    with open(out_path, "w") as f:
        json.dump(report, f, indent=2, default=str)

    print(f"\n{'='*60}")
    print(f"COOKIES:        {list(cookie_map.keys())}")
    print(f"COOKIE VALUES:  {cookie_map}")
    print(f"\nFORM FIELDS (id/name -> value):")
    for k, v in form_fields.items():
        if v.get("id") or v.get("name"):
            print(f"  [{v['tag']}] id={v['id']!r:20s} name={v['name']!r:20s} value={v['value']!r}")
    print(f"\nCAPTCHA: {captcha_info}")
    print(f"\nPOST requests during navigation: {len(post_reqs)}")
    for r in post_reqs:
        print(f"  {r['url']}")
        print(f"  body: {r['post_data']}")
    print(f"\nFull report: {out_path}")
    print(f"{'='*60}")

    input("\nPress Enter to close browser...")
    await scraper.close()


if __name__ == "__main__":
    asyncio.run(inspect())
