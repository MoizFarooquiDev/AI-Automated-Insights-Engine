"""
Central configuration. All secrets come from environment variables
(optionally loaded from a local .env file). Never hardcode credentials.
"""

import os

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


def _require(name):
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


# Platform API
API_BASE_URL = _require("CNI_API_BASE_URL")
API_KEY      = _require("CNI_API_KEY")
USERNAME     = _require("CNI_USERNAME")
PASSWORD     = _require("CNI_PASSWORD")

# Elasticsearch
ES_BASE_URL = _require("ES_BASE_URL")
ES_AUTH     = (_require("ES_USER"), _require("ES_PASSWORD"))

# AWS
AWS_REGION = os.environ.get("AWS_REGION", "ap-southeast-1")
SQS_URL    = _require("SQS_URL")

# Insight mapping form
FORM_ID = _require("CNI_FORM_ID")
