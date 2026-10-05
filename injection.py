"""
Injection module — push insight records into Elasticsearch via SQS.
"""

import hashlib
import json
from datetime import datetime, timedelta


def ingest(sid, df, sqs_client, sqs_url):
    yesterday      = datetime.now().date() - timedelta(days=1)
    operation_date = yesterday.strftime("%Y-%m-%dT23:59:59")  # e.g. 2026-06-11T23:59:59
    date_str       = yesterday.strftime("%Y-%m-%d")           # used in _id

    chunks = [df[i:i + 5] for i in range(0, len(df), 5)]

    for chunk in chunks:
        lines = []
        for _, row in chunk.iterrows():
            doc_id = hashlib.sha256(f"{sid}_{date_str}_{row['category']}_{row['subCategory']}_{row['detailCategory']}".encode()).hexdigest()
            action = json.dumps({
                "update": {
                    "_index":            "cni-insights-processed",
                    "_id":               doc_id,
                    "retry_on_conflict": 1,
                }
            })
            body = json.dumps({
                "doc": {
                    "observationDate": operation_date,
                    "siteId":          row["siteId"],
                    "siteName":        row["siteName"],
                    "category":        row["category"],
                    "subCategory":     row["subCategory"],
                    "detailCategory":  row["detailCategory"],
                    "description":     row["description"],
                    "status":          row["status"],
                    "sev":             row["sev"],
                    "opportunityCost":             row["opportunityCost"],
                },
                "doc_as_upsert": True,
            })
            lines.append(action + "\n" + body + "\n")

        payload  = {"message": "".join(lines)}
        response = sqs_client.send_message(QueueUrl=sqs_url, MessageBody=json.dumps(payload))
        print(f"[{sid}] Ingest chunk: HTTP {response['ResponseMetadata']['HTTPStatusCode']}")