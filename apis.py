"""
APIs for authentication and site configuration retrieval.
"""

import json
import requests

from config import API_BASE_URL, API_KEY, USERNAME, PASSWORD


def login():
    """Authenticate and return the full response dict (token lives at response['response']['token'])."""
    url     = f"{API_BASE_URL}/login"
    payload = json.dumps({"username": USERNAME, "password": PASSWORD})
    headers = {
        "content-type":    "application/json",
        "accept-encoding": "gzip",
        "x-api-key":       API_KEY,
    }
    params   = {"regId": "cni-insights-job", "deviceName": "server"}
    response = requests.post(url, data=payload, headers=headers, params=params)
    return response.json()

def get_site_list(login_response):
    url = f"{API_BASE_URL}/user-sites"
    
    headers = {
        'authorization': login_response['response']['token'],
        'x-api-key': API_KEY
        }
    
    response = requests.request("GET", url, headers=headers)
    sites_list =  json.loads(response.text)

    site = []
    for sites in sites_list:
        site.append(sites["siteId"])
    return site



def get_site_db(site_id, login_response):
    """Fetch the full site config JSON for a given site id."""
    url     = f"{API_BASE_URL}/site-config?siteId={site_id}"
    headers = {
        "authorization": f"Bearer {login_response['response']['token']}",
        "x-api-key":     API_KEY,
    }
    response = requests.get(url, headers=headers)
    return response.json()


def get_std_details(siteconfig):
    """Build standard <-> raw field mappings from the site config's live_cache.STD."""
    stds       = siteconfig.get("live_cache", {}).get("STD", {})
    stdMapping = stds.copy()
    stdMapping["TIMESTAMP"] = "@timestamp"
    rawMapping = {v: k for k, v in stdMapping.items()}
    fields     = list(stdMapping.values())
    return fields, stdMapping, rawMapping


def get_dynamo_form(form_id, login_response):
    """Fetch the insight mapping form from DynamoDB via the platform API."""
    url     = f"{API_BASE_URL}/forms/form-info?formId={form_id}"
    headers = {
        "authorization": f"Bearer {login_response['response']['token']}",
        "x-api-key":     API_KEY,
    }
    response = requests.get(url, headers=headers)
    return response.status_code, response.json()


def get_description(mapping, category_code, sub_category_code, detail_category_code):
    """
    Walk the mapping tree to find the description + severity for a given
    (category, subCategory, detailCategory) code triple.
    """
    categories = mapping.get("mapping", {}).get("category", [])

    cat = next((c for c in categories if int(c["code"]) == category_code), None)
    if not cat:
        return None

    sub_cat = next((s for s in cat["subCategory"] if int(s["code"]) == sub_category_code), None)
    if not sub_cat:
        return None

    detail = next(
        (d for d in sub_cat.get("detailCategory", []) if int(d["code"]) == detail_category_code),
        None,
    )
    return detail
